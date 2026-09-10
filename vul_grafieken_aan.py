"""
vul_grafieken_aan.py -- eenmalig/herhaalbaar: geeft bestaande journal-
rijen ZONDER grafiek alsnog een grafiek en zet de bestandsnaam in de
kolom `chart`, zodat het dashboard hem toont.

Per rij zonder chart:
  1. bestaat er al een PNG in logs/charts/ van dit symbool op deze dag
     (bv. gemaakt door genereer_alle_historische_grafieken.py)? -> koppel die.
  2. anders: 5-min-candles ophalen (vereist een werkende IBKR-sessie; de
     Gateway geeft hoogstens enkele dagen terug) en de grafiek maken.
     Box: uit reversal_trade_*.log (QFS) of gereconstrueerd uit de
     Fibonacci-38,2%-formule (TTS). Lukt dat niet -> rij overslaan.

In- en uitstapmoment (9 sep 2026): worden uit logs/events.jsonl gehaald
("entry gevuld @" = instap, de resultaatmelding = uitstap) en, als de
journal-rij ze nog mist, ook in entry_time/exit_time weggeschreven.

Gebruik:
    cd /opt/strategy && python3 vul_grafieken_aan.py            # alleen vandaag, alleen rijen zonder grafiek
    cd /opt/strategy && python3 vul_grafieken_aan.py --alles    # hele journal
    cd /opt/strategy && python3 vul_grafieken_aan.py --opnieuw  # bestaande grafieken van vandaag opnieuw maken
    cd /opt/strategy && python3 vul_grafieken_aan.py --einde-dag # (cron, na beurssluiting) alle trades van
                                                                 # vandaag opnieuw met het HELE dagverloop +
                                                                 # nabeloop-tekst (TP/SL alsnog geraakt?)

OCA-groep/strategie (9 sep 2026): worden voor oude rijen teruggehaald uit
de proceslogs ("Positie verwijderd: SYM (OCA)"); anders gereconstrueerd
uit het naampatroon {PREFIX}_{SYM}_{RICHTING}_{entry*100}.
"""
import json
import csv
import glob
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

JOURNAL = "/opt/strategy/logs/trade_journal.csv"
EVENTS = "/opt/strategy/logs/events.jsonl"
CHARTS = "/opt/strategy/logs/charts"


def bestaande_png(symbool, datum):
    ymd = datum.replace("-", "")
    kandidaten = sorted(glob.glob(os.path.join(CHARTS, f"{symbool}_{ymd}_*.png")))
    return os.path.basename(kandidaten[-1]) if kandidaten else None


def box_uit_logs(symbool, datum):
    patroon = re.compile(re.escape(datum) + r".*?\|I\| " + re.escape(symbool)
                         + r": bewaking gestart.*?box=\[([\d.]+), ([\d.]+)\]")
    for pad in glob.glob("/opt/strategy/logs/reversal_trade_*.log"):
        try:
            m = patroon.search(open(pad, encoding="utf-8", errors="ignore").read())
        except Exception:
            continue
        if m:
            return float(m.group(2)), float(m.group(1))  # high, low
    return None


def tijden_uit_events(symbool, datum, volgnummer=0):
    """
    (instap, uitstap) als datetime uit events.jsonl. `volgnummer` kiest
    de n-de trade van dit symbool op deze dag (TTS en QFS kunnen
    hetzelfde aandeel op één dag handelen).
    """
    if not os.path.exists(EVENTS):
        return None, None
    fills, exits = [], []
    with open(EVENTS) as f:
        for regel in f:
            try:
                ev = json.loads(regel)
            except json.JSONDecodeError:
                continue
            t, tekst = ev.get("time", ""), ev.get("text", "")
            if not t.startswith(datum) or not tekst.lstrip("✅🛑⏰⚠️ ").startswith(symbool + " "):
                continue
            if "entry gevuld @" in tekst:
                fills.append(datetime.fromisoformat(t))
            elif any(w in tekst for w in ("take profit hit", "stop loss hit", "forced close", "timeout")):
                exits.append(datetime.fromisoformat(t))
    instap = fills[volgnummer] if len(fills) > volgnummer else None
    uitstap = exits[volgnummer] if len(exits) > volgnummer else None
    return instap, uitstap


