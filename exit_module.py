"""
exit_module.py — Touch & Turn Scalper, Module 4: Exit (Take Profit & Stop Loss)

Berekent, op basis van een EntrySignal (uit entry_module.py):
    - Take Profit: het 61.8% Fibonacci-niveau (primair) of 38.2%
      (conservatief), al berekend in entry_module.py.
    - Stop Loss: zodanig geplaatst dat de Reward/Risk-ratio minimaal
      2:1 is (SL-afstand = TP-afstand / R/R-ratio).
    - Positiegrootte: (Kapitaal x risico%) / |Instapprijs - Stop Loss|

Let op richting: bij SHORT ligt de Stop Loss BOVEN de entry-prijs
(verlies bij stijgende koers); bij LONG ligt de Stop Loss ERONDER
(verlies bij dalende koers). Dit wordt hier per richting correct
afgehandeld.

Deze module heeft GEEN live IBKR-verbinding nodig om te testen.

Gebruik in andere modules:
    from exit_module import calculate_exit_levels
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from entry_module import EntrySignal, generate_entry_signal
from data_module import Candle
from datetime import datetime

MAX_POSITION_VALUE_PCT = 0.50  # KRITIEKE FIX (21 aug 2026): zonder deze limiet kan de
                                 # risicoformule (risicobedrag/stop-afstand) een positiewaarde
                                 # berekenen die het beschikbare kapitaal FORS overschrijdt --
                                 # live gebeurd bij een MSFT-trade: een smalle stop-afstand
                                 # (~€0,78) t.o.v. een dure aandelenprijs (~€480) resulteerde
                                 # in een positiewaarde van ~€12.343 bij slechts €2.000
                                 # toegewezen kapitaal (6x overschrijding). Dit probleem was
                                 # al herkend en opgelost bij VIX Rider
                                 # (vix_rider_exit_module.py), maar nooit teruggeport naar de
                                 # scalper. Zelfde 50%-limiet nu hier ook toegepast.

logger = logging.getLogger("exit_module")

MIN_RR_RATIO = 2.0          # minimale Reward/Risk-ratio
RISK_PER_TRADE_PCT = 0.01   # 1% van kapitaal per trade


@dataclass
class ExitPlan:
    """Het resultaat van de exit-berekening: TP, SL en positiegrootte."""
    direction: str
    entry_price: float
    take_profit: float
    stop_loss: float
    rr_ratio: float
    position_size: float
    risk_amount: float
    reason: str
    capped_by_max_value: bool = False  # True als de 50%-max-positiewaarde-limiet
                                          # bepalend was i.p.v. de 1%-risicoformule

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "entry_price": self.entry_price,
            "take_profit": self.take_profit,
            "stop_loss": self.stop_loss,
            "rr_ratio": self.rr_ratio,
            "position_size": self.position_size,
            "risk_amount": self.risk_amount,
            "reason": self.reason,
            "capped_by_max_value": self.capped_by_max_value,
        }


def calculate_exit_levels(
    signal: EntrySignal,
    capital: float,
    min_rr_ratio: float = MIN_RR_RATIO,
    risk_pct: float = RISK_PER_TRADE_PCT,
) -> ExitPlan:
    """
    Berekent take-profit, stop-loss en positiegrootte voor een gegeven
    entry-signaal.

    KRITIEKE FIX (25 aug 2026): teruggevonden en gecontroleerd tegen
    het ORIGINELE strategie-document (de blauwdruk waarmee dit project
    ooit begon). Het origineel kent GEEN "primaire 61,8% / conservatieve
    38,2%"-keuze -- die had deze codebase er ooit zelf bij verzonnen,
    vermoedelijk een misinterpretatie tijdens de allereerste bouwsessie.
    Het origineel heeft maar ÉÉN formule:

        LONG:  TP = Low  + (Range x 0,382)
        SHORT: TP = High - (Range x 0,382)

    Onze interne representatie berekent Fibonacci-niveaus altijd als
    retracement VANAF DE HIGH (fib_382 = High - 0,382*Range, fib_618 =
    High - 0,618*Range) -- ongeacht handelsrichting. Met wiskunde
    uitgewerkt (High=110, Low=100 als voorbeeld) blijkt:
        - Origineel LONG-TP (Low + 0,382*Range) == onze fib_618
        - Origineel SHORT-TP (High - 0,382*Range) == onze fib_382

    De vorige code gebruikte ALTIJD fib_618 (de "primaire" modus),
    ongeacht richting -- correct voor LONG (toeval, door de wiskunde),
    maar FOUT voor SHORT: een SHORT-trade mikte zo op TP 103,82 i.p.v.
    de bedoelde 106,18 (in het 110/100-voorbeeld) -- een TP-afstand die
    RUIM TWEE KEER ZO GROOT was als het origineel bedoelde. Dit trof
    naar schatting de helft van alle trades (alle SHORT-trades) sinds
    de start van dit project, inclusief de GOOGL- en NVDA-trades van
    deze week.

    Args:
        signal: EntrySignal uit entry_module.generate_entry_signal()
        capital: beschikbaar handelskapitaal in euro's
        min_rr_ratio: minimale reward/risk-ratio (standaard 2.0 = 2:1)
        risk_pct: percentage van kapitaal dat op het spel staat per trade

    Returns:
        ExitPlan met alle berekende niveaus.

    Raises:
        ValueError: als take_profit gelijk is aan entry_price (geen
                    ruimte voor een TP-afstand).
    """
    # Richtingsafhankelijke TP-selectie, conform het originele
    # 38,2%-vanaf-entry-ontwerp -- GEEN vaste modus meer.
    take_profit = signal.fib_618 if signal.direction == "LONG" else signal.fib_382
    tp_distance = abs(take_profit - signal.entry_price)

    if tp_distance == 0:
        raise ValueError("Take-profit-afstand is 0 -- geen geldige trade mogelijk.")

    sl_distance = tp_distance / min_rr_ratio

    if signal.direction == "SHORT":
        # SHORT: TP ligt onder de entry, SL ligt BOVEN de entry.
        stop_loss = signal.entry_price + sl_distance
    elif signal.direction == "LONG":
        # LONG: TP ligt boven de entry, SL ligt ONDER de entry.
        stop_loss = signal.entry_price - sl_distance
    else:
        raise ValueError(f"Onbekende richting: {signal.direction}")

    risk_amount = capital * risk_pct
    # Fractionele aandelen worden ondersteund door IBKR (live bevestigd
    # 21 aug 2026), dus we ronden NIET meer af naar een heel getal --
    # dat gaf voorheen soms een aanzienlijke afwijking van het beoogde
    # risicobedrag, met name bij dure aandelen. Afgerond op 4 decimalen,
    # een gangbare precisie voor fractionele orders.
    position_size_from_risk = round(risk_amount / sl_distance, 4) if sl_distance > 0 else 0

    # KRITIEKE FIX (21 aug 2026): begrens de positiegrootte zodat de
    # TOTALE positiewaarde nooit meer dan MAX_POSITION_VALUE_PCT van
    # het kapitaal gebruikt -- zie de constante hierboven voor de
    # volledige uitleg van het live-incident dat dit noodzakelijk maakte.
    max_position_value = capital * MAX_POSITION_VALUE_PCT
    position_size_from_max_value = round(max_position_value / signal.entry_price, 4) if signal.entry_price > 0 else 0

    capped_by_max_value = position_size_from_max_value < position_size_from_risk
    position_size = min(position_size_from_risk, position_size_from_max_value)

    actual_rr = tp_distance / sl_distance if sl_distance > 0 else float("inf")

    reason = (
        f"{signal.direction}: entry {signal.entry_price:.4f}, "
        f"TP {take_profit:.4f} (38,2% v/d range), SL {stop_loss:.4f}, "
        f"R/R {actual_rr:.2f}:1, positiegrootte {position_size} "
        f"({'GELIMITEERD door max-positiewaarde' if capped_by_max_value else 'op basis van 1%-risico'}), "
        f"(risico €{risk_amount:.2f} van €{capital:.2f} kapitaal)"
    )

    position_value = position_size * signal.entry_price
    min_position_value = 5.0  # aanname, niet live geverifieerd wat IBKR's exacte minimum is
                                # voor fractionele orders -- veilige ondergrens, aan te passen
                                # als bij live gebruik een striktere IBKR-limiet blijkt

    if position_value < min_position_value:
        logger.warning(
            f"Positiewaarde (€{position_value:.2f}) onder het minimum (€{min_position_value:.2f}) "
            f"-- trade niet uitvoerbaar met dit kapitaal. {reason}"
        )
    else:
        logger.info(f"Exit-plan berekend: {reason}")

    return ExitPlan(
        direction=signal.direction,
        entry_price=signal.entry_price,
        take_profit=take_profit,
        stop_loss=stop_loss,
        rr_ratio=actual_rr,
        position_size=position_size,
        risk_amount=risk_amount,
        reason=reason,
        capped_by_max_value=capped_by_max_value,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Scenario 1: SHORT-signaal (bullish openingscandle), €1.000 kapitaal
    bullish_candle = Candle(
        timestamp=datetime(2026, 8, 14, 9, 0),
        open=100.0, high=102.0, low=99.5, close=101.5, volume=15000,
    )
    short_signal = generate_entry_signal(bullish_candle)
    plan = calculate_exit_levels(short_signal, capital=1000.0)
    print(f"Scenario 1 (SHORT): {plan.to_dict()}")

    # Scenario 2: LONG-signaal (bearish openingscandle), €500 kapitaal
    bearish_candle = Candle(
        timestamp=datetime(2026, 8, 14, 9, 0),
        open=101.5, high=102.0, low=99.5, close=100.0, volume=15000,
    )
    long_signal = generate_entry_signal(bearish_candle)
    plan = calculate_exit_levels(long_signal, capital=500.0)
    print(f"Scenario 2 (LONG): {plan.to_dict()}")

    # Scenario 3: KRITIEKE FIX-VERIFICATIE (25 aug 2026) -- exacte
    # reproductie van het handmatige rekenvoorbeeld (High=110, Low=100)
    # dat de bug blootlegde. Vergelijkt de nieuwe TP-berekening
    # rechtstreeks tegen het ORIGINELE strategie-document se formules.
    print("\n--- Fix-verificatie tegen het originele strategie-document (High=110, Low=100) ---")

    # SHORT (groene candle: close > open)
    groene_candle = Candle(
        timestamp=datetime(2026, 8, 14, 9, 0),
        open=100.0, high=110.0, low=100.0, close=105.0, volume=10000,
    )
    short_sig = generate_entry_signal(groene_candle)
    short_plan = calculate_exit_levels(short_sig, capital=1000.0)
    origineel_short_tp = 110.0 - (10.0 * 0.382)  # = 106.18, uit het originele document
    print(f"SHORT: onze TP = {short_plan.take_profit:.4f}, origineel document TP = {origineel_short_tp:.4f}")
    assert abs(short_plan.take_profit - origineel_short_tp) < 0.0001, "SHORT-TP komt niet overeen met het origineel!"
    print("(geverifieerd: SHORT-TP komt nu exact overeen met het originele document -- was voorheen 103.82, fout)")

    # LONG (rode candle: close < open)
    rode_candle = Candle(
        timestamp=datetime(2026, 8, 14, 9, 0),
        open=110.0, high=110.0, low=100.0, close=105.0, volume=10000,
    )
    long_sig = generate_entry_signal(rode_candle)
    long_plan = calculate_exit_levels(long_sig, capital=1000.0)
    origineel_long_tp = 100.0 + (10.0 * 0.382)  # = 103.82, uit het originele document
    print(f"LONG: onze TP = {long_plan.take_profit:.4f}, origineel document TP = {origineel_long_tp:.4f}")
    assert abs(long_plan.take_profit - origineel_long_tp) < 0.0001, "LONG-TP komt niet overeen met het origineel!"
    print("(geverifieerd: LONG-TP kwam al overeen, blijft correct)")

    # Verifieer ook de R/R-ratio blijft exact 2:1 voor beide
    print(f"\nSHORT R/R: {short_plan.rr_ratio:.4f}:1 (verwacht: 2.0000)")
    print(f"LONG R/R: {long_plan.rr_ratio:.4f}:1 (verwacht: 2.0000)")
