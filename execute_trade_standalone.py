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

HERSTELD (9 sep 2026, TWEEDE keer): dit bestand was OPNIEUW overschreven
met de inhoud van execute_reversal_trade_standalone.py (commit d5797a8,
"Add files via upload"), waardoor main.py's dispatch (--action/--quantity/
--entry-price ...) direct op een argparse-fout strandde en TTS-trades
stil wegvielen. Teruggezet vanuit commit 9062a8b. De grafiek wordt nu
CENTRAAL gemaakt in order_module.report_trade_outcome() (voor alle
strategieën), dus het losse grafiekblok van hieronder is verhuisd; de
box-reconstructie wordt via BracketOrderSpec.box_high/box_low meegegeven.
Oorspronkelijke toelichting over de grafiek, analoog aan hoe de
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

    # Box reconstrueren uit de Fibonacci-38,2%-formule (exit_module.py),
    # zodat de centrale grafiek de openingsrange kan tekenen:
    #   SHORT: entry=box_high, TP = box_high - 0,382*Range
    #   LONG:  entry=box_low,  TP = box_low  + 0,382*Range
    bereik = abs(args.entry_price - args.take_profit) / 0.382
    if args.action == "SELL":
        box_high, box_low = args.entry_price, args.entry_price - bereik
    else:
        box_low, box_high = args.entry_price, args.entry_price + bereik

    spec = BracketOrderSpec(
        action=args.action,
        quantity=args.quantity,
        entry_price=args.entry_price,
        take_profit=args.take_profit,
        stop_loss=args.stop_loss,
        oca_group=args.oca_group,
        reason=f"{args.action} {args.quantity}x {args.symbol} @ {args.entry_price:.4f} (losgekoppeld proces)",
        strategy="TTS",
        box_high=box_high,
        box_low=box_low,
    )

    logger.info(f"Losgekoppeld trade-proces gestart voor {args.symbol}, PID {os.getpid()}")

    try:
        result = execute_managed_trade(spec, args.symbol)
        logger.info(f"Trade afgerond: {result}")
        # Grafiek + volledige journal-rij: zie order_module.report_trade_outcome().

    except Exception as e:
        logger.error(f"Onverwachte fout in losgekoppeld trade-proces voor {args.symbol}: {e}")
        try:
            from telegram_notify import send_telegram_message
            send_telegram_message(f"⚠️ Onverwachte fout in trade-proces voor {args.symbol}: {e}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