def oca_uit_logs(symbool, datum):
    patroon = re.compile(re.escape(datum) + r"[^\n]*Positie verwijderd: " + re.escape(symbool) + r" \(([A-Z]+_[^)]+)\)")
    for pad in glob.glob("/opt/strategy/logs/*.log"):
        try:
            m = patroon.search(open(pad, encoding="utf-8", errors="ignore").read())
        except Exception:
            continue
        if m:
            return m.group(1)
    return None


def vul_oca_en_strategie(rij):
    """Vult oca_group en strategy als ze leeg zijn. Retourneert True bij wijziging."""
    if rij.get("oca_group") and rij.get("strategy"):
        return False
    symbool, datum = rij["symbol"], rij["date"]
    oca = rij.get("oca_group") or oca_uit_logs(symbool, datum)
    if not oca:
        direction = rij.get("direction") or ("SHORT" if float(rij["stop_loss"]) > float(rij["entry_price"]) else "LONG")
        prefix = "QFS" if box_uit_logs(symbool, datum) else "TTS"
        oca = f"{prefix}_{symbool}_{direction}_{int(float(rij['entry_price']) * 100)}"
    gewijzigd = False
    if not rij.get("oca_group"):
        rij["oca_group"] = oca
        gewijzigd = True
    if not rij.get("strategy"):
        rij["strategy"] = oca.split("_", 1)[0]
        gewijzigd = True
    return gewijzigd


def fees_uit_events(symbool, datum, volgnummer=0):
    """(pnl_net, fees, exit_price) uit de resultaatmelding in events.jsonl, of (None, None, None)."""
    if not os.path.exists(EVENTS):
        return None, None, None
    pat = re.compile(r"Exit ([\d.]+).*?€([+-]?[\d.,]+) netto \(na €([\d.,]+) fees\)", re.DOTALL)
    hits = []
    with open(EVENTS) as f:
        for regel in f:
            try:
                ev = json.loads(regel)
            except json.JSONDecodeError:
                continue
            tekst = ev.get("text", "")
            if ev.get("time", "").startswith(datum) and tekst.lstrip("✅🛑⏰⚠️ ").startswith(symbool + " "):
                m = pat.search(tekst)
                if m:
                    hits.append((float(m.group(2).replace(",", "")), float(m.group(3).replace(",", "")), float(m.group(1))))
    return hits[volgnummer] if len(hits) > volgnummer else (None, None, None)


def vul_fees(rij, volgnummer=0):
    if rij.get("fees") and rij.get("pnl_net"):
        return False
    netto, fees, exit_price = fees_uit_events(rij["symbol"], rij["date"], volgnummer)
    if fees is None:
        return False
    rij["fees"] = rij.get("fees") or f"{fees}"
    rij["pnl_net"] = rij.get("pnl_net") or f"{netto}"
    rij["exit_price"] = rij.get("exit_price") or f"{exit_price}"
    return True


