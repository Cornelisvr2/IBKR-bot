"""
backtest_qfs.py — De ECHTE Quick Flip Scalper op 120 dagen historie

Geen benadering: gebruikt de patroonfuncties van de bot zelf
(reversal_pattern_module: check_engulfing_signal, check_hamer_setup,
check_hamer_confirmation) en dezelfde flow als reversal_monitor_module:
    box = 15:30-15:45 openingscandle, ATR-filter 0,25×ATR(14),
    kleur -> verwachte richting, daarna candle voor candle (afgesloten)
    wachten op engulfing (1-staps) of hamer + break (2-staps) BUITEN de
    box, tot de deadline. Eén trade per symbool per dag.
Entry: limiet op trigger_price, vult als een latere candle het niveau
raakt (opent hij er al voorbij: fill op de open). Stop = patroon-SL.
TP-varianten: overkant box (zoals de bot), 2× stop, 3× stop.
Beide TP en SL in dezelfde candle -> verlies (pessimistisch).
Deadline: geen fill of open positie op de deadline -> annuleren / market-
close op de close van die candle.

Positie: min(1% risico / stopafstand, €1.000 / entry). Fees €2,50.

Gebruik:
    python3 backtest_qfs.py                    # 120 dagen, 5-min candles, deadline 18:00
    python3 backtest_qfs.py --bar 15min        # patronen op 15-min candles
    python3 backtest_qfs.py --deadline 21:55
    python3 backtest_qfs.py --symbolen META,GOOGL

Hergebruikt de data-cache van backtest_boxrand.py (data/backtest/).
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

from data_module import Candle
from reversal_pattern_module import check_engulfing_signal, check_hamer_setup, check_hamer_confirmation
from backtest_boxrand import haal_bars, atr14, in_rth, OUT_DIR, FEE_ROUND_TRIP, POSITIE_EUR, ATR_FACTOR, pct

KAPITAAL = 2000.0
RISICO_PCT = 0.01


@dataclass
class QfsTrade:
    symbol: str; datum: str; bar: str
    box_high: float; box_low: float; range_atr: float
    richting: str; patroon: str
    signaal_tijd: str; entry_tijd: str | None
    entry: float; sl: float; sl_afstand: float; qty: float; risico_eur: float
    tp_overkant: float
    r_overkant: float | None; r_2x: float | None; r_3x: float | None
    exit_overkant: str; exit_2x: str; exit_3x: str
    max_mee_R: float; max_tegen_R: float


def naar_candles(bars, bar_size: str) -> list[Candle]:
    """5-min Bars -> Candle-objecten; bij 15min aggregeren per kwartier."""
    cs = [Candle(b.t, b.o, b.h, b.l, b.c, b.v) for b in bars]
    if bar_size == "5min":
        return cs
    groepen: dict[datetime, list[Candle]] = defaultdict(list)
    for c in cs:
        sleutel = c.timestamp.replace(minute=(c.timestamp.minute // 15) * 15, second=0)
        groepen[sleutel].append(c)
    return [Candle(k, g[0].open, max(x.high for x in g), min(x.low for x in g), g[-1].close, sum(x.volume for x in g))
            for k, g in sorted(groepen.items())]


def simuleer_dag(symbol: str, dag: date, candles5: list[Candle], candles_pat: list[Candle],
                 atr: float | None, deadline: dt_time, bar: str) -> QfsTrade | None:
    opening = [c for c in candles5 if dt_time(15, 30) <= c.timestamp.time() < dt_time(15, 45)]
    if len(opening) < 3 or atr is None:
        return None
    box_high = max(c.high for c in opening); box_low = min(c.low for c in opening)
    rng = box_high - box_low
    if rng <= 0 or rng < ATR_FACTOR * atr:
        return None
    o_open, o_close = opening[0].open, opening[-1].close
    if o_close > o_open:
        richting = "SHORT"
    elif o_close < o_open:
        richting = "LONG"
    else:
        return None

    # --- bewaking: exact de lus van reversal_monitor_module ---------------------
    zoek = [c for c in candles_pat if c.timestamp.time() >= dt_time(15, 45)]
    vorige = None; wachtende_hamer = None; signaal = None; signaal_idx = None
    for i, c in enumerate(zoek):
        einde_min = c.timestamp.hour * 60 + c.timestamp.minute + (5 if bar == "5min" else 15)
        if einde_min > deadline.hour * 60 + deadline.minute:
            break
        if vorige is None:
            vorige = c; continue
        if wachtende_hamer is not None:
            s = check_hamer_confirmation(wachtende_hamer, c, richting)
            wachtende_hamer = None
            if s is not None:
                signaal, signaal_idx = s, i; break
        if wachtende_hamer is None:
            s = check_engulfing_signal(vorige, c, box_high=box_high, box_low=box_low, expected_direction=richting)
            if s is not None:
                signaal, signaal_idx = s, i; break
            if check_hamer_setup(vorige, c, box_high=box_high, box_low=box_low, expected_direction=richting):
                wachtende_hamer = c
        vorige = c
    if signaal is None:
        return None

    # --- entry: limiet op trigger, vanaf de candle NA de signaalcandle (5-min pad) --
    trigger, sl = signaal.trigger_price, signaal.stop_loss_price
    sig_t = zoek[signaal_idx].timestamp
    sig_min = sig_t.hour * 60 + sig_t.minute + (5 if bar == "5min" else 15)
    dl_min = deadline.hour * 60 + deadline.minute
    pad = [c for c in candles5 if sig_min <= c.timestamp.hour * 60 + c.timestamp.minute < dl_min]
    entry = None; entry_idx = None
    for j, c in enumerate(pad):
        if richting == "LONG" and c.low <= trigger:
            entry = min(trigger, c.open) if c.open < trigger else trigger; entry_idx = j; break
        if richting == "SHORT" and c.high >= trigger:
            entry = max(trigger, c.open) if c.open > trigger else trigger; entry_idx = j; break
    sl_afstand = abs(entry - sl) if entry is not None else abs(trigger - sl)
    if sl_afstand <= 0:
        return None
    if (richting == "LONG" and entry is not None and sl >= entry) or (richting == "SHORT" and entry is not None and sl <= entry):
        return None
    qty = min(KAPITAAL * RISICO_PCT / sl_afstand, POSITIE_EUR / (entry or trigger))
    tp_overkant = box_low if richting == "SHORT" else box_high

    def loop(tp: float):
        if entry is None:
            return None, "niet gevuld", 0.0, 0.0
        max_mee = max_tegen = 0.0
        for c in pad[entry_idx + 1:]:
            if richting == "LONG":
                mee, tegen = (c.high - entry) / sl_afstand, (entry - c.low) / sl_afstand
                tp_raak, sl_raak = c.high >= tp, c.low <= sl
            else:
                mee, tegen = (entry - c.low) / sl_afstand, (c.high - entry) / sl_afstand
                tp_raak, sl_raak = c.low <= tp, c.high >= sl
            max_mee, max_tegen = max(max_mee, mee), max(max_tegen, tegen)
            if sl_raak:
                return -1.0, "SL", max_mee, max_tegen
            if tp_raak:
                return abs(tp - entry) / sl_afstand, "TP", max_mee, max_tegen
        laatste = pad[-1] if pad else None
        if laatste is None or entry_idx + 1 >= len(pad):
            return 0.0, "deadline direct", max_mee, max_tegen
        r = ((laatste.close - entry) if richting == "LONG" else (entry - laatste.close)) / sl_afstand
        return r, "deadline", max_mee, max_tegen

    if entry is not None and (richting == "LONG" and tp_overkant <= entry or richting == "SHORT" and tp_overkant >= entry):
        r_ov, e_ov, mm, mt = None, "TP overkant al voorbij entry", 0.0, 0.0
    else:
        r_ov, e_ov, mm, mt = loop(tp_overkant)
    tp2 = entry + 2 * sl_afstand * (1 if richting == "LONG" else -1) if entry is not None else 0
    tp3 = entry + 3 * sl_afstand * (1 if richting == "LONG" else -1) if entry is not None else 0
    r2, e2, _, _ = loop(tp2)
    r3, e3, _, _ = loop(tp3)
    return QfsTrade(
        symbol=symbol, datum=dag.isoformat(), bar=bar,
        box_high=round(box_high, 2), box_low=round(box_low, 2), range_atr=round(rng / atr, 2),
        richting=richting, patroon=signaal.pattern_type,
        signaal_tijd=sig_t.strftime("%H:%M"), entry_tijd=pad[entry_idx].timestamp.strftime("%H:%M") if entry is not None else None,
        entry=round(entry, 2) if entry is not None else round(trigger, 2), sl=round(sl, 2), sl_afstand=round(sl_afstand, 3),
        qty=round(qty, 4), risico_eur=round(qty * sl_afstand, 2), tp_overkant=round(tp_overkant, 2),
        r_overkant=r_ov, r_2x=r2, r_3x=r3, exit_overkant=e_ov, exit_2x=e2, exit_3x=e3,
        max_mee_R=round(mm, 2), max_tegen_R=round(mt, 2),
    )


def rapporteer(trades: list[QfsTrade], titel: str, veld: str) -> str:
    gevuld = [t for t in trades if getattr(t, veld) is not None and t.entry_tijd is not None]
    n = len(gevuld)
    if not n:
        return f"\n{titel}: geen gevulde trades"
    rs = [getattr(t, veld) for t in gevuld]
    eur = [getattr(t, veld) * t.risico_eur - FEE_ROUND_TRIP for t in gevuld]
    bruto = sum(getattr(t, veld) * t.risico_eur for t in gevuld)
    wins = sum(1 for r in rs if r > 0)
    return (f"\n{titel}\n  signalen {len(trades)}  gevuld {n} ({pct(n, len(trades))})  winrate {pct(wins, n)}  "
            f"gem {sum(rs) / n:+.2f}R  risico gem €{sum(t.risico_eur for t in gevuld) / n:.2f}  "
            f"bruto €{bruto:+.0f}  fees €{-FEE_ROUND_TRIP * n:.0f}  NETTO €{sum(eur):+.0f}  (€{sum(eur) / n:+.2f} per trade)")


def per_groep(trades: list[QfsTrade], veld: str, sleutel, titel: str) -> str:
    g: dict[str, list[QfsTrade]] = defaultdict(list)
    for t in trades:
        if getattr(t, veld) is not None and t.entry_tijd is not None:
            g[sleutel(t)].append(t)
    regels = [f"\n{titel} (TP-variant: {veld})", f"{'bucket':<28}{'n':>5}  {'winrate':>8}  {'gem R':>7}  {'netto/trade':>12}"]
    for k in sorted(g):
        ts = g[k]; n = len(ts); rs = [getattr(t, veld) for t in ts]
        netto = sum(getattr(t, veld) * t.risico_eur - FEE_ROUND_TRIP for t in ts) / n
        regels.append(f"{k:<28}{n:>5}  {pct(sum(1 for r in rs if r > 0), n):>8}  {sum(rs) / n:>+7.2f}  {netto:>+12.2f}")
    return "\n".join(regels)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dagen", type=int, default=120)
    ap.add_argument("--bar", default="5min", choices=["5min", "15min"])
    ap.add_argument("--deadline", default="18:00")
    ap.add_argument("--symbolen", default=None)
    args = ap.parse_args()
    deadline = dt_time(*map(int, args.deadline.split(":")))
    if args.symbolen:
        symbolen = [s.strip().upper() for s in args.symbolen.split(",")]
    else:
        from news_module import FALLBACK_WATCHLIST
        symbolen = list(FALLBACK_WATCHLIST)

    trades: list[QfsTrade] = []
    for s in symbolen:
        try:
            bars5 = [b for b in haal_bars(s, args.dagen, "5Min") if in_rth(b.t)]
            dagbars = haal_bars(s, args.dagen + 30, "1Day")
            per_dag = defaultdict(list)
            for b in bars5:
                per_dag[b.t.date()].append(b)
            n_voor = len(trades)
            for dag in sorted(per_dag)[-args.dagen:]:
                c5 = naar_candles(per_dag[dag], "5min")
                cpat = c5 if args.bar == "5min" else naar_candles(per_dag[dag], "15min")
                t = simuleer_dag(s, dag, c5, cpat, atr14(dagbars, dag), deadline, args.bar)
                if t:
                    trades.append(t)
            print(f"{s}: {len(trades) - n_voor} signalen")
        except Exception as e:
            print(f"{s}: FOUT {e}")

    os.makedirs(OUT_DIR, exist_ok=True)
    stempel = f"{date.today()}_{args.bar}_{args.deadline.replace(':', '')}"
    if trades:
        with open(os.path.join(OUT_DIR, f"qfs_trades_{stempel}.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(asdict(trades[0]).keys())); w.writeheader()
            for t in trades:
                w.writerow(asdict(t))

    kop = f"QFS-backtest  {stempel}  |  {len(symbolen)} symbolen, {args.dagen} dagen, patronen op {args.bar}, deadline {args.deadline}"
    kop += f"\nSignalen: {len(trades)}  gevuld: {sum(1 for t in trades if t.entry_tijd)}  (bot-regels: 1 trade/symbool/dag, 1% risico, cap €1000, fee €{FEE_ROUND_TRIP})"
    delen = [kop,
             rapporteer(trades, "TP = overkant van de box (zoals de bot nu)", "r_overkant"),
             rapporteer(trades, "TP = 2× stop", "r_2x"),
             rapporteer(trades, "TP = 3× stop", "r_3x"),
             per_groep(trades, "r_2x", lambda t: t.patroon, "Per patroon"),
             per_groep(trades, "r_2x", lambda t: t.richting, "Per richting"),
             per_groep(trades, "r_2x", lambda t: "range <0.5 ATR" if t.range_atr < 0.5 else "range >=0.5 ATR", "Per boxgrootte"),
             per_groep(trades, "r_2x", lambda t: t.signaal_tijd[:2] + ":xx", "Per signaal-uur"),
             per_groep(trades, "r_2x", lambda t: t.symbol, "Per symbool")]
    tekst = "\n".join(delen)
    print("\n" + tekst)
    open(os.path.join(OUT_DIR, f"qfs_rapport_{stempel}.txt"), "w").write(tekst)


if __name__ == "__main__":
    main()
