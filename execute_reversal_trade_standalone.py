"""
execute_reversal_trade_standalone.py — Quick Flip Scalper, Losgekoppelde
Bewaking + Trade-uitvoering

ANALOOG aan execute_trade_standalone.py, maar voor de nieuwe, correcte
3-stappen-strategie (zie reversal_strategy_module.py). BELANGRIJK
VERSCHIL: het bestaande script krijgt een AL-COMPLETE order-spec mee
(entry/TP/SL al bekend, komen uit een synchrone berekening in main.py).

Bij de nieuwe strategie is dat NIET mogelijk -- entry/TP/SL zijn pas
bekend NADAT stap 3 (het wachten op een bevestigd omkeerpatroon, tot
75 minuten) is voltooid. Dit script krijgt daarom alleen de box-
gegevens (high/low) en de verwachte richting mee, en voert ZELF de
volledige stap 3 + trade-uitvoering uit, als losgekoppeld
achtergrondproces (start_new_session=True, overleeft het einde van de
cron-job die dit script start) -- exact dezelfde reden als het
bestaande execute_trade_standalone.py: voorkomt dat de cron-job (en
daarmee de flock-vergrendeling in run_cycle.sh) tot 75 minuten
geblokkeerd blijft.

Gebruik (aangeroepen door main.py, niet handmatig):
    python3 execute_reversal_trade_standalone.py --symbol AAPL \
        --box-high 320.50 --box-low 316.20 --direction SHORT \
        --capital 1980.00
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
    filename=f"/opt/strategy/logs/reversal_trade_{os.getpid()}.log",
)
logger = logging.getLogger("execute_reversal_trade_standalone")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--box-high", required=True, type=float)
    parser.add_argument("--box-low", required=True, type=float)
    parser.add_argument("--direction", required=True, choices=["LONG", "SHORT"])
    parser.add_argument("--capital", required=True, type=float)
    args = parser.parse_args()

    logger.info(
        f"Losgekoppeld reversal-bewakingsproces gestart voor {args.symbol}, PID {os.getpid()} "
        f"(box=[{args.box_low:.2f}, {args.box_high:.2f}], richting={args.direction})"
    )

    from reversal_monitor_module import wait_for_reversal_signal
    from reversal_strategy_module import calculate_reversal_position_size
    from order_module import BracketOrderSpec, FORCED_CLOSE_TIME, execute_managed_trade

    try:
        signaal = wait_for_reversal_signal(
            args.symbol, box_high=args.box_high, box_low=args.box_low,
            expected_direction=args.direction, deadline=FORCED_CLOSE_TIME,
        )

        if signaal is None:
            logger.info(f"{args.symbol}: geen bevestigd omkeerpatroon binnen de tijdslimiet -- geen trade vandaag.")
            try:
                from telegram_notify import send_telegram_message
                send_telegram_message(
                    f"ℹ️ {args.symbol}: geen bevestigd omkeerpatroon binnen 90 minuten -- geen trade vandaag."
                )
            except Exception:
                pass
            return

        take_profit = args.box_low if args.direction == "SHORT" else args.box_high

        try:
            position_size, risk_amount, capped = calculate_reversal_position_size(
                entry_price=signaal.trigger_price, stop_loss=signaal.stop_loss_price,
                capital=args.capital, direction=args.direction,
            )
        except ValueError as e:
            # NIEUW (1 sep 2026, bugfix): dit is een VERWACHT, legitiem
            # "sla deze trade over"-scenario (bv. de entry lag door een
            # koerssprong aan de verkeerde kant van de SL) -- geen
            # kritieke systeemfout, dus GEEN alarmerende foutmelding,
            # alleen een informatieve.
            logger.warning(f"{args.symbol}: trade overgeslagen -- {e}")
            try:
                from telegram_notify import send_telegram_message
                send_telegram_message(f"ℹ️ {args.symbol}: trade overgeslagen -- {e}")
            except Exception:
                pass
            return

        if position_size * signaal.trigger_price < 5.0:
            logger.warning(f"{args.symbol}: positiewaarde te klein met €{args.capital:.2f} kapitaal -- geen trade.")
            return

        spec = BracketOrderSpec(
            action="BUY" if args.direction == "LONG" else "SELL",
            quantity=position_size,
            entry_price=signaal.trigger_price,
            take_profit=take_profit,
            stop_loss=signaal.stop_loss_price,
            oca_group=f"TT2_{args.symbol}_{args.direction}_{int(signaal.trigger_price*100)}",
            reason=(
                f"{args.direction} {args.symbol} @ {signaal.trigger_price:.2f} "
                f"(patroon: {signaal.pattern_type}), TP {take_profit:.2f} (box-rand), "
                f"SL {signaal.stop_loss_price:.2f} (structuur), risico €{risk_amount:.2f}"
                + (" [GELIMITEERD door max-positiewaarde]" if capped else "")
            ),
        )

        logger.info(f"Omkeerpatroon bevestigd voor {args.symbol}: {spec.reason} -- trade wordt nu geplaatst.")

        # NIEUW (3 sep 2026, bugfix): bereken de RESTERENDE tijd tot de
        # 90-minuten-strategiedeadline (FORCED_CLOSE_TIME), en geef die
        # mee als bovengrens voor de entry-fill-wachttijd -- voorkomt
        # dat een laat-gevonden signaal (bv. vlak vóór 17:00) via de
        # eigen, losstaande 75-minuten-fill-timeout alsnog ver NA de
        # deadline kan vullen (live gebeurd bij META, 3 sep 2026: fill
        # om 17:14, 14 minuten na de bedoelde afkap). Een kleine,
        # positieve ondergrens (1 minuut) voorkomt een direct-annuleren
        # bij een deadline die al zeer dichtbij is.
        nu = datetime.now()
        deadline_vandaag = nu.replace(
            hour=FORCED_CLOSE_TIME.hour, minute=FORCED_CLOSE_TIME.minute,
            second=FORCED_CLOSE_TIME.second, microsecond=0,
        )
        resterende_minuten = max(1.0, (deadline_vandaag - nu).total_seconds() / 60)
        logger.info(f"{args.symbol}: nog {resterende_minuten:.1f} minuten tot de deadline -- fill-wachttijd hierop begrensd.")

        result = execute_managed_trade(spec, args.symbol, max_fill_wait_minutes=resterende_minuten)
        logger.info(f"Trade afgerond: {result}")

    except Exception as e:
        logger.error(f"Onverwachte fout in losgekoppeld reversal-proces voor {args.symbol}: {e}")
        try:
            from telegram_notify import send_telegram_message
            send_telegram_message(f"⚠️ Onverwachte fout in reversal-trade-proces voor {args.symbol}: {e}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
