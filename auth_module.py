"""
auth_module.py — Touch & Turn Scalper, IBKR Authenticatiebewaking

IBKR vereist elke ~24 uur een nieuwe sessie-authenticatie. Deze module:

    1. Checkt of de huidige IBeam-sessie nog geldig is.
    2. Triggert, indien nodig, een nieuwe authenticatie-aanvraag --
       dit stuurt een goedkeuringsverzoek naar je IBKR-app (IB Key of
       Mobile Authenticator), maar KAN dat verzoek niet zelf goedkeuren.
       Dat blijft een bewuste, menselijke handeling (het hele punt van
       2FA).
    3. Informeert je via Telegram zodra actie nodig is, en opnieuw
       zodra bekend is of het gelukt is.

Bedoeld om dagelijks via cron te draaien, ruim vóór de handelsvensters,
zodat je de tijd hebt om te reageren voordat er getrade moet worden.

LET OP: de functies die daadwerkelijk ibeam_starter.py aanroepen
(check_ibkr_authenticated, trigger_ibkr_authenticate) zijn NIET
end-to-end getest -- vereisen een live IBeam/IBKR-omgeving die niet
beschikbaar was tijdens de bouw van deze module. De parsing-logica
(parse_ibeam_check_output) is wel los getest, zie __main__.

============================================================
BELANGRIJKE VALKUIL (ontdekt 24 aug 2026) -- GEBRUIK NOOIT POORT
5001 ALS LOKALE SSH-TUNNELPOORT OP DEZE VPS.
============================================================
IBeam gebruikt intern poort 5001 voor zijn eigen health-server
(los van de Gateway zelf, die op 5000 draait). Als je handmatig
inlogt via een SSH-tunnel met `ssh -L 5001:127.0.0.1:5000 ...`
(bijvoorbeeld omdat poort 5000 al bezet leek), ontstaat er een
poortconflict: elke aanroep van `ibeam_starter.py --check` of
`--authenticate` crasht dan METEEN met "OSError: Address already
in use" -- VOORDAT het ooit de daadwerkelijke sessiestatus kan
controleren.

Het verraderlijke: dit gaf de MISLEIDENDE indruk dat de IBKR-sessie
ongeldig was (check_ibkr_authenticated() gaf steeds False terug),
terwijl de sessie in werkelijkheid prima geldig was -- het
controlerende proces zelf kon simpelweg nooit starten. Dit kostte
een lange sessie aan verwarrend debuggen (Selenium-patches,
Live/Paper-mismatches, timeouts) voordat de werkelijke, simpele
oorzaak (een poortconflict) werd gevonden.

GEBRUIK ALTIJD EEN ANDERE LOKALE TUNNELPOORT, bijv.:
    ssh -L 5555:127.0.0.1:5000 root@<VPS-IP>
en dan in de browser naar https://localhost:5555 -- NOOIT
poort 5000 of 5001 als het EERSTE (lokale) getal in -L.

Gebruik:
    from auth_module import check_ibkr_authenticated, trigger_ibkr_authenticate
"""

from __future__ import annotations

import logging
import os
import subprocess
import time

logger = logging.getLogger("auth_module")

# Pad naar ibeam_starter.py -- geverifieerd in eerdere sessie op deze
# VPS (versie 0.5.8). Aanpasbaar via env-variabele voor het geval het
# pad verandert bij een IBeam-update.
IBEAM_STARTER_PATH = os.environ.get(
    "IBEAM_STARTER_PATH",
    "/usr/local/lib/python3.12/dist-packages/ibeam/ibeam_starter.py",
)


def parse_ibeam_check_output(returncode: int, stdout: str) -> bool:
    """
    Bepaalt op basis van de output van `ibeam_starter.py --check` of
    de sessie geldig is.

    Losstaand van de daadwerkelijke subprocess-aanroep, dus volledig
    testbaar met voorbeeld-output.

    LET OP: de exacte output-vorm van IBeam's --check is niet
    geverifieerd tegen een live sessie -- deze functie gaat uit van
    een exitcode van 0 bij een geldige sessie (gangbare Unix-conventie
    voor CLI-tools), wat bij het live testen bevestigd moet worden.
    """
    return returncode == 0


