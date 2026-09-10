"""
papier_uitkomsten.py -- "papieren" resultaat van dry-run-signalen (RVB/VDB).

NIEUW (9 sep 2026, op verzoek): zolang RVB en VDB in dry-run draaien,
staat er niets in de journal en blijven hun dashboard-kaarten leeg.
Dit script neemt elk dry-run-signaal uit logs/rvb_signals.jsonl en
logs/vdb_signals.jsonl, speelt het na op de 5-min-candles van die dag
(TP of SL het eerst geraakt? anders gesloten op de geforceerde
sluitingstijd van de strategie) en schrijft de uitkomst terug in de
signaalregel: paper_result, paper_exit, paper_exit_time, paper_pnl,
paper_r. Het dashboard toont daaruit per strategie de papieren
metrics en per signaal de uitkomst.

Aannames (bewust conservatief):
  - fill op de signaalprijs (entry), geen slippage;
  - raken TP en SL in dezelfde candle, dan telt de SL;
  - vaste fees FEES_PER_TRADE (IBKR: ~€2,50 per rondje, zoals bij de
    echte trades van 9 sep).

Gebruik (cron 22:10, na de einde-dag-grafieken):
    python3 papier_uitkomsten.py            # vandaag
    python3 papier_uitkomsten.py --alles    # alle nog niet nagespeelde signalen
"""
import json
import os
import sys
from datetime import datetime, time as dt_time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FEES_PER_TRADE = 2.50
BESTANDEN = {
    "RVB": os.environ.get("RVB_SIGNAL_LOG", "/opt/strategy/logs/rvb_signals.jsonl"),
    "VDB": os.environ.get("VDB_SIGNAL_LOG", "/opt/strategy/logs/vdb_signals.jsonl"),
}


def _sluitingstijd(strategie: str) -> dt_time:
    try:
        if strategie == "RVB":
            from rvb_strategy_module import RVB_FORCED_CLOSE_TIME
            return RVB_FORCED_CLOSE_TIME
        from vwap_bounce_module import VDB_FORCED_CLOSE_TIME
        return VDB_FORCED_CLOSE_TIME
    except Exception:
        return dt_time(21, 55)


def vul_tp_sl_uit_events(sig: dict, strategie: str) -> bool:
    """
    Oudere signaalregels (vóór 9 sep 2026) missen TP/SL/aantal; die staan
    wel in de dry-run-melding in events.jsonl ("Entry x | TP y | SL z |
    n stuks"). Zoek de melding van dezelfde strategie+symbool binnen
    2 minuten van het signaalmoment.
    """
    import re
    pad = "/opt/strategy/logs/events.jsonl"
    if not os.path.exists(pad):
        return False
    t0 = datetime.fromisoformat(sig["time"])
    pat = re.compile(r"Entry ([\d.]+) \| TP ([\d.]+) \| SL ([\d.]+) \| ([\d.]+) stuks")
    with open(pad) as f:
        for regel in f:
            try:
                ev = json.loads(regel)
            except json.JSONDecodeError:
                continue
            tekst = ev.get("text", "")
            if f"{strategie} {sig['direction']} {sig['symbol']}:" not in tekst:
                continue
            try:
                dt = abs((datetime.fromisoformat(ev["time"]) - t0).total_seconds())
            except Exception:
                continue
            m = pat.search(tekst)
            if m and dt <= 120:
                sig["take_profit"], sig["stop_loss"], sig["quantity"] = float(m.group(2)), float(m.group(3)), float(m.group(4))
                return True
    return False


def speel_na(sig: dict, candles: list, sluit: dt_time) -> dict | None:
    """Simuleert één signaal op de candles NA het signaalmoment."""
    t0 = datetime.fromisoformat(sig["time"])
    entry, tp, sl = float(sig["entry"]), float(sig["take_profit"]), float(sig["stop_loss"])
    qty = float(sig.get("quantity") or 0)
    lang = sig["direction"] == "LONG"
    deadline = t0.replace(hour=sluit.hour, minute=sluit.minute, second=0, microsecond=0)
    later = [c for c in candles if c.timestamp > t0]
    if not later:
        return None
    result = exit_price = exit_time = None
    for c in later:
        if c.timestamp >= deadline:
            result, exit_price, exit_time = "forced_close", c.close, c.timestamp
            break
        raakt_sl = c.low <= sl if lang else c.high >= sl
        raakt_tp = c.high >= tp if lang else c.low <= tp
        if raakt_sl:
            result, exit_price, exit_time = "stop_loss_hit", sl, c.timestamp
            break
        if raakt_tp:
            result, exit_price, exit_time = "take_profit_hit", tp, c.timestamp
            break
    if result is None:  # dag nog niet voorbij (of geen candle op de deadline)
        if datetime.now() < deadline:
            return None
        result, exit_price, exit_time = "forced_close", later[-1].close, later[-1].timestamp
    bruto = (exit_price - entry) * qty * (1 if lang else -1)
    risico = abs(entry - sl) * qty
    return {
        "paper_result": result, "paper_exit": round(exit_price, 4),
        "paper_exit_time": exit_time.isoformat(timespec="seconds"),
        "paper_pnl": round(bruto - FEES_PER_TRADE, 2), "paper_fees": FEES_PER_TRADE,
        "paper_r": round((bruto / risico), 2) if risico else None,
    }


def main():
    alles = "--alles" in sys.argv
    vandaag = datetime.now().date().isoformat()
    from data_module import get_historical_candles
    candle_cache = {}
    totaal = 0
    for strategie, pad in BESTANDEN.items():
        if not os.path.exists(pad):
            continue
        regels, gewijzigd = [], 0
        with open(pad) as f:
            for regel in f:
                try:
                    sig = json.loads(regel)
                except json.JSONDecodeError:
                    continue
                regels.append(sig)
        sluit = _sluitingstijd(strategie)
        for sig in regels:
            datum = sig.get("time", "")[:10]
            if sig.get("status") != "dry-run" or "paper_result" in sig:
                continue
            if not alles and datum != vandaag:
                continue
            if not sig.get("take_profit") and not vul_tp_sl_uit_events(sig, strategie):
                print(f"  {strategie} {sig['symbol']} {datum}: geen TP/SL bekend -- overgeslagen")
                continue
            dag = datetime.fromisoformat(datum).date()
            sleutel = (sig["symbol"], datum)
            if sleutel not in candle_cache:
                terug = max((datetime.now().date() - dag).days + 1, 1)
                try:
                    candle_cache[sleutel] = [c for c in get_historical_candles(sig["symbol"], duration=f"{terug}d", bar_size="5min")
                                             if c.timestamp.date() == dag]
                except Exception as e:
                    print(f"  {strategie} {sig['symbol']} {datum}: candles mislukt -- {e}")
                    candle_cache[sleutel] = []
            uitkomst = speel_na(sig, candle_cache[sleutel], sluit)
            if uitkomst:
                sig.update(uitkomst)
                gewijzigd += 1
                print(f"  {strategie} {sig['symbol']} {sig['direction']} {sig['time'][11:16]}: {uitkomst['paper_result']} "
                      f"@ {uitkomst['paper_exit']} ({uitkomst['paper_exit_time'][11:16]}) -> €{uitkomst['paper_pnl']:+.2f}")
        if gewijzigd:
            tmp = pad + ".tmp"
            with open(tmp, "w") as f:
                for sig in regels:
                    f.write(json.dumps(sig) + "\n")
            os.replace(tmp, pad)
        totaal += gewijzigd
    print(f"Klaar: {totaal} signaal/signalen nagespeeld.")


if __name__ == "__main__":
    main()
