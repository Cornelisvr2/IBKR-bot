"""
alpaca_data.py — Realtime candles en koersen via Alpaca (gratis IEX-feed)

WAAROM (10 sep 2026): de IBKR-feed op het paper-account is vertraagd
(snapshot-status "DB"). Daardoor kreeg de bot om 15:46 een HALF gevulde
openingscandle (AAPL: high 320,43 gezien, echte high 323,13) en telde
RVB/VDB nog-lopende 5-min-bars als "afgesloten". Een realtime-abonnement
bij IBKR vereist eerst een storting op het live-account; Alpaca geeft
met een gratis paper-account realtime IEX-data via API, zonder storting.

ROLVERDELING: IBKR blijft de broker (orders, fills, TP/SL); Alpaca
levert alleen DATA. Deze module geeft dezelfde `Candle`-objecten terug
als data_module.get_historical_candles(), zodat geen enkele strategie
hoeft te veranderen.

BEPERKINGEN VAN IEX (bewust geaccepteerd voor de testfase):
  - Prijs: IEX handelt binnen de NBBO, O/H/L/C van liquide aandelen
    wijken hooguit centen af van de geconsolideerde candle.
  - Volume: alleen het IEX-aandeel (~2-3% van de markt). RVB's 3x-
    filter is relatief en de baseline komt ook uit deze feed, maar het
    IEX-marktaandeel schommelt -- RVB wordt hierdoor ruiser.
  - Bars zonder IEX-trades worden door Alpaca weggelaten.

Configuratie (/etc/environment):
    ALPACA_API_KEY=...        Key ID van het (paper-)account
    ALPACA_API_SECRET=...     Secret
    DATA_PROVIDER=alpaca      schakelt data_module om voor intraday-bars

Tijdzones: Alpaca geeft UTC (RFC3339); de rest van de bot vergelijkt
met datetime.now() in de lokale VPS-tijd (Europe/Amsterdam). Timestamps
worden hier omgezet naar NAÏEVE lokale datetimes, exact zoals de
IBKR-route (datetime.fromtimestamp) ze levert.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger("alpaca_data")

DATA_URL = "https://data.alpaca.markets/v2/stocks"
LOKALE_TZ = ZoneInfo("Europe/Amsterdam")
FEED = "iex"

_BAR_MAP = {"1min": "1Min", "5min": "5Min", "15min": "15Min", "30min": "30Min", "1h": "1Hour", "1d": "1Day"}


def is_configured() -> bool:
    return bool(os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_API_SECRET"))


def _headers() -> dict:
    return {
        "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.environ.get("ALPACA_API_SECRET", ""),
        "Accept": "application/json",
    }


def _to_local_naive(ts: str) -> datetime:
    """'2026-09-10T13:30:00Z' -> naïeve lokale datetime (15:30 CEST)."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt.astimezone(LOKALE_TZ).replace(tzinfo=None)


def _parse_duration_days(duration: str) -> int:
    """'1d' -> 1, '20d' -> 20. Onbekend formaat -> 5."""
    try:
        return max(1, int(duration.lower().rstrip("d")))
    except ValueError:
        return 5


