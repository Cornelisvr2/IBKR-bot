"""
data_module.py — Touch & Turn Scalper, Module 1: Data Acquisitie

Haalt real-time (of historische) 15-minuten candledata op voor een
gegeven instrument via ib_async. Retourneert een lijst van candles met:
timestamp, open, high, low, close, volume.

Deze module heeft GEEN live IBKR-verbinding nodig om te testen: de
functie `parse_bars_to_candles` en de validatielogica zijn los te
draaien met voorbeelddata (zie __main__ onderaan).

Gebruik in andere modules:
    from data_module import get_opening_candle, get_historical_candles
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from datetime import datetime

logger = logging.getLogger("data_module")


@dataclass
class Candle:
    """Eén candle (kaarsje) van 15 minuten."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def is_bullish(self) -> bool:
        """True als de candle sluit boven de openingsprijs."""
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        """True als de candle sluit onder de openingsprijs."""
        return self.close < self.open

    @property
    def range(self) -> float:
        """High - Low van de candle."""
        return self.high - self.low

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d


def parse_bars_to_candles(bars: list) -> list[Candle]:
    """
    Zet ib_async BarData-objecten (of gelijkvormige objecten/dicts) om
    naar onze eigen Candle-dataclass, los van de IBKR-specifieke vorm.

    Geaccepteerd: objecten met attributen .date/.open/.high/.low/.close/.volume
    (zoals ib_async.objects.BarData), of dicts met dezelfde velden.
    """
    candles = []
    for bar in bars:
        if isinstance(bar, dict):
            candles.append(Candle(
                timestamp=bar["timestamp"],
                open=float(bar["open"]),
                high=float(bar["high"]),
                low=float(bar["low"]),
                close=float(bar["close"]),
                volume=float(bar["volume"]),
            ))
        else:
            # ib_async BarData-achtig object
            candles.append(Candle(
                timestamp=bar.date,
                open=float(bar.open),
                high=float(bar.high),
                low=float(bar.low),
                close=float(bar.close),
                volume=float(bar.volume),
            ))
    return candles


def get_historical_candles(symbol: str, duration: str = "5d", bar_size: str = "15min") -> list[Candle]:
    """
    Haalt historische candles op voor een symbool via de Client Portal
    Web API (zie ibkr_web_api.py) -- vervangt de eerdere ib_async-
    gebaseerde aanpak, die bleek te verbinden met het verkeerde IBKR-
    product (zie ibkr_web_api.py's moduledocstring voor uitleg).

    Args:
        symbol: aandelensymbool, bijv. "AAPL"
        duration: hoeveel historie, bijv. "5d" (5 dagen), "1y"
        bar_size: candle-grootte, bijv. "15min", "1d"

    Returns:
        Lijst van Candle-objecten, oplopend in tijd. Lege lijst als
        het symbool niet gevonden kan worden of de API geen data
        teruggeeft.

    LET OP: get_historical_bars() zelf is nog niet end-to-end getest
    (wel resolve_conid() en tickle(), zie ibkr_web_api.py) -- de
    exacte veldnamen in de respons ('t','o','h','l','c','v') zijn een
    aanname gebaseerd op IBKR's documentatie-conventies, te bevestigen
    bij het eerste live gebruik.
    """
    from ibkr_web_api import resolve_conid, get_historical_bars

    conid = resolve_conid(symbol)
    if conid is None:
        logger.error(f"Kon geen conid vinden voor {symbol} -- geen candles opgehaald.")
        return []

    raw_bars = get_historical_bars(conid, period=duration, bar=bar_size)
    if not raw_bars:
        logger.warning(f"Geen historische data ontvangen voor {symbol} (conid {conid}).")
        return []

    candles = []
    for bar in raw_bars:
        try:
            candles.append(Candle(
                timestamp=datetime.fromtimestamp(bar["t"] / 1000),  # IBKR geeft ms sinds epoch
                open=float(bar["o"]),
                high=float(bar["h"]),
                low=float(bar["l"]),
                close=float(bar["c"]),
                volume=float(bar.get("v", 0)),
            ))
        except (KeyError, TypeError, ValueError) as e:
            logger.warning(f"Kon een candle niet parsen, overgeslagen: {bar} ({e})")

    logger.info(f"{len(candles)} candles opgehaald voor {symbol}")
    return candles


