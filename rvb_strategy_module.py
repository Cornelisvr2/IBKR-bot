"""
rvb_strategy_module.py — Relative Volume Breakout (RVB), Strategielogica

Derde strategie naast TTS en QFS. Fundamenteel verschil: RVB draait de
HELE handelsdag (16:30 tot sluiting), via een herhalend 5-minuten-
scanproces (rvb_scan.py) in plaats van de eenmalige 15:46-cron.

Regels (beide richtingen, gespiegeld):
    Referentie   : high/low van de 60-min opening range (15:30-16:30 CEST)
    Voorwaarde   : LONG  = close > ORB-high  EN volume > 3x baseline
                   SHORT = close < ORB-low   EN volume > 3x baseline
    Entry        : bij de doorbraak, ná 16:30
    Stop-loss    : entry * 0.98 (LONG) / entry * 1.02 (SHORT)
    Take-profit  : vaste 2:1 R/R (MVP; hybride scale-out is een latere upgrade)
    Max. één trade per aandeel per dag (bijgehouden in rvb_scan.py)

Volume-baseline: het dagvolume is U-vormig (hoog bij opening, dip rond
lunch, hoog bij sluiting), dus "3x het gemiddelde" moet per TIJDSTIP-
VAN-DE-DAG worden bepaald: het volume van dit 5-min-blok versus het
gemiddelde van HETZELFDE 5-min-blok over de afgelopen N handelsdagen.
De baseline wordt één keer per dag vooraf berekend en gecachet
(rvb_baseline_builder.py); deze module leest hem alleen.

Deze module bevat GEEN netwerk-/IBKR-aanroepen -- alles is puur op
Candle-lijsten en dicts, zodat het volledig testbaar is (zie __main__).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, time as dt_time, timedelta

from data_module import Candle
from reversal_strategy_module import calculate_reversal_position_size

logger = logging.getLogger("rvb_strategy_module")

# Tijden in CEST (server-lokale tijd, consistent met order_module.MARKET_OPEN_TIME)
MARKET_OPEN_TIME = dt_time(15, 30)
ORB_END_TIME = dt_time(16, 30)          # 60-min opening range
MARKET_CLOSE_TIME = dt_time(22, 0)
RVB_FORCED_CLOSE_TIME = dt_time(21, 55)  # positie uiterlijk 5 min vóór sluiting dicht

BAR_MINUTES = 5
VOLUME_FACTOR = 3.0                      # volume moet > 3x baseline zijn
STOP_LOSS_PCT = 0.02                     # 2% tegen de entry in
RR_RATIO = 2.0                           # TP-afstand = 2x SL-afstand
BASELINE_MIN_DAYS = 5                    # minder historie = baseline onbetrouwbaar
OCA_PREFIX = "RVB_"


@dataclass
class OpeningRange:
    high: float
    low: float
    candle_count: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RvbSignal:
    symbol: str
    direction: str            # "LONG" / "SHORT"
    trigger_price: float      # close van de doorbraakcandle (= beoogde entry)
    candle_time: datetime
    candle_volume: float
    baseline_volume: float
    volume_ratio: float
    orb_high: float
    orb_low: float

    def to_dict(self) -> dict:
        d = asdict(self)
        d["candle_time"] = self.candle_time.isoformat()
        return d


# ---------------------------------------------------------------------------
# Hulpfuncties
# ---------------------------------------------------------------------------

def slot_key(ts: datetime) -> str:
    """Tijdstip-van-de-dag-sleutel voor een candle, bv. '16:35'."""
    return ts.strftime("%H:%M")


def is_regular_session(ts: datetime) -> bool:
    """True als de candle binnen de reguliere sessie (15:30-22:00 CEST) valt."""
    return MARKET_OPEN_TIME <= ts.time() < MARKET_CLOSE_TIME


def closed_candles(candles: list[Candle], now: datetime, bar_minutes: int = BAR_MINUTES) -> list[Candle]:
    """
    Filtert de nog-lopende (onvolledige) laatste candle weg: alleen
    candles waarvan het blok volledig verstreken is. Voorkomt dat een
    half-gevuld volume of een tussentijdse close als signaal telt.
    """
    return [c for c in candles if c.timestamp + timedelta(minutes=bar_minutes) <= now]


# ---------------------------------------------------------------------------
# Baseline (tijdstip-gematcht gemiddeld volume)
# ---------------------------------------------------------------------------

def build_volume_baseline(candles: list[Candle], exclude_date=None) -> dict:
    """
    Bouwt de tijdstip-gematchte volume-baseline uit meerdere dagen
    5-min-candles.

    Returns:
        {
          "days": <aantal unieke handelsdagen in de data>,
          "slots": { "15:30": gemiddeld_volume, "15:35": ..., ... }
        }

    exclude_date: datum (date) die NIET meetelt -- typisch vandaag,
    zodat de eigen dag de baseline niet vervuilt.
    """
    per_slot: dict[str, list[float]] = defaultdict(list)
    dagen = set()
    for c in candles:
        if exclude_date is not None and c.timestamp.date() == exclude_date:
            continue
        if not is_regular_session(c.timestamp):
            continue
        per_slot[slot_key(c.timestamp)].append(c.volume)
        dagen.add(c.timestamp.date())

    slots = {k: (sum(v) / len(v)) for k, v in per_slot.items() if v}
    return {"days": len(dagen), "slots": slots}


def baseline_for(baseline: dict, ts: datetime) -> float | None:
    """Gemiddeld volume voor het 5-min-blok waar `ts` in valt, of None als onbekend."""
    if not baseline or baseline.get("days", 0) < BASELINE_MIN_DAYS:
        return None
    return baseline.get("slots", {}).get(slot_key(ts))


# ---------------------------------------------------------------------------
# Opening range
# ---------------------------------------------------------------------------

def compute_opening_range(candles: list[Candle], trade_date=None) -> OpeningRange | None:
    """
    High/low over alle candles van 15:30 t/m 16:25 (de 12 blokken van de
    eerste 60 minuten) op de gegeven dag. None als het venster nog niet
    compleet is (minder dan 12 candles) -- dan is het vóór 16:30 of de
    data is onvolledig, en mag er nog niet gehandeld worden.
    """
    expected = int((ORB_END_TIME.hour * 60 + ORB_END_TIME.minute
                    - MARKET_OPEN_TIME.hour * 60 - MARKET_OPEN_TIME.minute) / BAR_MINUTES)
    venster = [
        c for c in candles
        if (trade_date is None or c.timestamp.date() == trade_date)
        and MARKET_OPEN_TIME <= c.timestamp.time() < ORB_END_TIME
    ]
    if len(venster) < expected:
        logger.info(f"Opening range nog niet compleet: {len(venster)}/{expected} candles.")
        return None
    return OpeningRange(
        high=max(c.high for c in venster),
        low=min(c.low for c in venster),
        candle_count=len(venster),
    )


# ---------------------------------------------------------------------------
# Breakout-detectie
# ---------------------------------------------------------------------------

def check_breakout(symbol: str, candle: Candle, orb: OpeningRange, baseline: dict,
                   volume_factor: float = VOLUME_FACTOR) -> RvbSignal | None:
    """
    Toetst ÉÉN (afgesloten) candle tegen de RVB-regels. Geeft een
    RvbSignal terug bij een geldige doorbraak, anders None.

    Bewust alleen de CLOSE (niet de high/low) als doorbraakcriterium:
    een pin die even boven de ORB-high prikt en terugvalt is geen
    doorbraak, een sluiting erboven wel.
    """
    if candle.timestamp.time() < ORB_END_TIME:
        return None  # ORB-venster zelf telt nooit als doorbraak

    base = baseline_for(baseline, candle.timestamp)
    if base is None or base <= 0:
        logger.debug(f"{symbol}: geen baseline voor {slot_key(candle.timestamp)} -- overgeslagen.")
        return None

    ratio = candle.volume / base
    if candle.close > orb.high:
        direction = "LONG"
    elif candle.close < orb.low:
        direction = "SHORT"
    else:
        return None

    if ratio <= volume_factor:
        logger.info(
            f"{symbol}: prijs door ORB-{'high' if direction == 'LONG' else 'low'} "
            f"maar volume slechts {ratio:.1f}x baseline (< {volume_factor}x) -- geen signaal."
        )
        return None

    return RvbSignal(
        symbol=symbol, direction=direction, trigger_price=candle.close,
        candle_time=candle.timestamp, candle_volume=candle.volume,
        baseline_volume=base, volume_ratio=round(ratio, 2),
        orb_high=orb.high, orb_low=orb.low,
    )


def scan_symbol(symbol: str, today_candles: list[Candle], baseline: dict, now: datetime) -> RvbSignal | None:
    """
    Volledige toets voor één symbool op één scan-moment: ORB berekenen,
    de laatst AFGESLOTEN candle na 16:30 tegen de regels leggen.

    Alleen de laatste candle wordt getoetst (niet alle candles sinds
    16:30): een doorbraak van een uur geleden die toen géén signaal
    gaf (of gemist is door een uitval) is nu geen verse entry meer.
    """
    trade_date = now.date()
    orb = compute_opening_range(today_candles, trade_date=trade_date)
    if orb is None:
        return None

    afgesloten = [c for c in closed_candles(today_candles, now) if c.timestamp.date() == trade_date]
    if not afgesloten:
        return None
    laatste = afgesloten[-1]

    # Sanity: een candle die veel ouder is dan één blok wijst op
    # vertraagde/ontbrekende data -- dan liever geen entry op oud nieuws.
    leeftijd = now - laatste.timestamp
    if leeftijd > timedelta(minutes=3 * BAR_MINUTES + 12):   # ruimte voor 10-min data-vertraging
        logger.warning(f"{symbol}: laatste candle is {leeftijd} oud -- data te oud voor een entry.")
        return None

    return check_breakout(symbol, laatste, orb, baseline)


# ---------------------------------------------------------------------------
# Niveaus en positiegrootte
# ---------------------------------------------------------------------------

def calculate_rvb_levels(direction: str, entry_price: float,
                         stop_loss_pct: float = STOP_LOSS_PCT, rr_ratio: float = RR_RATIO) -> tuple[float, float]:
    """(take_profit, stop_loss) volgens de vaste 2%-SL en 2:1-R/R-regel."""
    sl_afstand = entry_price * stop_loss_pct
    if direction == "LONG":
        return round(entry_price + rr_ratio * sl_afstand, 2), round(entry_price - sl_afstand, 2)
    if direction == "SHORT":
        return round(entry_price - rr_ratio * sl_afstand, 2), round(entry_price + sl_afstand, 2)
    raise ValueError(f"Onbekende richting: {direction}")


def build_rvb_trade(signal: RvbSignal, capital: float) -> dict:
    """
    Zet een signaal om in de concrete trade-parameters (entry/TP/SL/
    aantal), met hetzelfde risicobeheer als TTS/QFS (1% risico, 50%
    max-positiewaarde). Raises ValueError als geen geldige trade
    mogelijk is (te weinig kapitaal e.d.).
    """
    tp, sl = calculate_rvb_levels(signal.direction, signal.trigger_price)
    size, risk_amount, capped = calculate_reversal_position_size(
        entry_price=signal.trigger_price, stop_loss=sl, capital=capital, direction=signal.direction,
    )
    if size * signal.trigger_price < 5.0:
        raise ValueError(f"positiewaarde te klein met €{capital:.2f} kapitaal")
    return {
        "symbol": signal.symbol,
        "direction": signal.direction,
        "entry_price": round(signal.trigger_price, 2),
        "take_profit": tp,
        "stop_loss": sl,
        "quantity": size,
        "risk_amount": round(risk_amount, 2),
        "capped_by_max_value": capped,
        "oca_group": f"{OCA_PREFIX}{signal.symbol}_{signal.direction}_{int(signal.trigger_price * 100)}",
    }


# ---------------------------------------------------------------------------
# Zelftest (geen IBKR nodig): python3 rvb_strategy_module.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)-.1s| %(message)s")
    from datetime import date

    def mk(day, hh, mm, o, h, l, c, v):
        return Candle(datetime(2026, 9, day, hh, 0) + timedelta(minutes=mm), o, h, l, c, v)

    # Baseline: 6 dagen historie, elk slot 100 volume, behalve 16:35 = 200
    hist = []
    for d in range(1, 7):
        t = datetime(2026, 9, d, 15, 30)
        while t.time() < MARKET_CLOSE_TIME:
            vol = 200 if t.time() == dt_time(16, 35) else 100
            hist.append(Candle(t, 10, 10, 10, 10, vol))
            t += timedelta(minutes=5)
    baseline = build_volume_baseline(hist, exclude_date=date(2026, 9, 9))
    assert baseline["days"] == 6 and abs(baseline["slots"]["16:35"] - 200) < 1e-9
    assert baseline_for(baseline, datetime(2026, 9, 9, 16, 35)) == 200
    assert baseline_for({"days": 2, "slots": {"16:35": 1}}, datetime(2026, 9, 9, 16, 35)) is None

    # Vandaag: ORB 15:30-16:25 met high 105 / low 95
    vandaag = [mk(9, 15, 30 + 5 * i, 100, 105 if i == 3 else 102, 95 if i == 7 else 98, 100, 100) for i in range(12)]
    assert compute_opening_range(vandaag[:11], date(2026, 9, 9)) is None      # onvolledig
    orb = compute_opening_range(vandaag, date(2026, 9, 9))
    assert orb.high == 105 and orb.low == 95

    now = datetime(2026, 9, 9, 16, 41)
    # 16:35-candle sluit boven ORB-high met 700 volume (3.5x de 200-baseline) -> LONG
    sig = scan_symbol("TEST", vandaag + [mk(9, 16, 35, 104, 106.5, 103.9, 106, 700)], baseline, now)
    assert sig and sig.direction == "LONG" and sig.volume_ratio == 3.5, sig
    # Zelfde prijs, maar volume 500 (2.5x) -> geen signaal
    assert scan_symbol("TEST", vandaag + [mk(9, 16, 35, 104, 106.5, 103.9, 106, 500)], baseline, now) is None
    # Pin boven high maar close erbinnen -> geen signaal
    assert scan_symbol("TEST", vandaag + [mk(9, 16, 35, 104, 106.5, 103.9, 104.5, 900)], baseline, now) is None
    # Close onder ORB-low met 400 volume (4x de 100-baseline om 16:40) -> SHORT
    sig = scan_symbol("TEST", vandaag + [mk(9, 16, 40, 96, 96, 93, 94, 400)], baseline, datetime(2026, 9, 9, 16, 46))
    assert sig and sig.direction == "SHORT", sig
    # Nog-lopende candle (16:40 om 16:41) wordt genegeerd
    assert scan_symbol("TEST", vandaag + [mk(9, 16, 40, 96, 96, 93, 94, 400)], baseline, now) is None
    # Verouderde laatste candle -> geen entry
    assert scan_symbol("TEST", vandaag + [mk(9, 16, 35, 104, 106.5, 103.9, 106, 700)], baseline, datetime(2026, 9, 9, 17, 30)) is None

    # Niveaus + positiegrootte
    assert calculate_rvb_levels("LONG", 100) == (104.0, 98.0)
    assert calculate_rvb_levels("SHORT", 100) == (96.0, 102.0)
    trade = build_rvb_trade(RvbSignal("TEST", "LONG", 100.0, now, 700, 200, 3.5, 105, 95), capital=2000)
    assert trade["quantity"] == 10 and trade["risk_amount"] == 20.0 and trade["oca_group"] == "RVB_TEST_LONG_10000", trade
    print("Alle RVB-zelftests geslaagd.")
    print(trade)
