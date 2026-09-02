"""
custom_ibkr_login.py — Eigen IBKR-loginscript (los van IBeam se automatisering)

VERVANGT IBeam's `--authenticate`-commando voor de Selenium-loginflow.
Gebouwd na herhaalde, moeilijk te doorgronden problemen met IBeam's
eigen automatisering (Live/Paper-toggle-mismatch, TimeoutExceptions --
zie TODO_Selenium_Auth.md voor de volledige geschiedenis).

KERNVERSCHIL met IBeam's aanpak: dit script CHECKT EERST de huidige
stand van de Live/Paper-toggle (via het element `#toggle1`, bevestigd
via browser-devtools-onderzoek op 25 aug 2026: checked=True betekent
Paper, checked=False betekent Live) en klikt ALLEEN als de huidige
stand niet overeenkomt met de gewenste stand -- in plaats van IBeam's
"klik altijd, submit, en bij falen klik nogmaals en submit opnieuw"-
aanpak, die vermoedelijk een race condition veroorzaakte tussen de
klik en de daadwerkelijke serverzijdige validatie.

Gebruikt dezelfde, al geïnstalleerde Selenium/Xvfb/Chromium-omgeving
die IBeam ook gebruikt -- geen nieuwe systeemafhankelijkheden nodig.

Gebruik:
    python3 custom_ibkr_login.py
    (of importeer login() vanuit een ander script/cron-wrapper)
"""

from __future__ import annotations

import logging
import os
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s|%(levelname)-.1s| %(message)s",
)
logger = logging.getLogger("custom_ibkr_login")

LOGIN_URL = "https://localhost:5000/sso/Login?forwardTo=22&RL=1&ip2loc=on"
BASE_URL = "https://127.0.0.1:5000/v1/api"
PAGE_LOAD_TIMEOUT = 20
ELEMENT_WAIT_TIMEOUT = 15
SUCCESS_WAIT_TIMEOUT = 20