def check_ibkr_authenticated(timeout: int = 10) -> bool:
    """
    Checkt of de huidige IBKR-sessie geldig is via een RECHTSTREEKSE
    HTTP-aanroep naar de Gateway's /iserver/auth/status endpoint --
    GEEN subprocess/Selenium meer.

    BELANGRIJKE HERSTRUCTURERING (24 aug 2026): de oorspronkelijke
    aanpak (dit subprocess `ibeam_starter.py --check` laten draaien en
    de output lezen) bleek STRUCTUREEL te falen wanneer aangeroepen
    vanuit een systemd-service-context (zoals de Telegram-bot) --
    zelfs met een timeout tot 60 seconden, en zelfs al werkte exact
    dezelfde code altijd probleemloos in een interactieve SSH-sessie.
    Bevestigd gereproduceerd via `systemd-run --pipe --wait ...`.
    Vermoedelijke oorzaak: het ontbreken van een gekoppelde TTY
    beïnvloedt hoe IBeam's Xvfb/Selenium-onderdelen zich gedragen,
    ook al zou een simpele statuscheck dat niet nodig moeten hebben.

    Deze nieuwe aanpak omzeilt dat probleem volledig: een simpele
    GET-aanroep naar de Gateway zelf, zonder ooit een nieuw subprocess,
    browser, of Selenium-sessie te starten. Dit is exact de aanpak die
    de hele dag door handmatig debuggen betrouwbaar bleek te werken
    (zie ibkr_web_api.py's andere functies, die dezelfde _get_session()
    gebruiken).

    Returns:
        True als de sessie geldig is, False bij ongeldig of een fout.
    """
    from ibkr_web_api import _get_session, BASE_URL

    session = _get_session()
    try:
        response = session.get(f"{BASE_URL}/iserver/auth/status", timeout=timeout)
        if response.status_code != 200:
            logger.warning(f"IBKR-sessiecheck gaf status {response.status_code} -- sessie waarschijnlijk ongeldig.")
            return False

        data = response.json()
        authenticated = bool(data.get("authenticated", False))
        logger.info(f"IBKR-sessie status: {'geldig' if authenticated else 'verlopen/ongeldig'}")
        return authenticated
    except Exception as e:
        logger.error(f"Kon IBKR-authenticatiestatus niet checken: {e}")
        return False


def trigger_ibkr_authenticate(timeout: int = 180) -> dict:
    """
    Triggert een nieuwe authenticatie-aanvraag.

    GROTE HERSTRUCTURERING (26 aug 2026): gebruikt niet langer IBeam's
    `--authenticate`-commando, dat na dagenlang debuggen bleek te
    lijden aan meerdere, moeilijk te doorgronden problemen (Live/Paper-
    toggle-mismatch, TimeoutExceptions -- zie TODO_Selenium_Auth.md
    voor de volledige geschiedenis). In plaats daarvan:

        1. Probeer eerst de SNELLE route: een simpele ssodh/init-
           aanroep, die werkt zolang de onderliggende IBKR-sessie nog
           "warm" genoeg is (vaak het geval, bleek deze week
           herhaaldelijk te werken zonder ooit een browser nodig te
           hebben).
        2. Alleen als dat niet werkt: gebruik ons EIGEN, volledig
           zelfgebouwde en bevestigd werkende Selenium-loginscript
           (custom_ibkr_login.py) -- dat controleert expliciet de
           Live/Paper-toggle-status vóór het klikken (in plaats van
           IBeam's blinde klik-en-hoop-aanpak), en gebruikt de
           daadwerkelijk juiste inlogknop-selector
           (.xyz-button-login, niet .btn-primary zoals IBeam's
           configuratie ten onrechte aannam).

    Returns:
        dict met "triggered" (bool) en "message" (str, voor Telegram).
    """
    from ibkr_web_api import _get_session, BASE_URL

    # Stap 1: de snelle route.
    try:
        session = _get_session()
        response = session.post(
            f"{BASE_URL}/iserver/auth/ssodh/init",
            json={"compete": True, "publish": True}, timeout=15,
        )
        if response.status_code == 200 and response.json().get("authenticated"):
            message = "Authenticatie gelukt via de snelle route (geen browser-login nodig)."
            logger.info(message)
            return {"triggered": True, "message": message}
    except Exception as e:
        logger.info(f"Snelle route niet gelukt ({e}) -- val terug op het volledige loginscript.")

    # Stap 2: het volledige, eigen loginscript.
    try:
        from custom_ibkr_login import login
        result = login()
        if result["success"]:
            logger.info(f"Authenticatie gelukt via het eigen loginscript: {result['message']}")
            return {"triggered": True, "message": f"Authenticatie gelukt (volledige login). {result['message']}"}
        else:
            logger.error(f"Eigen loginscript mislukt: {result['message']}")
            return {"triggered": False, "message": f"Authenticatie mislukt: {result['message']}"}
    except Exception as e:
        message = f"Fout bij het uitvoeren van het loginscript: {e}"
        logger.error(message)
        return {"triggered": False, "message": message}


