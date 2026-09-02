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
    except Exception as e:
        logger.error(f"Onverwachte fout in losgekoppeld trade-proces voor {args.symbol}: {e}")
        try:
            from telegram_notify import send_telegram_message
            send_telegram_message(f"⚠️ Onverwachte fout in trade-proces voor {args.symbol}: {e}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
