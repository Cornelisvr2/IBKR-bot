"""
rvb_baseline_builder.py — RVB, dagelijkse volume-baseline-cache

Draait ÉÉN keer per handelsdag, vóór de markt opent (cron, bv. 15:00
CEST), en schrijft per symbool de tijdstip-gematchte gemiddelde
volumes weg naar een JSON-bestand. De 5-min-scanner (rvb_scan.py)
leest dat bestand alleen -- zo hoeft de zware 20-dagen-historie niet
elke 5 minuten opnieuw opgehaald te worden (26 symbolen x 80 scans =
~2.000 extra history-aanvragen per dag, tegen de 429-limiet in).

Uitvoer: /opt/strategy/data/rvb_baseline.json
    {
      "built_at": "...", "period": "20d",
      "symbols": { "AAPL": {"days": 19, "slots": {"15:30": 1234567.0, ...}}, ... }
    }

Gebruik:
    python3 rvb_baseline_builder.py            # volledige watchlist
    python3 rvb_baseline_builder.py AAPL MSFT  # losse test
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, format="%(asctime)s|%(levelname)-.1s| %(message)s")
logger = logging.getLogger("rvb_baseline_builder")

BASELINE_PATH = os.environ.get("RVB_BASELINE_FILE", "/opt/strategy/data/rvb_baseline.json")
BASELINE_PERIOD = "20d"
BAR_SIZE = "5min"
PAUSE_BETWEEN_SYMBOLS_S = 1.5   # sequentieel + korte pauze: 26 aanvragen, ver onder de rate-limiet


def build_all(symbols: list[str]) -> dict:
    from data_module import get_historical_candles
    from rvb_strategy_module import build_volume_baseline, BASELINE_MIN_DAYS

    vandaag = datetime.now().date()
    result = {"built_at": datetime.now().isoformat(timespec="seconds"), "period": BASELINE_PERIOD, "symbols": {}}

    for symbol in symbols:
        candles = get_historical_candles(symbol, duration=BASELINE_PERIOD, bar_size=BAR_SIZE)
        if not candles:
            logger.warning(f"{symbol}: geen historie ontvangen -- geen baseline.")
            time.sleep(PAUSE_BETWEEN_SYMBOLS_S)
            continue
        baseline = build_volume_baseline(candles, exclude_date=vandaag)
        if baseline["days"] < BASELINE_MIN_DAYS:
            logger.warning(f"{symbol}: slechts {baseline['days']} handelsdagen in de data -- baseline onbetrouwbaar, niet opgeslagen.")
        else:
            result["symbols"][symbol] = baseline
            logger.info(f"{symbol}: baseline over {baseline['days']} dagen, {len(baseline['slots'])} tijdslots.")
        time.sleep(PAUSE_BETWEEN_SYMBOLS_S)

    return result


def load_baseline(path: str = BASELINE_PATH) -> dict:
    """Leest de cache; geeft {} als het bestand ontbreekt of van een andere dag is."""
    try:
        with open(path) as f:
            data = json.load(f)
        built = datetime.fromisoformat(data.get("built_at", "1970-01-01T00:00:00"))
        if built.date() != datetime.now().date():
            logger.warning(f"Baseline is van {built.date()} (niet vandaag) -- wordt genegeerd.")
            return {}
        return data
    except FileNotFoundError:
        logger.error(f"Geen baseline-bestand op {path} -- draai rvb_baseline_builder.py eerst.")
        return {}
    except Exception as e:
        logger.error(f"Kon baseline niet lezen: {e}")
        return {}


def main():
    from news_module import FALLBACK_WATCHLIST
    symbols = sys.argv[1:] or FALLBACK_WATCHLIST

    from ibkr_web_api import tickle
    if not tickle():
        logger.error("Gateway reageert niet -- baseline niet gebouwd.")
        sys.exit(1)

    result = build_all(symbols)
    os.makedirs(os.path.dirname(BASELINE_PATH), exist_ok=True)
    tmp = BASELINE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(result, f)
    os.replace(tmp, BASELINE_PATH)   # atomair: de scanner ziet nooit een half geschreven bestand

    ok, totaal = len(result["symbols"]), len(symbols)
    logger.info(f"Baseline geschreven naar {BASELINE_PATH}: {ok}/{totaal} symbolen.")
    if ok < totaal:
        try:
            from telegram_notify import send_telegram_message
            send_telegram_message(f"⚠️ RVB-baseline: {ok}/{totaal} symbolen gelukt -- ontbrekende symbolen worden vandaag niet gescand.")
        except Exception:
            pass


if __name__ == "__main__":
    main()
