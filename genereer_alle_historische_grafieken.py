"""
genereer_alle_historische_grafieken.py -- eenmalig script: doorloopt
trade_journal.csv, zoekt per trade de box-niveaus op uit de
reversal_trade_*.log-bestanden, haalt historische candle-data op, en
genereert + verstuurt een grafiek per trade naar Telegram.
"""
import csv
import re
import glob
import time
from datetime import datetime

from data_module import get_historical_candles
from chart_module import genereer_trade_grafiek
from telegram_notify import send_telegram_photo

JOURNAL_PAD = "/opt/strategy/logs/trade_journal.csv"


def vind_box_niveaus(symbool, datum_str):
    """Doorzoekt alle reversal_trade_*.log-bestanden naar de
    box-declaratie voor dit symbool op deze datum."""
    patroon = re.compile(
        re.escape(datum_str) + r".*?\|I\| " + re.escape(symbool) +
        r": bewaking gestart.*?box=\[([\d.]+), ([\d.]+)\]"
    )
    for logpad in glob.glob("/opt/strategy/logs/reversal_trade_*.log"):
        try:
            with open(logpad, encoding="utf-8", errors="ignore") as f:
                inhoud = f.read()
        except Exception:
            continue
        match = patroon.search(inhoud)
        if match:
            # LET OP: de logregel heeft het formaat box=[box_low, box_high]
            # (lage waarde eerst) -- group(1)=box_low, group(2)=box_high.
            return float(match.group(2)), float(match.group(1))
    return None, None


def verwerk_trade(trade):
    symbool = trade["symbol"]
    datum_str = trade["date"]
    richting = trade["direction"]
    entry_price = float(trade["entry_price"])
    take_profit = float(trade["take_profit"])
    stop_loss = float(trade["stop_loss"])
    result = trade["result"]

    print(f"{datum_str} {symbool} {richting} ({result})...")

    box_high, box_low = vind_box_niveaus(symbool, datum_str)
    if box_high is None:
        waarden = [entry_price, take_profit, stop_loss]
        box_high = max(waarden)
        box_low = min(waarden)
        melding = "    geen box gevonden, benadering: "
        melding += str(round(box_low, 2)) + " tot " + str(round(box_high, 2))
        print(melding)

    try:
        candles = get_historical_candles(symbool, duration="30d", bar_size="5min")
        doel_datum = datetime.strptime(datum_str, "%Y-%m-%d").date()
        dag_candles = []
        for c in candles:
            if c.timestamp.date() == doel_datum:
                dag_candles.append(c)

        if not dag_candles:
            print("    geen candle-data beschikbaar, overgeslagen")
            return

        exit_prijs = None
        if result == "take_profit_hit":
            exit_prijs = take_profit
        elif result == "stop_loss_hit":
            exit_prijs = stop_loss

        grafiek_pad = genereer_trade_grafiek(
            symbol=symbool,
            candles=dag_candles,
            box_high=box_high,
            box_low=box_low,
            entry_price=entry_price,
            take_profit=take_profit,
            stop_loss=stop_loss,
            exit_price=exit_prijs,
            direction=richting,
            result=result,
        )

        if grafiek_pad:
            bijschrift = "[Historisch] " + datum_str + " " + symbool + " " + richting + " -- " + result
            verstuurd = send_telegram_photo(grafiek_pad, caption=bijschrift)
            print("    grafiek verstuurd: " + str(verstuurd))
        else:
            print("    grafiek genereren mislukt")

    except Exception as e:
        print("    FOUT: " + str(e))


def main():
    with open(JOURNAL_PAD) as f:
        reader = csv.DictReader(f)
        trades = list(reader)

    print(str(len(trades)) + " trades gevonden in de journal.\n")

    for trade in trades:
        verwerk_trade(trade)
        time.sleep(2)

    print("\nKlaar.")


if __name__ == "__main__":
    main()