def get_opening_candle(candles: list[Candle]) -> Candle | None:
    """
    Geeft de EERSTE candle van VANDAAG terug -- niet zomaar candles[0]
    van een teruggekeken periode.

    KRITIEKE FIX (24 aug 2026): de vorige versie gaf simpelweg
    candles[0] terug van een 1-dag-teruggekeken lijst. Bij een
    aanroep rond of vlak na marktopening (voordat vandaag's eerste
    candle er is, of er net is) gaf dit een OUDE candle van een
    vorige handelsdag terug in plaats van vandaag's daadwerkelijke
    opening -- een reëel bug die op 24 aug 2026 live tot een trade op
    verkeerde/oude data leidde (ontdekt doordat een trade binnen
    enkele seconden na de cron-trigger plaatsvond, onmogelijk snel
    voor een daadwerkelijk voltooide, actuele openingscandle-analyse).

    LET OP (niet volledig geverifieerd): deze functie vergelijkt
    candle.timestamp.date() met de datum van vandaag op basis van de
    LOKALE tijd van de VPS (Europe/Amsterdam). Aangenomen wordt dat
    IBKR's candle-timestamps in dezelfde tijdzone worden teruggegeven
    (gebaseerd op eerdere observatie: de laatste candle van een
    handelsdag kwam overeen met 21:45 lokale tijd, consistent met een
    marktsluiting om 22:00 CEST) -- niet expliciet met documentatie
    bevestigd.
    """
    if not candles:
        logger.warning("Geen candles beschikbaar om openingscandle te bepalen.")
        return None

    vandaag = datetime.now().date()
    candles_vandaag = [c for c in candles if c.timestamp.date() == vandaag]

    if not candles_vandaag:
        logger.warning(
            f"Geen candles gevonden voor vandaag ({vandaag}) -- markt mogelijk nog niet "
            f"open, of de eerste candle van vandaag is nog niet beschikbaar/voltooid."
        )
        return None

    return candles_vandaag[0]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    vandaag = datetime.now()

    voorbeeld_bars = [
        {"timestamp": vandaag.replace(hour=15, minute=30, second=0, microsecond=0), "open": 100.0, "high": 101.2, "low": 99.5, "close": 100.8, "volume": 15000},
        {"timestamp": vandaag.replace(hour=15, minute=45, second=0, microsecond=0), "open": 100.8, "high": 102.0, "low": 100.5, "close": 101.5, "volume": 12000},
    ]

    candles = parse_bars_to_candles(voorbeeld_bars)
    opening = get_opening_candle(candles)

    print(f"Scenario 1 (candles van vandaag): {opening}")
    print(f"Bullish? {opening.is_bullish}")
    print(f"Range: {opening.range:.2f}")

    oude_bars = [
        {"timestamp": datetime(2026, 8, 20, 21, 30), "open": 200.0, "high": 201.0, "low": 199.0, "close": 200.5, "volume": 20000},
        {"timestamp": datetime(2026, 8, 20, 21, 45), "open": 200.5, "high": 202.0, "low": 200.0, "close": 201.0, "volume": 18000},
    ]
    oude_candles = parse_bars_to_candles(oude_bars)
    opening_oud = get_opening_candle(oude_candles)
    print(f"\nScenario 2 (alleen oude candles, geen vandaag): {opening_oud}")
    print("(verwacht: None -- dit was de bug die op 24 aug 2026 tot een verkeerde trade leidde)")

    gemengde_bars = oude_bars + [
        {"timestamp": vandaag.replace(hour=15, minute=30, second=0, microsecond=0), "open": 300.0, "high": 301.0, "low": 299.0, "close": 300.5, "volume": 25000},
    ]
    gemengde_candles = parse_bars_to_candles(gemengde_bars)
    opening_gemengd = get_opening_candle(gemengde_candles)
    print(f"\nScenario 3 (gemengd, oud + vandaag): {opening_gemengd}")
    print(f"(verwacht: de candle met open=300.0 -- vandaag's candle, NIET de oude 200.0-candle)")

