"""
backtest_vwap_ema.py — "VWAP + 8 EMA" (YouTube, dagelijks gebruikte 2-indicator-setup) op de cache-historie

Regels uit het transcript:
  - VWAP van de dag (RTH, anker 15:30 CEST) en 8 EMA op 5-min.
  - Nooit long onder VWAP, nooit short boven VWAP.
  - Setup A "VWAP-retest": koers gevestigd boven VWAP (>= MIN_BOVEN closes), candle raakt
    VWAP (binnen RETEST_TOL) en sluit erboven -> LONG. Spiegelbeeld SHORT.
  - Setup B "PM-retest": eerst een 5-min close boven de pre-market high (PMH), daarna een
    candle die PMH raakt, erboven sluit en boven VWAP staat -> LONG. Spiegel: PML / SHORT.
    PMH onder VWAP -> geen long op PMH (regel uit de video), dan alleen setup A.
  - "A+": VWAP binnen CONFL_TOL van PMH/PML (twee confluenties) -> apart gelabeld.
  - Exit: eerste 5-min CLOSE onder (LONG) / boven (SHORT) de EMA, nadat de koers eerst een
    keer aan de goede kant van de EMA gesloten heeft ("armed"). Harde SL op de extreme van
    de signaalcandle (intrabar, pessimistisch bij SL+TP in dezelfde candle).
  - Varianten: exit op close onder/boven VWAP ("if it got under VWAP we take the loss"),
    vaste TP 2x SL; entry op close van de signaalcandle of op de break van zijn high/low.
  - Nog open om 21:55 -> market close. Eén trade per aandeel per dag (eerste signaal).

Pre-market = bars van dezelfde dag vóór 15:30 CEST. Ontbreekt die in de cache, dan wordt
setup B overgeslagen en telt het rapport dat.

Gebruik:
    python3 backtest_vwap_ema.py                     # 240 dagen, hele watchlist
    python3 backtest_vwap_ema.py --ema 9 --dagen 480 --symbolen PLTR,AAPL
    python3 backtest_vwap_ema.py --vwap-premarket     # VWAP incl. pre-market volume
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
OPEN = dt_time(15, 30)
EERSTE_ENTRY = dt_time(15, 40)    # niet in de openingscandle zelf
DEADLINE = dt_time(18, 0)         # laatste entry
SLUITING = dt_time(21, 55)
PM_START = dt_time(10, 0)         # 04:00 ET
RETEST_TOL = 0.0010               # 0,10 % rond het niveau
CONFL_TOL = 0.0020                # VWAP binnen 0,2 % van PMH/PML = "A+"
SL_BUFFER = 0.0005
MIN_BOVEN = 3                     # closes aan de goede kant van VWAP vóór de retest
BREAK_WACHT = 3                   # candles om de break van de signaalcandle te krijgen


@dataclass
class VETrade:
    symbol: str; datum: str; richting: str; setup: str; a_plus: bool
    boven_pm: bool; pm_aanwezig: bool
    signaal_tijd: str; entry_tijd: str | None; entry: float; sl: float; sl_afstand: float
    qty: float; risico_eur: float; vwap_bij_entry: float; ema_bij_entry: float
    r_ema: float | None; exit_ema: str; exit_tijd_ema: str | None
    r_vwap: float | None; exit_vwap: str
    r_tp2: float | None; exit_tp2: str
    mfe_r: float | None                     # max gunstige uitslag in R (tot sluiting)


def minuten(t: datetime) -> int:
    return t.hour * 60 + t.minute


def ema_reeks(closes: list[float], n: int) -> list[float]:
    k = 2 / (n + 1); out = []; e = None
    for c in closes:
        e = c if e is None else c * k + e * (1 - k)
        out.append(e)
    return out


def vwap_reeks(bars: list[Bar]) -> list[float]:
    pv = v = 0.0; out = []
    for b in bars:
        tp = (b.h + b.l + b.c) / 3
        pv += tp * b.v; v += b.v
        out.append(pv / v if v > 0 else tp)
    return out


def simuleer_dag(symbol: str, dag: date, c5: list[Bar], vwap: list[float], ema: list[float],
                 pmh: float | None, pml: float | None, entry_mode: str) -> VETrade | None:
    eerste = minuten(datetime.combine(dag, EERSTE_ENTRY)); deadline = minuten(datetime.combine(dag, DEADLINE))
    pm_ok = pmh is not None
    boven_teller = onder_teller = 0            # opeenvolgende closes boven/onder VWAP
    pm_break_long = pm_break_short = False     # al een close buiten de pre-market range gezien

    for i, c in enumerate(c5):
        vw, em = vwap[i], ema[i]
        # context bijwerken op de close van deze candle
        if c.c > vw: boven_teller += 1; onder_teller = 0
        elif c.c < vw: onder_teller += 1; boven_teller = 0
        if pm_ok:
            if c.c > pmh: pm_break_long = True
            if c.c < pml: pm_break_short = True
        m = minuten(c.t)
        if m < eerste: continue
        if m > deadline: break

        # --- LONG (alleen boven VWAP) ---
        if c.c > vw:
            setup = None
            if pm_ok and pm_break_long and c.l <= pmh * (1 + RETEST_TOL) and c.c > pmh:
                setup = "PMH-retest"
            if boven_teller >= MIN_BOVEN + 1 and c.l <= vw * (1 + RETEST_TOL) and \
                    all(c5[j].c > vwap[j] for j in range(max(0, i - MIN_BOVEN), i)):
                setup = "VWAP-retest" if setup is None else "VWAP+PMH-retest"
            if setup:
                a_plus = pm_ok and abs(vw - pmh) / pmh <= CONFL_TOL
                return voer_uit(symbol, dag, c5, vwap, ema, i, "LONG", setup, a_plus,
                                pm_ok and c.c > pmh, pm_ok, entry_mode)
        # --- SHORT (alleen onder VWAP) ---
        if c.c < vw:
            setup = None
            if pm_ok and pm_break_short and c.h >= pml * (1 - RETEST_TOL) and c.c < pml:
                setup = "PML-retest"
            if onder_teller >= MIN_BOVEN + 1 and c.h >= vw * (1 - RETEST_TOL) and \
                    all(c5[j].c < vwap[j] for j in range(max(0, i - MIN_BOVEN), i)):
                setup = "VWAP-retest" if setup is None else "VWAP+PML-retest"
            if setup:
                a_plus = pm_ok and abs(vw - pml) / pml <= CONFL_TOL
                return voer_uit(symbol, dag, c5, vwap, ema, i, "SHORT", setup, a_plus,
                                pm_ok and c.c < pml, pm_ok, entry_mode)
    return None


def voer_uit(symbol, dag, c5, vwap, ema, i, richting, setup, a_plus, boven_pm, pm_ok, entry_mode) -> VETrade:
    sig = c5[i]; sl_min = minuten(datetime.combine(dag, SLUITING))
    lng = richting == "LONG"
    sl = sig.l * (1 - SL_BUFFER) if lng else sig.h * (1 + SL_BUFFER)
    entry = entry_t = None; start = i + 1
    if entry_mode == "close":
        entry, entry_t = sig.c, sig.t
    else:  # break van de signaalcandle-high/low, stop-order, max BREAK_WACHT candles
        trig = sig.h if lng else sig.l
        for j in range(i + 1, min(i + 1 + BREAK_WACHT, len(c5))):
            c = c5[j]
            if minuten(c.t) >= sl_min: break
            if (lng and c.h >= trig) or (not lng and c.l <= trig):
                entry = (max(trig, c.o) if lng else min(trig, c.o)); entry_t = c.t; start = j + 1
                # zelfde candle kan ook de SL raken (pessimistisch)
                if (lng and c.l <= sl) or (not lng and c.h >= sl):
                    start = j  # laat de SL-check hem pakken
                break
    sl_afstand = abs((entry if entry is not None else sig.c) - sl)
    qty = min(KAPITAAL * RISICO_PCT / sl_afstand, POSITIE_EUR / (entry or sig.c)) if sl_afstand > 0 else 0
    pad = [(c, vwap[k], ema[k]) for k, c in enumerate(c5) if k >= start and minuten(c.t) < sl_min]

    def r_van(prijs):
        return ((prijs - entry) if lng else (entry - prijs)) / sl_afstand

    def loop(modus):
        if entry is None or sl_afstand <= 0:
            return None, "niet gevuld", None
        armed = (sig.c > ema[i]) if lng else (sig.c < ema[i])  # signaalcandle zelf al aan de goede kant?
        tp = entry + 2 * sl_afstand * (1 if lng else -1)
        for c, vw, em in pad:
            if (lng and c.l <= sl) or (not lng and c.h >= sl):
                return -1.0, "SL", c.t.strftime("%H:%M")
            if modus == "tp2" and ((lng and c.h >= tp) or (not lng and c.l <= tp)):
                return 2.0, "TP", c.t.strftime("%H:%M")
            if modus == "ema":
                goed = c.c > em if lng else c.c < em
                if goed: armed = True
                elif armed:
                    return max(-1.0, r_van(c.c)), "EMA-close", c.t.strftime("%H:%M")
            if modus == "vwap":
                if (lng and c.c < vw) or (not lng and c.c > vw):
                    return max(-1.0, r_van(c.c)), "VWAP-close", c.t.strftime("%H:%M")
        if not pad:
            return 0.0, "sluiting direct", None
        return max(-1.0, r_van(pad[-1][0].c)), "sluiting", pad[-1][0].t.strftime("%H:%M")

    re_, ee, te = loop("ema"); rv, ev, _ = loop("vwap"); rt, et, _ = loop("tp2")
    mfe = None
    if entry is not None and sl_afstand > 0:
        mfe = max([r_van(c.h if lng else c.l) for c, _, _ in pad], default=0.0)
    return VETrade(symbol, dag.isoformat(), richting, setup, a_plus, boven_pm, pm_ok,
                   sig.t.strftime("%H:%M"), entry_t.strftime("%H:%M") if entry_t else None,
                   round(entry if entry is not None else sig.c, 2), round(sl, 2), round(sl_afstand, 3),
                   round(qty, 4), round(qty * sl_afstand, 2), round(vwap[i], 2), round(ema[i], 2),
                   re_, ee, te, rv, ev, rt, et, round(mfe, 2) if mfe is not None else None)


# --- rapport ---------------------------------------------------------------------
def rapport(trades: list[VETrade], titel: str, veld: str) -> str:
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
    g: dict[str, list[VETrade]] = defaultdict(list)
    for t in trades:
        if getattr(t, veld) is not None:
            g[str(sleutel(t))].append(t)
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
    ap.add_argument("--ema", type=int, default=8)
    ap.add_argument("--vwap-premarket", action="store_true", help="VWAP ankeren op de eerste pre-market bar i.p.v. 15:30")
    args = ap.parse_args()
    if args.symbolen:
        symbolen = [s.strip().upper() for s in args.symbolen.split(",")]
    else:
        from news_module import FALLBACK_WATCHLIST
        symbolen = list(FALLBACK_WATCHLIST)

    varianten = {"entry=close": "close", "entry=break": "break"}
    resultaten: dict[str, list[VETrade]] = {k: [] for k in varianten}
    dagen_totaal = dagen_zonder_pm = 0
    for s in symbolen:
        try:
            alle = haal_bars(s, args.dagen, "5Min")
            rth = [b for b in alle if in_rth(b.t)]
            pm_per_dag: dict[date, list[Bar]] = defaultdict(list)
            for b in alle:
                if not in_rth(b.t) and PM_START <= b.t.time() < OPEN:
                    pm_per_dag[b.t.date()].append(b)
            per_dag: dict[date, list[Bar]] = defaultdict(list)
            for b in rth:
                per_dag[b.t.date()].append(b)
            # EMA doorlopend over de RTH-reeks (zoals TradingView met extended hours uit)
            ema_alle = ema_reeks([b.c for b in rth], args.ema)
            ema_idx = {(b.t): e for b, e in zip(rth, ema_alle)}
            n0 = {k: len(v) for k, v in resultaten.items()}
            for dag in sorted(per_dag):
                c5 = per_dag[dag]
                if len(c5) < 60:
                    continue
                dagen_totaal += 1
                pm = pm_per_dag.get(dag, [])
                pmh = max(b.h for b in pm) if pm else None
                pml = min(b.l for b in pm) if pm else None
                if not pm: dagen_zonder_pm += 1
                if args.vwap_premarket and pm:
                    vw = vwap_reeks(pm + c5)[len(pm):]
                else:
                    vw = vwap_reeks(c5)
                em = [ema_idx[b.t] for b in c5]
                for naam, mode in varianten.items():
                    t = simuleer_dag(s, dag, c5, vw, em, pmh, pml, mode)
                    if t: resultaten[naam].append(t)
            print(f"{s}: " + ", ".join(f"{k} {len(v) - n0[k]}" for k, v in resultaten.items()))
        except Exception as e:
            print(f"{s}: FOUT {e}")

    os.makedirs(OUT_DIR, exist_ok=True)
    stempel = f"{date.today()}_{args.dagen}d_ema{args.ema}{'_pmvwap' if args.vwap_premarket else ''}"
    hoofd = resultaten["entry=close"]
    if hoofd:
        with open(os.path.join(OUT_DIR, f"vwapema_trades_{stempel}.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(asdict(hoofd[0]).keys())); w.writeheader()
            for t in hoofd:
                w.writerow(asdict(t))

    delen = [f"VWAP+EMA-backtest  {stempel}  |  {len(symbolen)} symbolen, {dagen_totaal} symbool-dagen, "
             f"{dagen_zonder_pm} zonder pre-market-data  (1 trade/symbool/dag, 1% risico, cap €1000, "
             f"fee €{FEE_ROUND_TRIP}, entries 15:40-18:00, sluiting 21:55)",
             f"Signalen per maand per aandeel: {len(hoofd) / max(dagen_totaal, 1) * 21:.1f}"]
    for naam, ts in resultaten.items():
        delen.append(rapport(ts, f"{naam} — exit EMA{args.ema}-close (video)", "r_ema"))
        delen.append(rapport(ts, f"{naam} — exit VWAP-close", "r_vwap"))
        delen.append(rapport(ts, f"{naam} — vaste TP 2x", "r_tp2"))
    delen += [per_groep(hoofd, "r_ema", lambda t: t.setup, "Hoofdvariant (entry=close, exit EMA) — per setup"),
              per_groep(hoofd, "r_ema", lambda t: t.richting, "— per richting"),
              per_groep(hoofd, "r_ema", lambda t: "A+ (VWAP≈PM-niveau)" if t.a_plus else "gewoon", "— A+ confluentie"),
              per_groep(hoofd, "r_ema", lambda t: ("boven PMH/onder PML" if t.boven_pm else "binnen PM-range") if t.pm_aanwezig else "geen PM-data", "— positie t.o.v. pre-market range"),
              per_groep(hoofd, "r_ema", lambda t: t.exit_ema, "— per exit-reden"),
              per_groep(hoofd, "r_ema", lambda t: t.signaal_tijd[:2] + ":xx", "— per signaal-uur"),
              per_groep(hoofd, "r_ema", lambda t: f"MFE {min(int(t.mfe_r or 0), 4)}R+" , "— per max gunstige uitslag (wat een perfecte exit had kunnen halen)"),
              per_groep(hoofd, "r_ema", lambda t: t.symbol, "— per symbool")]
    tekst = "\n".join(delen)
    print("\n" + tekst)
    open(os.path.join(OUT_DIR, f"vwapema_rapport_{stempel}.txt"), "w").write(tekst)


if __name__ == "__main__":
    main()
