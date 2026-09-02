"""
shadow_journal_module.py — Schaduw-scan Journal

Twee losse CSV-bestanden:
    - shadow_signals.csv: elk geldig ATR-signaal gevonden tijdens de
      dagelijkse scan van ALLE 26 aandelen (niet alleen de 3
      nieuws-geselecteerde), met een markering of het aandeel ook
      daadwerkelijk nieuws-geselecteerd was die dag.
    - shadow_outcomes.csv: de latere, hypothetische uitkomst van elk
      gelogd signaal (TP geraakt, SL geraakt, entry nooit bereikt,
      etc.), bepaald door shadow_outcome_checker.py ná marktsluiting.

Gekoppeld via (date, symbol) -- geen in-place CSV-updates nodig, wat
CSV-bestanden notoir lastig maken; in plaats daarvan twee append-only
bestanden die je achteraf op elkaar aansluit (bijv. in een spreadsheet
of analysescript).

Dit is PUUR OBSERVATIE -- er wordt nooit een order geplaatst op basis
van deze data. Het doel is empirisch meten of nieuwsselectie
daadwerkelijk een hogere winratio oplevert dan een brede scan van de
hele watchlist, in plaats van dat te veronderstellen.
"""

from __future__ import annotations

import csv
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger("shadow_journal_module")

SIGNALS_PATH = os.environ.get("SHADOW_SIGNALS_FILE", "/opt/strategy/logs/shadow_signals.csv")
OUTCOMES_PATH = os.environ.get("SHADOW_OUTCOMES_FILE", "/opt/strategy/logs/shadow_outcomes.csv")

SIGNAL_FIELDNAMES = [
    "date", "time", "symbol", "news_selected",
    "atr", "opening_range", "direction", "quantity",
    "entry_price", "take_profit", "stop_loss", "reason",
]

OUTCOME_FIELDNAMES = [
    "date", "symbol", "outcome", "exit_price_estimate",
    "pnl_gross", "pnl_net", "checked_at",
]


def _append_row(path: str, fieldnames: list[str], row: dict) -> bool:
    """Gedeelde, generieke append-logica voor beide journal-bestanden."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        file_exists = os.path.exists(path)
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
        return True
    except Exception as e:
        logger.error(f"Kon rij niet toevoegen aan {path}: {e}")
        return False


def log_shadow_signal(signal: dict, path: str = None) -> bool:
    """Logt één gevonden ATR-signaal tijdens de brede scan."""
    now = datetime.now(timezone.utc)
    row = {
        "date": signal.get("date", now.date().isoformat()),
        "time": signal.get("time", now.strftime("%H:%M:%S")),
        **{k: signal.get(k, "") for k in SIGNAL_FIELDNAMES if k not in ("date", "time")},
    }
    success = _append_row(path or SIGNALS_PATH, SIGNAL_FIELDNAMES, row)
    if success:
        logger.info(f"Schaduw-signaal gelogd: {row['symbol']} ({'nieuws-geselecteerd' if row['news_selected'] else 'alleen ATR'})")
    return success


def log_shadow_outcome(outcome: dict, path: str = None) -> bool:
    """Logt de hypothetische uitkomst van een eerder gelogd signaal."""
    now = datetime.now(timezone.utc)
    row = {
        "date": outcome.get("date", now.date().isoformat()),
        "checked_at": outcome.get("checked_at", now.strftime("%H:%M:%S")),
        **{k: outcome.get(k, "") for k in OUTCOME_FIELDNAMES if k not in ("date", "checked_at")},
    }
    success = _append_row(path or OUTCOMES_PATH, OUTCOME_FIELDNAMES, row)
    if success:
        logger.info(f"Schaduw-uitkomst gelogd: {row['symbol']} -> {row['outcome']}")
    return success


def read_todays_signals(date_str: str = None, path: str = None) -> list[dict]:
    """Leest alle gelogde signalen van een specifieke dag (standaard: vandaag)."""
    if date_str is None:
        date_str = datetime.now(timezone.utc).date().isoformat()
    p = path or SIGNALS_PATH
    if not os.path.exists(p):
        return []
    with open(p, newline="") as f:
        reader = csv.DictReader(f)
        return [row for row in reader if row.get("date") == date_str]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        signals_path = os.path.join(tmp_dir, "test_signals.csv")
        outcomes_path = os.path.join(tmp_dir, "test_outcomes.csv")

        # Scenario 1: signaal loggen voor een nieuws-geselecteerd aandeel
        log_shadow_signal({
            "date": "2026-08-25", "symbol": "NVDA", "news_selected": True,
            "atr": 5.75, "opening_range": 2.01, "direction": "SHORT",
            "entry_price": 216.73, "take_profit": 217.50, "stop_loss": 216.35,
            "reason": "test",
        }, signals_path)

        # Scenario 2: signaal loggen voor een NIET-geselecteerd aandeel (alleen ATR)
        log_shadow_signal({
            "date": "2026-08-25", "symbol": "ORCL", "news_selected": False,
            "atr": 3.20, "opening_range": 1.10, "direction": "LONG",
            "entry_price": 145.20, "take_profit": 146.80, "stop_loss": 144.40,
            "reason": "test",
        }, signals_path)

        signalen = read_todays_signals("2026-08-25", signals_path)
        print(f"Scenario 1+2 (signalen van vandaag): {len(signalen)} gevonden")
        for s in signalen:
            print(f"  {s['symbol']}: news_selected={s['news_selected']}, {s['direction']} @ {s['entry_price']}")

        # Scenario 3: uitkomst loggen
        log_shadow_outcome({
            "date": "2026-08-25", "symbol": "NVDA",
            "outcome": "stop_loss_hit", "exit_price_estimate": 216.35,
        }, outcomes_path)

        with open(outcomes_path) as f:
            print(f"\nScenario 3 (outcomes.csv inhoud):\n{f.read()}")
