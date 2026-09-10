"""
box_guard.py — Touch & Turn Scalper, Box-guard (bewuste toevoeging, 10 sep 2026)

Het originele TTS-plan plaatst na de 15-minuten-openingscandle direct
een limietorder op de High (SHORT) of Low (LONG) en gaat er
stilzwijgend van uit dat de koers op dat moment nog BINNEN de
openingsrange zit -- zodat de limiet pas vult als de koers terugkomt
naar de rand ("touch"). Het plan zegt niets over de situatie waarin de
koers in minuut 16 al door die rand heen is gebroken. Dan vult een
SELL LMT op de High onmiddellijk, tegen de trend in, met een stop op
een halve TP-afstand -- precies wat op 10 sep 2026 bij AAPL (SHORT
boven de box) en META (LONG onder de box) gebeurde: beide 15:46 in,
15:47 uit op de stop.

Deze guard is een TOEVOEGING op het origineel (zelfde categorie als de
positiegrootte en de dagstop): vóór het plaatsen van de entry-order
wordt de actuele koers opgevraagd en moet die nog aan de "goede" kant
van het entry-niveau liggen:

    SHORT: last < opening high   (koers moet nog omhoog naar de rand)
    LONG:  last > opening low    (koers moet nog omlaag naar de rand)

Zit de koers er al doorheen, dan wordt het symbool overgeslagen met een
duidelijke beslissingsregel op het dashboard. Is er geen koers
beschikbaar (snapshot mislukt), dan wordt de trade NIET geblokkeerd --
dan geldt het originele gedrag, met een waarschuwing in het log.

LET OP: bij een VERTRAAGDE marketdata-feed is de snapshot-koers ook
vertraagd. Dan is de guard blind voor de laatste minuten en laat hij
(net als het origineel) gewoon door -- nooit slechter dan zonder
guard. De datastatus (R = realtime, D = delayed) wordt daarom expliciet
meegelogd.

Pure logica hier, geen IBKR-verbinding nodig om te testen (zie __main__).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("box_guard")


@dataclass
class BoxGuardResult:
    allowed: bool
    last_price: float | None
    reason: str


def check_price_inside_box(direction: str, entry_price: float, last_price: float | None,
                           data_status: str = "?") -> BoxGuardResult:
    """Pure controle: mag de entry-limietorder geplaatst worden?"""
    if last_price is None or last_price <= 0:
        return BoxGuardResult(
            allowed=True, last_price=None,
            reason="geen actuele koers beschikbaar -- guard overgeslagen, order volgens origineel geplaatst",
        )

    if direction == "SHORT":
        ok = last_price < entry_price
        kant = "onder" if ok else "op/boven"
    elif direction == "LONG":
        ok = last_price > entry_price
        kant = "boven" if ok else "op/onder"
    else:
        return BoxGuardResult(allowed=False, last_price=last_price, reason=f"onbekende richting {direction}")

    afwijking_pct = (last_price - entry_price) / entry_price * 100
    tekst = (
        f"koers {last_price:.2f} ligt {kant} entry-niveau {entry_price:.2f} "
        f"({afwijking_pct:+.2f}%, data {data_status})"
    )
    if ok:
        return BoxGuardResult(allowed=True, last_price=last_price, reason=f"box-guard OK: {tekst}")
    return BoxGuardResult(
        allowed=False, last_price=last_price,
        reason=f"koers al door boxrand, geen touch-vanuit-de-box mogelijk -- {tekst}",
    )


def fetch_last_price(symbol: str) -> tuple[float | None, str]:
    """
    Actuele koers via de Client Portal snapshot (veld 31 = last,
    veld 6509 = datastatus). Geeft (koers, status); koers is None bij
    een fout. Twee aanroepen: de eerste initialiseert de datastroom.
    """
    try:
        import time
        from ibkr_web_api import resolve_conid, get_market_data_snapshot
        conid = resolve_conid(symbol)
        if conid is None:
            return None, "?"
        snapshot = None
        for _ in range(2):
            snapshot = get_market_data_snapshot(conid)
            if _snapshot_last(snapshot) is not None:
                break
            time.sleep(1.5)
        return _snapshot_last(snapshot), _snapshot_status(snapshot)
    except Exception as e:
        logger.warning(f"Box-guard: kon actuele koers niet ophalen voor {symbol}: {e}")
        return None, "?"


def _snapshot_record(snapshot) -> dict:
    if isinstance(snapshot, list) and snapshot and isinstance(snapshot[0], dict):
        return snapshot[0]
    if isinstance(snapshot, dict):
        return snapshot
    return {}


def _snapshot_last(snapshot) -> float | None:
    """Veld 31 kan een letterprefix dragen (bv. 'C320.43' = laatste slotkoers, 'H' = halted)."""
    raw = _snapshot_record(snapshot).get("31")
    if raw is None:
        return None
    s = str(raw).strip()
    while s and not (s[0].isdigit() or s[0] in ".-"):
        s = s[1:]
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _snapshot_status(snapshot) -> str:
    raw = _snapshot_record(snapshot).get("6509")
    return str(raw).strip() if raw else "?"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    r = check_price_inside_box("SHORT", 320.43, 320.60, "R")
    print("Scenario 1 (SHORT, koers boven high):", r.allowed, "--", r.reason)
    assert r.allowed is False

    r = check_price_inside_box("SHORT", 320.43, 319.10, "R")
    print("Scenario 2 (SHORT, koers in box):", r.allowed, "--", r.reason)
    assert r.allowed is True

    r = check_price_inside_box("LONG", 647.33, 646.90, "R")
    print("Scenario 3 (LONG, koers onder low):", r.allowed, "--", r.reason)
    assert r.allowed is False

    r = check_price_inside_box("LONG", 647.33, 648.50, "R")
    print("Scenario 4 (LONG, koers in box):", r.allowed, "--", r.reason)
    assert r.allowed is True

    r = check_price_inside_box("LONG", 647.33, None)
    print("Scenario 5 (geen koers):", r.allowed, "--", r.reason)
    assert r.allowed is True

    assert _snapshot_last([{"31": "C320.43", "6509": "DPB"}]) == 320.43
    assert _snapshot_status([{"31": "320.43", "6509": "RpB"}]) == "RpB"
    assert _snapshot_last([]) is None
    print("Snapshot-parsing OK")
    print("\nAlle scenario's OK")
