"""
telegram_bot.py — Touch & Turn Scalper, Telegram Bot

Biedt commando's om de strategie op afstand te bedienen:
    /ibkr_stop_trading       -- pauzeert nieuwe trades onmiddellijk
    /ibkr_start_trading       -- hervat trading
    /ibkr_status               -- huidige status + open posities
    /ibkr_performance          -- winrate en totaal resultaat
    /ibkr_update_risk <pct>    -- past risicopercentage per trade aan (bijv. 0.5)

Architectuur: de kernlogica van elk commando (cmd_*) is een pure
functie die een tekstantwoord teruggeeft, volledig los van de
Telegram-library -- daarom hier zonder een live Telegram-verbinding
te testen (zie __main__). De dunne async-wrappers onderaan koppelen
deze functies aan python-telegram-bot; DIE laag is nog niet getest,
want de library kon niet worden geïnstalleerd in de sandbox waarin
dit is gebouwd (geen internettoegang) -- test dit op de VPS zelf.

Alleen de eigenaar (TELEGRAM_CHAT_ID) mag commando's uitvoeren --
elk commando controleert dit eerst.

Vereist op de VPS:
    pip3 install python-telegram-bot --break-system-packages

Omgevingsvariabelen (al gezet in ~/.bashrc):
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
"""

from __future__ import annotations

import logging
import os

from state_module import (
    load_state,
    set_trading_enabled,
    set_risk_pct,
    get_performance_summary,
)

logger = logging.getLogger("telegram_bot")

OWNER_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def is_authorized(chat_id: str | int) -> bool:
    """
    Controleert of het binnenkomende bericht van de eigenaar afkomstig is.
    Vergelijkt als string, want Telegram chat_id's zijn getallen maar
    TELEGRAM_CHAT_ID komt als string uit de omgevingsvariabele.
    """
    if OWNER_CHAT_ID is None:
        logger.error("TELEGRAM_CHAT_ID is niet gezet -- alle commando's worden geweigerd.")
        return False
    return str(chat_id) == str(OWNER_CHAT_ID)


# ---------------------------------------------------------------------
# Kernlogica per commando -- pure functies, geen Telegram-library nodig.
# ---------------------------------------------------------------------

def cmd_stop_trading(chat_id: str | int) -> str:
    if not is_authorized(chat_id):
        return "Niet geautoriseerd."
    set_trading_enabled(False)
    return "Trading gepauzeerd. Geen nieuwe trades tot /ibkr_start_trading."


def cmd_start_trading(chat_id: str | int) -> str:
    if not is_authorized(chat_id):
        return "Niet geautoriseerd."
    set_trading_enabled(True)
    return "Trading hervat. Nieuwe trades worden weer geopend."


def cmd_status(chat_id: str | int) -> str:
    if not is_authorized(chat_id):
        return "Niet geautoriseerd."
    state = load_state()
    positions = state.get("positions", [])

    # TOEVOEGING (26 aug 2026): het gedeelde compounding-saldo tonen --
    # ontbrak eerder, terwijl dit sinds vandaag het daadwerkelijke,
    # actuele kapitaal is dat beide strategieën gebruiken (i.p.v. een
    # vast bedrag), dus waardevolle info om direct te zien.
    from state_module import get_simulated_balance
    saldo = get_simulated_balance()

    lines = [
        f"Trading: {'AAN' if state['trading_enabled'] else 'GEPAUZEERD'}",
        f"Gesimuleerd saldo (compounding): €{saldo:.2f}",
        f"Risico per trade: {state['risk_pct'] * 100:.2f}%",
        f"Open posities: {len(positions)}",
    ]
    for pos in positions:
        lines.append(
            f"  - {pos.get('symbol', '?')} {pos.get('direction', '?')} "
            f"@ {pos.get('entry_price', '?')}"
        )
    return "\n".join(lines)


def cmd_performance(chat_id: str | int) -> str:
    if not is_authorized(chat_id):
        return "Niet geautoriseerd."
    summary = get_performance_summary()
    return (
        f"Trades: {summary['trade_count']}\n"
        f"Winrate: {summary['win_rate']}%\n"
        f"Totaal resultaat: €{summary['total_pnl']:.2f}"
    )


def cmd_update_risk(chat_id: str | int, args: list[str]) -> str:
    if not is_authorized(chat_id):
        return "Niet geautoriseerd."
    if not args:
        return "Gebruik: /ibkr_update_risk <percentage>  (bijv. /ibkr_update_risk 0.5 voor 0,5%)"

    try:
        pct_value = float(args[0]) / 100  # gebruiker geeft procenten op, bijv. "0.5" -> 0.005
        set_risk_pct(pct_value)
        return f"Risico per trade aangepast naar {pct_value * 100:.2f}%."
    except ValueError as e:
        return f"Ongeldige waarde: {e}"


def cmd_check_ibkr(chat_id: str | int) -> str:
    if not is_authorized(chat_id):
        return "Niet geautoriseerd."
    from auth_module import check_ibkr_authenticated, trigger_ibkr_authenticate

    try:
        authenticated = check_ibkr_authenticated()
        if authenticated:
            return "IBKR-sessie is geldig, geen actie nodig."

        # AANPASSING (26 aug 2026): consistent met de expliciete wens
        # om NIET meer handmatig te hoeven her-authenticeren -- bij een
        # verlopen sessie lost /ibkr_check het nu ZELF meteen op, in
        # plaats van te vragen om een los /ibkr_reauth-commando.
        result = trigger_ibkr_authenticate()
        if result["triggered"]:
            return f"IBKR-sessie was verlopen, automatisch hersteld. {result['message']}"
        else:
            return f"IBKR-sessie was verlopen EN automatisch herstel is mislukt: {result['message']}"
    except Exception as e:
        return f"Kon status niet checken: {e}"