def run_daily_auth_check(capital_context: str = "") -> dict:
    """
    De dagelijkse cyclus die cron aanroept: checkt de sessie, en
    logt ZELF automatisch opnieuw in als de sessie verlopen is --
    GEEN handmatige tussenkomst meer nodig.

    HERONTWERP (26 aug 2026): voorheen stuurde deze functie altijd een
    Telegram-melding bij een verlopen sessie, met het verzoek om zelf
    /reauth_ibkr te sturen en daarna te bevestigen -- expliciet
    ongewenst gebleken: "ik wil niet elke keer handmatig re-authen...
    als het systeem er niet in kan mag hij dit van mij zelf gewoon
    doen." Nu:

        1. Sessie geldig -> niets doen, geen melding (zoals voorheen)
        2. Sessie verlopen -> ZELF automatisch trigger_ibkr_authenticate()
           aanroepen (die intern eerst de snelle route probeert, dan
           het eigen, betrouwbare Selenium-loginscript)
        3. Gelukt -> KORTE succesbevestiging (geen actie van jou nodig)
        4. Mislukt -> DUIDELIJKE, dringende foutmelding (dít is het
           enige moment waarop jij daadwerkelijk zelf moet ingrijpen,
           bijv. via de handmatige tunnel-procedure)

    Returns:
        dict met de uitkomst, bruikbaar voor logging.
    """
    from telegram_notify import send_telegram_message

    if check_ibkr_authenticated():
        logger.info("IBKR-sessie is geldig -- geen actie nodig.")
        return {"status": "already_authenticated"}

    logger.warning("IBKR-sessie verlopen -- automatisch opnieuw inloggen.")
    result = trigger_ibkr_authenticate()

    if result["triggered"]:
        logger.info(f"Automatische her-authenticatie geslaagd: {result['message']}")
        send_telegram_message(f"✅ IBKR-sessie automatisch hersteld. {result['message']}")
        return {"status": "reauth_success", "triggered": True}
    else:
        logger.error(f"Automatische her-authenticatie MISLUKT: {result['message']}")
        send_telegram_message(
            f"🚨 IBKR-sessie verlopen EN automatisch herstel is mislukt: {result['message']}\n\n"
            f"HANDMATIGE ACTIE NODIG: log in via de SSH-tunnel-procedure "
            f"(zie TODO_Selenium_Auth.md) en bevestig daarna met /check_ibkr."
        )
        return {"status": "reauth_failed", "triggered": False}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Scenario 1-3: parse-logica testen zonder live IBeam nodig
    print(f"Scenario 1 (returncode 0, geldig): {parse_ibeam_check_output(0, 'authenticated')}")
    print(f"Scenario 2 (returncode 1, ongeldig): {parse_ibeam_check_output(1, 'not authenticated')}")
    print(f"Scenario 3 (returncode 0, lege output): {parse_ibeam_check_output(0, '')}")

    print("\n--- Dry-run parsing-tests klaar. ---")
    print("Live tests (vereisen IBeam op de VPS):")
    print("  python3 -c \"from auth_module import check_ibkr_authenticated; print(check_ibkr_authenticated())\"")
    print("  python3 -c \"from auth_module import run_daily_auth_check; run_daily_auth_check()\"")	
