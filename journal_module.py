"""
journal_module.py — Touch & Turn Scalper, Trade Journal

Legt per afgeronde trade een rij vast in trade_journal.csv, met de
context die nodig is om later te kunnen analyseren en verbeteren:
selectiemoment (nieuwsscore), setup-moment (ATR/Fibonacci), resultaat.

Los van state_module.py's trade_log (die is voor de operationele
status/dagstop) -- dit is puur voor analyse achteraf, groeit
onbeperkt, en staat in een los CSV-bestand i.p.v. in state.json.

LET OP: de exacte gevulde prijs (fill price) kon vandaag niet
betrouwbaar via de Web API worden opgehaald (/iserver/account/trades
gaf een lege lijst terug, ook na bevestigde fills). Dit journal
gebruikt daarom de BEOOGDE prijzen (entry/TP/SL uit de strategie) als
benadering -- nauwkeurig zolang orders exact op de limietprijs vullen
(wat bij alle tests vandaag het geval was), maar bij slippage zou dit
kunnen afwijken van de werkelijke fill. Te verbeteren zodra een
betrouwbare fill-prijs-bron gevonden is.

Gebruik:
    from journal_module import log_trade
    log_trade({...})
"""

from __future__ import annotations

import csv
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger("journal_module")

JOURNAL_PATH = os.environ.get("TTS_JOURNAL_FILE", "/opt/strategy/logs/trade_journal.csv")

FIELDNAMES = [
    "date", "time", "symbol", "direction",
    "news_score", "news_reason",
    "atr", "atr_ratio",
    "entry_price", "take_profit", "stop_loss", "quantity",
    "result", "pnl_estimate", "pnl_note",
]


def log_trade(trade: dict, path: str = None) -> bool:
    """
    Voegt één rij toe aan het trade journal (CSV). Maakt het bestand
    met header aan als het nog niet bestaat.

    Args:
        trade: dict met (waar mogelijk) de FIELDNAMES-sleutels.
               Ontbrekende velden worden leeg gelaten -- dit mag nooit
               een crash veroorzaken, want het journal is aanvullend,
               niet kritiek voor de trading-logica zelf.
        path: override voor het bestandspad (voor testen).

    Returns:
        True bij succes, False bij een fout (gelogd, niet ge-raised).
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

        logger.info(f"Trade gelogd in journal: {row['symbol']} {row['result']}")
        return True
    except Exception as e:
        logger.error(f"Kon trade niet loggen in journal: {e}")
        return False


def estimate_pnl(direction: str, entry_price: float, exit_price: float, quantity: float) -> float:
    """
    Schat het BRUTO resultaat in dollars, op basis van de BEOOGDE
    prijzen (zie moduledocstring voor de kanttekening over exacte
    fill-prijs). Bevat GEEN fees -- gebruik estimate_net_pnl() voor
    het resultaat na aftrek van commissiekosten.

    LONG:  winst = (exit - entry) * quantity
    SHORT: winst = (entry - exit) * quantity
    """
    if direction == "LONG":
        return (exit_price - entry_price) * quantity
    elif direction == "SHORT":
        return (entry_price - exit_price) * quantity
    else:
        raise ValueError(f"Onbekende richting: {direction}")


# IBKR-TARIEVEN VOOR NEDERLAND (bevestigd door de gebruiker via de
# officiële prijspagina op 25 aug 2026) -- FRACTIONELE AANDELEN,
# aangezien deze strategieën vrijwel altijd fractionele posities
# gebruiken: Fixed - IB SmartRouting, 0,05% van de handelswaarde,
# minimum EUR 1,25 per order. GECENTRALISEERD hier (26 aug 2026) zodat
# zowel de live-trade-rapportage (order_module.py, vix_rider_order_
# module.py) als de schaduw-scan (shadow_outcome_checker.py) dezelfde,
# ene berekening gebruiken -- voorheen alleen in de schaduw-scan
# geïmplementeerd, wat een inconsistentie gaf tussen wat live-meldingen
# toonden (bruto) en wat de schaduw-scan mat (netto na fees).
FEE_PERCENTAGE = 0.0005  # 0,05% van de handelswaarde
MIN_FEE_PER_ORDER = 1.25  # EUR, minimum voor fractionele aandelen (Fixed-tarief)


def calculate_order_fee(quantity: float, price: float) -> float:
    """
    Berekent de commissie voor één order volgens IBKR's Nederlandse
    Fixed-tarief voor fractionele aandelen: het hoogste van
    (handelswaarde x 0,05%) of het minimumbedrag van EUR 1,25.
    """
    handelswaarde = quantity * price
    return max(handelswaarde * FEE_PERCENTAGE, MIN_FEE_PER_ORDER)


def estimate_net_pnl(direction: str, entry_price: float, exit_price: float, quantity: float) -> dict:
    """
    Schat het resultaat NA aftrek van entry- en exit-commissie.

    Returns:
        dict met "pnl_gross", "entry_fee", "exit_fee", "pnl_net".
    """
    pnl_gross = estimate_pnl(direction, entry_price, exit_price, quantity)
    entry_fee = calculate_order_fee(quantity, entry_price)
    exit_fee = calculate_order_fee(quantity, exit_price)
    return {
        "pnl_gross": pnl_gross,
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "pnl_net": pnl_gross - entry_fee - exit_fee,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        test_path = os.path.join(tmp_dir, "test_journal.csv")

        # Scenario 1: PnL-berekening
        pnl = estimate_pnl("SHORT", entry_price=316.50, exit_price=310.00, quantity=12)
        print(f"Scenario 1 (SHORT, TP geraakt): pnl={pnl:.2f} (verwacht: 78.00)")

        pnl = estimate_pnl("LONG", entry_price=99.50, exit_price=99.02, quantity=10)
        print(f"Scenario 2 (LONG, SL geraakt): pnl={pnl:.2f} (verwacht: -4.80)")

        # Scenario 3: trade loggen naar CSV
        trade = {
            "symbol": "AAPL", "direction": "SHORT",
            "news_score": 24.0, "news_reason": "225 berichten, sentiment -0.05",
            "atr": 3.0, "atr_ratio": 1.17,
            "entry_price": 316.50, "take_profit": 310.00, "stop_loss": 320.00,
            "quantity": 12, "result": "take_profit_hit",
            "pnl_estimate": 78.00, "pnl_note": "benadering o.b.v. beoogde prijzen",
        }
        success = log_trade(trade, test_path)
        print(f"\nScenario 3 (loggen): success={success}")

        with open(test_path) as f:
            print(f.read())

    # Scenario 4: netto PnL na fees, gecentraliseerde berekening
    netto = estimate_net_pnl("SHORT", entry_price=316.50, exit_price=310.00, quantity=12)
    print(f"\nScenario 4 (netto PnL na fees): {netto}")
    print(f"(bruto verwacht: 78.00, entry-fee en exit-fee elk het minimum €1,25 tenzij hoger)")
