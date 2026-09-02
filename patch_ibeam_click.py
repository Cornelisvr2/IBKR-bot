"""
patch_ibeam_click.py — Eenmalig patch-script voor IBeam's Selenium-klikbug

Vervangt de twee kwetsbare `submit_form_el.click()`-aanroepen in
IBeam's login_handler.py door een versie met een JavaScript-klik-
fallback. Dit is de vermoedelijke oorzaak van de ElementNotInteractable-
Exception die de hele dag (19-20 aug 2026) de geautomatiseerde login
(`ibeam_starter.py --authenticate`) liet mislukken -- terwijl handmatig
inloggen via de browser altijd wel lukte, wat duidt op een tim­ing/
overlay-probleem dat Selenium's normale klik-methode raakt, maar een
directe JavaScript-klik meestal omzeilt.

LET OP: dit patcht een geïnstalleerd site-package direct. Bij een
toekomstige `pip install --upgrade ibeam` wordt deze patch overschreven
en moet dit script opnieuw gedraaid worden.

Gebruik (eenmalig, als root op de VPS):
    python3 patch_ibeam_click.py
"""

import re

TARGET_FILE = "/usr/local/lib/python3.12/dist-packages/ibeam/src/handlers/login_handler.py"

OLD_LINE = "        submit_form_el.click()"
NEW_BLOCK = """        try:
            submit_form_el.click()
        except Exception as click_error:
            # Fallback: JavaScript-klik, omzeilt vaak een
            # ElementNotInteractableException die door een overlay
            # of animatie-timing wordt veroorzaakt. Toegevoegd via
            # patch_ibeam_click.py op 20 aug 2026.
            self.driver.execute_script("arguments[0].click();", submit_form_el)"""


def main():
    with open(TARGET_FILE, "r") as f:
        content = f.read()

    count = content.count(OLD_LINE)
    print(f"Gevonden: {count} exemplaren van de kwetsbare klik-regel.")

    if count == 0:
        print("Niets te patchen -- mogelijk al gepatcht, of de regel is anders dan verwacht.")
        return

    # Check of er al gepatcht is (voorkomt dubbele patch bij herhaald draaien)
    if "patch_ibeam_click.py" in content:
        print("Bestand lijkt al gepatcht te zijn -- geen actie ondernomen.")
        return

    new_content = content.replace(OLD_LINE, NEW_BLOCK)

    # Backup maken voordat we het originele bestand overschrijven
    backup_path = TARGET_FILE + ".backup_voor_patch"
    with open(backup_path, "w") as f:
        f.write(content)
    print(f"Backup gemaakt: {backup_path}")

    with open(TARGET_FILE, "w") as f:
        f.write(new_content)

    print(f"Patch toegepast op {TARGET_FILE} -- {count} regel(s) vervangen.")
    print("Test nu met: python3 /usr/local/lib/python3.12/dist-packages/ibeam/ibeam_starter.py --authenticate --verbose")


if __name__ == "__main__":
    main()
