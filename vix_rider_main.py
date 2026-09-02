"""
vix_rider_main.py — VIX Rider, Hoofdorkestratie

In tegenstelling tot Touch & Turn Scalper's main.py (één cron-moment,
snel klaar) is dit een DOORLOPEND script: het wordt rond marktopening
gestart (bijv. via cron om 15:29 CEST) en blijft actief tot een
afkaptijd (bijv. 17:30 CEST) -- de hele periode waarin het op een
Opening-Range-doorbraak wacht.

Stappen:
    1. VIX-allocatie ophalen (risk_module.get_allocated_capital) --
       als VIX Rider 0% toegewezen krijgt (lage VIX), stopt het script
       meteen. Geen aparte aan/uit-schakelaar nodig, de allocatie doet
       dat werk al.
    2. Symbolen kiezen (hergebruikt dezelfde nieuwsscoring als de
       scalper, voor een eerlijke vergelijking tussen beide
       strategieën op hetzelfde soort selectiecriteria).
    3. Voor elk gekozen symbool GELIJKTIJDIG (asyncio, zelfde patroon
       als de scalper): Opening Range vastleggen, dan periodiek
       candles ophalen en op een doorbraak checken tot de afkaptijd.
    4. Bij een doorbraak: positie berekenen, trade dispatchen naar een
       losgekoppeld achtergrondproces (niet blokkerend, zelfde
       patroon als de scalper se execute_trade_standalone.py).

BELANGRIJK: nog niet end-to-end live getest als geheel -- de losse
bouwstenen (vix_rider_entry_module, vix_rider_exit_module,
vix_rider_order_module) zijn elk apart getest.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from datetime import datetime, time as dt_time

from vix_rider_entry_module import calculate_opening_range, detect_breakout, OpeningRange
from vix_rider_exit_module import calculate_vix_rider_position

logger = logging.getLogger("vix_rider_main")

DEFAULT_TOTAL_CAPITAL = 2000.0  # zelfde totaalbedrag als de scalper -- ze DELEN dit
                                  # via de VIX-glijdende-schaal, geen apart budget
MAX_TRADES_PER_DAY = 3
BREAKOUT_CHECK_INTERVAL_SECONDS = 300  # elke 5 minuten een nieuwe candle checken
CUTOFF_TIME = dt_time(17, 30)  # 17:30 CEST -- stop met wachten op een doorbraak

# Zelfde watchlist als de scalper, voor een eerlijke, vergelijkbare
# symboolselectie tussen beide strategieën.
from news_module import FALLBACK_WATCHLIST, select_top_symbols_from_watchlist, select_top_symbols


def get_opening_range_dry_run(symbol: str) -> OpeningRange:
    """Voorbeeld-Opening-Range voor dry-run tests, geen live data nodig."""
    from vix_rider_entry_module import calculate_opening_range
    from data_module import Candle

    or_candles = [
        Candle(timestamp=datetime.now(), open=450.0, high=458.0, low=449.0, close=455.0, volume=50000),
        Candle(timestamp=datetime.now(), open=455.0, high=460.0, low=452.0, close=456.0, volume=45000),
    ]
    return calculate_opening_range(or_candles)


def get_opening_range_live(symbol: str) -> OpeningRange | None:
    """
    Haalt de eerste 30 minuten candledata op en berekent de Opening
    Range. Wordt aangeroepen vlak na 16:00 CEST (30 min na de 15:30
    marktopening).

    LET OP: nog niet live getest.
    """
    from data_module import get_historical_candles

    candles = get_historical_candles(symbol, duration="1d", bar_size="15min")
    if len(candles) < 2:
        logger.error(f"Te weinig candles voor Opening Range van {symbol}.")
        return None

    try:
        return calculate_opening_range(candles)
    except ValueError as e:
        logger.error(f"Kon Opening Range niet berekenen voor {symbol}: {e}")
        return None


def get_latest_candle_live(symbol: str):
    """Haalt de meest recente 15-min candle op, voor de doorbraak-check."""
    from data_module import get_historical_candles

    candles = get_historical_candles(symbol, duration="1d", bar_size="15min")
    if not candles:
        return None
    return candles[-1]


def dispatch_vix_rider_trade(plan, symbol: str) -> None:
    """
    Start execute_vix_rider_trade() als losgekoppeld achtergrondproces
    -- zelfde patroon als de scalper se execute_trade_standalone.py,
    zodat de monitoringlus niet blokkeert op het wachten op een fill.
    """
    log_path = f"/opt/strategy/logs/vix_rider_dispatch_{symbol}.log"
    with open(log_path, "a") as log_file:
        subprocess.Popen(
            [
                "python3", "/opt/strategy/execute_vix_rider_trade_standalone.py",
                "--symbol", symbol,
                "--direction", plan.direction,
                "--entry-price", str(plan.entry_price),
                "--initial-stop-loss", str(plan.initial_stop_loss),
                "--quantity", str(plan.quantity),
            ],
            stdout=log_file, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    logger.info(f"VIX Rider trade voor {symbol} gedispatcht naar losgekoppeld proces.")


async def monitor_symbol_for_breakout(symbol: str, capital: float, dry_run: bool) -> dict:
    """
    Legt de Opening Range vast voor één symbool, en blijft daarna
    periodiek checken op een doorbraak tot CUTOFF_TIME.

    Draait als eigen asyncio-taak per symbool (via to_thread voor de
    blokkerende delen), zodat meerdere symbolen gelijktijdig bewaakt
    worden -- een doorbraak bij het ene symbool blokkeert de
    bewaking van de andere niet.
    """
    logger.info(f"--- VIX Rider bewaking gestart voor {symbol} ---")

    if dry_run:
        opening_range = await asyncio.to_thread(get_opening_range_dry_run, symbol)
    else:
        opening_range = await asyncio.to_thread(get_opening_range_live, symbol)

    if opening_range is None:
        return {"status": "no_opening_range", "symbol": symbol}

    logger.info(f"{symbol}: Opening Range vastgelegd, High {opening_range.high:.2f} / Low {opening_range.low:.2f}")

    if dry_run:
        # In dry-run: simuleer direct één candle die een doorbraak vormt,
        # in plaats van een minutenlange polling-lus te simuleren.
        from data_module import Candle
        breakout_candle = Candle(
            timestamp=datetime.now(), open=459.0, high=463.0, low=458.5, close=462.0, volume=30000,
        )
        signal = detect_breakout(opening_range, breakout_candle)
    else:
        signal = None
        while datetime.now().time() < CUTOFF_TIME:
            candle = await asyncio.to_thread(get_latest_candle_live, symbol)
            if candle is not None:
                signal = detect_breakout(opening_range, candle)
                if signal is not None:
                    break
            await asyncio.sleep(BREAKOUT_CHECK_INTERVAL_SECONDS)

    if signal is None:
        logger.info(f"{symbol}: geen doorbraak gevonden vóór de afkaptijd.")
        return {"status": "no_breakout", "symbol": symbol}

    plan = calculate_vix_rider_position(signal, capital=capital)
    if plan.quantity * plan.entry_price < 5.0:
        reason = f"Positiewaarde te klein voor {symbol} met €{capital:.2f} toegewezen kapitaal."
        logger.warning(reason)
        return {"status": "position_too_small", "symbol": symbol, "reason": reason}

    if dry_run:
        logger.info(f"[DRY-RUN] {symbol}: trade zou gedispatcht worden: {plan.reason}")
        return {"status": "dry_run_complete", "symbol": symbol, "plan": plan.to_dict()}

    await asyncio.to_thread(dispatch_vix_rider_trade, plan, symbol)
    return {"status": "trade_dispatched", "symbol": symbol, "plan": plan.to_dict()}


def run_vix_rider_cycle(total_capital: float = None, dry_run: bool = True, max_trades: int = MAX_TRADES_PER_DAY) -> dict:
    """
    Voert de volledige VIX Rider-cyclus uit: VIX-allocatie check,
    symboolselectie, en gelijktijdige breakout-monitoring per symbool.

    Bedoeld om rond 15:29 CEST gestart te worden (via cron) en tot de
    afkaptijd (17:30 CEST) actief te blijven.

    KRITIEKE FIX (26 aug 2026): als `total_capital` niet expliciet is
    meegegeven, wordt het GEDEELDE, gesimuleerde compounding-saldo
    opgehaald (state_module.get_simulated_balance()) -- hetzelfde
    saldo dat de scalper gebruikt en bijwerkt. Voorheen gebruikte
    VIX Rider altijd het vaste DEFAULT_TOTAL_CAPITAL (€2000), waardoor
    de twee strategieën NIET daadwerkelijk hetzelfde, groeiende/
    krimpende kapitaal deelden zoals bedoeld -- VIX Rider "zag" nooit
    de winst/verliezen van de scalper, en andersom.
    """
    logger.info(f"=== VIX Rider cyclus gestart ({'DRY-RUN' if dry_run else 'LIVE'}) ===")

    if total_capital is None:
        from state_module import get_simulated_balance
        total_capital = get_simulated_balance()
        logger.info(f"Gedeeld compounding-kapitaal opgehaald: €{total_capital:.2f}")

    if dry_run:
        capital = total_capital
        vix_info = "dry-run, geen live VIX-check"
    else:
        from risk_module import check_circuit_breakers, get_allocated_capital

        # KRITIEKE FIX (26 aug 2026): VIX Rider controleerde voorheen
        # NOOIT de gedeelde 3%-dagstop-circuit-breaker (alleen
        # main.py, de scalper, deed dat) -- ook al draagt VIX Rider
        # sinds de vorige fix wel BIJ aan die registratie. Zonder deze
        # check zou VIX Rider gewoon door kunnen blijven handelen, ook
        # als de scalper (of VIX Rider zelf, eerder op de dag) de
        # dagstop al had bereikt.
        breaker_result = check_circuit_breakers(capital=total_capital)
        if not breaker_result["safe_to_trade"]:
            reason = f"Circuit breaker actief: {breaker_result['reason']}"
            logger.warning(reason)
            return {"status": "circuit_breaker_triggered", "reason": breaker_result["reason"]}

        from risk_module import get_allocated_capital
        allocation = get_allocated_capital(total_capital, "macro_panic")
        capital = allocation["allocated_capital"]
        vix_info = f"VIX {allocation['vix']}, allocatie {allocation['allocation_pct']*100:.0f}%"

        if capital <= 0:
            reason = f"Geen kapitaal toegewezen aan VIX Rider ({vix_info}) -- cyclus overgeslagen."
            logger.info(reason)
            return {"status": "skipped", "reason": reason}

    logger.info(f"Toegewezen kapitaal: €{capital:.2f} ({vix_info})")

    finnhub_key = os.environ.get("FINNHUB_API_KEY")
    if finnhub_key:
        chosen = select_top_symbols_from_watchlist(FALLBACK_WATCHLIST, finnhub_key, n=max_trades)
    else:
        chosen = select_top_symbols([], n=max_trades, fallback_watchlist=FALLBACK_WATCHLIST)

    if not chosen:
        return {"status": "skipped", "reason": "Geen symbolen gekozen."}

    async def _monitor_all():
        tasks = [monitor_symbol_for_breakout(symbol, capital, dry_run) for symbol, _ in chosen]
        return await asyncio.gather(*tasks, return_exceptions=True)

    raw_results = asyncio.run(_monitor_all())

    results = []
    for (symbol, _), r in zip(chosen, raw_results):
        if isinstance(r, Exception):
            logger.error(f"Onafgevangen fout bij {symbol}: {r}")
            results.append({"status": "error", "symbol": symbol, "reason": str(r)})
        else:
            results.append(r)

    executed = [r for r in results if r["status"] in ("dry_run_complete", "trade_dispatched")]
    logger.info(f"=== VIX Rider cyclus afgerond: {len(executed)}/{len(chosen)} trades ===")

    return {"status": "cycle_complete", "trade_count": len(executed), "results": results}


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    if "--live" in sys.argv:
        result = run_vix_rider_cycle(dry_run=False)
        print(f"Resultaat: {result}")
    else:
        result = run_vix_rider_cycle(dry_run=True)
        print(f"\nCyclus-resultaat: {result['status']}, {result.get('trade_count', 0)} trade(s)")
        for r in result.get("results", []):
            print(f"  {r['symbol']}: {r['status']}")
