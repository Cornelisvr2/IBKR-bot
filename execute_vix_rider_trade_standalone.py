"""
execute_vix_rider_trade_standalone.py — VIX Rider, Losgekoppelde Trade-uitvoering

Voert execute_vix_rider_trade() uit als losgekoppeld achtergrondproces
-- zelfde reden als bij de scalper: de trade-uitvoering (wachten op
fill, dan op de TRAIL-order) kan lang duren, en mag de aanroepende
monitoringlus (vix_rider_main.py) niet blokkeren.

Gebruik (aangeroepen door vix_rider_main.py, niet handmatig):
    python3 execute_vix_rider_trade_standalone.py --symbol AAPL \
        --direction LONG --entry-price 462.0 --initial-stop-loss 454.5 \
        --quantity 1
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
    filename=f"/opt/strategy/logs/vix_rider_trade_{os.getpid()}.log",
)
logger = logging.getLogger("execute_vix_rider_trade_standalone")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--direction", required=True, choices=["LONG", "SHORT"])
    parser.add_argument("--entry-price", required=True, type=float)
    parser.add_argument("--initial-stop-loss", required=True, type=float)
    parser.add_argument("--quantity", required=True, type=float)
    args = parser.parse_args()

    from vix_rider_exit_module import VixRiderPositionPlan
    from vix_rider_order_module import execute_vix_rider_trade

    plan = VixRiderPositionPlan(
        direction=args.direction,
        entry_price=args.entry_price,
        initial_stop_loss=args.initial_stop_loss,
        quantity=args.quantity,
        risk_amount=abs(args.entry_price - args.initial_stop_loss) * args.quantity,
        position_value=args.quantity * args.entry_price,
        capped_by_max_value=False,  # niet relevant meer op dit punt, positie staat al vast
        trailing_distance_pct=0.0,   # niet gebruikt -- trailing_amt wordt herberekend uit entry/SL
        reason=f"Losgekoppeld proces voor {args.symbol}",
    )

    logger.info(f"Losgekoppeld VIX Rider trade-proces gestart voor {args.symbol}, PID {os.getpid()}")

    try:
        result = execute_vix_rider_trade(plan, args.symbol)
        logger.info(f"Trade afgerond: {result}")
    except Exception as e:
        logger.error(f"Onverwachte fout in losgekoppeld VIX Rider trade-proces voor {args.symbol}: {e}")
        try:
            from telegram_notify import send_telegram_message
            send_telegram_message(f"[VIX Rider] ⚠️ Onverwachte fout in trade-proces voor {args.symbol}: {e}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
