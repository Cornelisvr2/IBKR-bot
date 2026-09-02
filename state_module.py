"""
state_module.py — Touch & Turn Scalper, Gedeelde Status

Eén JSON-bestand op schijf dat fungeert als gedeeld geheugen tussen
de Telegram-bot (telegram_bot.py, luistert continu) en de strategie
(main.py, draait via cron-jobs) -- twee losse processen die niet
zomaar een Python-variabele kunnen delen.

Bevat:
    trading_enabled: bool   -- of main.py nieuwe trades mag openen
    risk_pct: float         -- huidig risicopercentage per trade
    positions: list         -- actieve open posities
    trade_log: list         -- afgeronde trades (voor /performance)
    last_updated: str       -- ISO-timestamp van laatste wijziging

Deze module heeft GEEN Telegram- of IBKR-verbinding nodig om te
testen -- puur bestands-I/O.

Gebruik in andere modules:
    from state_module import load_state, save_state, set_trading_enabled
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger("state_module")

STATE_FILE_PATH = os.environ.get("TTS_STATE_FILE", "/opt/strategy/state.json")

DEFAULT_STATE = {
    "trading_enabled": True,
    "risk_pct": 0.01,
    "positions": [],
    "trade_log": [],
    "last_updated": None,
}


def load_state(path: str = None) -> dict:
    """
    Laadt de huidige status van schijf. Als het bestand nog niet
    bestaat, wordt een verse standaardstatus aangemaakt en opgeslagen.

    path=None (standaard) zoekt STATE_FILE_PATH dynamisch op bij elke
    aanroep -- zo kan het pad ook nog op runtime aangepast worden
    (bijv. door tests), in tegenstelling tot een vastgelegde default
    die maar één keer bij het definiëren van de functie wordt bepaald.
    """
    if path is None:
        path = STATE_FILE_PATH

    if not os.path.exists(path):
        logger.info(f"Geen statusbestand gevonden op {path} -- nieuwe status aanmaken.")
        save_state(DEFAULT_STATE.copy(), path)
        return DEFAULT_STATE.copy()

    with open(path, "r") as f:
        state = json.load(f)
    return state


def save_state(state: dict, path: str = None) -> None:
    if path is None:
        path = STATE_FILE_PATH
    """Slaat de status op naar schijf, met een bijgewerkte timestamp."""
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, path)  # atomaire schrijfactie, voorkomt corrupte state bij crash
    logger.info(f"Status opgeslagen naar {path}")


def set_trading_enabled(enabled: bool, path: str = None) -> dict:
    """Zet de trading_enabled-vlag aan of uit. Gebruikt door /stop_trading en /start_trading."""
    state = load_state(path)
    state["trading_enabled"] = enabled
    save_state(state, path)
    logger.info(f"trading_enabled gezet op {enabled}")
    return state


def set_risk_pct(risk_pct: float, path: str = None) -> dict:
    """Past het risicopercentage per trade aan. Gebruikt door /update_risk."""
    if not (0 < risk_pct <= 0.1):
        raise ValueError(f"risk_pct {risk_pct} buiten redelijke grenzen (0-10%).")
    state = load_state(path)
    state["risk_pct"] = risk_pct
    save_state(state, path)
    logger.info(f"risk_pct gezet op {risk_pct}")
    return state


STARTING_SIMULATED_BALANCE = 2000.0


def get_simulated_balance(path: str = None) -> float:
    """
    Haalt het huidige gesimuleerde saldo op -- start op
    STARTING_SIMULATED_BALANCE (€2000) en wordt bijgewerkt na elke
    gesloten trade (zowel scalper als VIX Rider, want het is
    conceptueel dezelfde inleg die via de VIX-schaal verdeeld wordt).

    Dit is compounding: winst/verlies wordt herbelegd, dus de
    positiegrootte-berekeningen van morgen zijn gebaseerd op het
    resultaat van vandaag, niet op een vast startbedrag.
    """
    state = load_state(path)
    return state.get("simulated_balance", STARTING_SIMULATED_BALANCE)


def update_simulated_balance(pnl: float, path: str = None) -> float:
    """
    Werkt het gesimuleerde saldo bij met het resultaat van een
    afgeronde trade (compounding). Aan te roepen door ELKE strategie
    zodra een trade sluit (TP/SL/trailing-stop geraakt).

    Returns:
        Het NIEUWE saldo, na verwerking van deze trade.
    """
    state = load_state(path)
    current_balance = state.get("simulated_balance", STARTING_SIMULATED_BALANCE)
    new_balance = current_balance + pnl
    state["simulated_balance"] = new_balance
    save_state(state, path)
    logger.info(f"Gesimuleerd saldo bijgewerkt: €{current_balance:.2f} {'+' if pnl >= 0 else ''}{pnl:.2f} -> €{new_balance:.2f}")
    return new_balance


def add_position(position: dict, path: str = None) -> dict:
    """
    Voegt een open positie toe aan de positielijst -- aangeroepen
    zodra een entry-order bevestigd is gevuld (zie order_module.py's
    execute_managed_trade).

    Args:
        position: dict met minimaal 'symbol', 'direction', 'entry_price',
                  'quantity' -- getoond door /status in de Telegram-bot.
    """
    state = load_state(path)
    state["positions"].append(position)
    save_state(state, path)
    logger.info(f"Positie toegevoegd: {position.get('symbol')} {position.get('direction')}")
    return state


def remove_position(symbol: str, path: str = None) -> dict:
    """
    Verwijdert een positie uit de positielijst op basis van symbool --
    aangeroepen zodra een trade sluit (TP/SL geraakt, zie
    order_module.py's report_trade_outcome).

    Verwijdert de EERSTE match op symbool -- bij meerdere gelijktijdige
    posities in hetzelfde symbool (zou niet moeten voorkomen gezien
    max 3 trades/sessie op verschillende symbolen) is dit niet
    eenduidig, maar dat scenario is voor deze strategie niet van
    toepassing.
    """
    state = load_state(path)
    positions = state.get("positions", [])
    for i, pos in enumerate(positions):
        if pos.get("symbol") == symbol:
            removed = positions.pop(i)
            save_state(state, path)
            logger.info(f"Positie verwijderd: {symbol}")
            return state
    logger.warning(f"Geen open positie gevonden voor {symbol} om te verwijderen.")
    return state


def add_trade_to_log(trade_summary: dict, path: str = None) -> dict:
    """
    Voegt een afgeronde trade toe aan de log. Gebruikt door order_module
    na sluiting van een positie.

    Voegt automatisch een 'date' veld toe (ISO-datum, vandaag) als dat
    nog niet in trade_summary zit -- nodig om later "resultaat vandaag"
    te kunnen filteren (bijv. voor een 3%-dagstop).
    """
    if "date" not in trade_summary:
        trade_summary["date"] = datetime.now(timezone.utc).date().isoformat()

    state = load_state(path)
    state["trade_log"].append(trade_summary)
    save_state(state, path)
    return state


def get_performance_summary(path: str = None) -> dict:
    """
    Vat de trade_log samen voor het /performance-commando: aantal
    trades, winrate, totaal resultaat.
    """
    state = load_state(path)
    trades = state["trade_log"]

    if not trades:
        return {"trade_count": 0, "win_count": 0, "win_rate": 0.0, "total_pnl": 0.0}

    win_count = sum(1 for t in trades if t.get("pnl", 0) > 0)
    total_pnl = sum(t.get("pnl", 0) for t in trades)

    return {
        "trade_count": len(trades),
        "win_count": win_count,
        "win_rate": round(win_count / len(trades) * 100, 1),
        "total_pnl": round(total_pnl, 2),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import tempfile

    # Test met een tijdelijk bestand, zodat we de echte state.json niet raken.
    with tempfile.TemporaryDirectory() as tmp_dir:
        test_path = os.path.join(tmp_dir, "test_state.json")

        # Scenario 1: eerste keer laden -> standaardstatus
        state = load_state(test_path)
        print(f"Scenario 1 (initiële status): trading_enabled={state['trading_enabled']}")

        # Scenario 2: trading uitzetten
        state = set_trading_enabled(False, test_path)
        print(f"Scenario 2 (na stop): trading_enabled={state['trading_enabled']}")

        # Scenario 3: trading weer aanzetten
        state = set_trading_enabled(True, test_path)
        print(f"Scenario 3 (na herstart): trading_enabled={state['trading_enabled']}")

        # Scenario 4: risico aanpassen
        state = set_risk_pct(0.005, test_path)
        print(f"Scenario 4 (risico aangepast): risk_pct={state['risk_pct']}")

        # Scenario 5: trades loggen en performance opvragen
        add_trade_to_log({"symbol": "ASML", "direction": "SHORT", "pnl": 15.50}, test_path)
        add_trade_to_log({"symbol": "AAPL", "direction": "LONG", "pnl": -7.25}, test_path)
        add_trade_to_log({"symbol": "ASML", "direction": "LONG", "pnl": 22.00}, test_path)
        summary = get_performance_summary(test_path)
        print(f"Scenario 5 (performance): {summary}")

        # Scenario 7: automatisch datumveld
        state = load_state(test_path)
        laatste_trade = state["trade_log"][-1]
        print(f"Scenario 7 (auto-datum): 'date' in trade = {'date' in laatste_trade}, waarde = {laatste_trade.get('date')}")

        # Scenario 8: positie toevoegen en verwijderen
        state = add_position({"symbol": "AAPL", "direction": "SHORT", "entry_price": 316.5, "quantity": 12}, test_path)
        print(f"\nScenario 8 (positie toegevoegd): {len(state['positions'])} open posities")

        state = add_position({"symbol": "MSFT", "direction": "LONG", "entry_price": 420.0, "quantity": 5}, test_path)
        print(f"Scenario 9 (tweede positie): {len(state['positions'])} open posities")

        state = remove_position("AAPL", test_path)
        symbolen = [p["symbol"] for p in state["positions"]]
        print(f"Scenario 10 (AAPL gesloten): resterende symbolen = {symbolen}")

        state = remove_position("NIETBESTAAND", test_path)
        print(f"Scenario 11 (niet-bestaand symbool, geen crash): {len(state['positions'])} open posities")

        # Scenario 12: compounding saldo -- start op €2000
        balans = get_simulated_balance(test_path)
        print(f"\nScenario 12 (startsaldo): €{balans:.2f} (verwacht: €2000.00)")

        # Scenario 13: winst compounden
        nieuw_saldo = update_simulated_balance(5.0, test_path)
        print(f"Scenario 13 (na +€5 winst): €{nieuw_saldo:.2f} (verwacht: €2005.00)")

        # Scenario 14: tweede trade, saldo bouwt cumulatief op de vorige voort
        nieuw_saldo = update_simulated_balance(-2.0, test_path)
        print(f"Scenario 14 (na -€2 verlies): €{nieuw_saldo:.2f} (verwacht: €2003.00 -- cumulatief op vorige, niet terug naar €2000)")

        # Scenario 6: ongeldig risicopercentage -> moet een fout geven
        try:
            set_risk_pct(0.5, test_path)
        except ValueError as e:
            print(f"Scenario 6 (verwachte fout): {e}")
