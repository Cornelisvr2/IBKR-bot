"""
order_module.py — Touch & Turn Scalper, Module 5: Order Uitvoering

Stuurt orders door naar IBKR via de Client Portal Web API (zie
ibkr_web_api.py). Gebruikt ZELF-BEHEERDE OCO-logica in plaats van
IBKR's ingebouwde bracket-orderstructuur:

    ONTDEKKING (19 aug 2026): de parent/child-bracketstructuur
    (cOID/parentId in één aanroep) bleek bij herhaald live testen
    onbetrouwbaar -- faalde met "Invalid order price fields" en
    "Parent order ... is no more existed", ook na meerdere reparaties
    (acctId/secType toevoegen, cOID-formaat verkorten, outsideRTH
    toevoegen). Een LOSSE, ongekoppelde order bleek wel betrouwbaar te
    werken. Daarom: entry plaatsen (place_entry_order) -> wachten op
    fill (wait_for_entry_fill) -> pas dan TP/SL plaatsen als twee
    losse orders (place_exit_orders) -> de één actief annuleren zodra
    de ander vult (monitor_oco_exit). execute_managed_trade()
    orkestreert deze hele flow.

BELANGRIJK: de functies die daadwerkelijk met IBKR praten zijn nog
niet end-to-end getest voor een VOLLEDIGE trade (entry-fill -> TP/SL
-> exit-fill) -- wel bevestigd: losse order plaatsen en annuleren
werkt betrouwbaar (meerdere keren getest). Wel getest zonder IBKR: de
orderconstructie-logica (build_bracket_orders, dat nu enkel de
prijzen/hoeveelheid berekent, niet meer de IBKR-payload) -- zie
__main__.

Gebruik in andere modules:
    from order_module import execute_managed_trade, build_bracket_orders
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, time as dt_time

from exit_module import ExitPlan

logger = logging.getLogger("order_module")

ORDER_TIMEOUT_MINUTES = 60

# KRITIEKE TOEVOEGING (25 aug 2026): het originele strategie-document
# specificeert een harde 90-minuten-regel na marktopening (15:30 CEST),
# dus tot 17:00 CEST -- na dit tijdstip wordt een positie ALTIJD
# geforceerd gesloten, ongeacht of TP/SL geraakt is. Dit was een gat in
# de eerdere implementatie: een GOOGL-trade op 24 aug 2026 bleef een
# hele nacht (8+ uur) open staan zonder dat dit ooit gedetecteerd werd,
# tot de positie uiteindelijk volledig ONBESCHERMD bleek (TP/SL-orders
# waren al verlopen/verdwenen) en handmatig gesloten moest worden.
#
# Bewuste keuze van de gebruiker (25 aug 2026): een eventueel verlies
# op dat moment wordt geaccepteerd -- de 90-minuten-regel is de
# bedoelde aard van deze SCALPING-strategie, geen bug om te vermijden.
MARKET_OPEN_TIME = dt_time(15, 30)  # CEST
FORCED_CLOSE_MINUTES_AFTER_OPEN = 90


def get_forced_close_time() -> dt_time:
    """Berekent het vaste tijdstip (CEST) waarop elke positie uiterlijk geforceerd gesloten wordt."""
    open_minutes = MARKET_OPEN_TIME.hour * 60 + MARKET_OPEN_TIME.minute
    close_minutes = open_minutes + FORCED_CLOSE_MINUTES_AFTER_OPEN
    return dt_time(close_minutes // 60, close_minutes % 60)


FORCED_CLOSE_TIME = get_forced_close_time()  # 17:00 CEST, bij marktopening 15:30 + 90 min


@dataclass
class BracketOrderSpec:
    """
    Platte, ib_async-onafhankelijke beschrijving van de drie orders
    die samen een bracket vormen. Handig om te loggen, naar Telegram
    te sturen, en om te testen zonder een echte Order-klasse nodig te
    hebben.
    """
    action: str          # "BUY" of "SELL" voor de entry
    quantity: int
    entry_price: float
    take_profit: float
    stop_loss: float
    oca_group: str
    reason: str

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "take_profit": self.take_profit,
            "stop_loss": self.stop_loss,
            "oca_group": self.oca_group,
            "reason": self.reason,
        }


def build_bracket_orders(exit_plan: ExitPlan, symbol: str) -> BracketOrderSpec:
    """
    Bouwt de platte orderspecificatie op uit een ExitPlan.

    LONG  -> entry is een BUY  limietorder op exit_plan.entry_price
    SHORT -> entry is een SELL limietorder op exit_plan.entry_price

    Dit is pure logica, geen IBKR nodig -- volledig testbaar.
    """
    if exit_plan.direction == "LONG":
        action = "BUY"
    elif exit_plan.direction == "SHORT":
        action = "SELL"
    else:
        raise ValueError(f"Onbekende richting: {exit_plan.direction}")

    if exit_plan.position_size < 1:
        raise ValueError(
            f"Positiegrootte {exit_plan.position_size} is te klein om een "
            "order te plaatsen -- trade wordt overgeslagen."
        )

    oca_group = f"TTS_{symbol}_{exit_plan.direction}_{int(exit_plan.entry_price * 100)}"

    spec = BracketOrderSpec(
        action=action,
        quantity=exit_plan.position_size,
        entry_price=exit_plan.entry_price,
        take_profit=exit_plan.take_profit,
        stop_loss=exit_plan.stop_loss,
        oca_group=oca_group,
        reason=(
            f"{action} {exit_plan.position_size}x {symbol} @ {exit_plan.entry_price:.4f}, "
            f"TP {exit_plan.take_profit:.4f}, SL {exit_plan.stop_loss:.4f} "
            f"(OCA-groep: {oca_group})"
        ),
    )
    logger.info(f"Bracket-order opgebouwd: {spec.reason}")
    return spec


def place_entry_order(spec: BracketOrderSpec, symbol: str) -> dict:
    """
    Plaatst ALLEEN de entry-order (geen bracket-koppeling) -- eerste
    stap van onze zelf-beheerde OCO-flow.

    ACHTERGROND: IBKR's ingebouwde parent/child-bracketstructuur
    (cOID/parentId in één aanroep) bleek bij live testen (19 aug 2026)
    herhaaldelijk te falen ("Invalid order price fields", "Parent
    order ... is no more existed"), ondanks meerdere pogingen tot
    reparatie. Een LOSSE order plaatsen bleek wel betrouwbaar te
    werken. Daarom beheren we OCO-gedrag zelf: entry plaatsen -> wachten
    op fill (wait_for_entry_fill) -> pas dan TP/SL plaatsen
    (place_exit_orders) -> de één annuleren zodra de ander vult
    (monitor_oco_exit).

    Returns:
        dict met "order_id" bij succes, of "error" bij falen.
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

    coid = f"e{int(time.time()) % 1000000}"
    result = place_single_order(
        conid=conid, account_id=account_id, action=spec.action,
        quantity=spec.quantity, order_type="LMT", price=spec.entry_price,
        coid=coid,
    )

    if "error" not in result:
        result["account_id"] = account_id
        result["conid"] = conid

    logger.info(f"Entry-order voor {symbol}: {result}")
    return result


def wait_for_entry_fill(order_id: str, account_id: str, timeout_minutes: int = ORDER_TIMEOUT_MINUTES) -> str:
    """
    Wacht tot de entry-order gevuld is, of annuleert 'm na
    `timeout_minutes` als dat niet gebeurt. Werkt via polling (de Web
    API heeft geen blijvende socket-verbinding).

    LET OP: nog niet end-to-end getest. De exacte statuswaarden die de
    Web API teruggeeft zijn een aanname op basis van eerder
    waargenomen waarden ("PendingSubmit", "Inactive") -- "Filled" zelf
    is nog niet live waargenomen, aangezien onze test-orders bewust
    ver van de marktprijs geplaatst zijn om niet te vullen.
    """
    from ibkr_web_api import get_order_status, cancel_order

    check_interval = 30
    waited_seconds = 0
    timeout_seconds = timeout_minutes * 60

    while waited_seconds < timeout_seconds:
        status = get_order_status(order_id)
        order_status = status.get("order_status", status.get("status", "Unknown"))

        if order_status in ("Filled", "Cancelled", "ApiCancelled"):
            logger.info(f"Entry-order {order_id} status: {order_status}")
            return order_status

        time.sleep(check_interval)
        waited_seconds += check_interval

    logger.warning(f"Entry-order {order_id} niet gevuld binnen {timeout_minutes} minuten -- annuleren.")
    cancel_order(account_id, order_id)
    return "TimeoutCancelled"


def place_exit_orders(spec: BracketOrderSpec, symbol: str, conid: int, account_id: str) -> dict:
    """
    Plaatst de take-profit (LMT) en stop-loss (STP) als twee LOSSE
    orders, ná bevestigde vulling van de entry -- tweede stap van de
    zelf-beheerde OCO-flow.

    BELANGRIJKE FIX (21 aug 2026): probeert ALTIJD BEIDE orders,
    ongeacht of de één faalt. De vorige versie stopte direct als de
    TP-order faalde, waardoor de SL-order NOOIT geplaatst werd --
    live gebeurd op 21 aug 2026 (TP faalde door een niet-afgeronde
    prijs, zie ibkr_web_api.py), met een tijdelijk volledig
    onbeschermde open positie tot gevolg totdat handmatig ingegrepen
    werd. Nu wordt de stop-loss ALTIJD geprobeerd, ook als de take-
    profit mislukt -- een SL-only positie is veel veiliger dan een
    volledig onbeschermde positie.

    Returns:
        dict met "tp_order_id" en "sl_order_id" (elk kan None zijn
        als die specifieke order mislukte), en "error" als GEEN VAN
        BEIDE orders lukte (de gevaarlijkste situatie).
    """
    from ibkr_web_api import place_single_order

    exit_action = "SELL" if spec.action == "BUY" else "BUY"

    tp_coid = f"tp{int(time.time()) % 1000000}"
    tp_result = place_single_order(
        conid=conid, account_id=account_id, action=exit_action,
        quantity=spec.quantity, order_type="LMT", price=spec.take_profit,
        coid=tp_coid,
    )
    tp_order_id = None
    tp_error = None
    if "error" in tp_result:
        tp_error = tp_result["error"]
        logger.error(f"Take-profit order mislukt voor {symbol}: {tp_error}")
    else:
        tp_order_id = tp_result.get("order_id")

    # ALTIJD proberen, ook als de TP hierboven faalde -- zie fix-uitleg.
    sl_coid = f"sl{int(time.time()) % 1000000}"
    sl_result = place_single_order(
        conid=conid, account_id=account_id, action=exit_action,
        quantity=spec.quantity, order_type="STP", aux_price=spec.stop_loss,
        coid=sl_coid,
    )
    sl_order_id = None
    sl_error = None
    if "error" in sl_result:
        sl_error = sl_result["error"]
        logger.error(f"Stop-loss order mislukt voor {symbol}: {sl_error}")
    else:
        sl_order_id = sl_result.get("order_id")

    if tp_order_id is None and sl_order_id is None:
        # Geen van beide gelukt -- de gevaarlijkste situatie, positie
        # volledig onbeschermd. Dit MOET teruggemeld worden zodat
        # execute_managed_trade() een duidelijke Telegram-waarschuwing
        # stuurt voor handmatige controle.
        return {"error": f"TP mislukt: {tp_error}; SL mislukt: {sl_error}", "tp_order_id": None, "sl_order_id": None}

    if tp_order_id is None:
        logger.warning(f"{symbol}: alleen SL geplaatst (order {sl_order_id}) -- TP mislukte, handmatige controle aanbevolen.")
    if sl_order_id is None:
        logger.warning(f"{symbol}: alleen TP geplaatst (order {tp_order_id}) -- SL mislukte, handmatige controle aanbevolen.")

    return {"tp_order_id": tp_order_id, "sl_order_id": sl_order_id}


def monitor_oco_exit(tp_order_id: str | None, sl_order_id: str | None, account_id: str, max_wait_minutes: int = 480) -> dict:
    """
    Bewaakt de TP- en SL-order gelijktijdig (polling). Zodra de één
    vult, annuleert deze functie ACTIEF de andere -- dit is de kern
    van de zelf-beheerde OCO-logica (in plaats van IBKR's ingebouwde
    ocaGroup, die we niet betrouwbaar konden krijgen).

    BELANGRIJKE FIX (21 aug 2026): kan nu ook omgaan met een PARTIAL-
    FAILURE-situatie waarbij slechts ÉÉN van beide order-ID's geldig
    is (de ander None, omdat die order mislukte bij het plaatsen --
    zie place_exit_orders()). In dat geval wordt alleen de geldige
    order bewaakt, zonder te proberen de ontbrekende te annuleren.

    LET OP: nog niet end-to-end getest (de reguliere, beide-orders-
    geldig-situatie); de partial-failure-tak is getest naar aanleiding
    van het live-incident van 21 aug 2026, maar nog niet in een
    daadwerkelijke live partial-failure-situatie herhaald.
    """
    from ibkr_web_api import get_order_status, cancel_order

    if tp_order_id is None and sl_order_id is None:
        logger.error("monitor_oco_exit aangeroepen zonder geldige order-ID's -- kan niets bewaken.")
        return {"result": "no_valid_orders"}

    check_interval = 30
    waited_seconds = 0
    timeout_seconds = max_wait_minutes * 60
    opeenvolgende_fouten = 0
    waarschuwing_verstuurd = False
    # KRITIEKE FIX (25 aug 2026): na 5 opeenvolgende mislukte status-
    # aanroepen (~2,5 minuut bij het huidige interval) een DRINGENDE
    # Telegram-waarschuwing sturen. Live-incident op 25 aug 2026: een
    # verlopen sessie om 01:22 werd 4,5 uur lang stilzwijgend genegeerd
    # (elke fout werd verborgen als order_status="Unknown"), tot de
    # volledige 480-minuten-timeout afliep -- de positie bleek
    # uiteindelijk volledig ONBESCHERMD (geen actieve TP/SL meer) en
    # moest handmatig gesloten worden. Dit voorkomt herhaling: bij een
    # aanhoudend probleem hoor je het nu binnen enkele minuten, niet
    # pas na 8 uur.
    FOUTDREMPEL_VOOR_WAARSCHUWING = 5

    while waited_seconds < timeout_seconds:
        # KRITIEKE TOEVOEGING (25 aug 2026): geforceerde sluiting na de
        # vaste 90-minuten-regel (17:00 CEST) uit het originele
        # strategie-document -- ongeacht TP/SL-status. Voorkomt dat een
        # trade een hele nacht blijft openstaan (zoals bij GOOGL op
        # 24 aug 2026 gebeurde). Bewuste keuze van de gebruiker: een
        # eventueel verlies op dit moment wordt geaccepteerd.
        if datetime.now().time() >= FORCED_CLOSE_TIME:
            logger.warning(f"Geforceerde sluiting (90-minuten-regel, {FORCED_CLOSE_TIME}) bereikt -- positie wordt nu gesloten ongeacht TP/SL-status.")
            return {"result": "forced_close_90min", "tp_status": "closing", "sl_status": "closing"}

        tp_result = get_order_status(tp_order_id) if tp_order_id else {}
        sl_result = get_order_status(sl_order_id) if sl_order_id else {}

        tp_had_fout = tp_order_id is not None and "error" in tp_result
        sl_had_fout = sl_order_id is not None and "error" in sl_result

        if tp_had_fout or sl_had_fout:
            opeenvolgende_fouten += 1
            if opeenvolgende_fouten >= FOUTDREMPEL_VOOR_WAARSCHUWING and not waarschuwing_verstuurd:
                fout_detail = tp_result.get("error") or sl_result.get("error") or "onbekende fout"
                _notify_safe(
                    f"🚨 WAARSCHUWING: kan al {opeenvolgende_fouten}x achtereen de status van de "
                    f"open TP/SL-orders niet ophalen ({fout_detail}). De positie is mogelijk "
                    f"ONBESCHERMD als de sessie is verlopen. Controleer handmatig met /check_ibkr "
                    f"en, indien nodig, /reauth_ibkr of handmatig inloggen."
                )
                waarschuwing_verstuurd = True
                logger.error(f"Aanhoudende fouten bij orderstatus-check ({opeenvolgende_fouten}x) -- waarschuwing verstuurd.")
        else:
            opeenvolgende_fouten = 0  # reset bij een succesvolle aanroep

        tp_status = tp_result.get("order_status", "Unknown")
        sl_status = sl_result.get("order_status", "Unknown")

        if tp_status == "Filled":
            logger.info(f"Take-profit {tp_order_id} gevuld" + (f" -- stop-loss {sl_order_id} annuleren." if sl_order_id else " (geen SL om te annuleren -- die was al mislukt bij plaatsing)."))
            if sl_order_id:
                cancel_order(account_id, sl_order_id)
            return {"result": "take_profit_hit", "tp_status": tp_status, "sl_status": "Cancelled" if sl_order_id else "never_placed"}

        if sl_status == "Filled":
            logger.info(f"Stop-loss {sl_order_id} gevuld" + (f" -- take-profit {tp_order_id} annuleren." if tp_order_id else " (geen TP om te annuleren -- die was al mislukt bij plaatsing)."))
            if tp_order_id:
                cancel_order(account_id, tp_order_id)
            return {"result": "stop_loss_hit", "tp_status": "Cancelled" if tp_order_id else "never_placed", "sl_status": sl_status}

        time.sleep(check_interval)
        waited_seconds += check_interval

    if opeenvolgende_fouten > 0:
        logger.warning(f"Timeout bereikt MET aanhoudende fouten ({opeenvolgende_fouten}x) -- status van de orders is ONZEKER, niet per se 'gewoon niet geraakt'.")
        _notify_safe(
            f"⚠️ Trade-bewaking gestopt na {max_wait_minutes} minuten, MET aanhoudende fouten bij het "
            f"ophalen van de orderstatus. De daadwerkelijke status van deze positie is ONBEKEND -- "
            f"controleer handmatig of de positie nog open staat en of TP/SL nog actief zijn."
        )
    else:
        logger.warning(f"Geen van beide exit-orders gevuld binnen {max_wait_minutes} minuten.")
    return {"result": "timeout", "tp_status": tp_status, "sl_status": sl_status, "had_errors": opeenvolgende_fouten > 0}


def get_actual_fill_price(order_id: str) -> float | None:
    """
    Haalt de EXACTE gevulde prijs op van een order via het
    'average_price'-veld van get_order_status() -- alleen aanwezig
    zodra een order daadwerkelijk gevuld is.

    VERBETERING (24 aug 2026): eerder gebruikte report_trade_outcome()
    alleen de BEOOGDE prijzen (spec.entry_price/take_profit/stop_loss)
    voor de PnL-schatting in Telegram-meldingen -- dat weekt af van
    het werkelijke resultaat bij slippage. Live bevestigd bij een
    NVDA-trade: geschat verlies €20,00, werkelijk verlies (via IBKR's
    portfolio-overzicht) €56,28. Deze functie haalt nu de daadwerkelijke
    fill-prijs op voor een nauwkeurigere melding.

    Returns:
        De gemiddelde fill-prijs als float, of None als de order nog
        niet gevuld is of het veld ontbreekt.
    """
    from ibkr_web_api import get_order_status

    status = get_order_status(order_id)
    avg_price = status.get("average_price")
    if avg_price is None:
        return None
    try:
        return float(avg_price)
    except (ValueError, TypeError):
        return None


def report_trade_outcome(spec: BracketOrderSpec, symbol: str, outcome: dict, entry_order_id: str = None) -> None:
    """
    Rapporteert een afgeronde trade: stuurt een Telegram-melding,
    logt naar het trade journal (CSV), en schrijft naar de
    state-trade-log (voor toekomstige dagstop-functionaliteit).

    Wordt aangeroepen vanuit execute_managed_trade() zodra
    monitor_oco_exit() een resultaat heeft (TP/SL geraakt, of timeout).
    Faalt nooit hard -- een mislukte melding mag de trading-logica
    zelf niet verstoren, dus alle fouten worden gelogd, niet ge-raised.

    VERBETERING (24 aug 2026): gebruikt nu de EXACTE fill-prijzen
    (via get_actual_fill_price()) voor de PnL-berekening, met de
    beoogde prijzen als terugval als de exacte prijs niet opgehaald
    kan worden (bijv. bij een netwerkfout) -- zie moduledocstring-
    achtige toelichting bij get_actual_fill_price() hierboven.
    """
    from journal_module import log_trade, estimate_pnl
    from state_module import add_trade_to_log

    result = outcome.get("result", "unknown")
    # BracketOrderSpec heeft 'action' (BUY/SELL voor de entry), geen
    # los 'direction' veld -- SELL-entry komt overeen met een SHORT-
    # positie, BUY-entry met LONG.
    direction = "SHORT" if spec.action == "SELL" else "LONG"

    if result == "take_profit_hit":
        exit_order_id = outcome.get("tp_order_id_ref")  # zie execute_managed_trade voor doorgifte
        intended_exit_price = spec.take_profit
        emoji = "✅"
    elif result == "stop_loss_hit":
        exit_order_id = outcome.get("sl_order_id_ref")
        intended_exit_price = spec.stop_loss
        emoji = "🛑"
    elif result == "forced_close_90min":
        # 90-minuten-geforceerde sluiting -- de daadwerkelijke exit-
        # prijs komt van de force_close_position()-order, niet van TP/SL.
        exit_order_id = outcome.get("forced_close_order_id")
        intended_exit_price = None  # geen vooraf beoogde prijs, was een marktconforme sluiting
        emoji = "⏰"
    else:
        exit_order_id = None
        intended_exit_price = None
        emoji = "⚠️"

    # Probeer de EXACTE fill-prijzen op te halen; val terug op de
    # beoogde prijzen als dat niet lukt (bijv. netwerkfout, of het
    # 'average_price'-veld ontbreekt om een andere reden).
    actual_entry_price = get_actual_fill_price(entry_order_id) if entry_order_id else None
    actual_exit_price = get_actual_fill_price(exit_order_id) if exit_order_id else None

    used_entry_price = actual_entry_price if actual_entry_price is not None else spec.entry_price
    used_exit_price = actual_exit_price if actual_exit_price is not None else intended_exit_price
    prices_are_exact = actual_entry_price is not None and actual_exit_price is not None

    exit_price = used_exit_price  # behoud oude variabelenaam voor de rest van de functie

    pnl = None
    pnl_net = None
    fees_totaal = None
    if exit_price is not None:
        try:
            from journal_module import estimate_net_pnl
            netto_resultaat = estimate_net_pnl(direction, used_entry_price, exit_price, spec.quantity)
            pnl = netto_resultaat["pnl_gross"]
            pnl_net = netto_resultaat["pnl_net"]
            fees_totaal = netto_resultaat["entry_fee"] + netto_resultaat["exit_fee"]
        except Exception as e:
            logger.error(f"Kon PnL niet berekenen voor {symbol}: {e}")

    # Telegram-melding (faalt stil bij ontbrekende config, zie telegram_notify.py)
    try:
        from telegram_notify import send_telegram_message
        pnl_bron = "exacte fill-prijzen" if prices_are_exact else "schatting o.b.v. beoogde prijzen"
        exit_value = spec.quantity * exit_price if exit_price is not None else None
        exit_value_text = f"€{exit_value:,.2f}" if exit_value is not None else "?"

        if pnl_net is not None:
            resultaat_regel = f"Resultaat: €{pnl:+.2f} bruto | €{pnl_net:+.2f} netto (na €{fees_totaal:.2f} fees) ({pnl_bron})"
        else:
            resultaat_regel = f"Resultaat: onbekend ({pnl_bron})"

        message = (
            f"{emoji} {symbol} {direction}: {result.replace('_', ' ')}\n"
            f"Aantal: {spec.quantity:g}\n"
            f"Entry {used_entry_price:.2f} -> Exit {exit_price if exit_price else '?'} ({exit_value_text})\n"
            f"{resultaat_regel}"
        )
        send_telegram_message(message)
    except Exception as e:
        logger.error(f"Kon Telegram-melding niet versturen voor {symbol}: {e}")

    # Trade journal (CSV, voor latere analyse)
    try:
        # BUGFIX (4 sep 2026): de journal-notitie was HARDGECODEERD op
        # "benadering", ONGEACHT de al-correct-berekende
        # `prices_are_exact`-variabele (die al wél correct werd
        # gebruikt voor de Telegram-melding hierboven). De daadwerkelijk
        # OPGESLAGEN prijzen (used_entry_price/exit_price) waren al
        # correct exact-voorkeurend -- alleen het LABEL loog altijd.
        journal_pnl_note = "exacte fill-prijzen" if prices_are_exact else "benadering o.b.v. beoogde prijzen (fill niet volledig opgehaald)"
        log_trade({
            "symbol": symbol, "direction": direction,
            "entry_price": used_entry_price, "take_profit": spec.take_profit,
            "stop_loss": spec.stop_loss, "quantity": spec.quantity,
            "result": result, "pnl_estimate": pnl,
            "pnl_note": journal_pnl_note,
        })
    except Exception as e:
        logger.error(f"Kon trade niet loggen in journal voor {symbol}: {e}")

    # State trade-log (voor toekomstige dagstop-functionaliteit)
    try:
        add_trade_to_log({
            "symbol": symbol, "direction": direction, "pnl": pnl or 0.0,
        })
    except Exception as e:
        logger.error(f"Kon trade niet loggen in state voor {symbol}: {e}")

    # COMPOUNDING (22 aug 2026, gecorrigeerd 26 aug 2026): het
    # gesimuleerde saldo bijwerken met het NETTO resultaat (na fees)
    # van deze trade -- winst/verlies wordt herbelegd, dus toekomstige
    # positiegroottes zijn gebaseerd op dit nieuwe saldo. Voorheen werd
    # hier het BRUTO bedrag gebruikt, wat het saldo te optimistisch
    # liet compounden -- fees zijn een reëel, terugkerend verlies op
    # elke trade, ongeacht de uitkomst.
    try:
        from state_module import update_simulated_balance
        if pnl_net is not None:
            update_simulated_balance(pnl_net)
    except Exception as e:
        logger.error(f"Kon gesimuleerd saldo niet bijwerken voor {symbol}: {e}")

    # Positie uit de open-positielijst verwijderen -- de trade is nu afgerond.
    try:
        from state_module import remove_position
        remove_position(symbol)
    except Exception as e:
        logger.error(f"Kon positie niet verwijderen uit state voor {symbol}: {e}")


def _notify_safe(message: str) -> None:
    """
    Fail-safe wrapper om send_telegram_message() -- een mislukte
    melding mag de trading-logica nooit onderbreken. Gebruikt door
    execute_managed_trade() op elk belangrijk beslispunt (order
    geplaatst, entry gevuld/niet gevuld, exit-orders mislukt).
    """
    try:
        from telegram_notify import send_telegram_message
        send_telegram_message(message)
    except Exception as e:
        logger.error(f"Kon Telegram-melding niet versturen: {e}")


def execute_managed_trade(spec: BracketOrderSpec, symbol: str, max_fill_wait_minutes: float = None) -> dict:
    """
    Orkestreert de volledige zelf-beheerde OCO-flow: entry plaatsen ->
    wachten op fill -> TP/SL plaatsen -> bewaken tot de één de ander
    triggert. Dit is de functie die main.py aanroept in plaats van de
    eerdere (niet-werkende) place_bracket_order().

    LET OP: dit is een LANGLOPENDE, BLOKKERENDE functie -- kan uren
    duren als de markt traag beweegt. Voor gebruik binnen main.py's
    cron-gebaseerde cyclus moet dit mogelijk worden losgekoppeld naar
    een apart proces per open positie, in plaats van main.py zelf te
    laten wachten. Dit ontwerp-punt is nog niet opgelost.
    """
    entry_result = place_entry_order(spec, symbol)
    if "error" in entry_result:
        _notify_safe(f"❌ {symbol}: entry-order plaatsen mislukt -- {entry_result['error']}")
        return {"status": "entry_failed", "symbol": symbol, "reason": entry_result["error"]}

    order_id = entry_result["order_id"]
    account_id = entry_result["account_id"]
    conid = entry_result["conid"]

    # Bedragen voor in de melding: totale positiewaarde en het bedrag
    # dat daadwerkelijk op het spel staat als de stop-loss geraakt wordt.
    position_value = spec.quantity * spec.entry_price
    risk_amount = abs(spec.entry_price - spec.stop_loss) * spec.quantity

    _notify_safe(
        f"📤 {symbol} {spec.action}: order geplaatst @ {spec.entry_price:.2f}\n"
        f"Aantal: {spec.quantity:g} | Investering: €{position_value:,.2f}\n"
        f"TP {spec.take_profit:.2f} / SL {spec.stop_loss:.2f} | Risico: €{risk_amount:,.2f}"
    )

    # NIEUW (3 sep 2026, bugfix): de fill-wachttijd wordt nu begrensd
    # door de RESTERENDE tijd tot de 90-minuten-strategiedeadline
    # (indien meegegeven door de aanroeper), i.p.v. altijd de eigen,
    # losstaande ORDER_TIMEOUT_MINUTES te gebruiken -- loste een live
    # gevonden bug op waarbij een entry gevonden vlak vóór de deadline
    # alsnog tot 14 minuten NA de deadline kon vullen (META, 3 sep 2026).
    if max_fill_wait_minutes is not None:
        fill_status = wait_for_entry_fill(order_id, account_id, timeout_minutes=max_fill_wait_minutes)
    else:
        fill_status = wait_for_entry_fill(order_id, account_id)
    if fill_status != "Filled":
        _notify_safe(f"⏱️ {symbol}: entry niet gevuld binnen de tijdslimiet ({fill_status}) -- order geannuleerd.")
        return {"status": "entry_not_filled", "symbol": symbol, "fill_status": fill_status}

    # Positie registreren in state.json zodra de entry bevestigd is
    # gevuld -- zodat /status in de Telegram-bot deze trade toont
    # terwijl hij nog open staat.
    direction = "SHORT" if spec.action == "SELL" else "LONG"
    try:
        from state_module import add_position
        add_position({
            "symbol": symbol, "direction": direction,
            "entry_price": spec.entry_price, "quantity": spec.quantity,
        })
    except Exception as e:
        logger.error(f"Kon positie niet registreren in state voor {symbol}: {e}")

    # KRITIEKE FIX (31 aug 2026): dit bericht toonde voorheen ALTIJD
    # spec.entry_price (de BEOOGDE limietprijs), terwijl het latere,
    # definitieve eindresultaat-bericht de EXACTE fill-prijs toont --
    # bij slippage (positief of negatief) leek dat een discrepantie
    # tussen twee verschillende "entry-prijzen" voor dezelfde trade,
    # terwijl het gewoon twee verschillende meetmomenten/-bronnen
    # waren. Nu wordt de exacte fill-prijs opgehaald en getoond, met
    # een duidelijke terugval + label als dat niet lukt.
    actual_fill_price = get_actual_fill_price(order_id)
    entry_prijs_voor_bericht = actual_fill_price if actual_fill_price is not None else spec.entry_price
    prijs_label = "" if actual_fill_price is not None else " (beoogd, exacte fill-prijs nog niet beschikbaar)"

    # KRITIEKE FIX (1 sep 2026): TP/SL herberekenen op basis van de
    # WERKELIJKE fill-prijs, niet de oorspronkelijk beoogde entry-prijs
    # -- bij aanzienlijke slippage (bv. MSFT, 500.53 beoogd vs. 505.32
    # werkelijk) kon de stop-loss anders al bij het openen van de
    # positie feitelijk overschreden zijn, waardoor de trade vrijwel
    # onmiddellijk moest sluiten. Behoudt dezelfde AFSTANDEN (en dus
    # dezelfde 2:1 R/R-verhouding), alleen het ankerpunt verschuift.
    if actual_fill_price is not None and abs(actual_fill_price - spec.entry_price) > 0.001:
        from dataclasses import replace
        tp_afstand = abs(spec.take_profit - spec.entry_price)
        sl_afstand = abs(spec.entry_price - spec.stop_loss)
        if direction == "LONG":
            nieuwe_tp = actual_fill_price + tp_afstand
            nieuwe_sl = actual_fill_price - sl_afstand
        else:
            nieuwe_tp = actual_fill_price - tp_afstand
            nieuwe_sl = actual_fill_price + sl_afstand
        logger.info(
            f"Slippage gedetecteerd voor {symbol}: beoogd {spec.entry_price:.2f} -> werkelijk "
            f"{actual_fill_price:.2f}. TP/SL herberekend: TP {spec.take_profit:.2f}->{nieuwe_tp:.2f}, "
            f"SL {spec.stop_loss:.2f}->{nieuwe_sl:.2f} (afstanden behouden)."
        )
        spec = replace(spec, entry_price=actual_fill_price, take_profit=round(nieuwe_tp, 2), stop_loss=round(nieuwe_sl, 2))

    _notify_safe(
        f"✅ {symbol} {direction}: entry gevuld @ {entry_prijs_voor_bericht:.2f}{prijs_label} "
        f"(€{position_value:,.2f} ingezet) -- TP/SL worden geplaatst."
    )

    exit_result = place_exit_orders(spec, symbol, conid, account_id)
    if "error" in exit_result:
        _notify_safe(
            f"❌ {symbol}: ZOWEL TP als SL plaatsen mislukt na gevulde entry -- "
            f"{exit_result['error']}. URGENT: positie is volledig ONBESCHERMD, handmatig ingrijpen vereist."
        )
        return {"status": "exit_orders_failed", "symbol": symbol, "reason": exit_result["error"]}

    # Partial-failure-situatie (één van beide gelukt, de ander niet) --
    # minder ernstig dan beide mislukt, maar wel een expliciete melding
    # waard zodat je weet dat de positie slechts gedeeltelijk beschermd is.
    if exit_result["tp_order_id"] is None:
        _notify_safe(f"⚠️ {symbol}: TP-order mislukt, alleen SL actief (order {exit_result['sl_order_id']}). Controleer handmatig of een TP alsnog gewenst is.")
    elif exit_result["sl_order_id"] is None:
        _notify_safe(f"⚠️ {symbol}: SL-order mislukt, alleen TP actief (order {exit_result['tp_order_id']}). URGENT: positie heeft geen stop-loss, controleer handmatig.")

    outcome = monitor_oco_exit(exit_result["tp_order_id"], exit_result["sl_order_id"], account_id)
    # Order-ID's toevoegen aan de outcome, zodat report_trade_outcome()
    # de exacte fill-prijs van de GERAAKTE exit-order kan opzoeken.
    outcome["tp_order_id_ref"] = exit_result["tp_order_id"]
    outcome["sl_order_id_ref"] = exit_result["sl_order_id"]

    # KRITIEKE TOEVOEGING (25 aug 2026): geforceerde sluiting na de
    # 90-minuten-regel -- annuleer de openstaande TP/SL-orders en sluit
    # de positie ACTIEF met een marktconforme order, in plaats van de
    # positie onbeheerd achter te laten (het GOOGL-incident van
    # 24 aug 2026). Bewuste keuze van de gebruiker: een eventueel
    # verlies op dit moment wordt geaccepteerd -- dat is de bedoelde
    # aard van deze scalping-strategie.
    if outcome.get("result") == "forced_close_90min":
        forced_outcome = force_close_position(spec, symbol, conid, account_id, exit_result)
        outcome.update(forced_outcome)

    if outcome.get("result") in ("take_profit_hit", "stop_loss_hit", "forced_close_90min"):
        report_trade_outcome(spec, symbol, outcome, entry_order_id=order_id)

    return {"status": "trade_complete", "symbol": symbol, **outcome}


def force_close_position(spec: BracketOrderSpec, symbol: str, conid: int, account_id: str, exit_result: dict) -> dict:
    """
    Voert de 90-minuten-geforceerde-sluiting daadwerkelijk uit:
    annuleert de openstaande TP/SL-orders, en sluit de positie met een
    marktconforme limietorder in de tegengestelde richting.

    Conform het originele strategie-document ("scalper", handelt
    binnen 90 minuten na marktopening) en de bewuste keuze van de
    gebruiker op 25 aug 2026: een eventueel verlies op dit moment
    wordt geaccepteerd, in ruil voor de zekerheid dat er NOOIT meer
    een positie een hele nacht onbeheerd blijft staan.

    LET OP: gebruikt de laatst bekende marktprijs (spec.entry_price
    als referentie, met een kleine marge) om de sluitorder snel te
    laten vullen -- niet noodzakelijk de daadwerkelijke actuele
    marktprijs op het moment van sluiten, aangezien we die hier niet
    apart ophalen. Bij een sterk bewogen markt kan de daadwerkelijke
    fill-prijs afwijken van deze schatting.
    """
    from ibkr_web_api import place_single_order, cancel_order, get_order_status

    if exit_result.get("tp_order_id"):
        cancel_order(account_id, exit_result["tp_order_id"])
    if exit_result.get("sl_order_id"):
        cancel_order(account_id, exit_result["sl_order_id"])

    close_action = "BUY" if spec.action == "SELL" else "SELL"
    # Marge van 0,5% richting een snelle fill, conform de aanpak die
    # we ook bij handmatige sluitingen deze week gebruikten.
    close_price = spec.entry_price * (1.005 if close_action == "BUY" else 0.995)

    result = place_single_order(
        conid=conid, account_id=account_id, action=close_action,
        quantity=spec.quantity, order_type="LMT", price=round(close_price, 2),
    )

    if "error" in result:
        logger.error(f"Geforceerde sluiting mislukt voor {symbol}: {result['error']}")
        _notify_safe(
            f"🚨 URGENT: geforceerde 90-minuten-sluiting MISLUKT voor {symbol} -- "
            f"{result['error']}. Positie is mogelijk nog open, controleer HANDMATIG direct."
        )
        return {"forced_close_status": "failed", "forced_close_error": result["error"]}

    order_id = result.get("order_id")
    _notify_safe(
        f"⏰ {symbol} ({spec.action}): "
        f"90-minuten-regel bereikt -- positie wordt nu geforceerd gesloten "
        f"(TP/SL niet op tijd geraakt, conform de strategie)."
    )

    try:
        from state_module import remove_position
        remove_position(symbol)
    except Exception as e:
        logger.error(f"Kon positie niet verwijderen uit state voor {symbol}: {e}")

    return {"forced_close_status": "submitted", "forced_close_order_id": order_id}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    from exit_module import calculate_exit_levels
    from entry_module import generate_entry_signal
    from data_module import Candle
    from datetime import datetime

    # Scenario 1: SHORT-order opbouwen (geen live IBKR nodig)
    bullish_candle = Candle(
        timestamp=datetime(2026, 8, 14, 9, 0),
        open=100.0, high=102.0, low=99.5, close=101.5, volume=15000,
    )
    signal = generate_entry_signal(bullish_candle)
    plan = calculate_exit_levels(signal, capital=1000.0)
    spec = build_bracket_orders(plan, symbol="ASML")
    print(f"Scenario 1 (SHORT order-spec): {spec.to_dict()}")

    # Scenario 2: LONG-order opbouwen
    bearish_candle = Candle(
        timestamp=datetime(2026, 8, 14, 9, 0),
        open=101.5, high=102.0, low=99.5, close=100.0, volume=15000,
    )
    signal = generate_entry_signal(bearish_candle)
    plan = calculate_exit_levels(signal, capital=500.0)
    spec = build_bracket_orders(plan, symbol="AAPL")
    print(f"Scenario 2 (LONG order-spec): {spec.to_dict()}")

    # Scenario 3: te kleine positiegrootte -> moet een fout geven
    try:
        onhaalbaar_plan = calculate_exit_levels(signal, capital=1.0)
        build_bracket_orders(onhaalbaar_plan, symbol="AAPL")
    except ValueError as e:
        print(f"Scenario 3 (verwachte fout): {e}")

    print("\n--- Constructie-tests klaar. Live tests vereisen een geauthenticeerde Gateway-sessie: ---")
    print("  python3 -c \"from order_module import execute_managed_trade, build_bracket_orders; ...\"")

    # Scenario 4: report_trade_outcome testen (met tijdelijke state/journal-paden)
    import tempfile
    import state_module
    import journal_module

    with tempfile.TemporaryDirectory() as tmp_dir:
        state_module.STATE_FILE_PATH = f"{tmp_dir}/state.json"
        journal_module.JOURNAL_PATH = f"{tmp_dir}/journal.csv"

        tp_spec = build_bracket_orders(plan, symbol="TESTSYM")
        report_trade_outcome(tp_spec, "TESTSYM", {"result": "take_profit_hit"})

        with open(journal_module.JOURNAL_PATH) as f:
            journal_inhoud = f.read()
        print(f"\nScenario 4 (report_trade_outcome, TP hit): journal bevat 'TESTSYM' = {'TESTSYM' in journal_inhoud}")

        summary = state_module.get_performance_summary()
        print(f"State trade_log bijgewerkt: trade_count = {summary['trade_count']}")
