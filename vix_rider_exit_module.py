"""
vix_rider_exit_module.py — VIX Rider, Module 2: Positiegrootte & Trailing Stop

BELANGRIJK RISICOPUNT: de stop-loss van VIX Rider ligt op het midden
van de Opening Range -- vaak een veel KLEINERE afstand dan de
Fibonacci-gebaseerde stop van Touch & Turn Scalper. De normale 1%-
risicoformule (risicobedrag / stop-afstand) zou bij een smalle range
een gevaarlijk GROTE positie berekenen. Deze module bouwt daarom een
expliciete maximale-positiewaarde-limiet, los van de risicoformule.

Take-profit is GEEN vaste prijs (in tegenstelling tot de scalper) --
VIX Rider gebruikt een TRAILING stop-loss die meebeweegt met de koers,
om zoveel mogelijk van een aanhoudende beweging mee te pakken. Deze
module berekent de INITIËLE stop-loss en positiegrootte; het
meebewegen zelf gebeurt in een aparte bewakingslus (nog te bouwen,
afhankelijk van of IBKR's Web API een ingebouwd TRAIL-ordertype
ondersteunt -- nog te verifiëren).

Deze module heeft GEEN live IBKR-verbinding nodig om te testen.

Gebruik in andere modules:
    from vix_rider_exit_module import calculate_vix_rider_position
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from vix_rider_entry_module import BreakoutSignal

logger = logging.getLogger("vix_rider_exit_module")

RISK_PER_TRADE_PCT = 0.01      # 1% van het toegewezen kapitaal, zelfde als de scalper
MAX_POSITION_VALUE_PCT = 0.50  # NOOIT meer dan 50% van het toegewezen kapitaal in één positie,
                                # ongeacht wat de risicoformule zou berekenen bij een smalle stop.
                                # (Was aanvankelijk 25%, maar bleek bij realistische kapitaal-
                                # bedragen van €1000-2000 tegen dure aandelen zoals NVDA (~€460)
                                # vrijwel elke trade te blokkeren -- zelfs 1 aandeel kan al
                                # 25-45% van het kapitaal beslaan. 50% is nog steeds een
                                # zinvolle concentratielimiet, maar laat normale trades door.
TRAILING_STOP_DISTANCE_PCT = 0.015  # 1,5% -- initiële trailing-afstand, aan te passen bij live-testen


@dataclass
class VixRiderPositionPlan:
    """Het resultaat van de positiegrootte-berekening voor een VIX Rider-trade."""
    direction: str
    entry_price: float
    initial_stop_loss: float
    quantity: int
    risk_amount: float
    position_value: float
    capped_by_max_value: bool  # True als de 25%-limiet de bepalende factor was, niet de 1%-risicoformule
    trailing_distance_pct: float
    reason: str

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "entry_price": self.entry_price,
            "initial_stop_loss": self.initial_stop_loss,
            "quantity": self.quantity,
            "risk_amount": self.risk_amount,
            "position_value": self.position_value,
            "capped_by_max_value": self.capped_by_max_value,
            "trailing_distance_pct": self.trailing_distance_pct,
            "reason": self.reason,
        }


def calculate_vix_rider_position(
    signal: BreakoutSignal,
    capital: float,
    risk_pct: float = RISK_PER_TRADE_PCT,
    max_position_value_pct: float = MAX_POSITION_VALUE_PCT,
    trailing_distance_pct: float = TRAILING_STOP_DISTANCE_PCT,
) -> VixRiderPositionPlan:
    """
    Berekent de positiegrootte voor een VIX Rider-trade, met een
    veiligheidslimiet tegen de smalle-stop-valkuil.

    Twee limieten worden berekend, en de KLEINSTE van de twee wint:
        1. Risicoformule: risicobedrag (1% van kapitaal) / stop-afstand
        2. Maximale positiewaarde: max_position_value_pct van kapitaal / entry-prijs

    Args:
        signal: BreakoutSignal uit vix_rider_entry_module
        capital: TOEGEWEZEN kapitaal (via risk_module.get_allocated_capital,
                 NIET het volledige totale kapitaal -- VIX Rider deelt
                 het kapitaal met de scalper via de VIX-glijdende-schaal)

    Returns:
        VixRiderPositionPlan met de uiteindelijke positiegrootte.

    Raises:
        ValueError: als de stop-afstand 0 is (entry == opening_range.midpoint,
                    zou niet moeten voorkomen bij een geldig signaal).
    """
    entry_price = signal.entry_price
    stop_loss = signal.opening_range.midpoint
    stop_distance = abs(entry_price - stop_loss)

    if stop_distance == 0:
        raise ValueError("Stop-afstand is 0 -- geen geldige trade mogelijk.")

    risk_amount = capital * risk_pct
    # Fractionele aandelen worden ondersteund door IBKR (live bevestigd
    # 21 aug 2026), dus we ronden NIET meer af naar een heel getal.
    quantity_from_risk = round(risk_amount / stop_distance, 4)

    max_position_value = capital * max_position_value_pct
    quantity_from_max_value = round(max_position_value / entry_price, 4)

    capped_by_max_value = quantity_from_max_value < quantity_from_risk
    quantity = min(quantity_from_risk, quantity_from_max_value)

    position_value = quantity * entry_price
    actual_risk = quantity * stop_distance

    reason = (
        f"{signal.direction}: entry {entry_price:.4f}, initiële SL {stop_loss:.4f} "
        f"(OR-midpoint), positiegrootte {quantity} "
        f"({'GELIMITEERD door max-positiewaarde' if capped_by_max_value else 'op basis van 1%-risico'}), "
        f"positiewaarde €{position_value:.2f}, werkelijk risico €{actual_risk:.2f}"
    )

    min_position_value = 5.0  # aanname, niet live geverifieerd -- zie exit_module.py
    if position_value < min_position_value:
        logger.warning(f"Positiewaarde (€{position_value:.2f}) onder het minimum -- trade niet uitvoerbaar. {reason}")
    else:
        logger.info(f"VIX Rider positieplan berekend: {reason}")

    return VixRiderPositionPlan(
        direction=signal.direction,
        entry_price=entry_price,
        initial_stop_loss=stop_loss,
        quantity=quantity,
        risk_amount=actual_risk,
        position_value=position_value,
        capped_by_max_value=capped_by_max_value,
        trailing_distance_pct=trailing_distance_pct,
        reason=reason,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from vix_rider_entry_module import OpeningRange
    from datetime import datetime

    # Scenario 1: normale range, risicoformule is bepalend (niet gelimiteerd)
    normale_range = OpeningRange(high=460.0, low=449.0, midpoint=454.5)
    signal = BreakoutSignal(
        direction="LONG", entry_price=462.0, opening_range=normale_range,
        breakout_time=datetime(2026, 8, 21, 16, 10), reason="test",
    )
    plan = calculate_vix_rider_position(signal, capital=1000.0)
    print(f"Scenario 1 (normale range): {plan.to_dict()}")
    print(f"(stop-afstand = {462.0 - 454.5:.2f} -- met 50%-cap (€500) past 1 aandeel a.d. €462, dus capped_by_max_value=False verwacht)")

    # Scenario 2: EXTREEM smalle range -> max-positiewaarde-limiet moet ingrijpen
    smalle_range = OpeningRange(high=460.1, low=459.9, midpoint=460.0)
    smal_signal = BreakoutSignal(
        direction="LONG", entry_price=460.5, opening_range=smalle_range,
        breakout_time=datetime(2026, 8, 21, 16, 10), reason="test",
    )
    plan = calculate_vix_rider_position(smal_signal, capital=1000.0)
    print(f"\nScenario 2 (smalle range, verwacht capped): {plan.to_dict()}")
    print(f"(stop-afstand = 0.5, risicoformule zou {int(10.0/0.5)} aandelen geven a.d. €460.5 = €{int(10.0/0.5)*460.5:.0f} -- veel te veel, moet gelimiteerd worden tot 1 aandeel (50%-cap = €500))")

    # Scenario 3: SHORT-richting, controleren dat stop-afstand correct absoluut wordt berekend
    short_signal = BreakoutSignal(
        direction="SHORT", entry_price=447.0, opening_range=normale_range,
        breakout_time=datetime(2026, 8, 21, 16, 10), reason="test",
    )
    plan = calculate_vix_rider_position(short_signal, capital=1000.0)
    print(f"\nScenario 3 (SHORT): {plan.to_dict()}")

    # Scenario 4: stop-afstand 0 -> ValueError
    try:
        nul_range = OpeningRange(high=460.0, low=449.0, midpoint=460.0)  # kunstmatig, entry == midpoint
        nul_signal = BreakoutSignal(
            direction="LONG", entry_price=460.0, opening_range=nul_range,
            breakout_time=datetime(2026, 8, 21, 16, 10), reason="test",
        )
        calculate_vix_rider_position(nul_signal, capital=1000.0)
    except ValueError as e:
        print(f"\nScenario 4 (verwachte fout): {e}")