def maak_grafiek(rij, volgnummer=0):
    from data_module import get_historical_candles
    from chart_module import genereer_trade_grafiek

    symbool, datum = rij["symbol"], rij["date"]
    direction = rij.get("direction") or ("SHORT" if float(rij["stop_loss"]) > float(rij["entry_price"]) else "LONG")
    entry, tp, sl = float(rij["entry_price"]), float(rij["take_profit"]), float(rij["stop_loss"])
    box = box_uit_logs(symbool, datum)
    dag = datetime.fromisoformat(datum).date()
    dagen_terug = max((datetime.now().date() - dag).days + 1, 1)
    if box is None:
        # TOEGEVOEGD (10 sep 2026): de ECHTE 15-min openingscandle als box,
        # niet de teruggerekende (mogelijk halve) box waar de bot mee rekende.
        try:
            kw = [c for c in get_historical_candles(symbool, duration=f"{dagen_terug}d", bar_size="15min")
                  if c.timestamp.date() == dag and c.timestamp.hour == 15 and c.timestamp.minute == 30]
            if kw:
                box = (kw[0].high, kw[0].low)
        except Exception as e:
            print(f"  {symbool} {datum}: openingscandle niet opgehaald ({e}), val terug op formule.")
    if box is None:
        bereik = abs(entry - tp) / 0.382
        box = (entry, entry - bereik) if direction == "SHORT" else (entry + bereik, entry)
    candles = [c for c in get_historical_candles(symbool, duration=f"{dagen_terug}d", bar_size="5min")
               if c.timestamp.date() == dag]
    if not candles:
        print(f"  {symbool} {datum}: geen candles beschikbaar -- overgeslagen.")
        return None
    exit_price = float(rij["exit_price"]) if rij.get("exit_price") else None
    if exit_price is None:
        # oude rijen missen exit_price: beoogde prijs als benadering
        exit_price = {"take_profit_hit": tp, "stop_loss_hit": sl}.get(rij.get("result"))

    def _tijd(veld):
        if not rij.get(veld):
            return None
        try:
            return datetime.combine(dag, datetime.strptime(rij[veld][:8], "%H:%M:%S").time())
        except ValueError:
            return None

    entry_time, exit_time = _tijd("entry_time"), _tijd("exit_time")
    ev_in, ev_uit = tijden_uit_events(symbool, datum, volgnummer)
    entry_time = entry_time or ev_in
    exit_time = exit_time or ev_uit
    if entry_time and not rij.get("entry_time"):
        rij["entry_time"] = entry_time.strftime("%H:%M:%S")
    if exit_time and not rij.get("exit_time"):
        rij["exit_time"] = exit_time.strftime("%H:%M:%S")
    if exit_price is not None and not rij.get("exit_price"):
        rij["exit_price"] = f"{exit_price}"

    pad = genereer_trade_grafiek(symbol=symbool, candles=candles, box_high=box[0], box_low=box[1],
                                 entry_price=entry, take_profit=tp, stop_loss=sl, exit_price=exit_price,
                                 direction=direction, result=rij.get("result", "unknown"),
                                 entry_time=entry_time, exit_time=exit_time)
    return os.path.basename(pad) if pad else None


def main():
    alles = "--alles" in sys.argv
    einde_dag = "--einde-dag" in sys.argv
    opnieuw = "--opnieuw" in sys.argv or einde_dag
    vandaag = datetime.now().date().isoformat()
    teller = {}  # (symbool, datum) -> hoeveelste trade op die dag
    with open(JOURNAL, newline="") as f:
        reader = csv.DictReader(f)
        velden, rijen = reader.fieldnames, list(reader)
    if "chart" not in velden:
        print("Journal heeft nog geen chart-kolom -- draai eerst één trade met de nieuwe code, of voeg de kolom toe.")
        return
    gewijzigd = 0
    for rij in rijen:
        sleutel = (rij["symbol"], rij["date"])
        volgnummer = teller.get(sleutel, 0)
        teller[sleutel] = volgnummer + 1
        if not alles and rij["date"] != vandaag:
            continue
        if "fees" in velden and vul_fees(rij, volgnummer):
            print(f"  {rij['symbol']} {rij['date']}: fees/netto aangevuld (€{rij['fees']} / €{rij['pnl_net']})")
            gewijzigd += 1
        if "oca_group" in velden and vul_oca_en_strategie(rij):
            print(f"  {rij['symbol']} {rij['date']}: OCA/strategie aangevuld ({rij['oca_group']})")
            gewijzigd += 1
        if rij.get("chart") and not opnieuw:
            continue
        naam = None if opnieuw else bestaande_png(rij["symbol"], rij["date"])
        if naam:
            print(f"  {rij['symbol']} {rij['date']}: bestaande grafiek gekoppeld ({naam})")
        else:
            try:
                naam = maak_grafiek(rij, volgnummer)
            except Exception as e:
                print(f"  {rij['symbol']} {rij['date']}: grafiek mislukt -- {e}")
                naam = None
            if naam:
                print(f"  {rij['symbol']} {rij['date']}: grafiek gemaakt ({naam})")
        if naam:
            rij["chart"] = naam
            gewijzigd += 1
    if gewijzigd:
        tmp = JOURNAL + ".tmp"
        with open(tmp, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=velden)
            w.writeheader()
            w.writerows(rijen)
        os.replace(tmp, JOURNAL)
    print(f"Klaar: {gewijzigd} rij(en) van een grafiek voorzien.")


if __name__ == "__main__":
    main()
