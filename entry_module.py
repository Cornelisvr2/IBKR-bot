"""
entry_module.py — Touch & Turn Scalper, Module 3: Entry Signaal

Identificeert High en Low van de openingscandle, berekent de
Fibonacci-retracementniveaus (38.2% en 61.8%) en bepaalt de
handelsrichting op basis van de close/open-relatie:

    Bullish candle (close > open) -> SHORT verwacht (mean reversion
        na een liquiditeitsgreep naar boven)
    Bearish candle (close < open) -> LONG verwacht (mean reversion
        na een liquiditeitsgreep naar beneden)

Entry-prijs is de High (voor SHORT) of Low (voor LONG) van de
openingscandle zelf. De Fibonacci-niveaus worden alvast berekend
zodat exit_module.py (module 4) ze kan hergebruiken voor de
take-profit-berekening.

Deze module heeft GEEN live IBKR-verbinding nodig om te testen.

Gebruik in andere modules:
    from entry_module import generate_entry_signal
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from data_module import Candle

logger = logging.getLogger("entry_module")

FIB_382 = 0.382
FIB_618 = 0.618


@dataclass
class EntrySignal:
    """Het resultaat van de entry-analyse: richting, prijs en Fib-niveaus."""
    direction: str          # "LONG" of "SHORT"
    entry_price: float      # limietprijs voor de order (High of Low)
    opening_high: float
    opening_low: float
    fib_382: float          # Fibonacci 38.2%-niveau (retracement vanaf de High)
    fib_618: float          # Fibonacci 61.8%-niveau (retracement vanaf de High)
    reason: str

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "entry_price": self.entry_price,
            "opening_high": self.opening_high,
            "opening_low": self.opening_low,
            "fib_382": self.fib_382,
            "fib_618": self.fib_618,
            "reason": self.reason,
        }


def calculate_fib_levels(high: float, low: float) -> tuple[float, float]:
    """
    Berekent de 38.2% en 61.8% Fibonacci-retracementniveaus tussen
    High en Low van de openingscandle.

    Retracement wordt gemeten vanaf de High naar beneden richting de
    Low (het gebruikelijke gebruik bij mean-reversion na een
    liquiditeitsgreep, ongeacht of de candle bullish of bearish sloot
    -- de niveaus liggen puur tussen het candle-bereik zelf).
    """
    candle_range = high - low
    fib_382 = high - (candle_range * FIB_382)
    fib_618 = high - (candle_range * FIB_618)
    return fib_382, fib_618


def generate_entry_signal(opening_candle: Candle) -> EntrySignal | None:
    """
    Genereert een entry-signaal op basis van de openingscandle.

    Args:
        opening_candle: de eerste 15-minutencandle van de handelsdag
                         (uit data_module.get_opening_candle()).

    Returns:
        EntrySignal, of None als de candle neutraal sluit (close == open
        -- geen duidelijke richting, dus geen signaal).
    """
    fib_382, fib_618 = calculate_fib_levels(opening_candle.high, opening_candle.low)

    if opening_candle.is_bullish:
        signal = EntrySignal(
            direction="SHORT",
            entry_price=opening_candle.high,
            opening_high=opening_candle.high,
            opening_low=opening_candle.low,
            fib_382=fib_382,
            fib_618=fib_618,
            reason=(
                f"Bullish openingscandle (close {opening_candle.close:.2f} > "
                f"open {opening_candle.open:.2f}) -> SHORT verwacht, "
                f"limiet SELL nabij High {opening_candle.high:.2f}"
            ),
        )
    elif opening_candle.is_bearish:
        signal = EntrySignal(
            direction="LONG",
            entry_price=opening_candle.low,
            opening_high=opening_candle.high,
            opening_low=opening_candle.low,
            fib_382=fib_382,
            fib_618=fib_618,
            reason=(
                f"Bearish openingscandle (close {opening_candle.close:.2f} < "
                f"open {opening_candle.open:.2f}) -> LONG verwacht, "
                f"limiet BUY nabij Low {opening_candle.low:.2f}"
            ),
        )
    else:
        logger.warning(
            f"Neutrale openingscandle (close == open == {opening_candle.close:.2f}) "
            "-- geen duidelijke richting, geen signaal gegenereerd."
        )
        return None

    logger.info(f"Entry-signaal: {signal.reason}")
    return signal


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Scenario 1: bullish candle -> verwacht SHORT
    bullish_candle = Candle(
        timestamp=datetime(2026, 8, 14, 9, 0),
        open=100.0, high=102.0, low=99.5, close=101.5, volume=15000,
    )
    signal = generate_entry_signal(bullish_candle)
    print(f"Scenario 1 (bullish): {signal.to_dict()}")

    # Scenario 2: bearish candle -> verwacht LONG
    bearish_candle = Candle(
        timestamp=datetime(2026, 8, 14, 9, 0),
        open=101.5, high=102.0, low=99.5, close=100.0, volume=15000,
    )
    signal = generate_entry_signal(bearish_candle)
    print(f"Scenario 2 (bearish): {signal.to_dict()}")

    # Scenario 3: neutrale candle -> geen signaal
    neutral_candle = Candle(
        timestamp=datetime(2026, 8, 14, 9, 0),
        open=100.0, high=101.0, low=99.0, close=100.0, volume=15000,
    )
    signal = generate_entry_signal(neutral_candle)
    print(f"Scenario 3 (neutraal): {signal}")
