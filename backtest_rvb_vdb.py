"""
backtest_rvb_vdb.py — RVB en VDB op de cache-historie, met de bot's eigen modules

RVB: rvb_strategy_module (60-min ORB, close door de rand, volume >
     factor × tijdslot-baseline van de 20 dagen ervoor), elke 5 min
     gescand van 16:30 tot 21:30, entry limiet op de doorbraak-close
     (15 min fill-wacht), SL 2%, TP 2× SL, gedwongen sluiting 21:55.
     Extra: dezelfde scan met volume-factor 2,0 en 1,5.
VDB: vwap_bounce_module.scan_symbol (10 candles trend aan één kant van
     de VWAP, touch-candle, bevestigingscandle), gescand van 16:20 tot
     21:30, entry limiet op de bevestigings-close, SL = touch-extreme
     ± 0,01, TP 2× SL (en 3× als variant), gedwongen sluiting 21:55.
Beide: één trade per symbool per dag (zoals traded_today), positie
min(1% risico / SL, €1.000 / entry), fee €2,50. De limiet "max 2 open
posities per strategie" is NIET gesimuleerd (elk signaal telt), dus de
trade-aantallen zijn een bovengrens.
SL en TP in dezelfde candle -> verlies (pessimistisch).

Gebruik:
    python3 backtest_rvb_vdb.py                  # 240 dagen (cache van de QFS-run)
    python3 backtest_rvb_vdb.py --dagen 120
    python3 backtest_rvb_vdb.py --symbolen META,NVDA
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, date, time as dt_time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data_module import Candle
import rvb_strategy_module as rvb
import vwap_bounce_module as vdb
from backtest_boxrand import haal_bars, in_rth, OUT_DIR, FEE_ROUND_TRIP, POSITIE_EUR, pct

KAPITAAL = 2000.0
RISICO_PCT = 0.01
FILL_WACHT_CANDLES = 3          # 15 min
SLUITING = dt_time(21, 55)


@dataclass
class Trade:
    strategie: str; variant: str; symbol: str; datum: str
    richting: str; signaal_tijd: str; entry_tijd: str | None
    entry: float; sl: float; tp: float; sl_afstand: float; qty: float; risico_eur: float
    extra: str
    r: float | None; exit: str


def naar_candles(bars) -> list[Candle]:
    return [Candle(b.t, b.o, b.h, b.l, b.c, b.v) for b in bars]


def voer_uit(strategie, variant, symbol, dag, dagcandles, richting, sig_t, trigger, sl, tp, extra) -> Trade:
    """Gemeenschappelijke uitvoering: limiet op trigger vanaf de candle NA de signaalcandle."""
    sig_min = sig_t.hour * 60 + sig_t.minute + 5
    sl_min = SLUITING.hour * 60 + SLUITING.minute
    pad = [c for c in dagcandles if sig_min <= c.timestamp.hour * 60 + c.timestamp.minute < sl_min]
    entry = None; idx = None
    for j, c in enumerate(pad[:FILL_WACHT_CANDLES]):
        if richting == "LONG" and c.low <= trigger:
            entry = c.open if c.open < trigger else trigger; idx = j; break
        if richting == "SHORT" and c.high >= trigger:
            entry = c.open if c.open > trigger else trigger; idx = j; break
    sl_afstand = abs((entry if entry is not None else trigger) - sl)
    qty = min(KAPITAAL * RISICO_PCT / sl_afstand, POSITIE_EUR / (entry or trigger)) if sl_afstand > 0 else 0
    r, exit_reden = None, "niet gevuld"
    if entry is not None and sl_afstand > 0:
        if richting == "LONG":
            tp_ = entry + (tp - trigger); sl_ = entry - (trigger - sl)
        else:
            tp_ = entry - (trigger - tp); sl_ = entry + (sl - trigger)
        sl_afstand = abs(entry - sl_)
        r, exit_reden = None, "sluiting"
        for c in pad[idx + 1:]:
            if richting == "LONG":
                sl_raak, tp_raak = c.low <= sl_, c.high >= tp_
            else:
                sl_raak, tp_raak = c.high >= sl_, c.low <= tp_
            if sl_raak:
                r, exit_reden = -1.0, "SL"; break
            if tp_raak:
                r, exit_reden = abs(tp_ - entry) / sl_afstand, "TP"; break
        if r is None:
            laatste = pad[-1]
            r = ((laatste.close - entry) if richting == "LONG" else (entry - laatste.close)) / sl_afstand
        sl, tp = sl_, tp_
    return Trade(strategie, variant, symbol, dag.isoformat(), richting, sig_t.strftime("%H:%M"),
                 pad[idx].timestamp.strftime("%H:%M") if entry is not None else None,
                 round(entry if entry is not None else trigger, 2), round(sl, 2), round(tp, 2), round(sl_afstand, 3),
                 round(qty, 4), round(qty * sl_afstand, 2), extra, r, exit_reden)


def rvb_dag(symbol, dag, dagcandles, baseline, factor) -> Trade | None:
    scan_tijden = []
    t = datetime.combine(dag, dt_time(16, 30))
    while t.time() <= dt_time(21, 30):
        scan_tijden.append(t); t += timedelta(minutes=5)
    for nu in scan_tijden:
        afgesloten = [c for c in rvb.closed_candles(dagcandles, nu) if c.timestamp.date() == dag]
        orb = rvb.compute_opening_range(dagcandles, trade_date=dag)
        if orb is None or not afgesloten:
            continue
        laatste = afgesloten[-1]
        if nu - laatste.timestamp > timedelta(minutes=3 * rvb.BAR_MINUTES + 12):
            continue
        sig = rvb.check_breakout(symbol, laatste, orb, baseline, volume_factor=factor)
        if sig is None:
            continue
        tp, sl = rvb.calculate_rvb_levels(sig.direction, sig.trigger_price)
        return voer_uit("RVB", f"vol>{factor}x", symbol, dag, dagcandles, sig.direction, sig.candle_time,
                        sig.trigger_price, sl, tp, f"{sig.volume_ratio:.1f}x")
    return None


def vdb_dag(symbol, dag, dagcandles, tp_mult) -> Trade | None:
    t = datetime.combine(dag, dt_time(16, 20))
    while t.time() <= dt_time(21, 30):
        sig = vdb.scan_symbol(symbol, dagcandles, t)
        if sig is not None:
            sl = vdb.calculate_vdb_stop_loss(sig)
            tp = vdb.calculate_vdb_take_profit(sig.direction, sig.trigger_price, sl, rr_ratio=tp_mult)
            return voer_uit("VDB", f"TP {tp_mult:g}x", symbol, dag, dagcandles, sig.direction, sig.candle_time,
                            sig.trigger_price, sl, tp, f"vwap {sig.vwap_at_touch:.2f}")
        t += timedelta(minutes=5)
    return None


def rapport(trades: list[Trade], titel: str) -> str:
    g = [t for t in trades if t.r is not None]
    n = len(g)
    if not n:
        return f"\n{titel}: geen gevulde trades ({len(trades)} signalen)"
    rs = [t.r for t in g]; wins = sum(1 for r in rs if r > 0)
    bruto = sum(t.r * t.risico_eur for t in g); netto = bruto - FEE_ROUND_TRIP * n
    return (f"\n{titel}\n  signalen {len(trades)}  gevuld {n} ({pct(n, len(trades))})  winrate {pct(wins, n)}  "
            f"gem {sum(rs) / n:+.2f}R  risico gem €{sum(t.risico_eur for t in g) / n:.2f}  "
            f"bruto €{bruto:+.0f}  fees €{-FEE_ROUND_TRIP * n:.0f}  NETTO €{netto:+.0f}  (€{netto / n:+.2f} per trade)")


def per_groep(trades: list[Trade], sleutel, titel: str) -> str:
    g: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        if t.r is not None:
            g[sleutel(t)].append(t)
    regels = [f"\n{titel}", f"{'bucket':<26}{'n':>5}  {'winrate':>8}  {'gem R':>7}  {'netto/trade':>12}"]
    for k in sorted(g):
        ts = g[k]; n = len(ts); rs = [t.r for t in ts]
        netto = sum(t.r * t.risico_eur - FEE_ROUND_TRIP for t in ts) / n
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

    alle: list[Trade] = []
    for s in symbolen:
        try:
            bars = [b for b in haal_bars(s, args.dagen, "5Min") if in_rth(b.t)]
            per_dag: dict[date, list[Candle]] = defaultdict(list)
            for c in naar_candles(bars):
                per_dag[c.timestamp.date()].append(c)
            dagen = sorted(per_dag)
            n0 = len(alle)
            for i, dag in enumerate(dagen):
                if i < 20:
                    continue
                hist = [c for d in dagen[i - 20:i] for c in per_dag[d]]
                baseline = rvb.build_volume_baseline(hist)
                if baseline["days"] < rvb.BASELINE_MIN_DAYS:
                    continue
                dc = per_dag[dag]
                for factor in (3.0, 2.0, 1.5):
                    t = rvb_dag(s, dag, dc, baseline, factor)
                    if t: alle.append(t)
                for tp_mult in (2.0, 3.0):
                    t = vdb_dag(s, dag, dc, tp_mult)
                    if t: alle.append(t)
            print(f"{s}: {len(alle) - n0} signalen (alle varianten samen)")
        except Exception as e:
            print(f"{s}: FOUT {e}")

    os.makedirs(OUT_DIR, exist_ok=True)
    stempel = f"{date.today()}_{args.dagen}d"
    if alle:
        with open(os.path.join(OUT_DIR, f"rvb_vdb_trades_{stempel}.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(asdict(alle[0]).keys())); w.writeheader()
            for t in alle:
                w.writerow(asdict(t))

    def sel(strat, var): return [t for t in alle if t.strategie == strat and t.variant == var]
    rvb3, rvb2, rvb15 = sel("RVB", "vol>3.0x"), sel("RVB", "vol>2.0x"), sel("RVB", "vol>1.5x")
    vdb2, vdb3 = sel("VDB", "TP 2x"), sel("VDB", "TP 3x")
    delen = [
        f"RVB/VDB-backtest  {stempel}  |  {len(symbolen)} symbolen  (1 trade/symbool/dag, 1% risico, cap €1000, fee €{FEE_ROUND_TRIP}, sluiting 21:55)",
        rapport(rvb3, "RVB zoals de bot: volume > 3x baseline, SL 2%, TP 4%"),
        rapport(rvb2, "RVB volume > 2x"),
        rapport(rvb15, "RVB volume > 1.5x"),
        per_groep(rvb3, lambda t: t.richting, "RVB 3x — per richting"),
        per_groep(rvb3, lambda t: t.signaal_tijd[:2] + ":xx", "RVB 3x — per signaal-uur"),
        per_groep(rvb3, lambda t: t.exit, "RVB 3x — per exit-reden"),
        rapport(vdb2, "VDB zoals de bot: touch + bevestiging na 10-candle trend, SL touch-extreme, TP 2x"),
        rapport(vdb3, "VDB TP 3x"),
        per_groep(vdb2, lambda t: t.richting, "VDB — per richting"),
        per_groep(vdb2, lambda t: t.signaal_tijd[:2] + ":xx", "VDB — per signaal-uur"),
        per_groep(vdb2, lambda t: t.exit, "VDB — per exit-reden"),
        per_groep(rvb3, lambda t: t.symbol, "RVB 3x — per symbool"),
        per_groep(vdb2, lambda t: t.symbol, "VDB — per symbool"),
    ]
    tekst = "\n".join(delen)
    print("\n" + tekst)
    open(os.path.join(OUT_DIR, f"rvb_vdb_rapport_{stempel}.txt"), "w").write(tekst)


if __name__ == "__main__":
    main()
