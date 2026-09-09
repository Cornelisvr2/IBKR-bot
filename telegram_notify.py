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


# ---------------------------------------------------------------------------
# NIEUW (9 sep 2026, op verzoek): meldingen ROUTEREN i.p.v. alles naar
# Telegram. Sinds TTS+QFS+RVB samen draaien is de stroom te groot.
#
#   URGENT   -> Telegram + gebeurtenissenlog   (🚨 en ❌: onbeschermde
#               positie, sessie-herstel mislukt, order plaatsen mislukt)
#   WARNING  -> alleen gebeurtenissenlog        (⚠️)
#   INFO     -> alleen gebeurtenissenlog        (al het overige: order
#               geplaatst, entry gevuld, TP/SL geraakt, dry-run-signalen)
#
# De gebeurtenissenlog (logs/events.jsonl) wordt per dag getoond op het
# dashboard. Bestaande aanroepen hoeven niet aangepast: het niveau volgt
# uit de emoji waarmee elk bericht al begint. Wil je een bericht tóch
# altijd op Telegram (bv. het dagrapport), geef dan urgent=True mee.
#
# Omgevingsvariabele TELEGRAM_LEVEL:
#   urgent  (standaard) -> alleen 🚨/❌ naar Telegram
#   warning             -> ook ⚠️
#   all                 -> oud gedrag, alles naar Telegram
# ---------------------------------------------------------------------------

EVENT_LOG_PATH = os.environ.get("EVENT_LOG_FILE", "/opt/strategy/logs/events.jsonl")
_URGENT_PREFIXES = ("🚨", "❌")
_WARNING_PREFIXES = ("⚠️", "⚠")


def classify_level(text: str) -> str:
    t = text.lstrip()
    if t.startswith(_URGENT_PREFIXES):
        return "urgent"
    if t.startswith(_WARNING_PREFIXES):
        return "warning"
    return "info"


def log_event(level: str, text: str, sent_to_telegram: bool = False,
              strategy: str = "", symbol: str = "") -> None:
    """Schrijft één regel naar logs/events.jsonl -- nooit ge-raised."""
    import json
    from datetime import datetime
    try:
        os.makedirs(os.path.dirname(EVENT_LOG_PATH), exist_ok=True)
        rij = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "level": level, "text": text, "telegram": sent_to_telegram,
        }
        if strategy:
            rij["strategy"] = strategy
        if symbol:
            rij["symbol"] = symbol
        with open(EVENT_LOG_PATH, "a") as f:
            f.write(json.dumps(rij, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error(f"Kon gebeurtenis niet loggen: {e}")


def log_decision(text: str, strategy: str = "", symbol: str = "") -> None:
    """
    NIEUW (9 sep 2026, op verzoek): BESLISSINGEN van de bot vastleggen
    in dezelfde gebeurtenissenlog als de meldingen, met niveau
    "decision" -- nooit naar Telegram, altijd op het dashboard. Bedoeld
    voor elke stap waarop de bot iets kiest of afwijst: symbool
    overgeslagen (waarom), box gevonden, hamer-opstelling wacht op
    bevestiging, patroon bevestigd/vervallen, trade gedispatcht, enz.
    Zo is per dag terug te lezen WAAROM er wel/niet gehandeld is.
    """
    log_event("decision", text, sent_to_telegram=False, strategy=strategy, symbol=symbol)


def send_telegram_message(text: str, parse_mode: str = None, urgent: bool = False,
                          strategy: str = "", symbol: str = "") -> bool:
    """
    Routeert een melding: logt hem ALTIJD in de gebeurtenissenlog en
    verstuurt hem alleen naar Telegram als het niveau dat rechtvaardigt
    (zie toelichting hierboven). Faalt stil met een gelogde
    waarschuwing als Telegram niet bereikbaar is.

    Returns:
        True als de melding naar Telegram is verstuurd, anders False
        (dus ook False als hij bewust alleen gelogd is).
    """
    level = "urgent" if urgent else classify_level(text)
    drempel = os.environ.get("TELEGRAM_LEVEL", "urgent").lower()
    naar_telegram = (
        level == "urgent"
        or (drempel == "warning" and level == "warning")
        or drempel == "all"
    )
    if not naar_telegram:
        log_event(level, text, sent_to_telegram=False, strategy=strategy, symbol=symbol)
        logger.info(f"Melding ({level}) alleen gelogd, niet naar Telegram: {text[:80]}")
        return False

    verstuurd = _send_raw(text, parse_mode)
    log_event(level, text, sent_to_telegram=verstuurd, strategy=strategy, symbol=symbol)
    return verstuurd


def _send_raw(text: str, parse_mode: str = None) -> bool:
    """De daadwerkelijke Telegram-aanroep (oude send_telegram_message)."""
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
