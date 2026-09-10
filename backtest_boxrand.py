"""
backtest_boxrand.py — Wat gebeurt er na een touch van de 15-min boxrand?

Los onderzoeksscript, verandert NIETS aan de bot. Beantwoordt op 60
handelsdagen × de watchlist de vraag: als de koers de high of low van de
openingscandle (15:30–15:45) raakt, komt daarna eerst 1R DOOR de rand
(breakout wint) of eerst 1R TERUG de box in (fade wint)? En is dat
vooraf te zien aan volume, veeg-of-niet, VWAP-afstand of tijdstip?

Daarnaast simuleert het regelsets op exact dezelfde touches, netto na
fees, zodat TTS (blinde limiet), QFS-achtig (veeg + terugkeer) en een
breakout-variant (door de rand op hoog volume) direct vergelijkbaar zijn.

Data: Alpaca, feed=sip (geconsolideerd, volledig volume). Op het gratis
plan is alles ouder dan 15 minuten beschikbaar. Wordt gecachet in
data/backtest/, dus varianten draaien zonder opnieuw te laden.

Gebruik (op de VPS, keys in /etc/environment):
    python3 backtest_boxrand.py                 # 60 dagen, hele watchlist
    python3 backtest_boxrand.py --dagen 120
    python3 backtest_boxrand.py --symbolen AAPL,META,GOOGL
    python3 backtest_boxrand.py --deadline 18:00  # vergelijk met 21:55

Uitvoer in logs/backtest/:
    touches_<datum>.csv      alle touches, één regel per touch
    rapport_<datum>.txt      de tabellen
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone, date, time as dt_time
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DATA_URL = "https://data.alpaca.markets/v2/stocks"
TZ = ZoneInfo("Europe/Amsterdam")
NY = ZoneInfo("America/New_York")
CACHE_DIR = "/opt/strategy/data/backtest" if os.path.isdir("/opt/strategy") else "data/backtest"
OUT_DIR = "/opt/strategy/logs/backtest" if os.path.isdir("/opt/strategy") else "logs/backtest"

# --- parameters van de toets -------------------------------------------------
R_FRACTIE = 0.191        # 1R = 0,191 × boxrange = de SL-afstand van TTS (0,382 / 2)
FEE_ROUND_TRIP = 2.50    # euro, zoals in het journal
POSITIE_EUR = 1000.0     # de cap waar vrijwel elke trade op zit
ATR_FACTOR = 0.25        # TTS/QFS-drempel: range >= 0,25 × ATR(14)
VOLUME_HOOG = 3.0        # RVB-drempel
LAATSTE_TOUCH = dt_time(21, 30)
DEFAULT_DEADLINE = dt_time(21, 55)


@dataclass
class Bar:
    t: datetime  # lokale naïeve tijd (CEST)
    o: float; h: float; l: float; c: float; v: float


@dataclass
class Touch:
    symbol: str; datum: str; tijd: str; rand: str            # rand: HIGH / LOW
    box_high: float; box_low: float; box_range: float; box_kleur: str
    range_atr: float                                          # boxrange / ATR(14)
    dag_nr_touch: int                                         # 1e, 2e, ... touch van deze rand vandaag
    sluit_door: bool                                          # close voorbij de rand (breakout-close)
    veeg: bool                                                # high/low voorbij de rand, close terug in de box
    vol_ratio: float                                          # volume / slotgemiddelde 20 dagen ervoor
    vwap_afstand_pct: float                                   # (close - vwap) / vwap × 100
    minuten_na_open: int
    eerst: str                                                # BREAK / FADE / GEEN / BEIDE
    minuten_tot_eerst: int
    max_door_R: float                                         # max beweging door de rand, in R
    max_terug_R: float                                        # max beweging terug de box in, in R
    slot_R: float                                             # koers op de deadline, in R (+ = door de rand)
    inside_R: float = 0.0                                     # close touch-candle t.o.v. de rand, in R (+ = in de box)
    exc_R: float = 0.0                                        # veeg-diepte: hoe ver de touch-candle voorbij de rand ging, in R
    pad: list = None                                          # per volgende candle (door_R, terug_R) -- niet in de CSV


# --- data ------------------------------------------------------------------------
def _headers():
    return {"APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
            "APCA-API-SECRET-KEY": os.environ["ALPACA_API_SECRET"]}


def haal_bars(symbol: str, dagen: int, timeframe: str) -> list[Bar]:
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache = os.path.join(CACHE_DIR, f"{symbol}_{timeframe}_{dagen}d_{date.today()}.json")
    if os.path.exists(cache):
        raw = json.load(open(cache))
    else:
        start = (datetime.now(TZ) - timedelta(days=int(dagen * 1.5) + 7)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = datetime.now(timezone.utc) - timedelta(minutes=20)
        params = {"timeframe": timeframe, "start": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 10000, "feed": "sip",
                  "adjustment": "raw", "sort": "asc"}
        raw, token = [], None
        while True:
            if token:
                params["page_token"] = token
            r = requests.get(f"{DATA_URL}/{symbol}/bars", headers=_headers(), params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(5); continue
            r.raise_for_status()
            d = r.json()
            raw.extend(d.get("bars") or [])
            token = d.get("next_page_token")
            if not token:
                break
        json.dump(raw, open(cache, "w"))
    bars = []
    for b in raw:
        t = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(TZ).replace(tzinfo=None)
        bars.append(Bar(t, float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]), float(b["v"])))
    return bars


def in_rth(t: datetime) -> bool:
    ny = t.replace(tzinfo=TZ).astimezone(NY)
    m = ny.hour * 60 + ny.minute
    return 9 * 60 + 30 <= m < 16 * 60


def atr14(dagbars: list[Bar], tot: date) -> float | None:
    voor = [b for b in dagbars if b.t.date() < tot][-15:]
    if len(voor) < 15:
        return None
    trs = []
    for prev, cur in zip(voor[:-1], voor[1:]):
        trs.append(max(cur.h - cur.l, abs(cur.h - prev.c), abs(cur.l - prev.c)))
    return sum(trs) / len(trs)


# --- kern ------------------------------------------------------------------------
def analyseer_symbool(symbol: str, dagen: int, deadline: dt_time) -> list[Touch]:
    bars5 = [b for b in haal_bars(symbol, dagen, "5Min") if in_rth(b.t)]
    dagbars = haal_bars(symbol, dagen + 30, "1Day")
    per_dag: dict[date, list[Bar]] = defaultdict(list)
    for b in bars5:
        per_dag[b.t.date()].append(b)
    dagen_lijst = sorted(per_dag)[-dagen:]

    # slot-baseline: gemiddeld volume per 5-min-slot over de 20 dagen ervoor
    def baseline(dag: date, slot: str) -> float | None:
        voor = [d for d in sorted(per_dag) if d < dag][-20:]
        vols = [b.v for d in voor for b in per_dag[d] if b.t.strftime("%H:%M") == slot]
        return sum(vols) / len(vols) if len(vols) >= 5 else None

    touches: list[Touch] = []
    for dag in dagen_lijst:
        bars = per_dag[dag]
        opening = [b for b in bars if dt_time(15, 30) <= b.t.time() < dt_time(15, 45)]
        if len(opening) < 3:
            continue
        box_high = max(b.h for b in opening); box_low = min(b.l for b in opening)
        box_range = box_high - box_low
        if box_range <= 0:
            continue
        box_kleur = "groen" if opening[-1].c > opening[0].o else ("rood" if opening[-1].c < opening[0].o else "doji")
        atr = atr14(dagbars, dag)
        range_atr = box_range / atr if atr else float("nan")
        R = box_range * R_FRACTIE

        # VWAP cumulatief
        cum_pv = cum_v = 0.0
        vwap: dict[datetime, float] = {}
        for b in bars:
            tp = (b.h + b.l + b.c) / 3
            cum_pv += tp * b.v; cum_v += b.v
            vwap[b.t] = cum_pv / cum_v if cum_v else b.c

        na_open = [b for b in bars if b.t.time() >= dt_time(15, 45)]
        teller = {"HIGH": 0, "LOW": 0}
        binnen = True  # koers begint (per definitie) in de box na 15:45
        for i, b in enumerate(na_open):
            if b.t.time() > LAATSTE_TOUCH:
                break
            raakt = []
            if b.h >= box_high:
                raakt.append("HIGH")
            if b.l <= box_low:
                raakt.append("LOW")
            if not raakt:
                binnen = True
                continue
            if not binnen:
                continue  # nog steeds buiten de box sinds vorige touch: geen nieuwe touch
            binnen = False
            for rand in raakt:
                teller[rand] += 1
                sluit_door = b.c > box_high if rand == "HIGH" else b.c < box_low
                veeg = not sluit_door
                base = baseline(dag, b.t.strftime("%H:%M"))
                vol_ratio = b.v / base if base else float("nan")
                vw = vwap[b.t]
                # vooruitkijken vanaf de VOLGENDE candle
                eerst, min_eerst, max_door, max_terug, slot_R = "GEEN", 0, 0.0, 0.0, 0.0
                pad = []
                for j in range(i + 1, len(na_open)):
                    nb = na_open[j]
                    if nb.t.time() > deadline:
                        break
                    if rand == "HIGH":
                        door = (nb.h - box_high) / R; terug = (box_high - nb.l) / R
                        slot_R = (nb.c - box_high) / R
                    else:
                        door = (box_low - nb.l) / R; terug = (nb.h - box_low) / R
                        slot_R = (box_low - nb.c) / R
                    max_door = max(max_door, door); max_terug = max(max_terug, terug)
                    pad.append((round(door, 3), round(terug, 3)))
                    if eerst == "GEEN":
                        if door >= 1 and terug >= 1:
                            eerst = "BEIDE"; min_eerst = int((nb.t - b.t).total_seconds() / 60)
                        elif door >= 1:
                            eerst = "BREAK"; min_eerst = int((nb.t - b.t).total_seconds() / 60)
                        elif terug >= 1:
                            eerst = "FADE"; min_eerst = int((nb.t - b.t).total_seconds() / 60)
                touches.append(Touch(
                    symbol=symbol, datum=dag.isoformat(), tijd=b.t.strftime("%H:%M"), rand=rand,
                    box_high=round(box_high, 2), box_low=round(box_low, 2), box_range=round(box_range, 2),
                    box_kleur=box_kleur, range_atr=round(range_atr, 2), dag_nr_touch=teller[rand],
                    sluit_door=sluit_door, veeg=veeg, vol_ratio=round(vol_ratio, 2),
                    vwap_afstand_pct=round((b.c - vw) / vw * 100, 3),
                    minuten_na_open=int((b.t - datetime.combine(dag, dt_time(15, 30))).total_seconds() / 60),
                    eerst=eerst, minuten_tot_eerst=min_eerst,
                    max_door_R=round(max_door, 2), max_terug_R=round(max_terug, 2), slot_R=round(slot_R, 2),
                    inside_R=round(((box_high - b.c) if rand == "HIGH" else (b.c - box_low)) / R, 3),
                    exc_R=round(((b.h - box_high) if rand == "HIGH" else (box_low - b.l)) / R, 3),
                    pad=pad,
                ))
    return touches


# --- rapport ---------------------------------------------------------------------
def pct(n, d):
    return f"{100 * n / d:5.1f}%" if d else "   -  "


def tabel(touches: list[Touch], titel: str, sleutel) -> str:
    groepen: dict[str, list[Touch]] = defaultdict(list)
    for t in touches:
        groepen[sleutel(t)].append(t)
    regels = [f"\n{titel}", f"{'bucket':<22}{'n':>6}  {'FADE eerst':>11}  {'BREAK eerst':>12}  {'geen':>6}  {'gem max terug R':>16}  {'gem max door R':>15}"]
    for k in sorted(groepen):
        g = groepen[k]; n = len(g)
        fade = sum(1 for t in g if t.eerst == "FADE"); brk = sum(1 for t in g if t.eerst == "BREAK")
        geen = sum(1 for t in g if t.eerst in ("GEEN", "BEIDE"))
        regels.append(f"{k:<22}{n:>6}  {pct(fade, n):>11}  {pct(brk, n):>12}  {pct(geen, n):>6}  "
                      f"{sum(t.max_terug_R for t in g) / n:>16.2f}  {sum(t.max_door_R for t in g) / n:>15.2f}")
    return "\n".join(regels)


def simuleer(touches: list[Touch], naam: str, filter_fn, richting: str, tp_R: float, sl_R: float = 1.0,
             entry_op_close: bool = False) -> str:
    """
    richting FADE: entry op de rand, winst als de koers TERUG de box in beweegt.
    richting BREAK: entry op de rand, winst als de koers DOOR de rand beweegt.
    Loopt het pad candle voor candle af: SL bij sl_R tegen, TP bij tp_R mee,
    wat eerst komt wint. Raken beide in DEZELFDE 5-min candle -> telt als
    verlies (volgorde binnen een candle is onbekend, dus pessimistisch).
    Geen van beide voor de deadline -> mark-to-market op de slotkoers. Fees eraf.
    """
    resultaten = []
    for t in touches:
        if not filter_fn(t) or not t.pad:
            continue
        R_eur = POSITIE_EUR / ((t.box_high + t.box_low) / 2) * (t.box_range * R_FRACTIE)
        r = None
        x = t.inside_R if entry_op_close else 0.0   # verschuiving rand -> close
        for door, terug in t.pad:
            if richting == "FADE":
                mee, tegen = terug - x, door + x
            else:
                mee, tegen = door + x, terug - x
            if tegen >= sl_R and mee >= tp_R:
                r = -sl_R; break
            if tegen >= sl_R:
                r = -sl_R; break
            if mee >= tp_R:
                r = tp_R; break
        if r is None:
            slot = (-t.slot_R - x) if richting == "FADE" else (t.slot_R + x)
            r = max(-sl_R, min(tp_R, slot))
        resultaten.append((r, r * R_eur - FEE_ROUND_TRIP, R_eur))
    n = len(resultaten)
    if not n:
        return f"\n{naam}: geen trades"
    wins = sum(1 for r, _, _ in resultaten if r > 0)
    netto = sum(e for _, e, _ in resultaten)
    gem_R = sum(r for r, _, _ in resultaten) / n
    gem_R_eur = sum(x for _, _, x in resultaten) / n
    return (f"\n{naam}\n  trades {n}  winrate {pct(wins, n)}  gem {gem_R:+.2f}R  1R gemiddeld €{gem_R_eur:.2f}"
            f"  bruto €{sum(r * x for r, _, x in resultaten):+.0f}  fees €{-FEE_ROUND_TRIP * n:.0f}  NETTO €{netto:+.0f}"
            f"  (€{netto / n:+.2f} per trade)")


def simuleer_qfs(touches: list[Touch], naam: str, filter_fn, tp_mult: float | None = 2.0,
                 buffer_R: float = 0.1) -> str:
    """
    Entry op de CLOSE van de veeg-candle. Stop VOORBIJ de veeg-extreme
    (+ buffer). TP = tp_mult × stopafstand, of (tp_mult=None) de overkant
    van de box. Resultaat in eenheden van de stopafstand; euro's op een
    €1000-positie. Beide in dezelfde candle -> verlies (pessimistisch).
    """
    res = []
    for t in touches:
        if not filter_fn(t) or not t.pad:
            continue
        x = t.inside_R
        sl = x + t.exc_R + buffer_R                     # stopafstand vanaf de close, in R
        if sl <= 0:
            continue
        tp = tp_mult * sl if tp_mult else (1 / R_FRACTIE) - x   # overkant box = boxrange (in R) minus wat al binnen is
        if tp <= 0:
            continue
        R_eur = POSITIE_EUR / ((t.box_high + t.box_low) / 2) * (t.box_range * R_FRACTIE)
        r = None
        for door, terug in t.pad:
            tegen, mee = door + x, terug - x
            if tegen >= sl:
                r = -1.0; break
            if mee >= tp:
                r = tp / sl; break
        if r is None:
            r = max(-1.0, min(tp / sl, (-t.slot_R - x) / sl))
        res.append((r, r * sl * R_eur - FEE_ROUND_TRIP, sl * R_eur))
    n = len(res)
    if not n:
        return f"\n{naam}: geen trades"
    wins = sum(1 for r, _, _ in res if r > 0)
    netto = sum(e for _, e, _ in res)
    return (f"\n{naam}\n  trades {n}  winrate {pct(wins, n)}  gem {sum(r for r, _, _ in res) / n:+.2f}× stop"
            f"  stop gemiddeld €{sum(x for _, _, x in res) / n:.2f}  bruto €{sum(r * x for r, _, x in res):+.0f}"
            f"  fees €{-FEE_ROUND_TRIP * n:.0f}  NETTO €{netto:+.0f}  (€{netto / n:+.2f} per trade)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dagen", type=int, default=60)
    ap.add_argument("--symbolen", default=None)
    ap.add_argument("--deadline", default="21:55")
    args = ap.parse_args()
    deadline = dt_time(*map(int, args.deadline.split(":")))

    if args.symbolen:
        symbolen = [s.strip().upper() for s in args.symbolen.split(",")]
    else:
        from news_module import FALLBACK_WATCHLIST
        symbolen = list(FALLBACK_WATCHLIST)

    alle: list[Touch] = []
    for s in symbolen:
        try:
            ts = analyseer_symbool(s, args.dagen, deadline)
            print(f"{s}: {len(ts)} touches")
            alle.extend(ts)
        except Exception as e:
            print(f"{s}: FOUT {e}")

    os.makedirs(OUT_DIR, exist_ok=True)
    stempel = date.today().isoformat()
    with open(os.path.join(OUT_DIR, f"touches_{stempel}.csv"), "w", newline="") as f:
        velden = [k for k in asdict(alle[0]).keys() if k != "pad"]
        w = csv.DictWriter(f, fieldnames=velden)
        w.writeheader()
        for t in alle:
            w.writerow({k: v for k, v in asdict(t).items() if k != "pad"})

    def vol_bucket(t):
        if t.vol_ratio != t.vol_ratio: return "vol onbekend"
        return "vol <1x" if t.vol_ratio < 1 else ("vol 1-3x" if t.vol_ratio < 3 else "vol >=3x")

    def vwap_bucket(t):
        a = abs(t.vwap_afstand_pct)
        return "vwap <0.2%" if a < 0.2 else ("vwap 0.2-0.5%" if a < 0.5 else "vwap >0.5%")

    def tijd_bucket(t):
        return "15:45-16:30" if t.minuten_na_open < 60 else ("16:30-18:00" if t.minuten_na_open < 150 else "na 18:00")

    def atr_bucket(t):
        if t.range_atr != t.range_atr: return "atr onbekend"
        return "range <0.25 ATR" if t.range_atr < 0.25 else ("range 0.25-0.5 ATR" if t.range_atr < 0.5 else "range >=0.5 ATR")

    def kleur_rand(t):
        return f"{t.box_kleur} box, touch {t.rand}"

    eerste = [t for t in alle if t.dag_nr_touch == 1]
    rapport = [
        f"Boxrand-backtest  {stempel}  |  {len(symbolen)} symbolen, {args.dagen} dagen, deadline {args.deadline}",
        f"Touches totaal: {len(alle)}   eerste touch van een rand per dag: {len(eerste)}",
        f"1R = {R_FRACTIE} × boxrange (TTS-stop). Fee €{FEE_ROUND_TRIP}, positie €{POSITIE_EUR:.0f}.",
        tabel(alle, "ALLE TOUCHES — per rand", lambda t: t.rand),
        tabel(alle, "Per veeg (close terug in box) vs breakout-close", lambda t: "veeg" if t.veeg else "sluit door"),
        tabel(alle, "Per volume-ratio van de touch-candle", vol_bucket),
        tabel(alle, "Per VWAP-afstand", vwap_bucket),
        tabel(alle, "Per tijdstip", tijd_bucket),
        tabel(alle, "Per boxrange / ATR", atr_bucket),
        tabel(eerste, "EERSTE touch van de dag — boxkleur × rand (TTS-situatie: groen→HIGH short, rood→LOW long)", kleur_rand),
        tabel(alle, "Veeg × VWAP-afstand", lambda t: ("veeg" if t.veeg else "door") + " | " + vwap_bucket(t)),
        "\n=== SIMULATIES op dezelfde touches (SL 1R = TTS-stop, fees eraf) ===",
        simuleer_qfs(alle, "QFS ECHT: veeg, entry op close, SL voorbij veeg-extreme, TP 2x stop",
                     lambda t: t.veeg and t.range_atr >= ATR_FACTOR),
        simuleer_qfs(alle, "QFS ECHT + VWAP <0.2%",
                     lambda t: t.veeg and t.range_atr >= ATR_FACTOR and abs(t.vwap_afstand_pct) < 0.2),
        simuleer_qfs(alle, "QFS ECHT + VWAP <0.5%",
                     lambda t: t.veeg and t.range_atr >= ATR_FACTOR and abs(t.vwap_afstand_pct) < 0.5),
        simuleer_qfs(alle, "QFS ECHT, TP overkant box (zoals de bot nu)",
                     lambda t: t.veeg and t.range_atr >= ATR_FACTOR, tp_mult=None),
        simuleer_qfs(alle, "QFS ECHT + VWAP <0.2%, TP overkant box",
                     lambda t: t.veeg and t.range_atr >= ATR_FACTOR and abs(t.vwap_afstand_pct) < 0.2, tp_mult=None),
        simuleer_qfs(alle, "QFS ECHT + VWAP <0.2%, TP 3x stop",
                     lambda t: t.veeg and t.range_atr >= ATR_FACTOR and abs(t.vwap_afstand_pct) < 0.2, tp_mult=3.0),
        simuleer_qfs(alle, "QFS ECHT, alleen vóór 18:00 getouchte randen",
                     lambda t: t.veeg and t.range_atr >= ATR_FACTOR and t.minuten_na_open < 150),
        simuleer(eerste, "TTS zoals nu: eerste touch, groen→short HIGH / rood→long LOW, TP 2R, SL 1R, ATR>=0.25",
                 lambda t: t.range_atr >= ATR_FACTOR and ((t.box_kleur == "groen" and t.rand == "HIGH") or (t.box_kleur == "rood" and t.rand == "LOW")),
                 "FADE", tp_R=2.0),
        simuleer(eerste, "TTS zonder kleur-regel: elke eerste touch faden, TP 2R, SL 1R",
                 lambda t: t.range_atr >= ATR_FACTOR, "FADE", tp_R=2.0),
        simuleer(alle, "QFS-achtig: alleen touches die VEGEN (close terug in box), fade, TP 2R, SL 1R",
                 lambda t: t.veeg and t.range_atr >= ATR_FACTOR, "FADE", tp_R=2.0),
        simuleer(alle, "QFS-achtig, TP overkant box (=1/0.191 ≈ 5.2R), SL 1R",
                 lambda t: t.veeg and t.range_atr >= ATR_FACTOR, "FADE", tp_R=round(1 / R_FRACTIE, 1)),
        simuleer(alle, "Breakout: close DOOR de rand op volume >=3x, mee, TP 2R, SL 1R",
                 lambda t: t.sluit_door and t.vol_ratio >= VOLUME_HOOG, "BREAK", tp_R=2.0),
        simuleer(alle, "Breakout: close door de rand, elk volume, TP 2R, SL 1R",
                 lambda t: t.sluit_door, "BREAK", tp_R=2.0),
        simuleer(alle, "Fade bij LAAG volume (<1x): TP 2R, SL 1R",
                 lambda t: t.vol_ratio == t.vol_ratio and t.vol_ratio < 1, "FADE", tp_R=2.0),
    ]
    tekst = "\n".join(rapport)
    print("\n" + tekst)
    open(os.path.join(OUT_DIR, f"rapport_{stempel}.txt"), "w").write(tekst)
    print(f"\nCSV en rapport in {OUT_DIR}/")


if __name__ == "__main__":
    main()
