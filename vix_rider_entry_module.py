"""
vix_rider_entry_module.py — VIX Rider, Module 1: Opening Range & Doorbraak

Kernprincipe: waar Touch & Turn Scalper wedt op een TERUGKEER na een
grote openingsbeweging (mean-reversion), volgt VIX Rider juist de
RICHTING van een doorbraak (breakout-volgend) -- actief alleen bij
hoge VIX (verdeeld via risk_module.get_allocated_capital).

Twee stappen:
    1. calculate_opening_range(): legt de High/Low van de eerste 30
       minuten na marktopening vast.
    2. detect_breakout(): controleert candles NA die 30 minuten op een
       doorbraak boven de OR-High (-> LONG) of onder de OR-Low
       (-> SHORT).

Zodra een doorbraak wordt gedetecteerd, volgt er ÉÉN order in die
richting (geen dubbele stop-orders zoals bij een klassieke ORB-
implementatie) -- de richting wordt bepaald door te MONITOREN, niet
door twee tegengestelde orders vooraf te plaatsen.

Deze module heeft GEEN live IBKR-verbinding nodig om te testen.

Gebruik in andere modules:
    from vix_rider_entry_module import calculate_opening_range, detect_breakout
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from data_module import Candle

logger = logging.getLogger("vix_rider_entry_module")

OPENING_RANGE_MINUTES = 30  # i.p.v. de scalper's 15 minuten
CANDLES_IN_OPENING_RANGE = 2  # 2x 15-min candles = 30 minuten


@dataclass
class OpeningRange:
    """De vastgelegde High/Low van de eerste 30 minuten na opening."""
    high: float
    low: float
    midpoint: float  # wordt de stop-loss-referentie bij een latere trade

    def to_dict(self) -> dict:
        return {"high": self.high, "low": self.low, "midpoint": self.midpoint}


@dataclass
class BreakoutSignal:
    """Het resultaat van een gedetecteerde doorbraak."""
    direction: str          # "LONG" of "SHORT"
    entry_price: float      # sluitprijs van de doorbraakcandle
    opening_range: OpeningRange
    breakout_time: datetime
    reason: str

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "entry_price": self.entry_price,
            "opening_range": self.opening_range.to_dict(),
            "breakout_time": self.breakout_time.isoformat(),
            "reason": self.reason,
        }


def calculate_opening_range(candles: list[Candle]) -> OpeningRange:
    """
    Berekent de Opening Range uit de eerste CANDLES_IN_OPENING_RANGE
    (standaard 2x 15-min = 30 minuten) candles van VANDAAG.

    KRITIEKE FIX (24 aug 2026): filtert nu EERST op candles van
    vandaag, voordat de eerste twee gepakt worden -- de vorige versie
    nam blind candles[:2] van de volledige, meegegeven lijst, wat bij
    een aanroep rond marktopening (voordat vandaag's candles er zijn)
    OUDE candles van een vorige handelsdag als "Opening Range" gaf.
    Zelfde bugklasse als ontdekt in data_module.get_opening_candle()
    op 24 aug 2026, hier apart teruggevonden in VIX Rider se eigen
    Opening-Range-logica.

    Args:
        candles: lijst van 15-min Candle-objecten, oplopend in tijd
                 (kan meerdere dagen bevatten, bijv. bij een "1d"-
                 lookback-aanroep die ook gisteren se laatste candles
                 meegeeft).

    Returns:
        OpeningRange met de hoogste High en laagste Low van de eerste
        CANDLES_IN_OPENING_RANGE candles VAN VANDAAG, plus het midden.

    Raises:
        ValueError: als er te weinig candles VAN VANDAAG zijn.
    """
    vandaag = datetime.now().date()
    candles_vandaag = [c for c in candles if c.timestamp.date() == vandaag]

    if len(candles_vandaag) < CANDLES_IN_OPENING_RANGE:
        raise ValueError(
            f"Te weinig candles van VANDAAG ({vandaag}) voor de Opening Range: "
            f"{len(candles_vandaag)} beschikbaar, minimaal {CANDLES_IN_OPENING_RANGE} nodig "
            f"(mogelijk is de markt nog niet lang genoeg open)."
        )

    relevant = candles_vandaag[:CANDLES_IN_OPENING_RANGE]
    high = max(c.high for c in relevant)
    low = min(c.low for c in relevant)
    midpoint = (high + low) / 2

    logger.info(f"Opening Range vastgelegd: High {high:.2f}, Low {low:.2f}, Midpoint {midpoint:.2f}")
    return OpeningRange(high=high, low=low, midpoint=midpoint)


def detect_breakout(opening_range: OpeningRange, candle: Candle) -> BreakoutSignal | None:
    """
    Controleert of een candle (die NA de Opening Range-periode valt)
    een doorbraak vormt. Een doorbraak wordt bevestigd op basis van de
    SLUITPRIJS van de candle (niet alleen een korte piek/dip binnen de
    candle) -- consistent met hoe ORB-strategieën doorgaans "sluiting
    boven/onder de range" definiëren, om valse signalen door een korte
    prijspiek te vermijden.

    Args:
        opening_range: de eerder vastgelegde OpeningRange
        candle: een candle NA de opening-range-periode

    Returns:
        BreakoutSignal als er een doorbraak is, anders None (nog geen
        signaal -- de aanroepende code moet de volgende candle blijven
        checken, tot een ingestelde afkaptijd).
    """
    if candle.close > opening_range.high:
        signal = BreakoutSignal(
            direction="LONG",
            entry_price=candle.close,
            opening_range=opening_range,
            breakout_time=candle.timestamp,
            reason=(
                f"Sluiting {candle.close:.2f} boven OR-High {opening_range.high:.2f} "
                f"-> LONG-doorbraak"
            ),
        )
        logger.info(f"Doorbraak gedetecteerd: {signal.reason}")
        return signal

    if candle.close < opening_range.low:
        signal = BreakoutSignal(
            direction="SHORT",
            entry_price=candle.close,
            opening_range=opening_range,
            breakout_time=candle.timestamp,
            reason=(
                f"Sluiting {candle.close:.2f} onder OR-Low {opening_range.low:.2f} "
                f"-> SHORT-doorbraak"
            ),
        )
        logger.info(f"Doorbraak gedetecteerd: {signal.reason}")
        return signal

    return None


def scan_for_breakout(opening_range: OpeningRange, post_range_candles: list[Candle]) -> BreakoutSignal | None:
    """
    Doorloopt een reeks candles (ná de Opening Range-periode) en geeft
    het EERSTE doorbraaksignaal terug dat wordt gevonden -- gebruikt
    door de live-monitoringlus, die periodiek nieuwe candles ophaalt
    en deze functie aanroept tot een signaal wordt gevonden of de
    afkaptijd is bereikt.
    """
    for candle in post_range_candles:
        signal = detect_breakout(opening_range, candle)
        if signal is not None:
            return signal
    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    vandaag = datetime.now()

    # Scenario 1: Opening Range berekenen uit 2 candles VAN VANDAAG
    or_candles = [
        Candle(timestamp=vandaag.replace(hour=15, minute=30, second=0, microsecond=0), open=450.0, high=458.0, low=449.0, close=455.0, volume=50000),
        Candle(timestamp=vandaag.replace(hour=15, minute=45, second=0, microsecond=0), open=455.0, high=460.0, low=452.0, close=456.0, volume=45000),
    ]
    opening_range = calculate_opening_range(or_candles)
    print(f"Scenario 1 (Opening Range): {opening_range.to_dict()}")
    print("(verwacht: High=460.0, Low=449.0, Midpoint=454.5)")

    # Scenario 1b: KRITIEKE BUG-REPRODUCTIE (24 aug 2026) -- alleen
    # oude candles van een vorige handelsdag. Vóór de fix zou dit ten
    # onrechte een Opening Range hebben berekend op basis van oude
    # data; na de fix hoort dit een ValueError te geven.
    oude_or_candles = [
        Candle(timestamp=datetime(2026, 8, 20, 15, 30), open=200.0, high=205.0, low=199.0, close=202.0, volume=30000),
        Candle(timestamp=datetime(2026, 8, 20, 15, 45), open=202.0, high=206.0, low=201.0, close=203.0, volume=28000),
    ]
    try:
        calculate_opening_range(oude_or_candles)
        print("\nScenario 1b: FOUT -- had een ValueError moeten geven, maar deed dat niet!")
    except ValueError as e:
        print(f"\nScenario 1b (verwachte fout, oude candles): {e}")

    # Scenario 2: LONG-doorbraak (sluiting boven OR-High)
    long_breakout_candle = Candle(
        timestamp=datetime(2026, 8, 21, 16, 10),
        open=459.0, high=463.0, low=458.5, close=462.0, volume=30000,
    )
    signal = detect_breakout(opening_range, long_breakout_candle)
    print(f"\nScenario 2 (LONG-doorbraak): {signal.to_dict() if signal else None}")

    # Scenario 3: SHORT-doorbraak (sluiting onder OR-Low)
    short_breakout_candle = Candle(
        timestamp=datetime(2026, 8, 21, 16, 10),
        open=450.0, high=450.5, low=446.0, close=447.0, volume=30000,
    )
    signal = detect_breakout(opening_range, short_breakout_candle)
    print(f"\nScenario 3 (SHORT-doorbraak): {signal.to_dict() if signal else None}")

    # Scenario 4: geen doorbraak (candle binnen de range) -> None
    no_breakout_candle = Candle(
        timestamp=datetime(2026, 8, 21, 16, 10),
        open=455.0, high=458.0, low=452.0, close=456.0, volume=20000,
    )
    signal = detect_breakout(opening_range, no_breakout_candle)
    print(f"\nScenario 4 (geen doorbraak): {signal}")
    print("(verwacht: None)")

    # Scenario 5: scan_for_breakout vindt de EERSTE doorbraak in een reeks
    candle_reeks = [no_breakout_candle, no_breakout_candle, long_breakout_candle, short_breakout_candle]
    signal = scan_for_breakout(opening_range, candle_reeks)
    print(f"\nScenario 5 (eerste doorbraak in reeks): richting = {signal.direction if signal else None}")
    print("(verwacht: LONG -- de long_breakout_candle komt eerder in de reeks dan short)")

    # Scenario 6: te weinig candles voor Opening Range -> ValueError
    try:
        calculate_opening_range([or_candles[0]])
    except ValueError as e:
        print(f"\nScenario 6 (verwachte fout): {e}")