def login(timeout: int = 90) -> dict:
    """
    Voert de volledige loginflow uit: browser openen, gebruikersnaam/
    wachtwoord invullen, de Paper-toggle CONTROLEREN en zo nodig
    corrigeren, indienen, en de uitkomst detecteren.

    Returns:
        dict met "success" (bool) en "message" (str).
    """
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException
    from pyvirtualdisplay import Display

    username = os.environ.get("IB_USER")
    password = os.environ.get("IB_PASSWORD")
    if not username or not password:
        return {"success": False, "message": "IB_USER of IB_PASSWORD ontbreekt in de omgeving."}

    display = Display(visible=0, size=(1280, 1024))
    display.start()

    options = webdriver.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--ignore-certificate-errors")  # zelfondertekend certificaat van de Gateway
    # DERDE FIX (26 aug 2026): "DevToolsActivePort file doesn't exist" --
    # een bekend, veelvoorkomend probleem bij Chrome/Chromium in een
    # VPS/container-omgeving, meestal door een crash tijdens het
    # opstarten. Deze extra vlaggen lossen dat doorgaans op.
    options.add_argument("--remote-debugging-port=9222")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--headless=new")
    # KRITIEKE FIX (26 aug 2026): zonder dit pikte Selenium's ingebouwde
    # "Selenium Manager" automatisch een EIGEN, los gedownloade
    # Chrome-versie (152.x, in /root/.cache/selenium/) die niet
    # overeenkwam met de systeem-chromedriver (151.x) -- dit gaf een
    # SessionNotCreatedException. Door hier expliciet het systeem-
    # Chromium-binair aan te wijzen, gebruikt Selenium dezelfde,
    # bijpassende versie als de chromedriver op /usr/bin/chromedriver.
    options.binary_location = "/snap/bin/chromium"

    # TWEEDE FIX (26 aug 2026): zelfs met matchende versienummers
    # (beide 151.0.7922.108) gaf de systeembrede /usr/bin/chromedriver
    # nog een "Wrong browser/driver version"-fout tegen de snap-
    # verpakte Chromium -- vermoedelijk een net-niet-identieke interne
    # build ondanks het gelijke versienummer. Gebruik daarom expliciet
    # de chromedriver die BIJ DEZELFDE snap-package hoort.
    from selenium.webdriver.chrome.service import Service
    import glob
    snap_chromedriver_paths = glob.glob("/snap/chromium/*/usr/lib/chromium-browser/chromedriver")
    service = Service(executable_path=snap_chromedriver_paths[0]) if snap_chromedriver_paths else None

    if service:
        logger.info(f"Snap-eigen chromedriver gebruikt: {service.path}")
        driver = webdriver.Chrome(options=options, service=service)
    else:
        logger.warning("Geen snap-chromedriver gevonden -- terugval op standaard Selenium-detectie.")
        driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)

    try:
        logger.info(f"Loginpagina laden: {LOGIN_URL}")
        driver.get(LOGIN_URL)

        wait = WebDriverWait(driver, ELEMENT_WAIT_TIMEOUT)

        # Stap 1: gebruikersnaam invullen
        username_el = wait.until(EC.presence_of_element_located((By.NAME, "username")))
        username_el.clear()
        username_el.send_keys(username)
        logger.info("Gebruikersnaam ingevuld.")

        # Stap 2: wachtwoord invullen
        password_el = wait.until(EC.presence_of_element_located((By.NAME, "password")))
        password_el.clear()
        password_el.send_keys(password)
        logger.info("Wachtwoord ingevuld.")

        # Stap 3: KERNVERSCHIL met IBeam -- eerst de HUIDIGE toggle-status
        # lezen, alleen klikken als correctie nodig is.
        try:
            toggle_el = driver.find_element(By.ID, "toggle1")
            huidige_status = driver.execute_script("return arguments[0].checked;", toggle_el)
            # checked=True betekent Paper (bevestigd via devtools-onderzoek
            # op 25 aug 2026) -- we willen ALTIJD Paper voor dit account.
            gewenste_status = True

            logger.info(f"Toggle-status vóór correctie: checked={huidige_status} (True=Paper, False=Live)")

            if huidige_status != gewenste_status:
                logger.info("Toggle staat niet op Paper -- klikken om te corrigeren.")
                try:
                    toggle_el.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", toggle_el)

                time.sleep(1)  # even wachten tot de klik daadwerkelijk verwerkt is
                nieuwe_status = driver.execute_script("return arguments[0].checked;", toggle_el)
                logger.info(f"Toggle-status ná klik: checked={nieuwe_status}")

                if nieuwe_status != gewenste_status:
                    return {"success": False, "message": f"Kon de toggle niet naar Paper zetten (checked={nieuwe_status})."}
            else:
                logger.info("Toggle stond al correct op Paper -- geen klik nodig.")
        except Exception as e:
            logger.warning(f"Kon toggle-status niet controleren/corrigeren: {e} -- doorgaan met inloggen zoals de pagina standaard staat.")

        # Stap 4: submit-knop indienen -- NOOIT vergeten, zoals expliciet gevraagd.
        #
        # KRITIEKE ONTDEKKING (26 aug 2026): de daadwerkelijke,
        # ZICHTBARE inlogknop heeft class 'btn-danger' met tekst
        # 'Simulated Login' -- NIET 'btn-primary' zoals zowel onze
        # eerdere aanname als IBeam's eigen configuratie
        # (SUBMIT_EL: 'CSS_SELECTOR@@.btn.btn-lg.btn-primary')
        # veronderstelden. Alle 'btn-primary'-knoppen op deze pagina
        # bleken ONZICHTBAAR (displayed=False) -- vermoedelijk
        # gereserveerd voor andere loginstappen (2FA-varianten) die nu
        # niet relevant zijn. Dit verklaart mogelijk een deel van de
        # eerdere, moeilijk te doorgronden IBeam-problemen.
        try:
            submit_el = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".xyz-button-login")))
        except Exception:
            # DIAGNOSE: als de verwachte knop niet gevonden wordt, toon
            # alle daadwerkelijk aanwezige knoppen op de pagina.
            alle_knoppen = driver.find_elements(By.TAG_NAME, "button")
            knop_info = [f"class='{b.get_attribute('class')}' text='{b.text}' displayed={b.is_displayed()}" for b in alle_knoppen]
            logger.error(f"Submit-knop niet gevonden. Alle <button>-elementen op de pagina:\n" + "\n".join(knop_info))
            return {"success": False, "message": f"Submit-knop niet gevonden. Zie logs voor alle beschikbare knoppen."}
        try:
            submit_el.click()
        except Exception:
            driver.execute_script("arguments[0].click();", submit_el)
        logger.info("Formulier ingediend.")

        # Korte, expliciete pauze zodat de pagina daadwerkelijk de tijd
        # krijgt om te reageren op de indiening, vóórdat we de inhoud
        # proberen te lezen (ontdekt op 26 aug 2026: zonder deze pauze
        # lazen we soms nog de kale, ongewijzigde pagina-structuur uit).
        time.sleep(2)

        # Stap 5: wachten op een duidelijke uitkomst -- succes of foutmelding.
        try:
            success_wait = WebDriverWait(driver, SUCCESS_WAIT_TIMEOUT)
            success_wait.until(
                lambda d: "Client login succeeds" in d.page_source
                or d.find_elements(By.CSS_SELECTOR, ".xyz-errormessage")
            )
        except TimeoutException:
            return {"success": False, "message": f"Geen duidelijke uitkomst binnen {SUCCESS_WAIT_TIMEOUT}s na indienen."}

        if "Client login succeeds" in driver.page_source:
            logger.info("Client login succeeds -- browserlogin geslaagd.")
        else:
            foutmelding_el = driver.find_elements(By.CSS_SELECTOR, ".xyz-errormessage")
            fouttekst = foutmelding_el[0].text if foutmelding_el else ""
            if not fouttekst:
                # DIAGNOSE (26 aug 2026): de eerste 3000 tekens bleken
                # altijd de vaste head-sectie te zijn (scripts, CSS-
                # imports), ongeacht de daadwerkelijke status --
                # zoek gerichter naar herkenbare statustekst, en toon
                # ALLEEN het relevante fragment eromheen.
                pagina = driver.page_source
                for zoekterm in ["Client login succeeds", "error", "Error", "twofactbase", "IB Key",
                                  "incorrect", "Incorrect", "failed", "Failed", "Simulated Login"]:
                    idx = pagina.find(zoekterm)
                    if idx != -1:
                        fragment = pagina[max(0, idx-200):idx+300]
                        logger.error(f"Zoekterm '{zoekterm}' gevonden op positie {idx}:\n...{fragment}...")
                pagina_bevat_2fa = "twofactbase" in pagina or "IB Key" in pagina
                logger.error(f"Bevat de pagina mogelijk een 2FA-scherm? {pagina_bevat_2fa}")
                logger.error(f"Totale paginalengte: {len(pagina)} tekens")
            return {"success": False, "message": f"Login mislukt: {fouttekst or '(lege foutmelding, zie logs voor pagina-inhoud)'}"}

    finally:
        driver.quit()
        display.stop()

    # Stap 6: de CRUCIALE, eerder ontdekte stap -- de API-sessie
    # activeren via ssodh/init. Zonder dit gaf onze eigen requests-
    # sessie (los van de browsersessie) herhaaldelijk een 401, ook na
    # een zichtbaar geslaagde browserlogin.
    try:
        from ibkr_web_api import _get_session, BASE_URL as API_BASE_URL
        session = _get_session()
        response = session.post(
            f"{API_BASE_URL}/iserver/auth/ssodh/init",
            json={"compete": True, "publish": True}, timeout=15,
        )
        if response.status_code == 200 and response.json().get("authenticated"):
            logger.info("API-sessie succesvol geactiveerd via ssodh/init.")
            return {"success": True, "message": "Volledig ingelogd, API-sessie actief."}
        else:
            return {"success": False, "message": f"Browserlogin gelukt, maar ssodh/init gaf onverwacht resultaat: {response.status_code} {response.text}"}
    except Exception as e:
        return {"success": False, "message": f"Browserlogin gelukt, maar ssodh/init faalde: {e}"}


if __name__ == "__main__":
    result = login()
    print(result)