def _start_for(duration: str, bar_size: str) -> datetime:
    """
    Startmoment (UTC) voor de aanvraag. Intraday: vanaf lokale
    middernacht van (vandaag - (N-1) dagen), zodat '1d' = vandaag,
    net als de IBKR-route. Dagbars: ruim extra kalenderdagen zodat N
    HANDELSdagen binnen het venster vallen (weekenden/feestdagen).
    """
    dagen = _parse_duration_days(duration)
    nu_lokaal = datetime.now(LOKALE_TZ)
    if bar_size == "1d":
        dagen = int(dagen * 1.6) + 5
    start_lokaal = (nu_lokaal - timedelta(days=dagen - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start_lokaal.astimezone(timezone.utc)


def parse_bars(raw_bars: list[dict]) -> list:
    """Alpaca-bars ({t,o,h,l,c,v,...}) -> lijst van data_module.Candle, oplopend in tijd."""
    from data_module import Candle
    candles = []
    for b in raw_bars:
        try:
            candles.append(Candle(
                timestamp=_to_local_naive(b["t"]),
                open=float(b["o"]), high=float(b["h"]), low=float(b["l"]),
                close=float(b["c"]), volume=float(b.get("v", 0)),
            ))
        except (KeyError, TypeError, ValueError) as e:
            logger.warning(f"Kon Alpaca-bar niet parsen, overgeslagen: {b} ({e})")
    candles.sort(key=lambda c: c.timestamp)
    return candles


def get_historical_candles(symbol: str, duration: str = "5d", bar_size: str = "15min",
                           max_retries: int = 3) -> list:
    """
    Zelfde signatuur en output als data_module.get_historical_candles.
    Alleen REGULIERE handelsuren (Alpaca levert ook pre/post-market
    bars; die worden hier weggefilterd, net als IBKR's standaard
    outsideRth=false), zodat candles_vandaag[0] de 15:30-candle is.
    """
    timeframe = _BAR_MAP.get(bar_size.lower())
    if timeframe is None:
        logger.error(f"Onbekende bar_size voor Alpaca: {bar_size}")
        return []

    params = {
        "timeframe": timeframe,
        "start": _start_for(duration, bar_size.lower()).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": 10000,
        "feed": FEED,
        "adjustment": "raw",
        "sort": "asc",
    }

    raw: list[dict] = []
    page_token = None
    for poging in range(1, max_retries + 1):
        try:
            while True:
                if page_token:
                    params["page_token"] = page_token
                r = requests.get(f"{DATA_URL}/{symbol}/bars", headers=_headers(), params=params, timeout=20)
                if r.status_code == 429:
                    raise requests.HTTPError("429 Too Many Requests")
                r.raise_for_status()
                data = r.json()
                raw.extend(data.get("bars") or [])
                page_token = data.get("next_page_token")
                if not page_token:
                    break
            break
        except Exception as e:
            logger.warning(f"Alpaca-bars {symbol} poging {poging}/{max_retries} mislukt: {e}")
            if poging == max_retries:
                return []
            import time
            time.sleep(2 * poging)

    candles = parse_bars(raw)
    if bar_size.lower() != "1d":
        candles = [c for c in candles if _in_regular_hours(c.timestamp)]
    logger.info(f"{len(candles)} Alpaca-candles ({FEED}) opgehaald voor {symbol}")
    return candles


def _in_regular_hours(ts_local: datetime) -> bool:
    """Reguliere US-handel is 09:30-16:00 New York; toets in NY-tijd, niet in CEST (zomertijdverschillen)."""
    ny = ts_local.replace(tzinfo=LOKALE_TZ).astimezone(ZoneInfo("America/New_York"))
    minuten = ny.hour * 60 + ny.minute
    return 9 * 60 + 30 <= minuten < 16 * 60


def get_last_price(symbol: str) -> tuple[float | None, str]:
    """
    Laatste IEX-trade. Geeft (prijs, status): status 'R' als de trade
    minder dan 90 s oud is, anders 'S<leeftijd in s>' (stale) -- zelfde
    contract als box_guard.fetch_last_price().
    """
    try:
        r = requests.get(f"{DATA_URL}/{symbol}/trades/latest", headers=_headers(),
                         params={"feed": FEED}, timeout=10)
        r.raise_for_status()
        trade = r.json().get("trade") or {}
        prijs = float(trade["p"])
        leeftijd = (datetime.now(timezone.utc) - datetime.fromisoformat(trade["t"].replace("Z", "+00:00"))).total_seconds()
        status = "R" if leeftijd < 90 else f"S{int(leeftijd)}"
        return prijs, status
    except Exception as e:
        logger.warning(f"Alpaca laatste koers {symbol} mislukt: {e}")
        return None, "?"


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    voorbeeld = [
        {"t": "2026-09-10T13:25:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100},
        {"t": "2026-09-10T13:30:00Z", "o": 320.1, "h": 323.13, "l": 316.51, "c": 320.0, "v": 5000},
        {"t": "2026-09-10T13:45:00Z", "o": 320.0, "h": 321.5, "l": 319.0, "c": 319.4, "v": 4000},
        {"t": "2026-09-10T20:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100},
    ]
    c = parse_bars(voorbeeld)
    assert c[1].timestamp == datetime(2026, 9, 10, 15, 30), c[1].timestamp
    rth = [x for x in c if _in_regular_hours(x.timestamp)]
    assert [x.timestamp.strftime("%H:%M") for x in rth] == ["15:30", "15:45"], rth
    assert _parse_duration_days("20d") == 20 and _parse_duration_days("1d") == 1
    print("Offline zelftest OK (parsing, tijdzone, RTH-filter)")

    if not is_configured():
        print("ALPACA_API_KEY/SECRET niet gezet -- live test overgeslagen.")
        sys.exit(0)

    sym = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    print(f"\nLive test {sym}:")
    print("  laatste koers:", get_last_price(sym))
    bars = get_historical_candles(sym, duration="1d", bar_size="15min")
    print(f"  {len(bars)} 15-min candles vandaag; nu {datetime.now().strftime('%H:%M')}")
    for x in bars[:2] + bars[-2:]:
        print(f"    {x.timestamp.strftime('%H:%M')} O {x.open} H {x.high} L {x.low} C {x.close} V {int(x.volume)}")
