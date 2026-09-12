"""
backtest_break_bounce.py — "Break and Bounce" (Carl / ProRealAlgos) op de cache-historie

Regels uit het transcript:
  1. Dag:    box = high/low van de VORIGE handelsdag (hier uit 5-min RTH-data).
  2. 15-min: breakout = 15-min candle SLUIT boven de high (LONG-bias) of onder
             de low (SHORT-bias), binnen 150 min na de opening (15:30-18:00 CEST).
  3. 5-min:  na de breakout een retest van het niveau met een omkeercandle, ook
             binnen 150 min:
             - hamer (LONG) / inverted hamer (SHORT), na een rode/groene candle:
                 variant "break": entry op de break van de hamer-high/low (video 1)
                 variant "close": entry op de close van de hamer (video 2)
               SL net onder/boven de hamer-extreme.
             - engulfing: candle komt onder de low (LONG) van de vorige rode candle
               -> buy-stop op de high van die vorige candle; SL net onder de low van
               de engulfing-candle. Spiegelbeeld voor SHORT.
  4. TP = 2x of 3x de stopafstand. Nog open om 21:55 -> market close.
  5. Eén trade per aandeel per dag.
Claim van de maker: 70% winrate, profit factor 1,6.

Filters als variabele: "clear negative movement" vóór de hamer = 1 of 3 rode candles.
Retest-tolerantie: candle-extreme binnen RETEST_TOL van het niveau.
Positie: min(1% risico / SL, €1.000 / entry), fee €2,50. TP en SL in dezelfde
candle -> verlies (pessimistisch).

Gebruik:
    python3 backtest_break_bounce.py                 # 240 dagen, hele watchlist
    python3 backtest_break_bounce.py --dagen 480 --symbolen NFLX,AAPL
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, date, time as dt_time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest_boxrand import haal_bars, in_rth, OUT_DIR, FEE_ROUND_TRIP, POSITIE_EUR, pct, Bar

KAPITAAL = 2000.0
RISICO_PCT = 0.01
VENSTER_MIN = 150                 # 2,5 uur na opening
OPEN = dt_time(15, 30)
SLUITING = dt_time(21, 55)
RETEST_TOL = 0.0015               # 0,15%
SL_BUFFER = 0.0005                # "slightly below the low"


@dataclass
class BBTrade:
    symbol: str; datum: str; richting: str; niveau: float
    breakout_tijd: str; patroon: str; signaal_tijd: str; entry_tijd: str | None
    entry: float; sl: float; sl_afstand: float; qty: float; risico_eur: float
    r_2x: float | None; exit_2x: str; r_3x: float | None; exit_3x: str
    rode_voor: int


def is_hamer(c: Bar) -> bool:
    """Romp in het bovenste deel, onderlont >= 2x romp, bovenlont <= romp."""
    rng = c.h - c.l
    if rng <= 0: return False
    romp = abs(c.c - c.o); onder = min(c.o, c.c) - c.l; boven = c.h - max(c.o, c.c)
    return onder >= 2 * romp and boven <= max(romp, 0.1 * rng) and onder / rng >= 0.6


def is_inverted_hamer(c: Bar) -> bool:
    rng = c.h - c.l
    if rng <= 0: return False
    romp = abs(c.c - c.o); onder = min(c.o, c.c) - c.l; boven = c.h - max(c.o, c.c)
    return boven >= 2 * romp and onder <= max(romp, 0.1 * rng) and boven / rng >= 0.6


def rood(c: Bar) -> bool: return c.c < c.o
def groen(c: Bar) -> bool: return c.c > c.o


def aantal_rood_voor(cs: list[Bar], i: int, kleur) -> int:
    n = 0; j = i - 1
    while j >= 0 and kleur(cs[j]):
        n += 1; j -= 1
    return n


def minuten(t: datetime) -> int:
    return t.hour * 60 + t.minute


def naar_15(c5: list[Bar]) -> list[Bar]:
    g: dict[datetime, list[Bar]] = defaultdict(list)
    for c in c5:
        g[c.t.replace(minute=(c.t.minute // 15) * 15, second=0)].append(c)
    return [Bar(k, x[0].o, max(b.h for b in x), min(b.l for b in x), x[-1].c, sum(b.v for b in x)) for k, x in sorted(g.items())]


def simuleer_dag(symbol: str, dag: date, c5: list[Bar], prev_high: float, prev_low: float,
                 hamer_entry: str, min_rood: int) -> BBTrade | None:
    open_min = minuten(datetime.combine(dag, OPEN))
    venster_einde = open_min + VENSTER_MIN
    richting = niveau = None; breakout_t = None
    for c in naar_15(c5):
        if minuten(c.t) + 15 > venster_einde:
            break
        if c.c > prev_high:
            richting, niveau, breakout_t = "LONG", prev_high, c.t; break
        if c.c < prev_low:
            richting, niveau, breakout_t = "SHORT", prev_low, c.t; break
    if richting is None:
        return None
    na_break_min = minuten(breakout_t) + 15

    zoek = [c for c in c5 if na_break_min <= minuten(c.t)]
    for i, c in enumerate(zoek):
        if minuten(c.t) + 5 > venster_einde:
            break
        if i == 0:
            continue
        prev = zoek[i - 1]
        if richting == "LONG":
            raakt = c.l <= niveau * (1 + RETEST_TOL) and c.c >= niveau * (1 - RETEST_TOL)
            if not raakt:
                continue
            if is_hamer(c) and aantal_rood_voor(zoek, i, rood) >= min_rood:
                trigger = c.h if hamer_entry == "break" else c.c
                sl = c.l * (1 - SL_BUFFER)
                return voer_uit(symbol, dag, c5, richting, niveau, breakout_t, "hamer", c, trigger, sl,
                                aantal_rood_voor(zoek, i, rood), entry_in_signaal=(hamer_entry == "close"))
            if rood(prev) and c.l < prev.l and c.h >= prev.h and groen(c) and c.c > prev.o:
                trigger = prev.h
                sl = c.l * (1 - SL_BUFFER)
                return voer_uit(symbol, dag, c5, richting, niveau, breakout_t, "bullish_engulfing", c, trigger, sl,
                                aantal_rood_voor(zoek, i, rood), entry_in_signaal=True)
        else:
            raakt = c.h >= niveau * (1 - RETEST_TOL) and c.c <= niveau * (1 + RETEST_TOL)
            if not raakt:
                continue
            if is_inverted_hamer(c) and aantal_rood_voor(zoek, i, groen) >= min_rood:
                trigger = c.l if hamer_entry == "break" else c.c
                sl = c.h * (1 + SL_BUFFER)
                return voer_uit(symbol, dag, c5, richting, niveau, breakout_t, "inverted_hamer", c, trigger, sl,
                                aantal_rood_voor(zoek, i, groen), entry_in_signaal=(hamer_entry == "close"))
            if groen(prev) and c.h > prev.h and c.l <= prev.l and rood(c) and c.c < prev.o:
                trigger = prev.l
                sl = c.h * (1 + SL_BUFFER)
                return voer_uit(symbol, dag, c5, richting, niveau, breakout_t, "bearish_engulfing", c, trigger, sl,
                                aantal_rood_voor(zoek, i, groen), entry_in_signaal=True)
    return None


def voer_uit(symbol, dag, c5, richting, niveau, breakout_t, patroon, sig: Bar, trigger, sl, rode_voor,
             entry_in_signaal: bool) -> BBTrade:
    sl_min = minuten(datetime.combine(dag, SLUITING))
    if entry_in_signaal:
        entry, entry_t = trigger, sig.t
        pad = [c for c in c5 if minuten(sig.t) + 5 <= minuten(c.t) < sl_min]
    else:
        entry = None; entry_t = None
        kandidaten = [c for c in c5 if minuten(sig.t) + 5 <= minuten(c.t) < sl_min]
        for j, c in enumerate(kandidaten[:6]):
            if richting == "LONG" and c.h >= trigger:
                entry = max(trigger, c.o) if c.o > trigger else trigger; entry_t = c.t; pad = kandidaten[j + 1:]; break
            if richting == "SHORT" and c.l <= trigger:
                entry = min(trigger, c.o) if c.o < trigger else trigger; entry_t = c.t; pad = kandidaten[j + 1:]; break
        if entry is None:
            pad = []
    sl_afstand = abs((entry if entry is not None else trigger) - sl)
    qty = min(KAPITAAL * RISICO_PCT / sl_afstand, POSITIE_EUR / (entry or trigger)) if sl_afstand > 0 else 0

    def loop(mult):
        if entry is None or sl_afstand <= 0:
            return None, "niet gevuld"
        tp = entry + mult * sl_afstand * (1 if richting == "LONG" else -1)
        for c in pad:
            if richting == "LONG":
                sl_raak, tp_raak = c.l <= sl, c.h >= tp
            else:
                sl_raak, tp_raak = c.h >= sl, c.l <= tp
            if sl_raak: return -1.0, "SL"
            if tp_raak: return float(mult), "TP"
        if not pad:
            return 0.0, "sluiting direct"
        r = ((pad[-1].c - entry) if richting == "LONG" else (entry - pad[-1].c)) / sl_afstand
        return max(-1.0, min(float(mult), r)), "sluiting"

    r2, e2 = loop(2); r3, e3 = loop(3)
    return BBTrade(symbol, dag.isoformat(), richting, round(niveau, 2), breakout_t.strftime("%H:%M"), patroon,
                   sig.t.strftime("%H:%M"), entry_t.strftime("%H:%M") if entry_t else None,
                   round(entry if entry is not None else trigger, 2), round(sl, 2), round(sl_afstand, 3),
                   round(qty, 4), round(qty * sl_afstand, 2), r2, e2, r3, e3, rode_voor)


def rapport(trades: list[BBTrade], titel: str, veld: str) -> str:
    g = [t for t in trades if getattr(t, veld) is not None]
    n = len(g)
    if not n:
        return f"\n{titel}: geen gevulde trades ({len(trades)} signalen)"
    rs = [getattr(t, veld) for t in g]; wins = sum(1 for r in rs if r > 0)
    winst = sum(r * t.risico_eur for r, t in zip(rs, g) if r > 0); verlies = -sum(r * t.risico_eur for r, t in zip(rs, g) if r < 0)
    pf = winst / verlies if verlies else float("inf")
    bruto = winst - verlies; netto = bruto - FEE_ROUND_TRIP * n
    return (f"\n{titel}\n  signalen {len(trades)}  gevuld {n} ({pct(n, len(trades))})  winrate {pct(wins, n)}  "
            f"profit factor {pf:.2f}  gem {sum(rs) / n:+.2f}R  risico gem €{sum(t.risico_eur for t in g) / n:.2f}  "
            f"bruto €{bruto:+.0f}  fees €{-FEE_ROUND_TRIP * n:.0f}  NETTO €{netto:+.0f}  (€{netto / n:+.2f} per trade)")


def per_groep(trades, veld, sleutel, titel) -> str:
    g: dict[str, list[BBTrade]] = defaultdict(list)
    for t in trades:
        if getattr(t, veld) is not None:
            g[sleutel(t)].append(t)
    regels = [f"\n{titel}", f"{'bucket':<26}{'n':>5}  {'winrate':>8}  {'gem R':>7}  {'netto/trade':>12}"]
    for k in sorted(g):
        ts = g[k]; n = len(ts); rs = [getattr(t, veld) for t in ts]
        netto = sum(r * t.risico_eur - FEE_ROUND_TRIP for r, t in zip(rs, ts)) / n
        regels.append(f"{k:<26}{n:>5}  {pct(sum(1 for r in rs if r > 0), n):>8}  {sum(rs) / n:>+7.2f}  {netto:>+12.2f}")
    return "\n".join(regels)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dagen", type=int, default=240)
    ap.add_argument("--symbolen", default=None)
    args = ap.parse_args()
    if args.symbolen:
        symbolen = [s.strip().upper() for s in args.symbolen.split(",")]
    else:
        from news_module import FALLBACK_WATCHLIST
        symbolen = list(FALLBACK_WATCHLIST)

    varianten = {"hamer=break, ≥1 rood": ("break", 1), "hamer=close, ≥1 rood": ("close", 1), "hamer=break, ≥3 rood": ("break", 3)}
    resultaten: dict[str, list[BBTrade]] = {k: [] for k in varianten}
    dagen_totaal = 0
    for s in symbolen:
        try:
            bars = [b for b in haal_bars(s, args.dagen, "5Min") if in_rth(b.t)]
            per_dag: dict[date, list[Bar]] = defaultdict(list)
            for b in bars:
                per_dag[b.t.date()].append(b)
            dagen = sorted(per_dag)
            n0 = {k: len(v) for k, v in resultaten.items()}
            for i in range(1, len(dagen)):
                vorige, dag = per_dag[dagen[i - 1]], dagen[i]
                if len(vorige) < 60 or len(per_dag[dag]) < 60:
                    continue
                dagen_totaal += 1
                ph, pl = max(b.h for b in vorige), min(b.l for b in vorige)
                for naam, (he, mr) in varianten.items():
                    t = simuleer_dag(s, dag, per_dag[dag], ph, pl, he, mr)
                    if t: resultaten[naam].append(t)
            print(f"{s}: " + ", ".join(f"{k.split(',')[0]} {len(v) - n0[k]}" for k, v in resultaten.items()))
        except Exception as e:
            print(f"{s}: FOUT {e}")

    os.makedirs(OUT_DIR, exist_ok=True)
    stempel = f"{date.today()}_{args.dagen}d"
    hoofd = resultaten["hamer=break, ≥1 rood"]
    if hoofd:
        with open(os.path.join(OUT_DIR, f"bb_trades_{stempel}.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(asdict(hoofd[0]).keys())); w.writeheader()
            for t in hoofd:
                w.writerow(asdict(t))

    delen = [f"Break & Bounce-backtest  {stempel}  |  {len(symbolen)} symbolen, {dagen_totaal} symbool-dagen  "
             f"(1 trade/symbool/dag, 1% risico, cap €1000, fee €{FEE_ROUND_TRIP}, venster 150 min, sluiting 21:55)",
             f"Claim maker: winrate 70%, profit factor 1,6, 2-3 setups per maand per aandeel "
             f"(hier: {len(hoofd) / max(dagen_totaal, 1) * 21:.1f} per maand per aandeel)"]
    for naam, ts in resultaten.items():
        delen.append(rapport(ts, f"{naam} — TP 2x", "r_2x"))
        delen.append(rapport(ts, f"{naam} — TP 3x", "r_3x"))
    delen += [per_groep(hoofd, "r_2x", lambda t: t.patroon, "Hoofdvariant — per patroon (TP 2x)"),
              per_groep(hoofd, "r_2x", lambda t: t.richting, "Hoofdvariant — per richting"),
              per_groep(hoofd, "r_2x", lambda t: t.exit_2x, "Hoofdvariant — per exit-reden"),
              per_groep(hoofd, "r_2x", lambda t: t.signaal_tijd[:2] + ":xx", "Hoofdvariant — per signaal-uur"),
              per_groep(hoofd, "r_2x", lambda t: f"{min(t.rode_voor, 4)} candles ervoor", "Hoofdvariant — per lengte voorafgaande beweging"),
              per_groep(hoofd, "r_2x", lambda t: t.symbol, "Hoofdvariant — per symbool")]
    tekst = "\n".join(delen)
    print("\n" + tekst)
    open(os.path.join(OUT_DIR, f"bb_rapport_{stempel}.txt"), "w").write(tekst)


if __name__ == "__main__":
    main()
