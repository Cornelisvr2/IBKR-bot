"""
execute_rvb_trade_standalone.py — RVB, Losgekoppelde Trade-uitvoering

Analoog aan execute_trade_standalone.py (TTS): krijgt een AL-COMPLETE
order-spec mee (entry/TP/SL/aantal zijn al berekend in rvb_scan.py)
en voert de zelf-beheerde OCO-flow uit via execute_managed_trade().
Draait als losgekoppeld achtergrondproces (start_new_session=True in
rvb_scan.py), zodat de 5-minuten-cron niet geblokkeerd wordt.

Verschil met TTS/QFS: de geforceerde sluiting ligt NIET op 18:00
(150-minuten-regel) maar op 21:55 CEST -- RVB is een dagstrategie,
de positie mag tot vlak vóór het einde van de sessie doorlopen.

Gebruik (aangeroepen door rvb_scan.py, niet handmatig):
    python3 execute_rvb_trade_standalone.py --symbol AAPL --direction LONG \
        --entry 230.10 --take-profit 239.30 --stop-loss 225.50 --quantity 4.3 \
        --orb-high 229.80 --orb-low 226.10 --volume-ratio 3.6
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
    filename=f"/opt/strategy/logs/rvb_trade_{os.getpid()}.log",
)
logger = logging.getLogger("execute_rvb_trade_standalone")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--direction", required=True, choices=["LONG", "SHORT"])
    parser.add_argument("--entry", required=True, type=float)
    parser.add_argument("--take-profit", required=True, type=float)
    parser.add_argument("--stop-loss", required=True, type=float)
    parser.add_argument("--quantity", required=True, type=float)
    parser.add_argument("--orb-high", required=True, type=float)
    parser.add_argument("--orb-low", required=True, type=float)
    parser.add_argument("--volume-ratio", required=True, type=float)
    args = parser.parse_args()

    from order_module import BracketOrderSpec, execute_managed_trade
    from rvb_strategy_module import RVB_FORCED_CLOSE_TIME, OCA_PREFIX

    spec = BracketOrderSpec(
        action="BUY" if args.direction == "LONG" else "SELL",
        quantity=args.quantity,
        entry_price=args.entry,
        take_profit=args.take_profit,
        stop_loss=args.stop_loss,
        oca_group=f"{OCA_PREFIX}{args.symbol}_{args.direction}_{int(args.entry * 100)}",
        strategy="RVB", box_high=args.orb_high, box_low=args.orb_low,
        reason=(
            f"RVB {args.direction} {args.symbol} @ {args.entry:.2f} -- doorbraak "
            f"{'boven' if args.direction == 'LONG' else 'onder'} ORB [{args.orb_low:.2f}-{args.orb_high:.2f}] "
            f"op {args.volume_ratio:.1f}x volume; TP {args.take_profit:.2f} (2:1), SL {args.stop_loss:.2f} (2%)"
        ),
    )
    logger.info(f"Losgekoppeld RVB-tradeproces gestart, PID {os.getpid()}: {spec.reason}")

    # Fill-wachttijd: een doorbraak-entry moet SNEL vullen of niet --
    # als de koers na 15 minuten nog niet op de limiet is geweest, is
    # de doorbraak vermoedelijk mislukt en willen we niet alsnog laat
    # instappen. Bovendien begrensd door de resterende tijd tot 21:55.
    nu = datetime.now()
    deadline = nu.replace(hour=RVB_FORCED_CLOSE_TIME.hour, minute=RVB_FORCED_CLOSE_TIME.minute, second=0, microsecond=0)
    resterend = max(1.0, (deadline - nu).total_seconds() / 60)
    fill_wait = min(15.0, resterend)

    try:
        result = execute_managed_trade(spec, args.symbol, max_fill_wait_minutes=fill_wait,
                                       forced_close_time=RVB_FORCED_CLOSE_TIME)
        logger.info(f"Trade afgerond: {result}")
    except Exception as e:
        logger.error(f"Onverwachte fout in RVB-tradeproces voor {args.symbol}: {e}")
        try:
            from telegram_notify import send_telegram_message
            send_telegram_message(f"⚠️ Onverwachte fout in RVB-tradeproces voor {args.symbol}: {e}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
