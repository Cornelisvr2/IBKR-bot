"""
vwap_bounce_module.py — VWAP Dynamic Bounce (VDB), Strategielogica

Vijfde strategie naast TTS, QFS en RVB. In tegenstelling tot de Offside
Scalp (afgewezen wegens de real-time-databehoefte) werkt dit model op
gewone 5-minuten OHLCV-candles -- geen tick-data of Level 2 nodig, dus
ook bruikbaar op de bestaande (10-min vertraagde) databron.

Regels (beide richtingen, gespiegeld):
    Trend      : de laatste MIN_TREND_CANDLES candles sluiten allemaal
                 aan dezelfde kant van de intraday VWAP (boven = uptrend
                 voor LONG, onder = downtrend voor SHORT)
    Touch      : de eerstvolgende candle raakt de VWAP-lijn
                 (candle.low <= vwap <= candle.high), ongeacht de close
    Bevestiging: de candle DAARNA sluit in de trendrichting (groen voor
                 LONG, rood voor SHORT) EN weer aan de trendkant van de
                 VWAP
    Entry      : bij de close van de bevestigingscandle
    Stop-loss  : net voorbij de low (LONG) / high (SHORT) van de
                 touch-candle -- structuurgebaseerd, net als bij QFS,
                 dus NIET een vaste %-afstand zoals bij RVB
    Take-profit: vaste 2:1 R/R vanaf de entry (MVP; "exit bij nieuwe
                 intraday high/low" uit de oorspronkelijke beschrijving
                 is een latere upgrade -- zie onderaan)

Positiegrootte: dezelfde 1%-risico / 50%-max-positiewaarde-regel als
RVB en QFS (reversal_strategy_module.calculate_reversal_position_size).

BEWUSTE MVP-KEUZE: de brondescriptie geeft twee exit-condities ("2:1 RR
OF nieuwe intraday high") die kunnen conflicteren -- welke het eerst
geraakt wordt, wint. Dat vraagt een aparte, actieve monitoring-lus naast
de standaard TP/SL-bracket-order (die alleen twee vaste prijzen kent).
Voor de eerste versie gebruiken we uitsluitend de vaste 2:1-TP, exact
zoals destijds bij RVB is gedaan ("hybride scale-out is een latere
upgrade"). De "nieuwe intraday high"-vervroegde-exit kan later als
losse monitoring-check worden toegevoegd aan execute_managed_trade().

Deze module bevat GEEN netwerk-/IBKR-aanroepen -- alles is puur op
Candle-lijsten en dicts, zodat het volledig testbaar is (zie __main__).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from datetime import datetime, time as dt_time, timedelta

from data_module import Candle
from reversal_strategy_module import calculate_reversal_position_size

logger = logging.getLogger("vwap_bounce_module")

# Tijden in CEST (server-lokale tijd, consistent met order_module.MARKET_OPEN_TIME)
MARKET_OPEN_TIME = dt_time(15, 30)
MARKET_CLOSE_TIME = dt_time(22, 0)
VDB_FORCED_CLOSE_TIME = dt_time(21, 55)  # positie uiterlijk 5 min vóór sluiting dicht

BAR_MINUTES = 5
MIN_TREND_CANDLES = 10          # 10x 5-min = 50 min gevestigde trend vóór een touch telt
RR_RATIO = 2.0                   # TP-afstand = 2x SL-afstand
STOP_BUFFER = 0.01                # kleine marge voorbij de touch-candle low/high (tegen exact-op-de-rand-ruis)
OCA_PREFIX = "VDB_"


@dataclass
class VwapBounceSignal:
    symbol: str
    direction: str            # "LONG" / "SHORT"
    trigger_price: float      # close van de bevestigingscandle (= beoogde entry)
    candle_time: datetime
    vwap_at_touch: float
    touch_low: float
    touch_high: float

    def to_dict(self) -> dict:
        d = asdict(self)
        d["candle_time"] = self.candle_time.isoformat()
        return d


# ---------------------------------------------------------------------------
# Hulpfuncties
# ---------------------------------------------------------------------------

def is_regular_session(ts: datetime) -> bool:
    """True als de candle binnen de reguliere sessie (15:30-22:00 CEST) valt."""
    return MARKET_OPEN_TIME <= ts.time() < MARKET_CLOSE_TIME


def closed_candles(candles: list[Candle], now: datetime, bar_minutes: int = BAR_MINUTES) -> list[Candle]:
    """Filtert de nog-lopende (onvolledige) laatste candle weg."""
    return [c for c in candles if c.timestamp + timedelta(minutes=bar_minutes) <= now]


# ---------------------------------------------------------------------------
# VWAP
# ---------------------------------------------------------------------------

def compute_vwap_series(candles: list[Candle]) -> list[float]:
    """
    Cumulatieve intraday VWAP per candle, verankerd aan de EERSTE candle
    in de lijst (dus: geef alleen candles van ÉÉN handelsdag mee, vanaf
    market open). typical_price = (H+L+C)/3.
    """
    cum_pv = 0.0
    cum_vol = 0.0
    out = []
    for c in candles:
        typical = (c.high + c.low + c.close) / 3.0
        cum_pv += typical * c.volume
        cum_vol += c.volume
        out.append(cum_pv / cum_vol if cum_vol > 0 else c.close)
    return out


def trend_established(candles: list[Candle], vwap_series: list[float], direction: str,
                      n: int = MIN_TREND_CANDLES) -> bool:
    """
    True als de laatste n candles ALLEMAAL aan de trendkant van hun
    eigen (op dat moment geldende) VWAP-waarde sluiten.
    """
    if len(candles) < n:
        return False
    window = list(zip(candles[-n:], vwap_series[-n:]))
    if direction == "LONG":
        return all(c.close > v for c, v in window)
    if direction == "SHORT":
        return all(c.close < v for c, v in window)
    raise ValueError(f"Onbekende richting: {direction}")


def touches_vwap(candle: Candle, vwap: float) -> bool:
    """True als de candle de VWAP-lijn raakt of doorkruist (low<=vwap<=high)."""
    return candle.low <= vwap <= candle.high


# ---------------------------------------------------------------------------
# Bounce-detectie
# ---------------------------------------------------------------------------

def check_bounce(symbol: str, candles: list[Candle], vwap_series: list[float]) -> VwapBounceSignal | None:
    """
    Toetst de LAATSTE TWEE (afgesloten) candles tegen de VDB-regels:
    candles[-2] = de touch-candle, candles[-1] = de bevestigingscandle.
    `candles` en `vwap_series` moeten dezelfde lengte hebben (index-
    voor-index bij elkaar passend) en van ÉÉN handelsdag zijn.
    """
    if len(candles) != len(vwap_series):
        raise ValueError("candles en vwap_series moeten gelijke lengte hebben")
    if len(candles) < MIN_TREND_CANDLES + 2:
        return None

    touch_idx = len(candles) - 2
    confirm_idx = len(candles) - 1
    touch_c, confirm_c = candles[touch_idx], candles[confirm_idx]
    touch_vwap = vwap_series[touch_idx]

    if not touches_vwap(touch_c, touch_vwap):
        return None

    for direction in ("LONG", "SHORT"):
        if not trend_established(candles[:touch_idx], vwap_series[:touch_idx], direction):
            continue
        if direction == "LONG" and confirm_c.is_bullish and confirm_c.close > vwap_series[confirm_idx]:
            return VwapBounceSignal(
                symbol=symbol, direction="LONG", trigger_price=confirm_c.close,
                candle_time=confirm_c.timestamp, vwap_at_touch=touch_vwap,
                touch_low=touch_c.low, touch_high=touch_c.high,
            )
        if direction == "SHORT" and confirm_c.is_bearish and confirm_c.close < vwap_series[confirm_idx]:
            return VwapBounceSignal(
                symbol=symbol, direction="SHORT", trigger_price=confirm_c.close,
                candle_time=confirm_c.timestamp, vwap_at_touch=touch_vwap,
                touch_low=touch_c.low, touch_high=touch_c.high,
            )
    return None


def scan_symbol(symbol: str, today_candles: list[Candle], now: datetime) -> VwapBounceSignal | None:
    """
    Volledige toets voor één symbool op één scan-moment: VWAP-serie
    herberekenen over alle candles van vandaag, alleen de laatste twee
    AFGESLOTEN candles tegen de regels leggen.
    """
    trade_date = now.date()
    vandaag = [c for c in today_candles if c.timestamp.date() == trade_date and is_regular_session(c.timestamp)]
    afgesloten = closed_candles(vandaag, now)
    if len(afgesloten) < MIN_TREND_CANDLES + 2:
        return None

    # Sanity: een candle die veel ouder is dan één blok wijst op
    # vertraagde/ontbrekende data.
    leeftijd = now - afgesloten[-1].timestamp
    if leeftijd > timedelta(minutes=3 * BAR_MINUTES + 12):  # ruimte voor 10-min data-vertraging
        logger.warning(f"{symbol}: laatste candle is {leeftijd} oud -- data te oud voor een entry.")
        return None

    vwap_series = compute_vwap_series(afgesloten)
    return check_bounce(symbol, afgesloten, vwap_series)


# ---------------------------------------------------------------------------
# Niveaus en positiegrootte
# ---------------------------------------------------------------------------

def calculate_vdb_stop_loss(signal: VwapBounceSignal, buffer: float = STOP_BUFFER) -> float:
    if signal.direction == "LONG":
        return round(signal.touch_low - buffer, 2)
    if signal.direction == "SHORT":
        return round(signal.touch_high + buffer, 2)
    raise ValueError(f"Onbekende richting: {signal.direction}")


def calculate_vdb_take_profit(direction: str, entry_price: float, stop_loss: float,
                              rr_ratio: float = RR_RATIO) -> float:
    sl_afstand = abs(entry_price - stop_loss)
    if direction == "LONG":
        return round(entry_price + rr_ratio * sl_afstand, 2)
    if direction == "SHORT":
        return round(entry_price - rr_ratio * sl_afstand, 2)
    raise ValueError(f"Onbekende richting: {direction}")


def build_vdb_trade(signal: VwapBounceSignal, capital: float) -> dict:
    """
    Zet een signaal om in concrete trade-parameters, met hetzelfde
    risicobeheer als TTS/QFS/RVB (1% risico, 50% max-positiewaarde).
    Raises ValueError als geen geldige trade mogelijk is.
    """
    sl = calculate_vdb_stop_loss(signal)
    tp = calculate_vdb_take_profit(signal.direction, signal.trigger_price, sl)
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
# Zelftest (geen IBKR nodig): python3 vwap_bounce_module.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)-.1s| %(message)s")

    def mk(hh, mm, o, h, l, c, v):
        return Candle(datetime(2026, 9, 9, hh, mm), o, h, l, c, v)

    # Uptrend: 10 candles die allemaal boven hun VWAP sluiten (constant
    # oplopende prijs, gelijk volume -- VWAP loopt onder de prijs mee).
    candles = []
    prijs = 100.0
    t = (15, 30)
    for i in range(10):
        hh, mm = t
        candles.append(mk(hh, mm, prijs, prijs + 0.5, prijs - 0.1, prijs + 0.4, 100))
        prijs += 0.5
        mm += 5
        if mm >= 60:
            hh, mm = hh + 1, mm - 60
        t = (hh, mm)

    vwap_check = compute_vwap_series(candles)
    assert trend_established(candles, vwap_check, "LONG")
    assert not trend_established(candles, vwap_check, "SHORT")

    # Touch-candle: zakt terug tot exact op de lopende VWAP.
    hh, mm = t
    vwap_nu = compute_vwap_series(candles)[-1]
    touch = mk(hh, mm, prijs, prijs + 0.1, vwap_nu - 0.05, vwap_nu - 0.02, 100)
    mm += 5
    if mm >= 60:
        hh, mm = hh + 1, mm - 60

    # Bevestiging: groene candle, sluit weer boven VWAP -> LONG-signaal.
    confirm_long = mk(hh, mm, vwap_nu - 0.02, vwap_nu + 0.6, vwap_nu - 0.1, vwap_nu + 0.5, 100)
    reeks = candles + [touch, confirm_long]
    vwap_reeks = compute_vwap_series(reeks)
    sig = check_bounce("TEST", reeks, vwap_reeks)
    assert sig and sig.direction == "LONG", sig
    assert sig.touch_low == touch.low and sig.touch_high == touch.high

    # Bevestiging sluit rood -> geen signaal (bevestiging mist).
    confirm_geen = mk(hh, mm, vwap_nu - 0.02, vwap_nu + 0.1, vwap_nu - 0.3, vwap_nu - 0.2, 100)
    reeks_geen = candles + [touch, confirm_geen]
    assert check_bounce("TEST", reeks_geen, compute_vwap_series(reeks_geen)) is None

    # scan_symbol met een nog-lopende laatste candle wordt genegeerd.
    now_te_vroeg = datetime(2026, 9, 9, confirm_long.timestamp.hour, confirm_long.timestamp.minute) + timedelta(minutes=1)
    assert scan_symbol("TEST", reeks, now_te_vroeg) is None
    now_ok = confirm_long.timestamp + timedelta(minutes=BAR_MINUTES)
    sig2 = scan_symbol("TEST", reeks, now_ok)
    assert sig2 and sig2.direction == "LONG"

    # Niveaus + positiegrootte
    sl = calculate_vdb_stop_loss(sig)
    tp = calculate_vdb_take_profit("LONG", sig.trigger_price, sl)
    assert sl == round(touch.low - STOP_BUFFER, 2)
    assert tp > sig.trigger_price > sl

    trade = build_vdb_trade(sig, capital=2000)
    assert trade["oca_group"].startswith("VDB_TEST_LONG_")
    assert trade["stop_loss"] == sl and trade["take_profit"] == tp

    print("Alle VDB-zelftests geslaagd.")
    print(trade)
