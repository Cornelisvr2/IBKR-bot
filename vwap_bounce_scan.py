"""
vwap_bounce_scan.py — VDB, 5-minuten-scanner (cron-ingang)

Eén korte, complete scan-cyclus per cron-run (geen langlopend proces),
zelfde opzet als rvb_scan.py maar ZONDER baseline-stap -- VWAP wordt
elke run vers herberekend uit de candles van vandaag, er is geen
historische volume-baseline nodig zoals bij RVB:
    1. Sessie-check + tickle (automatische re-auth)
    2. "Al gehandeld vandaag"-bestand laden
    3. Per symbool (sequentieel, met korte pauze): candles van vandaag
       ophalen en scan_symbol() toepassen
    4. Bij een signaal: symbool direct als "gehandeld" markeren (VÓÓR
       het dispatchen) en execute_vwap_bounce_trade_standalone.py
       losgekoppeld starten

Cron (zie run_vwap_bounce_cycle.sh):
    */5 16-21 * * 1-5   -> scans om 16:35, 16:40, ... 21:55 (vóór 16:20
                           is er nog geen MIN_TREND_CANDLES-geschiedenis)

Gebruik:
    python3 vwap_bounce_scan.py            # dry-run: alleen signalen loggen/melden
    python3 vwap_bounce_scan.py --live     # signalen ook daadwerkelijk uitvoeren
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, time as dt_time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, format="%(asctime)s|%(levelname)-.1s| %(message)s")
logger = logging.getLogger("vwap_bounce_scan")

STRATEGY_DIR = os.path.dirname(os.path.abspath(__file__))
TRADED_TODAY_PATH = os.environ.get("VDB_TRADED_FILE", "/opt/strategy/data/vdb_traded_today.json")
SIGNAL_LOG_PATH = os.environ.get("VDB_SIGNAL_LOG", "/opt/strategy/logs/vdb_signals.jsonl")  # voor het dashboard
FIRST_SCAN_TIME = dt_time(16, 20)   # eerst mogelijke moment met genoeg trend-geschiedenis
LAST_ENTRY_TIME = dt_time(21, 30)   # na dit tijdstip geen nieuwe entries meer (te weinig tijd tot 21:55)
PAUSE_BETWEEN_SYMBOLS_S = 1.0
MAX_NEW_TRADES_PER_RUN = 2          # zelfde voorzichtigheidsgrens als RVB


# ---------------------------------------------------------------------------
# "Al gehandeld vandaag"
# ---------------------------------------------------------------------------

def load_traded_today(path: str = TRADED_TODAY_PATH) -> dict:
    vandaag = datetime.now().date().isoformat()
    try:
        with open(path) as f:
            data = json.load(f)
        if data.get("date") == vandaag:
            return data
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.error(f"Kon traded-today-bestand niet lezen ({e}) -- start leeg.")
    return {"date": vandaag, "symbols": {}}


def mark_traded(data: dict, symbol: str, info: dict, path: str = TRADED_TODAY_PATH) -> None:
    data["symbols"][symbol] = {"time": datetime.now().isoformat(timespec="seconds"), "strategy": "VDB", **info}
    try:  # zelfde info als regel in het signaal-log (dashboard: "signalen zonder trade")
        os.makedirs(os.path.dirname(SIGNAL_LOG_PATH), exist_ok=True)
        with open(SIGNAL_LOG_PATH, "a") as f:
            f.write(json.dumps({"symbol": symbol, **data["symbols"][symbol]}) + "\n")
    except Exception as e:
        logger.warning(f"Kon signaal-log niet schrijven: {e}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def ensure_session() -> bool:
    from auth_module import check_ibkr_authenticated, trigger_ibkr_authenticate
    from ibkr_web_api import tickle
    if check_ibkr_authenticated():
        tickle()
        return True
    logger.warning("IBKR-sessie ongeldig -- automatische re-auth wordt geprobeerd.")
    result = trigger_ibkr_authenticate()
    ok = bool(result.get("triggered"))
    if not ok:
        logger.error(f"Re-auth mislukt: {result}")
    return ok


def dispatch_trade(trade: dict, signal_info: dict, dry_run: bool) -> None:
    cmd = [
        sys.executable, os.path.join(STRATEGY_DIR, "execute_vwap_bounce_trade_standalone.py"),
        "--symbol", trade["symbol"], "--direction", trade["direction"],
        "--entry", str(trade["entry_price"]), "--take-profit", str(trade["take_profit"]),
        "--stop-loss", str(trade["stop_loss"]), "--quantity", str(trade["quantity"]),
        "--vwap", str(signal_info["vwap_at_touch"]),
        "--touch-low", str(signal_info["touch_low"]), "--touch-high", str(signal_info["touch_high"]),
    ]
    if dry_run:
        logger.info(f"[DRY-RUN] zou starten: {' '.join(cmd)}")
        return
    from dispatch_guard import start_bewaakt
    start_bewaakt(cmd, symbol=trade["symbol"], strategie="VDB", cwd=STRATEGY_DIR)
    logger.info(f"Losgekoppeld VDB-tradeproces gestart voor {trade['symbol']}.")


def run_scan(dry_run: bool = True) -> dict:
    from news_module import FALLBACK_WATCHLIST
    from data_module import get_historical_candles
    from vwap_bounce_module import scan_symbol, build_vdb_trade
    from state_module import get_strategy_balance

    now = datetime.now()
    samenvatting = {"scanned": 0, "signals": 0, "dispatched": 0, "skipped": []}

    if now.time() < FIRST_SCAN_TIME:
        logger.info(f"Vóór {FIRST_SCAN_TIME} -- nog niet genoeg trend-geschiedenis, niets te doen.")
        return samenvatting
    if now.time() >= LAST_ENTRY_TIME:
        logger.info(f"Na {LAST_ENTRY_TIME} -- geen nieuwe entries meer vandaag.")
        return samenvatting

    if not ensure_session():
        _notify("🚨 VDB-scan: geen geldige IBKR-sessie en re-auth mislukt -- scan overgeslagen.")
        return samenvatting

    traded = load_traded_today()
    capital = get_strategy_balance("VDB")

    # Kapitaal per trade: VDB's EIGEN, onafhankelijke compounding-saldo
    # (9 sep 2026 -- zelfde aanpak als bij RVB). De VIX-allocatie
    # (risk_module) is bewust NIET toegepast in de MVP.

    nieuwe_trades = 0
    for symbol in FALLBACK_WATCHLIST:
        if symbol in traded["symbols"]:
            continue
        if nieuwe_trades >= MAX_NEW_TRADES_PER_RUN:
            break

        candles = get_historical_candles(symbol, duration="1d", bar_size="5min")
        time.sleep(PAUSE_BETWEEN_SYMBOLS_S)
        samenvatting["scanned"] += 1
        if not candles:
            continue

        signal = scan_symbol(symbol, candles, datetime.now())
        if signal is None:
            continue
        samenvatting["signals"] += 1

        try:
            trade = build_vdb_trade(signal, capital)
        except ValueError as e:
            logger.warning(f"{symbol}: signaal maar geen trade -- {e}")
            mark_traded(traded, symbol, {"status": "skipped", "reason": str(e)})
            continue

        # Eerst markeren, dan pas dispatchen -- nooit andersom.
        mark_traded(traded, symbol, {"status": "dry-run" if dry_run else "dispatched",
                                     "direction": trade["direction"], "entry": trade["entry_price"],
                                     "take_profit": trade["take_profit"], "stop_loss": trade["stop_loss"],
                                     "quantity": trade["quantity"],
                                     "vwap_at_touch": signal.vwap_at_touch,
                                     "touch_low": signal.touch_low, "touch_high": signal.touch_high})
        _notify(
            f"{'🧪 [DRY-RUN] ' if dry_run else '📡 '}VDB {trade['direction']} {symbol}: bounce op VWAP "
            f"{signal.vwap_at_touch:.2f}, bevestiging @ {signal.trigger_price:.2f}\n"
            f"Entry {trade['entry_price']:.2f} | TP {trade['take_profit']:.2f} | SL {trade['stop_loss']:.2f} | "
            f"{trade['quantity']:g} stuks, risico €{trade['risk_amount']:.2f}"
        )
        dispatch_trade(trade, {
            "vwap_at_touch": signal.vwap_at_touch, "touch_low": signal.touch_low, "touch_high": signal.touch_high,
        }, dry_run)
        nieuwe_trades += 1
        samenvatting["dispatched"] += 1

    logger.info(f"Scan afgerond: {samenvatting}")
    return samenvatting


def _notify(text: str) -> None:
    try:
        from telegram_notify import send_telegram_message
        send_telegram_message(text)
    except Exception as e:
        logger.warning(f"Telegram-melding mislukt: {e}")


if __name__ == "__main__":
    run_scan(dry_run="--live" not in sys.argv)
