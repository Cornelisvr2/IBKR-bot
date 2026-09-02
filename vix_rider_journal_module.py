"""
vix_rider_journal_module.py — VIX Rider, Trade Journal

Eigen, LOS journal-bestand van Touch & Turn Scalper se journal_module.py
-- expliciet gescheiden zodat je de prestaties van beide strategieën
apart kunt analyseren en vergelijken (precies het doel waarvoor je
beide naast elkaar laat draaien).

Velden zijn toegespitst op VIX Rider se eigen mechaniek (Opening Range,
doorbraakrichting, trailing-afstand) in plaats van de scalper se
ATR/Fibonacci-velden.

Gebruik:
    from vix_rider_journal_module import log_vix_rider_trade
    log_vix_rider_trade({...})
"""

from __future__ import annotations

import csv
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger("vix_rider_journal_module")

JOURNAL_PATH = os.environ.get("VIX_RIDER_JOURNAL_FILE", "/opt/strategy/logs/vix_rider_trade_journal.csv")

FIELDNAMES = [
    "date", "time", "symbol", "direction",
    "vix_at_entry", "allocated_capital_pct",
    "opening_range_high", "opening_range_low", "opening_range_midpoint",
    "entry_price", "initial_stop_loss", "trailing_amt", "quantity",
    "result", "pnl_note",
]


def log_vix_rider_trade(trade: dict, path: str = None) -> bool:
    """
    Voegt één rij toe aan het VIX Rider trade journal. Maakt het
    bestand met header aan als het nog niet bestaat.

    Faalt nooit hard -- het journal is aanvullend, niet kritiek voor
    de trading-logica zelf.
    """
    if path is None:
        path = JOURNAL_PATH

    now = datetime.now(timezone.utc)
    row = {
        "date": trade.get("date", now.date().isoformat()),
        "time": trade.get("time", now.strftime("%H:%M:%S")),
        **{k: trade.get(k, "") for k in FIELDNAMES if k not in ("date", "time")},
    }

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        file_exists = os.path.exists(path)

        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)

        logger.info(f"VIX Rider trade gelogd in journal: {row['symbol']} {row['result']}")
        return True
    except Exception as e:
        logger.error(f"Kon VIX Rider trade niet loggen in journal: {e}")
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        test_path = os.path.join(tmp_dir, "test_vix_rider_journal.csv")

        trade = {
            "symbol": "NVDA", "direction": "LONG",
            "vix_at_entry": 27.5, "allocated_capital_pct": 0.75,
            "opening_range_high": 460.0, "opening_range_low": 449.0,
            "opening_range_midpoint": 454.5,
            "entry_price": 462.0, "initial_stop_loss": 454.5,
            "trailing_amt": 7.5, "quantity": 2.1645,
            "result": "trail_stop_hit", "pnl_note": "exacte exit-prijs niet opgehaald",
        }
        success = log_vix_rider_trade(trade, test_path)
        print(f"Scenario 1 (loggen): success={success}")

        with open(test_path) as f:
            print(f.read())
