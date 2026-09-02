"""
vix_rider_order_module.py — VIX Rider, Module 3: Order Uitvoering

Orkestreert de volledige trade: entry-order plaatsen, wachten op
fill, dan een TRAIL-order (meebewegende stop) plaatsen die door IBKR
zelf wordt bewaakt -- geen eigen bewakingslus nodig, in tegenstelling
tot Touch & Turn Scalper's zelf-beheerde TP/SL-bewaking.

ONTWERPBESLISSING: de trailing-afstand van de TRAIL-order is gelijk
aan de initiële stop-afstand (entry tot Opening-Range-midpoint) uit
vix_rider_exit_module.calculate_vix_rider_position(). Dit houdt
positiegrootte en risico consistent: de trailing stop begint exact op
het risiconiveau waarvoor de positie is berekend, en beweegt vandaar
mee zodra de trade in de winst komt.

Hergebruikt bevestigd-werkende bouwstenen uit ibkr_web_api.py
(place_single_order, get_order_status, cancel_order) en dezelfde
Telegram/journal/state-patronen als order_module.py.

BELANGRIJK: de functies die daadwerkelijk met IBKR praten zijn nog
niet end-to-end getest ALS GEHEEL (de losse onderdelen -- LMT-order
plaatsen, TRAIL-order plaatsen -- zijn wel elk apart live bevestigd
vandaag, maar niet in deze exacte samenhang).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, time as dt_time

# KRITIEKE TOEVOEGING (26 aug 2026): hetzelfde gat als bij de scalper
# ontdekt (zie order_module.py) -- MAX_EXIT_WAIT_MINUTES (480 = 8 uur)
# had geen vaste afkaptijd, waardoor een trade een hele nacht open kon
# blijven staan zonder ooit gedetecteerd te worden. VIX Rider is geen
# 90-minuten-scalper (dat is de andere strategie) maar een breakout-
# strategie die de hele handelsdag kan lopen -- vandaar een afkaptijd
# bij MARKTSLUITING (22:00 CEST) in plaats van 90 minuten na opening.
MARKET_CLOSE_TIME = dt_time(22, 0)  # CEST

from vix_rider_exit_module import VixRiderPositionPlan

logger = logging.getLogger("vix_rider_order_module")

ENTRY_TIMEOUT_MINUTES = 15  # korter dan de scalper's 60 min -- een doorbraak die niet
                             # snel vult, is vermoedelijk al voorbij het beste instapmoment
EXIT_CHECK_INTERVAL_SECONDS = 30
MAX_EXIT_WAIT_MINUTES = 480  # 8 uur, zelfde als de scalper


def _notify_safe(message: str) -> None:
    """Fail-safe Telegram-wrapper, zelfde patroon als order_module.py."""
    try:
        from telegram_notify import send_telegram_message
        send_telegram_message(f"[VIX Rider] {message}")
    except Exception as e:
        logger.error(f"Kon Telegram-melding niet versturen: {e}")


def place_vix_rider_entry(plan: VixRiderPositionPlan, symbol: str) -> dict:
    """
    Plaatst de entry-order voor een VIX Rider-trade: een limietorder
    op de doorbraakprijs.

    Returns:
        dict met "order_id", "account_id", "conid" bij succes, of
        "error" bij falen.
    """
    from ibkr_web_api import resolve_conid, get_account_id, place_single_order

    conid = resolve_conid(symbol)
    if conid is None:
        error = f"Kon geen conid vinden voor {symbol} -- order niet geplaatst."
        logger.error(error)
        return {"error": error}

    account_id = get_account_id()
    if account_id is None:
        error = "Kon geen account-ID ophalen -- order niet geplaatst."
        logger.error(error)
        return {"error": error}

    coid = f"vr{int(time.time()) % 1000000}"
    result = place_single_order(
        conid=conid, account_id=account_id, action="BUY" if plan.direction == "LONG" else "SELL",
        quantity=plan.quantity, order_type="LMT", price=plan.entry_price,
        coid=coid,
    )

    if "error" not in result:
        result["account_id"] = account_id
        result["conid"] = conid

    logger.info(f"VIX Rider entry-order voor {symbol}: {result}")
    return result


def wait_for_vix_rider_entry_fill(order_id: str, account_id: str, timeout_minutes: int = ENTRY_TIMEOUT_MINUTES) -> str:
    """
    Wacht tot de entry-order gevuld is, of annuleert 'm na
    `timeout_minutes` -- korter dan de scalper, want een doorbraak die
    niet snel vult, is vermoedelijk al voorbij het beste instapmoment.
    """
    from ibkr_web_api import get_order_status, cancel_order

    waited_seconds = 0
    timeout_seconds = timeout_minutes * 60

    while waited_seconds < timeout_seconds:
        status = get_order_status(order_id)
        order_status = status.get("order_status", status.get("status", "Unknown"))

        if order_status in ("Filled", "Cancelled", "ApiCancelled"):
            logger.info(f"VIX Rider entry-order {order_id} status: {order_status}")
            return order_status

        time.sleep(EXIT_CHECK_INTERVAL_SECONDS)
        waited_seconds += EXIT_CHECK_INTERVAL_SECONDS

    logger.warning(f"VIX Rider entry-order {order_id} niet gevuld binnen {timeout_minutes} minuten -- annuleren.")
    cancel_order(account_id, order_id)
    return "TimeoutCancelled"


def place_trailing_exit(plan: VixRiderPositionPlan, symbol: str, conid: int, account_id: str) -> dict:
    """
    Plaatst de TRAIL-order die de positie sluit -- de trailing-afstand
    is gelijk aan de initiële stop-afstand (entry tot OR-midpoint),
    zodat risico en positiegrootte consistent blijven.

    Returns:
        dict met "order_id" bij succes, of "error" bij falen.
    """
    from ibkr_web_api import place_single_order

    exit_action = "SELL" if plan.direction == "LONG" else "BUY"
    trailing_amt = abs(plan.entry_price - plan.initial_stop_loss)

    result = place_single_order(
        conid=conid, account_id=account_id, action=exit_action,
        quantity=plan.quantity, order_type="TRAIL", trailing_amt=trailing_amt,
    )

    if "error" in result:
        logger.error(f"TRAIL-exit-order mislukt voor {symbol}: {result['error']}")
        return {"error": result["error"]}

    logger.info(f"VIX Rider TRAIL-exit geplaatst voor {symbol}: trailing_amt={trailing_amt:.4f}")
    return {"order_id": result.get("order_id"), "trailing_amt": trailing_amt}


def monitor_trail_exit(order_id: str, account_id: str = None, max_wait_minutes: int = MAX_EXIT_WAIT_MINUTES) -> dict:
    """
    Bewaakt de TRAIL-order tot deze vult -- eenvoudiger dan de
    scalper's monitor_oco_exit(), want er is maar ÉÉN exit-order om
    te volgen (de TRAIL-order zelf regelt het meebewegen; wij hoeven
    alleen te wachten tot 'ie triggert en vult).

    KRITIEKE FIX (26 aug 2026): hetzelfde gat als bij de scalper
    ontdekt en gerepareerd -- een vaste afkaptijd (marktsluiting,
    22:00 CEST) EN detectie van aanhoudende fouten (bijv. een
    verlopen sessie), met een dringende Telegram-waarschuwing als dat
    langer dan een paar minuten aanhoudt. Voorkomt dat een VIX Rider-
    positie een hele nacht onbeheerd blijft staan, zoals bij de
    scalper's GOOGL-trade op 24 aug 2026 gebeurde.
    """
    from ibkr_web_api import get_order_status

    waited_seconds = 0
    timeout_seconds = max_wait_minutes * 60
    opeenvolgende_fouten = 0
    waarschuwing_verstuurd = False
    FOUTDREMPEL_VOOR_WAARSCHUWING = 5

    while waited_seconds < timeout_seconds:
        if datetime.now().time() >= MARKET_CLOSE_TIME:
            logger.warning(f"Marktsluiting ({MARKET_CLOSE_TIME}) bereikt -- VIX Rider-positie wordt nu geforceerd gesloten.")
            return {"result": "forced_close_market_close", "status": "closing"}

        status = get_order_status(order_id)

        if "error" in status:
            opeenvolgende_fouten += 1
            if opeenvolgende_fouten >= FOUTDREMPEL_VOOR_WAARSCHUWING and not waarschuwing_verstuurd:
                _notify_safe(
                    f"🚨 WAARSCHUWING (VIX Rider): kan al {opeenvolgende_fouten}x achtereen de status van "
                    f"de TRAIL-order niet ophalen ({status.get('error')}). De positie is mogelijk "
                    f"ONBESCHERMD als de sessie is verlopen. Controleer handmatig met /check_ibkr."
                )
                waarschuwing_verstuurd = True
                logger.error(f"Aanhoudende fouten bij VIX Rider orderstatus-check ({opeenvolgende_fouten}x) -- waarschuwing verstuurd.")
            time.sleep(30)
            waited_seconds += 30
            continue
        else:
            opeenvolgende_fouten = 0

        order_status = status.get("order_status", "Unknown")

        if order_status == "Filled":
            logger.info(f"TRAIL-order {order_id} gevuld -- positie gesloten.")
            return {"result": "trail_stop_hit", "status": order_status}

        if order_status in ("Cancelled", "ApiCancelled"):
            logger.warning(f"TRAIL-order {order_id} geannuleerd zonder te vullen.")
            return {"result": "cancelled", "status": order_status}

        time.sleep(EXIT_CHECK_INTERVAL_SECONDS)
        waited_seconds += EXIT_CHECK_INTERVAL_SECONDS

    logger.warning(f"TRAIL-order {order_id} nog niet gevuld na {max_wait_minutes} minuten.")
    return {"result": "timeout", "status": "still_open"}


def report_vix_rider_outcome(plan: VixRiderPositionPlan, symbol: str, outcome: dict, entry_order_id: str = None) -> None:
    """
    Rapporteert een afgeronde VIX Rider-trade: Telegram-melding +
    journal-logging + bijdrage aan de gedeelde dagstop-circuit-breaker.

    GROTE FIX (26 aug 2026), drie samenhangende gaten tegelijk
    gerepareerd (ontdekt bij een systematische audit ná het vinden van
    vergelijkbare problemen bij de scalper):

        1. Gebruikt nu get_actual_fill_price() (dezelfde, al bevestigd
           werkende functie als de scalper) om de EXACTE fill-prijs
           van zowel de entry- als de TRAIL-exit-order op te halen,
           i.p.v. helemaal geen PnL-schatting te geven.
        2. Roept nu add_trade_to_log() aan -- VIX Rider-trades telden
           voorheen NIET mee voor de gedeelde 3%-dagstop-circuit-
           breaker (risk_module.check_daily_loss_limit), een reëel
           risicobeheer-gat.
        3. Gebruikt nu estimate_net_pnl() (dezelfde, fee-bewuste
           berekening als de scalper) i.p.v. helemaal geen fee-
           correctie, en werkt het GEDEELDE compounding-saldo bij
           (state_module.update_simulated_balance) met het netto-
           resultaat.
    """
    from order_module import get_actual_fill_price
    from journal_module import estimate_net_pnl
    from state_module import add_trade_to_log, update_simulated_balance

    result = outcome.get("result", "unknown")
    emoji = "✅" if result == "trail_stop_hit" else "⚠️"
    position_value = plan.quantity * plan.entry_price

    exit_order_id = outcome.get("forced_close_order_id") if result == "forced_close_market_close" else outcome.get("order_id")
    actual_entry_price = get_actual_fill_price(entry_order_id) if entry_order_id else None
    actual_exit_price = get_actual_fill_price(exit_order_id) if exit_order_id else None

    used_entry_price = actual_entry_price if actual_entry_price is not None else plan.entry_price
    prices_zijn_exact = actual_entry_price is not None and actual_exit_price is not None

    pnl_gross = None
    pnl_net = None
    fees_totaal = None
    if actual_exit_price is not None:
        try:
            netto_resultaat = estimate_net_pnl(plan.direction, used_entry_price, actual_exit_price, plan.quantity)
            pnl_gross = netto_resultaat["pnl_gross"]
            pnl_net = netto_resultaat["pnl_net"]
            fees_totaal = netto_resultaat["entry_fee"] + netto_resultaat["exit_fee"]
        except Exception as e:
            logger.error(f"Kon PnL niet berekenen voor VIX Rider {symbol}: {e}")

    try:
        from vix_rider_journal_module import log_vix_rider_trade
        trailing_amt = abs(plan.entry_price - plan.initial_stop_loss)
        log_vix_rider_trade({
            "symbol": symbol, "direction": plan.direction,
            "opening_range_midpoint": plan.initial_stop_loss,
            "entry_price": used_entry_price, "initial_stop_loss": plan.initial_stop_loss,
            "trailing_amt": trailing_amt, "quantity": plan.quantity, "result": result,
            "pnl_note": "exacte fill-prijzen" if prices_zijn_exact else "geen exacte exit-prijs beschikbaar",
        })
    except Exception as e:
        logger.error(f"Kon VIX Rider-trade niet loggen in journal voor {symbol}: {e}")

    if pnl_net is not None:
        resultaat_regel = f"Resultaat: €{pnl_gross:+.2f} bruto | €{pnl_net:+.2f} netto (na €{fees_totaal:.2f} fees)"
    else:
        resultaat_regel = "Resultaat: onbekend (exacte exit-prijs niet beschikbaar)"

    _notify_safe(
        f"{emoji} {symbol} {plan.direction}: {result.replace('_', ' ')}\n"
        f"Aantal: {plan.quantity} | Entry: {used_entry_price:.2f} "
        f"(€{position_value:,.2f} ingezet)\n"
        f"{resultaat_regel}"
    )

    # Bijdrage aan de GEDEELDE dagstop-circuit-breaker -- voorheen
    # ontbrak dit volledig voor VIX Rider-trades.
    try:
        add_trade_to_log({"symbol": symbol, "direction": plan.direction, "pnl": pnl_net or 0.0})
    except Exception as e:
        logger.error(f"Kon VIX Rider-trade niet loggen in state voor {symbol}: {e}")

    # Bijdrage aan het GEDEELDE compounding-saldo -- voorheen gebruikte
    # VIX Rider een los, vast bedrag i.p.v. dit gedeelde, groeiende saldo.
    try:
        if pnl_net is not None:
            update_simulated_balance(pnl_net)
    except Exception as e:
        logger.error(f"Kon gesimuleerd saldo niet bijwerken voor VIX Rider {symbol}: {e}")

    try:
        from state_module import remove_position
        remove_position(symbol)
    except Exception as e:
        logger.error(f"Kon positie niet verwijderen uit state voor {symbol}: {e}")


def execute_vix_rider_trade(plan: VixRiderPositionPlan, symbol: str) -> dict:
    """
    Orkestreert de volledige VIX Rider-trade: entry -> wacht op fill ->
    TRAIL-exit plaatsen -> bewaken tot gevuld -> rapporteren.

    LET OP: BLOKKERENDE functie, net als de scalper's
    execute_managed_trade() -- moet daarom ook via een losgekoppeld
    achtergrondproces aangeroepen worden vanuit vix_rider_main.py, niet
    rechtstreeks vanuit een cron-gebonden script.
    """
    entry_result = place_vix_rider_entry(plan, symbol)
    if "error" in entry_result:
        _notify_safe(f"❌ {symbol}: entry-order plaatsen mislukt -- {entry_result['error']}")
        return {"status": "entry_failed", "symbol": symbol, "reason": entry_result["error"]}

    order_id = entry_result["order_id"]
    account_id = entry_result["account_id"]
    conid = entry_result["conid"]

    position_value = plan.quantity * plan.entry_price
    _notify_safe(
        f"📤 {symbol} {plan.direction}: entry geplaatst @ {plan.entry_price:.2f}\n"
        f"Aantal: {plan.quantity} | Investering: €{position_value:,.2f}\n"
        f"Initiële SL: {plan.initial_stop_loss:.2f} (trailing na fill)"
    )

    fill_status = wait_for_vix_rider_entry_fill(order_id, account_id)
    if fill_status != "Filled":
        _notify_safe(f"⏱️ {symbol}: entry niet gevuld binnen de tijdslimiet ({fill_status}) -- order geannuleerd.")
        return {"status": "entry_not_filled", "symbol": symbol, "fill_status": fill_status}

    try:
        from state_module import add_position
        add_position({
            "symbol": symbol, "direction": plan.direction,
            "entry_price": plan.entry_price, "quantity": plan.quantity,
        })
    except Exception as e:
        logger.error(f"Kon positie niet registreren in state voor {symbol}: {e}")

    exit_result = place_trailing_exit(plan, symbol, conid, account_id)
    if "error" in exit_result:
        _notify_safe(
            f"⚠️ {symbol}: TRAIL-exit plaatsen mislukt na gevulde entry -- "
            f"{exit_result['error']}. HANDMATIGE CONTROLE NODIG, positie is onbeschermd."
        )
        return {"status": "exit_order_failed", "symbol": symbol, "reason": exit_result["error"]}

    _notify_safe(f"✅ {symbol} {plan.direction}: entry gevuld, TRAIL-stop actief (afstand €{exit_result['trailing_amt']:.2f}).")

    outcome = monitor_trail_exit(exit_result["order_id"], account_id)
    # TRAIL-order-ID expliciet toevoegen, zodat report_vix_rider_outcome()
    # de exacte fill-prijs kan opzoeken (zelfde patroon als de scalper).
    outcome["order_id"] = exit_result["order_id"]

    if outcome.get("result") == "forced_close_market_close":
        forced_outcome = force_close_vix_rider_position(plan, symbol, conid, account_id, exit_result["order_id"])
        outcome.update(forced_outcome)

    if outcome.get("result") in ("trail_stop_hit", "forced_close_market_close"):
        report_vix_rider_outcome(plan, symbol, outcome, entry_order_id=order_id)

    return {"status": "trade_complete", "symbol": symbol, **outcome}


def force_close_vix_rider_position(plan, symbol: str, conid: int, account_id: str, trail_order_id: str) -> dict:
    """
    Voert de geforceerde sluiting bij marktsluiting daadwerkelijk uit:
    annuleert de TRAIL-order en sluit de positie met een marktconforme
    limietorder in de tegengestelde richting.

    Zelfde aanpak als de scalper's force_close_position() in
    order_module.py, toegepast op VIX Rider (26 aug 2026).
    """
    from ibkr_web_api import place_single_order, cancel_order

    cancel_order(account_id, trail_order_id)

    close_action = "SELL" if plan.direction == "LONG" else "BUY"
    close_price = plan.entry_price * (0.995 if close_action == "SELL" else 1.005)

    result = place_single_order(
        conid=conid, account_id=account_id, action=close_action,
        quantity=plan.quantity, order_type="LMT", price=round(close_price, 2),
    )

    if "error" in result:
        logger.error(f"Geforceerde sluiting mislukt voor {symbol}: {result['error']}")
        _notify_safe(
            f"🚨 URGENT: geforceerde marktsluiting-sluiting MISLUKT voor {symbol} -- "
            f"{result['error']}. Positie is mogelijk nog open, controleer HANDMATIG direct."
        )
        return {"forced_close_status": "failed", "forced_close_error": result["error"]}

    _notify_safe(f"⏰ {symbol}: marktsluiting bereikt -- VIX Rider-positie wordt nu geforceerd gesloten.")

    try:
        from state_module import remove_position
        remove_position(symbol)
    except Exception as e:
        logger.error(f"Kon positie niet verwijderen uit state voor {symbol}: {e}")

    return {"forced_close_status": "submitted", "forced_close_order_id": result.get("order_id")}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from vix_rider_entry_module import OpeningRange, BreakoutSignal
    from vix_rider_exit_module import calculate_vix_rider_position
    from datetime import datetime

    # Scenario 1: positieplan opbouwen en controleren dat de trailing_amt
    # correct wordt afgeleid uit de stop-afstand (geen live IBKR nodig)
    opening_range = OpeningRange(high=460.0, low=449.0, midpoint=454.5)
    signal = BreakoutSignal(
        direction="LONG", entry_price=462.0, opening_range=opening_range,
        breakout_time=datetime(2026, 8, 21, 16, 10), reason="test",
    )
    plan = calculate_vix_rider_position(signal, capital=1000.0)
    print(f"Positieplan: {plan.to_dict()}")

    verwachte_trailing_amt = abs(plan.entry_price - plan.initial_stop_loss)
    print(f"\nVerwachte trailing_amt (entry - initiële SL): {verwachte_trailing_amt:.4f}")
    print("(Dit is wat place_trailing_exit() zou berekenen -- hier alleen de rekenkundige verificatie, geen live order)")

    print("\n--- Pure logica-test klaar. Live tests vereisen een geauthenticeerde Gateway-sessie: ---")
    print("  python3 -c \"from vix_rider_order_module import execute_vix_rider_trade; ...\"")
