"""
backtest_valentini.py — de twee modellen van Fabio Valentini (Chart Fanatics,
NQ-orderflow scalper) mechanisch vertaald naar 5-min OHLCV op de aandelen-
watchlist. Hergebruikt de data-cache van backtest_boxrand.py (data/backtest/).

Dit test het STRUCTUUR-skelet: volume profile van de vorige dag (POC, value
area 70%), marktstaat (binnen/buiten balans), low-volume node (LVN) in de
impuls, break-retest met volledige candle close, POC/prev-day-high targets.
De orderflow-laag (grote orders, absorptie, CVD) zit hier NIET in — daar zijn
trades/ticks voor nodig. Als proxy voor "agressie" is er een volumefilter op
de triggercandle (variant "vol").

MODEL 1 — TREND / IMBALANCE (Valentini's NY-model)
  1. Marktstaat: een 5-min candle sluit buiten de value area van de vorige
     dag (boven VAH -> long-bias, onder VAL -> short-bias). Niet in de eerste
     20 min na de opening.
  2. Impuls: loopt door zolang de candles geen close tegen de richting in
     maken (long: close < low van de vorige candle beëindigt de impuls).
     Minimale impulsgrootte: --min-impuls × ATR14 (default 0,3).
  3. Locatie: volume profile over de impuls-candles, LVN = bin met het minste
     volume in het middenstuk (20–80% van de impuls). LVN moet buiten de
     value area liggen.
  4. Retracement: prijs raakt de LVN-zone (LVN ± 10% impulsrange). Sluit een
     candle weer BINNEN de value area -> setup ongeldig (fake-out).
  5. Trigger: eerste candle na de touch die in de trendrichting sluit
     (long: close > open én close > LVN). Entry = open van de volgende candle.
  6. SL = laagste low sinds de touch, min buffer. TP-varianten: 2R, 3R,
     prev-day high/low (PDH/PDL, alleen als ≥ 1R weg, anders 2R).
     BE-variant: stop naar entry zodra +1R is bereikt.

MODEL 2 — MEAN REVERSION / FAILED AUCTION (Valentini's Londen/zomer-model)
  1. Marktstaat: prijs sluit buiten de value area van de vorige dag (poging
     tot uitbraak), maar sluit binnen --faal-candles (default 3) candles
     weer BINNEN de value area -> failed auction.
  2. Retracement: prijs komt weer terug richting het extreme van de poging
     (minstens --retrace × afstand VAH->extreme, default 0,5). Variant
     "direct": geen retracement, entry meteen na de close terug binnen.
  3. Trigger: candle die richting POC sluit (short: close < open).
     Entry = open van de volgende candle.
  4. SL = extreme van de poging + buffer. TP = POC (skip als < 1R weg) of 2R.

Gemeenschappelijk: signalen 15:50–20:30 CEST, gedwongen sluiting 21:55,
één trade per model per symbool per dag, SL en TP in dezelfde candle =
verlies (pessimistisch), positie min(1% risico / SL-afstand, €1.000 / prijs),
fee €2,50 per round-trip.

Gebruik:
    python3 backtest_valentini.py                 # 240 dagen, hele watchlist
    python3 backtest_valentini.py --dagen 120 --symbolen META,NVDA
    python3 backtest_valentini.py --min-impuls 0.5 --vol-factor 2.0
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import date, datetime, time as dt_time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_boxrand import Bar, haal_bars, in_rth, atr14, OUT_DIR, FEE_ROUND_TRIP, POSITIE_EUR, pct

KAPITAAL = 2000.0
RISICO_PCT = 0.01
EERSTE_SIGNAAL = dt_time(15, 50)     # 20 min na de NY-opening (15:30 CEST)
LAATSTE_SIGNAAL = dt_time(20, 30)
SLUITING = dt_time(21, 55)
VA_PCT = 0.70
BIN_FRACTIE = 0.0005                # profielbin = 0,05% van de prijs
BUFFER_FRACTIE = 0.0005             # SL-buffer 0,05%


@dataclass
class Trade:
    model: str; variant: str; symbol: str; datum: str; richting: str
    signaal_tijd: str; entry_tijd: str; entry: float; sl: float; tp: float
    sl_afstand: float; qty: float; risico_eur: float; extra: str
    r: float; exit: str


# ---------------------------------------------------------------- volume profile

def bin_size(prijs: float) -> float:
    return max(0.01, round(prijs * BIN_FRACTIE, 2))


def profiel(bars: list[Bar], bs: float) -> dict[int, float]:
    """Volume per prijsbin; barvolume gelijk verdeeld over de high-low range."""
    vol: dict[int, float] = defaultdict(float)
    for b in bars:
        lo, hi = int(b.l // bs), int(b.h // bs)
        n = hi - lo + 1
        for k in range(lo, hi + 1):
            vol[k] += b.v / n
    return vol


def value_area(vol: dict[int, float], bs: float):
    """(POC, VAL, VAH) — 70% van het volume, uitgebreid vanaf de POC."""
    if not vol:
        return None
    poc = max(vol, key=vol.get)
    totaal = sum(vol.values()); doel = totaal * VA_PCT
    lo = hi = poc; som = vol[poc]
    while som < doel:
        boven = vol.get(hi + 1, 0.0); onder = vol.get(lo - 1, 0.0)
        if boven == 0 and onder == 0:
            break
        if boven >= onder:
            hi += 1; som += boven
        else:
            lo -= 1; som += onder
    return ((poc + 0.5) * bs, lo * bs, (hi + 1) * bs)


def lvn(bars: list[Bar], bs: float) -> float | None:
    """Prijs van de low-volume node in het middenstuk (20–80%) van de impuls."""
    vol = profiel(bars, bs)
    lo = min(b.l for b in bars); hi = max(b.h for b in bars)
    a, z = lo + 0.2 * (hi - lo), hi - 0.2 * (hi - lo)
    kandidaten = [(v, k) for k, v in vol.items() if a <= (k + 0.5) * bs <= z]
    if not kandidaten:
        return None
    v, k = min(kandidaten)
    return (k + 0.5) * bs


# ---------------------------------------------------------------- uitvoering

def in_venster(t: datetime) -> bool:
    return EERSTE_SIGNAAL <= t.time() <= LAATSTE_SIGNAAL


def simuleer(model, variant, symbol, dag, dagbars, idx_trigger, richting, sl, tp, extra,
             break_even: bool) -> Trade | None:
    """Entry op de open van de candle NA de triggercandle; loopt tot SL/TP/sluiting."""
    pad = [b for b in dagbars[idx_trigger + 1:] if b.t.time() < SLUITING]
    if not pad:
        return None
    entry = pad[0].o
    sl_afstand = abs(entry - sl)
    if sl_afstand <= 0:
        return None
    if richting == "LONG" and tp <= entry or richting == "SHORT" and tp >= entry:
        return None
    qty = min(KAPITAAL * RISICO_PCT / sl_afstand, POSITIE_EUR / entry)
    r, exit_reden = None, "sluiting"
    be_actief = False
    for b in pad:
        if richting == "LONG":
            sl_raak, tp_raak, r1 = b.l <= sl, b.h >= tp, b.h >= entry + sl_afstand
        else:
            sl_raak, tp_raak, r1 = b.h >= sl, b.l <= tp, b.l <= entry - sl_afstand
        if sl_raak:
            r, exit_reden = (0.0, "BE") if be_actief else (-1.0, "SL"); break
        if tp_raak:
            r, exit_reden = abs(tp - entry) / sl_afstand, "TP"; break
        if break_even and r1 and not be_actief:
            be_actief = True; sl = entry
    if r is None:
        laatste = pad[-1]
        r = ((laatste.c - entry) if richting == "LONG" else (entry - laatste.c)) / sl_afstand
    return Trade(model, variant, symbol, dag.isoformat(), richting, dagbars[idx_trigger].t.strftime("%H:%M"),
                 pad[0].t.strftime("%H:%M"), round(entry, 2), round(sl, 2), round(tp, 2), round(sl_afstand, 3),
                 round(qty, 4), round(qty * sl_afstand, 2), extra, round(r, 3), exit_reden)


def vol_ok(dagbars: list[Bar], i: int, factor: float) -> bool:
    """Agressie-proxy: triggercandle-volume ≥ factor × mediaan van de dag tot dan."""
    eerder = sorted(b.v for b in dagbars[:i])
    if len(eerder) < 4:
        return False
    return dagbars[i].v >= factor * eerder[len(eerder) // 2]


# ---------------------------------------------------------------- model 1: trend / imbalance

def model1_dag(symbol, dag, dagbars, poc, val, vah, pdh, pdl, atr, min_impuls, vol_factor) -> list[Trade]:
    bs = bin_size(dagbars[0].c)
    out: list[Trade] = []
    for richting in ("LONG", "SHORT"):
        t = _model1_richting(symbol, dag, dagbars, richting, poc, val, vah, pdh, pdl, atr, min_impuls, vol_factor, bs)
        out.extend(t)
    return out


def _model1_richting(symbol, dag, bars, richting, poc, val, vah, pdh, pdl, atr, min_impuls, vol_factor, bs):
    lang = richting == "LONG"
    grens = vah if lang else val
    buiten = (lambda b: b.c > grens) if lang else (lambda b: b.c < grens)
    binnen = (lambda b: val <= b.c <= vah)
    fase = "wacht_break"; start = None; lvn_p = None; tol = None; touch_lo = None; touch_hi = None
    for i, b in enumerate(bars):
        if fase == "wacht_break":
            if in_venster(b.t) and buiten(b) and (i == 0 or not buiten(bars[i - 1])):
                fase = "impuls"; start = i
        elif fase == "impuls":
            prev = bars[i - 1]
            einde = (b.c < prev.l) if lang else (b.c > prev.h)
            if not einde:
                continue
            impuls = bars[start:i]
            rng = max(x.h for x in impuls) - min(x.l for x in impuls)
            if atr is None or rng < min_impuls * atr:
                fase = "wacht_break"; continue
            lvn_p = lvn(impuls, bs)
            if lvn_p is None or (lang and lvn_p <= vah) or (not lang and lvn_p >= val):
                fase = "wacht_break"; continue
            tol = 0.10 * rng; touch_lo = touch_hi = None
            fase = "wacht_retrace"
            # de pullback-candle zelf kan de LVN al raken
            if (lang and b.l <= lvn_p + tol) or (not lang and b.h >= lvn_p - tol):
                touch_lo, touch_hi = b.l, b.h; fase = "wacht_trigger"
        elif fase == "wacht_retrace":
            if binnen(b):
                fase = "wacht_break"; continue          # terug in balans = fake-out
            if (lang and b.l <= lvn_p + tol) or (not lang and b.h >= lvn_p - tol):
                touch_lo, touch_hi = b.l, b.h; fase = "wacht_trigger"
        elif fase == "wacht_trigger":
            if binnen(b) or not in_venster(b.t):
                fase = "wacht_break"; continue
            touch_lo, touch_hi = min(touch_lo, b.l), max(touch_hi, b.h)
            trig = (b.c > b.o and b.c > lvn_p) if lang else (b.c < b.o and b.c < lvn_p)
            if not trig:
                continue
            buf = b.c * BUFFER_FRACTIE
            sl = touch_lo - buf if lang else touch_hi + buf
            entry_ref = b.c; d = abs(entry_ref - sl)
            if d <= 0:
                fase = "wacht_break"; continue
            extra = f"lvn {lvn_p:.2f} vol {'ja' if vol_ok(bars, i, vol_factor) else 'nee'}"
            trades = []
            for naam, tp in (("TP2R", entry_ref + 2 * d if lang else entry_ref - 2 * d),
                             ("TP3R", entry_ref + 3 * d if lang else entry_ref - 3 * d)):
                trades.append(simuleer("M1-trend", naam, symbol, dag, bars, i, richting, sl, tp, extra, False))
            pdx = pdh if lang else pdl
            if pdx is not None and abs(pdx - entry_ref) >= d and ((lang and pdx > entry_ref) or (not lang and pdx < entry_ref)):
                trades.append(simuleer("M1-trend", "TP-PD", symbol, dag, bars, i, richting, sl, pdx, extra, False))
            trades.append(simuleer("M1-trend", "TP3R+BE", symbol, dag, bars, i, richting, sl,
                                   entry_ref + 3 * d if lang else entry_ref - 3 * d, extra, True))
            return [t for t in trades if t]
    return []


# ---------------------------------------------------------------- model 2: failed auction / mean reversion

def model2_dag(symbol, dag, bars, poc, val, vah, faal_candles, retrace_frac, vol_factor) -> list[Trade]:
    out: list[Trade] = []
    for richting in ("SHORT", "LONG"):        # SHORT = gefaalde uitbraak boven VAH
        out.extend(_model2_richting(symbol, dag, bars, richting, poc, val, vah, faal_candles, retrace_frac, vol_factor))
    return out


def _model2_richting(symbol, dag, bars, richting, poc, val, vah, faal_candles, retrace_frac, vol_factor):
    short = richting == "SHORT"
    grens = vah if short else val
    buiten = (lambda b: b.c > grens) if short else (lambda b: b.c < grens)
    binnen = (lambda b: val <= b.c <= vah)
    fase = "wacht_poging"; extreme = None; n_buiten = 0; idx_terug = None
    for i, b in enumerate(bars):
        if fase == "wacht_poging":
            if in_venster(b.t) and buiten(b):
                fase = "buiten"; extreme = b.h if short else b.l; n_buiten = 1
        elif fase == "buiten":
            extreme = max(extreme, b.h) if short else min(extreme, b.l)
            if binnen(b):
                # failed auction: terug binnen de value area
                fase = "wacht_retrace"; idx_terug = i
                if retrace_frac <= 0:
                    fase = "wacht_trigger"
                continue
            n_buiten += 1
            if n_buiten > faal_candles or buiten(b) and n_buiten > faal_candles:
                fase = "wacht_poging"           # echte uitbraak, geen failed auction
        elif fase == "wacht_retrace":
            if buiten(b):
                fase = "wacht_poging"; continue  # tweede uitbraakpoging slaagt -> weg
            doel = grens + retrace_frac * (extreme - grens)   # tekent zelf de richting
            if (short and b.h >= doel) or (not short and b.l <= doel):
                fase = "wacht_trigger"
        elif fase == "wacht_trigger":
            if buiten(b) or not in_venster(b.t):
                fase = "wacht_poging"; continue
            trig = (b.c < b.o) if short else (b.c > b.o)
            if not trig:
                continue
            buf = b.c * BUFFER_FRACTIE
            sl = extreme + buf if short else extreme - buf
            entry_ref = b.c; d = abs(entry_ref - sl)
            if d <= 0:
                fase = "wacht_poging"; continue
            extra = f"extreme {extreme:.2f} poc {poc:.2f} vol {'ja' if vol_ok(bars, i, vol_factor) else 'nee'}"
            trades = []
            if abs(poc - entry_ref) >= d and ((short and poc < entry_ref) or (not short and poc > entry_ref)):
                trades.append(simuleer("M2-revert", "TP-POC", symbol, dag, bars, i, richting, sl, poc, extra, False))
            trades.append(simuleer("M2-revert", "TP2R", symbol, dag, bars, i, richting, sl,
                                   entry_ref - 2 * d if short else entry_ref + 2 * d, extra, False))
            return [t for t in trades if t]
    return []


# ---------------------------------------------------------------- rapport

def rapport(trades: list[Trade], titel: str) -> str:
    n = len(trades)
    if not n:
        return f"\n{titel}: geen trades"
    rs = [t.r for t in trades]; wins = sum(1 for r in rs if r > 0)
    bruto = sum(t.r * t.risico_eur for t in trades); netto = bruto - FEE_ROUND_TRIP * n
    return (f"\n{titel}\n  trades {n}  winrate {pct(wins, n)}  gem {sum(rs) / n:+.2f}R  "
            f"risico gem €{sum(t.risico_eur for t in trades) / n:.2f}  bruto €{bruto:+.0f}  "
            f"fees €{-FEE_ROUND_TRIP * n:.0f}  NETTO €{netto:+.0f}  (€{netto / n:+.2f} per trade)")


def per_groep(trades: list[Trade], sleutel, titel: str) -> str:
    g: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        g[sleutel(t)].append(t)
    regels = [f"\n{titel}", f"{'bucket':<28}{'n':>5}  {'winrate':>8}  {'gem R':>7}  {'netto/trade':>12}"]
    for k in sorted(g):
        ts = g[k]; n = len(ts); rs = [t.r for t in ts]
        netto = sum(t.r * t.risico_eur - FEE_ROUND_TRIP for t in ts) / n
        regels.append(f"{k:<28}{n:>5}  {pct(sum(1 for r in rs if r > 0), n):>8}  {sum(rs) / n:>+7.2f}  {netto:>+12.2f}")
    return "\n".join(regels)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dagen", type=int, default=240)
    ap.add_argument("--symbolen", default=None)
    ap.add_argument("--min-impuls", type=float, default=0.3, help="min impulsgrootte in ATR14 (model 1)")
    ap.add_argument("--faal-candles", type=int, default=3, help="max candles buiten de VA voor failed auction (model 2)")
    ap.add_argument("--retrace", type=float, default=0.5, help="retracement-fractie VAH->extreme (model 2); 0 = direct")
    ap.add_argument("--vol-factor", type=float, default=1.5, help="triggercandle-volume ≥ factor × dagmediaan (proxy agressie)")
    args = ap.parse_args()
    if args.symbolen:
        symbolen = [s.strip().upper() for s in args.symbolen.split(",")]
    else:
        from news_module import FALLBACK_WATCHLIST
        symbolen = list(FALLBACK_WATCHLIST)

    alle: list[Trade] = []
    for s in symbolen:
        try:
            bars = [b for b in haal_bars(s, args.dagen, "5Min") if in_rth(b.t)]
            dagbars_d = haal_bars(s, args.dagen + 30, "1Day")
            per_dag: dict[date, list[Bar]] = defaultdict(list)
            for b in bars:
                per_dag[b.t.date()].append(b)
            dagen = sorted(per_dag)
            n0 = len(alle)
            for i in range(1, len(dagen)):
                vorige, dag = per_dag[dagen[i - 1]], per_dag[dagen[i]]
                if len(vorige) < 60 or len(dag) < 60:
                    continue
                bs = bin_size(vorige[-1].c)
                va = value_area(profiel(vorige, bs), bs)
                if va is None:
                    continue
                poc, val, vah = va
                pdh, pdl = max(b.h for b in vorige), min(b.l for b in vorige)
                atr = atr14(dagbars_d, dagen[i])
                alle.extend(model1_dag(s, dagen[i], dag, poc, val, vah, pdh, pdl, atr, args.min_impuls, args.vol_factor))
                alle.extend(model2_dag(s, dagen[i], dag, poc, val, vah, args.faal_candles, args.retrace, args.vol_factor))
            print(f"{s}: {len(alle) - n0} trades (alle varianten samen)")
        except Exception as e:
            print(f"{s}: FOUT {e}")

    os.makedirs(OUT_DIR, exist_ok=True)
    stempel = f"{date.today()}_{args.dagen}d"
    if alle:
        with open(os.path.join(OUT_DIR, f"valentini_trades_{stempel}.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(asdict(alle[0]).keys())); w.writeheader()
            for t in alle:
                w.writerow(asdict(t))

    regels = [f"VALENTINI-BACKTEST {stempel}  symbolen {len(symbolen)}  "
              f"min-impuls {args.min_impuls} ATR  faal-candles {args.faal_candles}  retrace {args.retrace}  "
              f"vol-factor {args.vol_factor}",
              f"Fee €{FEE_ROUND_TRIP}, positie min(1% risico, €{POSITIE_EUR:.0f}), signalen "
              f"{EERSTE_SIGNAAL:%H:%M}–{LAATSTE_SIGNAAL:%H:%M}, sluiting {SLUITING:%H:%M}. "
              "Elke variant is dezelfde entry met andere TP/BE — per model telt één 'echte' trade per dag."]
    for model in ("M1-trend", "M2-revert"):
        mt = [t for t in alle if t.model == model]
        regels.append(f"\n{'=' * 78}\n{model}")
        varianten = sorted({t.variant for t in mt})
        for v in varianten:
            vt = [t for t in mt if t.variant == v]
            regels.append(rapport(vt, f"{model} {v}"))
            regels.append(rapport([t for t in vt if "vol ja" in t.extra], f"   └ met volumefilter (vol ≥ {args.vol_factor}× mediaan)"))
            regels.append(rapport([t for t in vt if "vol nee" in t.extra], f"   └ zonder volumefilter"))
        hoofd = "TP3R" if model == "M1-trend" else "TP2R"
        ht = [t for t in mt if t.variant == hoofd]
        regels.append(per_groep(ht, lambda t: t.richting, f"{model} {hoofd} per richting"))
        regels.append(per_groep(ht, lambda t: t.signaal_tijd[:2] + ":00", f"{model} {hoofd} per signaaluur"))
        regels.append(per_groep(ht, lambda t: t.exit, f"{model} {hoofd} per exit"))
        regels.append(per_groep(ht, lambda t: t.symbol, f"{model} {hoofd} per symbool"))
        regels.append(per_groep(ht, lambda t: t.datum[:7], f"{model} {hoofd} per maand"))
    tekst = "\n".join(regels)
    print(tekst)
    open(os.path.join(OUT_DIR, f"valentini_rapport_{stempel}.txt"), "w").write(tekst)
    print(f"\nCSV en rapport in {OUT_DIR}/")


if __name__ == "__main__":
    main()
