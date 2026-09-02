"""
atr_module.py — Touch & Turn Scalper, Module 2: ATR Berekening

Berekent de 14-daagse Average True Range (ATR) en toetst of de
openingscandle groot genoeg is om een trade te rechtvaardigen:

    Range(openingscandle) >= 0.25 * ATR_14

Is dat niet het geval, dan wordt de trade voor die dag overgeslagen
(de markt beweegt te weinig t.o.v. het gebruikelijke niveau).

Deze module heeft GEEN live IBKR-verbinding nodig om te testen — hij
werkt op een lijst dagelijkse candles (Candle uit data_module.py) die
je ook met voorbeelddata kunt vullen (zie __main__ onderaan).

Gebruik in andere modules:
    from atr_module import calculate_atr, validate_opening_range
"""

from __future__ import annotations

import logging
from datetime import datetime

from data_module import Candle

logger = logging.getLogger("atr_module")

ATR_PERIOD = 14
ATR_DREMPEL_FACTOR = 0.25  # Range >= 0.25 * ATR is de validatie-eis


def true_range(candle: Candle, previous_close: float | None) -> float:
    """
    Berekent de True Range van één candle: de grootste van
      - high - low
      - |high - vorige_close|
      - |low - vorige_close|

    Bij de eerste candle in een reeks (geen vorige_close) valt dit
    terug op simpelweg high - low.
    """
    if previous_close is None:
        return candle.high - candle.low

    return max(
        candle.high - candle.low,
        abs(candle.high - previous_close),
        abs(candle.low - previous_close),
    )


def calculate_atr(daily_candles: list[Candle], period: int = ATR_PERIOD) -> float:
    """
    Berekent de Average True Range over de laatste `period` dagelijkse
    candles (dus: geef hier DAGcandles, niet 15-min candles).
    """
    if len(daily_candles) < period + 1:
        raise ValueError(
            f"Te weinig candles voor ATR({period}): "
            f"{len(daily_candles)} beschikbaar, minimaal {period + 1} nodig."
        )

    relevant = daily_candles[-(period + 1):]

    true_ranges = []
    for i in range(1, len(relevant)):
        prev_close = relevant[i - 1].close
        true_ranges.append(true_range(relevant[i], prev_close))

    atr = sum(true_ranges) / len(true_ranges)
    logger.info(f"ATR({period}) berekend: {atr:.4f}")
    return atr


def validate_opening_range(opening_candle: Candle, atr: float, factor: float = ATR_DREMPEL_FACTOR) -> dict:
    """
    Toetst of de openingscandle groot genoeg is om een trade te
    rechtvaardigen: Range >= factor * ATR.
    """
    threshold = factor * atr
    candle_range = opening_candle.range
    valid = candle_range >= threshold

    reason = (
        f"Range {candle_range:.4f} {'>=' if valid else '<'} drempel {threshold:.4f} "
        f"({factor} x ATR van {atr:.4f})"
    )

    if valid:
        logger.info(f"ATR-validatie GESLAAGD: {reason}")
    else:
        logger.warning(f"ATR-validatie MISLUKT — trade overgeslagen: {reason}")

    return {
        "valid": valid,
        "range": candle_range,
        "threshold": threshold,
        "atr": atr,
        "reason": reason,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    voorbeeld_daily_candles = []
    prijs = 100.0
    for i in range(15):
        low = prijs - 1.5
        high = prijs + 1.5
        voorbeeld_daily_candles.append(Candle(
            timestamp=datetime(2026, 7, i + 1),
            open=prijs,
            high=high,
            low=low,
            close=prijs + 0.3,
            volume=10000,
        ))
        prijs += 0.3

    atr = calculate_atr(voorbeeld_daily_candles)
    print(f"ATR(14): {atr:.4f}")

    grote_candle = Candle(
        timestamp=datetime(2026, 8, 14, 9, 0),
        open=104.0, high=106.0, low=102.5, close=105.5, volume=15000,
    )
    resultaat = validate_opening_range(grote_candle, atr)
    print(f"Scenario 1 (grote candle): {resultaat}")

    kleine_candle = Candle(
        timestamp=datetime(2026, 8, 14, 9, 0),
        open=104.0, high=104.3, low=104.0, close=104.2, volume=15000,
    )
    resultaat = validate_opening_range(kleine_candle, atr)
    print(f"Scenario 2 (kleine candle): {resultaat}")
