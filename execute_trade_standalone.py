"""
execute_trade_standalone.py — Touch & Turn Scalper, Losgekoppelde Trade-uitvoering

Voert execute_managed_trade() uit als LOSGEKOPPELD achtergrondproces --
opgelost probleem: execute_managed_trade() is blokkerend (kan uren
duren), waardoor main.py (aangeroepen door cron) anders zelf uren zou
blijven hangen. Dat zou de volgende cron-slot laten overslaan (de
flock-vergrendeling in run_cycle.sh voorkomt overlap), waardoor er
effectief maar één cyclus per dag zou draaien.

Met dit script: main.py plaatst de entry-order, start dit script als
losgekoppeld proces (start_new_session=True, overleeft het einde van
de cron-job), en keert direct terug. Dit script zelf doet de rest:
wachten op fill, TP/SL plaatsen, bewaken, rapporteren -- volledig
onafhankelijk van de cron-job die het startte.

HERSTELD (9 sep 2026): dit bestand was per ongeluk overschreven met de
inhoud van execute_reversal_trade_standalone.py -- gereconstrueerd op
basis van de oorspronkelijke versie, en meteen uitgebreid met
grafiekgeneratie (zie chart_module.py), analoog aan hoe de
reversal-flow dat eerder kreeg.

Gebruik (aangeroepen door main.py, niet handmatig):
    python3 execute_trade_standalone.py --symbol AAPL --action SELL \
        --quantity 9 --entry-price 316.50 --take-profit 310.00 \
        --stop-loss 320.00 --oca-group TTS_AAPL_SHORT_31650
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s|%(levelname)-.1s| %(message)s",
    filename=f"/opt/strategy/logs/trade_{os.getpid()}.log",
)
logger = logging.getLogger("execute_trade_standalone")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--action", required=True, choices=["BUY", "SELL"])
    parser.add_argument("--quantity", required=True, type=float)
    parser.add_argument("--entry-price", required=True, type=float)
    parser.add_argument("--take-profit", required=True, type=float)
    parser.add_argument("--stop-loss", required=True, type=float)
    parser.add_argument("--oca-group", required=True)
    args = parser.parse_args()

    from order_module import BracketOrderSpec, execute_managed_trade

    spec = BracketOrderSpec(
        action=args.action,
        quantity=args.quantity,
        entry_price=args.entry_price,
        take_profit=args.take_profit,
        stop_loss=args.stop_loss,
        oca_group=args.oca_group,
        reason=f"{args.action} {args.quantity}x {args.symbol} @ {args.entry_price:.4f} (losgekoppeld proces)",
    )

    logger.info(f"Losgekoppeld trade-proces gestart voor {args.symbol}, PID {os.getpid()}")

    try:
        result = execute_managed_trade(spec, args.symbol)
        logger.info(f"Trade afgerond: {result}")

        # NIEUW (9 sep 2026, op verzoek): stuur na afloop van een
        # AFGERONDE trade een grafiek naar Telegram, net als de
        # reversal-flow al kreeg. BELANGRIJK VERSCHIL: dit script krijgt
        # GEEN box-grenzen mee als los argument (alleen de al-berekende
        # entry/TP/SL) -- de box wordt daarom WISKUNDIG GERECONSTRUEERD
        # uit de bekende Fibonacci-38,2%-formule (exit_module.py):
        #   SHORT: entry=box_high, TP = box_high - 0,382*Range
        #          -> Range = (entry-TP)/0,382, box_low = entry-Range
        #   LONG:  entry=box_low,  TP = box_low + 0,382*Range
        #          -> Range = (TP-entry)/0,382, box_high = entry+Range
        # Een mislukte grafiek mag de rest van de afhandeling nooit
        # blokkeren (vandaar de brede try/except eromheen).
        if result.get("status") == "trade_complete":
            try:
                from chart_module import genereer_trade_grafiek
                from telegram_notify import send_telegram_photo
                from data_module import get_historical_candles

                bereik = abs(args.entry_price - args.take_profit) / 0.382
                if args.action == "SELL":
                    box_high = args.entry_price
                    box_low = args.entry_price - bereik
                else:
                    box_low = args.entry_price
                    box_high = args.entry_price + bereik

                grafiek_candles = get_historical_candles(args.symbol, duration="1d", bar_size="5min")
                vandaag_grafiek_candles = [
                    c for c in grafiek_candles if c.timestamp.date() == datetime.now().date()
                ]

                trade_resultaat = result.get("result", "onbekend")
                benaderde_exit_prijs = None
                if trade_resultaat == "take_profit_hit":
                    benaderde_exit_prijs = args.take_profit
                elif trade_resultaat == "stop_loss_hit":
                    benaderde_exit_prijs = args.stop_loss

                richting = "LONG" if args.action == "BUY" else "SHORT"

                grafiek_pad = genereer_trade_grafiek(
                    symbol=args.symbol, candles=vandaag_grafiek_candles,
                    box_high=box_high, box_low=box_low,
                    entry_price=args.entry_price, take_profit=args.take_profit,
                    stop_loss=args.stop_loss, exit_price=benaderde_exit_prijs,
                    direction=richting, result=trade_resultaat,
                )
                if grafiek_pad:
                    bijschrift = f"{args.symbol} {richting} -- {trade_resultaat}"
                    send_telegram_photo(grafiek_pad, caption=bijschrift)
            except Exception as e:
                logger.error(f"{args.symbol}: kon trade-grafiek niet genereren/versturen (niet kritiek): {e}")

    except Exception as e:
        logger.error(f"Onverwachte fout in losgekoppeld trade-proces voor {args.symbol}: {e}")
        try:
            from telegram_notify import send_telegram_message
            send_telegram_message(f"⚠️ Onverwachte fout in trade-proces voor {args.symbol}: {e}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
