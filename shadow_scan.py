"""
shadow_scan.py — Brede ATR-scan van de VOLLEDIGE watchlist (observatie)

Draait rond dezelfde tijd als de live scalper-cyclus (na voltooiing
van de openingscandle), maar controleert ALLE 26 aandelen uit de
watchlist op een geldig ATR-signaal -- niet alleen de 3 nieuws-
geselecteerde. Logt elk gevonden signaal naar shadow_signals.csv, met
een markering of dat aandeel ook daadwerkelijk nieuws-geselecteerd was
die dag (zodat je later kunt vergelijken).

PLAATST NOOIT EEN ORDER -- puur observatie, voor het empirisch meten
of nieuwsselectie een hogere winratio oplevert dan een brede ATR-scan.

Hergebruikt dezelfde, al bevestigd werkende bouwstenen als main.py
(data_module, atr_module, entry_module, exit_module) -- geen nieuwe,
ongeteste logica voor de signaal-detectie zelf.

Gebruik (via cron, kort na de live cyclus):
    python3 shadow_scan.py
"""

from __future__ import annotations

import asyncio
import logging
import os

from data_module import get_historical_candles, get_opening_candle
from atr_module import calculate_atr, validate_opening_range
from entry_module import generate_entry_signal
from exit_module import calculate_exit_levels
from news_module import FALLBACK_WATCHLIST, select_top_symbols_from_watchlist, select_top_symbols
from shadow_journal_module import log_shadow_signal

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("shadow_scan")

SHADOW_CAPITAL = 2000.0  # vast referentiebedrag voor de hypothetische positiegrootte-berekening


async def scan_symbol(symbol: str, news_selected_symbols: set[str]) -> dict | None:
    """
    Checkt één symbool op een geldig ATR-signaal, en logt het resultaat
    als er een geldig signaal is. Plaatst NOOIT een order.
    """
    try:
        daily_candles = await asyncio.to_thread(get_historical_candles, symbol, "20d", "1d")
        intraday_candles = await asyncio.to_thread(get_historical_candles, symbol, "1d", "15min")
        opening_candle = get_opening_candle(intraday_candles)

        if opening_candle is None:
            return {"symbol": symbol, "status": "geen_openingscandle"}

        atr = calculate_atr(daily_candles)
        validation = validate_opening_range(opening_candle, atr)
        if not validation["valid"]:
            return {"symbol": symbol, "status": "atr_validatie_mislukt"}

        signal = generate_entry_signal(opening_candle)
        if signal is None:
            return {"symbol": symbol, "status": "neutrale_candle"}

        plan = calculate_exit_levels(signal, capital=SHADOW_CAPITAL)

        log_shadow_signal({
            "symbol": symbol,
            "news_selected": symbol in news_selected_symbols,
            "atr": round(atr, 4),
            "opening_range": round(opening_candle.range, 4),
            "direction": signal.direction,
            "quantity": plan.position_size,
            "entry_price": signal.entry_price,
            "take_profit": plan.take_profit,
            "stop_loss": plan.stop_loss,
            "reason": plan.reason,
        })

        return {"symbol": symbol, "status": "geldig_signaal", "direction": signal.direction}

    except Exception as e:
        logger.error(f"Fout bij het scannen van {symbol}: {e}")
        return {"symbol": symbol, "status": "error", "reason": str(e)}


async def run_shadow_scan() -> dict:
    """
    Voert de brede scan uit over de VOLLEDIGE watchlist, gelijktijdig
    per symbool (asyncio, zelfde patroon als main.py se run_cycle).
    """
    logger.info("=== Schaduw-scan gestart (volledige watchlist, alleen observatie) ===")

    # Dezelfde nieuwsselectie als de live cyclus, puur om te MARKEREN
    # welke symbolen ook daadwerkelijk live geselecteerd zouden zijn --
    # geen invloed op WELKE symbolen gescand worden (dat zijn er altijd 26).
    finnhub_key = os.environ.get("FINNHUB_API_KEY")
    if finnhub_key:
        chosen = select_top_symbols_from_watchlist(FALLBACK_WATCHLIST, finnhub_key, n=3)
    else:
        chosen = select_top_symbols([], n=3, fallback_watchlist=FALLBACK_WATCHLIST)
    news_selected_symbols = {symbol for symbol, _ in chosen}

    logger.info(f"Nieuws-geselecteerd vandaag (ter referentie): {news_selected_symbols}")

    tasks = [scan_symbol(symbol, news_selected_symbols) for symbol in FALLBACK_WATCHLIST]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    geldige_signalen = [r for r in results if isinstance(r, dict) and r.get("status") == "geldig_signaal"]
    nieuws_met_signaal = [r for r in geldige_signalen if r["symbol"] in news_selected_symbols]

    logger.info(
        f"=== Schaduw-scan afgerond: {len(geldige_signalen)}/{len(FALLBACK_WATCHLIST)} aandelen "
        f"met een geldig signaal, waarvan {len(nieuws_met_signaal)} ook nieuws-geselecteerd ==="
    )

    return {
        "total_scanned": len(FALLBACK_WATCHLIST),
        "valid_signals": len(geldige_signalen),
        "news_selected_with_signal": len(nieuws_met_signaal),
        "news_selected_symbols": list(news_selected_symbols),
        "signals": geldige_signalen,
    }


if __name__ == "__main__":
    result = asyncio.run(run_shadow_scan())
    print(f"\nResultaat: {result}")
