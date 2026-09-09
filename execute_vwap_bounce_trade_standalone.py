"""
execute_vwap_bounce_trade_standalone.py — VDB, Losgekoppelde Trade-uitvoering

Analoog aan execute_rvb_trade_standalone.py: krijgt een AL-COMPLETE
order-spec mee (entry/TP/SL/aantal al berekend in vwap_bounce_scan.py)
en voert de zelf-beheerde OCO-flow uit via execute_managed_trade().
Draait als losgekoppeld achtergrondproces zodat de 5-minuten-cron niet
geblokkeerd wordt.

Zelfde geforceerde-sluitingstijd als RVB (21:55 CEST) -- VDB is net als
RVB een dagstrategie, geen 90-minuten-venster zoals TTS/QFS.

Gebruik (aangeroepen door vwap_bounce_scan.py, niet handmatig):
    python3 execute_vwap_bounce_trade_standalone.py --symbol AAPL --direction LONG \\
        --entry 230.10 --take-profit 232.32 --stop-loss 229.10 --quantity 4.3 \\
        --vwap 229.95 --touch-low 229.10 --touch-high 230.40
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
    filename=f"/opt/strategy/logs/vdb_trade_{os.getpid()}.log",
)
logger = logging.getLogger("execute_vwap_bounce_trade_standalone")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--direction", required=True, choices=["LONG", "SHORT"])
    parser.add_argument("--entry", required=True, type=float)
    parser.add_argument("--take-profit", required=True, type=float)
    parser.add_argument("--stop-loss", required=True, type=float)
    parser.add_argument("--quantity", required=True, type=float)
    parser.add_argument("--vwap", required=True, type=float)
    parser.add_argument("--touch-low", required=True, type=float)
    parser.add_argument("--touch-high", required=True, type=float)
    args = parser.parse_args()

    from order_module import BracketOrderSpec, execute_managed_trade
    from vwap_bounce_module import VDB_FORCED_CLOSE_TIME, OCA_PREFIX

    spec = BracketOrderSpec(
        action="BUY" if args.direction == "LONG" else "SELL",
        quantity=args.quantity,
        entry_price=args.entry,
        take_profit=args.take_profit,
        stop_loss=args.stop_loss,
        oca_group=f"{OCA_PREFIX}{args.symbol}_{args.direction}_{int(args.entry * 100)}",
        strategy="VDB", box_high=args.touch_high, box_low=args.touch_low,
        reason=(
            f"VDB {args.direction} {args.symbol} @ {args.entry:.2f} -- bounce op VWAP {args.vwap:.2f} "
            f"(touch-candle [{args.touch_low:.2f}-{args.touch_high:.2f}]); "
            f"TP {args.take_profit:.2f} (2:1), SL {args.stop_loss:.2f} (structuurgebaseerd)"
        ),
    )
    logger.info(f"Losgekoppeld VDB-tradeproces gestart, PID {os.getpid()}: {spec.reason}")

    nu = datetime.now()
    deadline = nu.replace(hour=VDB_FORCED_CLOSE_TIME.hour, minute=VDB_FORCED_CLOSE_TIME.minute, second=0, microsecond=0)
    resterend = max(1.0, (deadline - nu).total_seconds() / 60)
    fill_wait = min(15.0, resterend)

    try:
        result = execute_managed_trade(spec, args.symbol, max_fill_wait_minutes=fill_wait,
                                       forced_close_time=VDB_FORCED_CLOSE_TIME)
        logger.info(f"Trade afgerond: {result}")
    except Exception as e:
        logger.error(f"Onverwachte fout in VDB-tradeproces voor {args.symbol}: {e}")
        try:
            from telegram_notify import send_telegram_message
            send_telegram_message(f"⚠️ Onverwachte fout in VDB-tradeproces voor {args.symbol}: {e}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
