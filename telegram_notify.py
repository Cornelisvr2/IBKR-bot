"""
telegram_notify.py — Touch & Turn Scalper, Proactieve Telegram-meldingen

Los van telegram_bot.py (dat luistert naar inkomende commando's via
python-telegram-bot's polling), biedt deze module een simpele functie
om VANUIT andere scripts (main.py, auth_module.py) proactief een
bericht te versturen -- bijvoorbeeld "IBKR-sessie verlopen" of een
dagafsluitings-samenvatting.

Gebruikt een rechtstreekse HTTP-aanroep naar Telegram's sendMessage-
endpoint, zodat er geen zware afhankelijkheid nodig is op de
python-telegram-bot-library puur om een enkel bericht te versturen.

Gebruik:
    from telegram_notify import send_telegram_message
    send_telegram_message("IBKR-sessie verlopen, authenticatie nodig.")
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("telegram_notify")


def send_telegram_photo(photo_path: str, caption: str = None) -> bool:
    """
    Verstuurt een afbeelding (bv. een trade-grafiek uit chart_module.py)
    naar TELEGRAM_CHAT_ID, met optioneel een bijschrift. Gebruikt
    Telegram's sendPhoto-eindpunt (multipart file-upload), in
    tegenstelling tot send_telegram_message()'s eenvoudige JSON-POST.

    Faalt stil met een gelogde waarschuwing bij ontbrekende
    env-variabelen of een mislukte aanroep -- een mislukte grafiek mag
    nooit de rest van de trade-afhandeling blokkeren.
    """
    import requests

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN of TELEGRAM_CHAT_ID ontbreekt -- afbeelding niet verstuurd.")
        return False

    if not os.path.exists(photo_path):
        logger.error(f"Kan afbeelding niet versturen -- bestand bestaat niet: {photo_path}")
        return False

    url = f"https://api.telegram.org/bot{token}/sendPhoto"

    try:
        with open(photo_path, "rb") as f:
            files = {"photo": f}
            data = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption
            response = requests.post(url, data=data, files=files, timeout=20)
        response.raise_for_status()
        logger.info(f"Telegram-afbeelding verstuurd: {photo_path}")
        return True
    except Exception as e:
        logger.error(f"Kon Telegram-afbeelding niet versturen: {e}")
        return False


def send_telegram_message(text: str, parse_mode: str = None) -> bool:
    """
    Verstuurt een bericht naar TELEGRAM_CHAT_ID via de bot met
    TELEGRAM_BOT_TOKEN. Faalt stil met een gelogde waarschuwing als
    de vereiste omgevingsvariabelen ontbreken of de aanroep mislukt --
    een ontbrekende melding mag nooit de rest van een cyclus laten
    crashen.

    LET OP: vereist een live internetverbinding -- niet end-to-end
    getest in de sandbox waarin dit gebouwd is (geen internettoegang
    daar). Test dit als eerste, apart, op de VPS.

    Args:
        text: berichttekst
        parse_mode: optioneel "Markdown" of "HTML" voor opmaak

    Returns:
        True bij (waarschijnlijk) succes, False bij een bekende fout.
    """
    import requests

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN of TELEGRAM_CHAT_ID ontbreekt -- melding niet verstuurd.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        logger.info("Telegram-melding verstuurd.")
        return True
    except Exception as e:
        logger.error(f"Kon Telegram-melding niet versturen: {e}")
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import sys

    if "--live" in sys.argv:
        # Echte test: verstuurt een testbericht naar je eigen chat.
        success = send_telegram_message("Testbericht vanuit telegram_notify.py -- als je dit ziet, werkt het.")
        print(f"Verstuurd: {success}")
    else:
        # Dry-run: test de fail-safe zonder env-variabelen te vereisen.
        import os as _os
        old_token = _os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        old_chat = _os.environ.pop("TELEGRAM_CHAT_ID", None)

        result = send_telegram_message("dit zou niet verstuurd moeten worden")
        print(f"Scenario 1 (ontbrekende env-variabelen, moet False zijn): {result}")

        if old_token:
            _os.environ["TELEGRAM_BOT_TOKEN"] = old_token
        if old_chat:
            _os.environ["TELEGRAM_CHAT_ID"] = old_chat

        print("\n--- Dry-run klaar. Live testen met: python3 telegram_notify.py --live ---")