def cmd_reauth_ibkr(chat_id: str | int) -> str:
    if not is_authorized(chat_id):
        return "Niet geautoriseerd."
    from auth_module import trigger_ibkr_authenticate

    try:
        result = trigger_ibkr_authenticate()
        return result["message"]
    except Exception as e:
        return f"Kon authenticatie niet triggeren: {e}"


# ---------------------------------------------------------------------
# Telegram-specifieke wrappers (python-telegram-bot, v20+ async-stijl).
# NIET getest in deze sessie -- library kon niet worden geïnstalleerd
# zonder internettoegang. Test dit op de VPS met: python3 telegram_bot.py
# ---------------------------------------------------------------------

def run_bot():
    """Start de bot en blijft luisteren naar commando's (blocking call)."""
    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is niet gezet in de omgeving.")

    async def handle_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = cmd_stop_trading(update.effective_chat.id)
        await update.message.reply_text(text)

    async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = cmd_start_trading(update.effective_chat.id)
        await update.message.reply_text(text)

    async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = cmd_status(update.effective_chat.id)
        await update.message.reply_text(text)

    async def handle_performance(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = cmd_performance(update.effective_chat.id)
        await update.message.reply_text(text)

    async def handle_update_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = cmd_update_risk(update.effective_chat.id, context.args)
        await update.message.reply_text(text)

    async def handle_check_ibkr(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = cmd_check_ibkr(update.effective_chat.id)
        await update.message.reply_text(text)

    async def handle_reauth_ibkr(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = cmd_reauth_ibkr(update.effective_chat.id)
        await update.message.reply_text(text)

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("ibkr_stop_trading", handle_stop))
    app.add_handler(CommandHandler("ibkr_start_trading", handle_start))
    app.add_handler(CommandHandler("ibkr_status", handle_status))
    app.add_handler(CommandHandler("ibkr_performance", handle_performance))
    app.add_handler(CommandHandler("ibkr_update_risk", handle_update_risk))
    app.add_handler(CommandHandler("ibkr_check", handle_check_ibkr))
    app.add_handler(CommandHandler("ibkr_reauth", handle_reauth_ibkr))

    logger.info("Telegram-bot gestart, wacht op commando's...")
    app.run_polling()


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    # KRITIEKE FIX (26 aug 2026): het script draaide voorheen ALTIJD
    # de onderstaande tests, ongeacht welke argumenten werden
    # meegegeven -- inclusief `--live`, wat bedoeld was om de
    # daadwerkelijke, doorlopende bot-service te starten. Dit betekende
    # dat de bot-service in werkelijkheid NOOIT als live bot draaide:
    # elke herstart voerde de tests uit en sloot daarna netjes af
    # (ontdekt op 26 aug 2026 doordat /ibkr_status niets teruggaf -- de
    # service was "actief" volgens systemd tijdens het opstarten, maar
    # stopte meteen weer na de tests, in plaats van te blijven luisteren).
    if "--live" in sys.argv:
        run_bot()
        sys.exit(0)

    import tempfile
    import os as _os
    import state_module

    # Test de kernlogica met een tijdelijk statusbestand en een
    # gesimuleerde owner chat_id, zonder de Telegram-library nodig te
    # hebben.
    with tempfile.TemporaryDirectory() as tmp_dir:
        test_state_path = _os.path.join(tmp_dir, "test_state.json")
        # state_module zoekt STATE_FILE_PATH dynamisch op bij elke
        # aanroep (zie fix in state_module.py), dus deze aanpassing
        # is voldoende om load_state()/save_state() zonder expliciet
        # pad-argument naar het testbestand te laten schrijven.
        state_module.STATE_FILE_PATH = test_state_path

        os.environ["TELEGRAM_CHAT_ID"] = "999999"
        OWNER_CHAT_ID = "999999"  # noqa: F841 (voor de duidelijkheid van de test)
        globals()["OWNER_CHAT_ID"] = "999999"

        # Scenario 1: niet-geautoriseerde gebruiker
        print(f"Scenario 1 (verkeerde chat_id): {cmd_status('111111')}")

        # Scenario 2: status vóór wijzigingen
        print(f"Scenario 2 (initiële status): {cmd_status('999999')}")

        # Scenario 3: trading stoppen
        print(f"Scenario 3 (stop): {cmd_stop_trading('999999')}")
        print(f"   Status daarna: {cmd_status('999999')}")

        # Scenario 4: trading hervatten
        print(f"Scenario 4 (herstart): {cmd_start_trading('999999')}")

        # Scenario 5: risico aanpassen
        print(f"Scenario 5 (risico): {cmd_update_risk('999999', ['0.5'])}")

        # Scenario 6: ongeldige risico-invoer
        print(f"Scenario 6 (ongeldig risico): {cmd_update_risk('999999', ['abc'])}")

        # Scenario 7: performance zonder trades
        print(f"Scenario 7 (performance, leeg): {cmd_performance('999999')}")
