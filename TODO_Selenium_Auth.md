# TODO: Selenium-authenticatie repareren

*Vastgelegd: 24 augustus 2026, na een lange debug-sessie*

## Status samengevat

De **detectie** van je IBKR-sessiestatus werkt nu betrouwbaar (poort-5001-conflict opgelost, zie `auth_module.py`'s moduledocstring voor de volledige uitleg). De **automatische her-authenticatie** via Selenium (`ibeam_starter.py --authenticate`) werkt echter nog steeds niet betrouwbaar. Praktisch gevolg: bij een verlopen sessie krijg je een correcte Telegram-melding, maar moet je nog steeds **handmatig** inloggen via de SSH-tunnel + browser (zie procedure onderaan).

---

## Twee nog openstaande, losse problemen

### Probleem 1 — Live/Paper-modus-mismatch bij `step_paper_toggle`

**Foutmelding (herhaald, 10x geprobeerd voordat het proces opgaf):**
```
Error displayed by the login webpage: You have selected the Live Account Mode,
but the specified user is a Paper Trading user. Please select the correct Login mode.
```

**Wat al geprobeerd is:**
- `IBEAM_USE_PAPER_ACCOUNT=True` als omgevingsvariabele — leek de eerste keer te helpen (geen mismatch-melding meer), maar leidde toen naar Probleem 2 in plaats van een succesvolle login
- De JS-klik-fallback-patch (zie `patch_ibeam_click.py`, al toegepast op `login_handler.py` regel 201/318/332) loste een aparte `AttributeError`/`ElementNotInteractableException` op, maar niet dit Live/Paper-probleem zelf

**Vermoedelijke oorzaak (niet bevestigd):** de toggle-klik zelf lijkt te "lukken" (geen crash), maar de paginastatus (wat de pagina intern registreert als geselecteerde modus) lijkt niet mee te veranderen — een soort desynchronisatie tussen de visuele klik en de onderliggende JavaScript-state.

**Suggestie voor volgende keer:** inspecteer de pagina via de browser's ontwikkelaarstools (F12 → Elements) **terwijl je zelf bent ingelogd** via de tunnel, en zoek specifiek naar:
- Het exacte DOM-element/attribuut dat de Live/Paper-status bijhoudt (mogelijk een `data-`-attribuut, een verborgen `<input>`-veld, of een JavaScript-variabele)
- Of de toggle een `change`-event vereist (niet alleen een `click`) om de onderliggende state bij te werken — dat zou verklaren waarom een kale JS-`click()` niet voldoende is

### Probleem 2 — `TimeoutException` bij `step_login`

**Foutmelding:**
```
File "login_handler.py", line 67, in _wait_and_identify_trigger
    trigger = WebDriverWait(driver, timeout).until(any_of(*expected_conditions))
selenium.common.exceptions.TimeoutException
```

Dit gebeurde **na** het invullen van gebruikersnaam/wachtwoord, tijdens het wachten op een van de verwachte volgende-stap-indicatoren (succesbericht, foutmelding, 2FA-scherm, etc.) — geen daarvan verscheen binnen de ingestelde timeout.

**Nog niet onderzocht:** wat de exacte `timeout`-waarde is in deze `WebDriverWait`-aanroep, en of de pagina simpelweg langzamer laadt dan verwacht op deze VPS (vergelijkbaar met eerdere page-load-timeoutproblemen deze week), of dat er een structureel andere oorzaak is (bijv. een gewijzigde paginastructuur bij IBKR sinds IBeam's laatste update).

**Suggestie voor volgende keer:**
- Zoek de exacte `timeout`-parameter op in `login_handler.py`'s `_wait_and_identify_trigger`-aanroep en overweeg deze te verhogen (vergelijkbaar met de patches die deze week al zijn toegepast)
- Screenshot-functionaliteit aanzetten (`IBEAM_ERROR_SCREENSHOTS=True`, eerder deze week al eens gebruikt) om te zien wat er daadwerkelijk op het scherm stond op het moment van de timeout

---

## Werkende tijdelijke oplossing (gebruik dit voorlopig)

Zodra je een Telegram-melding krijgt over een verlopen sessie:

1. **Open een NIEUW Command Prompt-venster** op je eigen pc (niet 5000 of 5001 als lokale poort!):
   ```
   ssh -L 5555:127.0.0.1:5000 root@<VPS-IP>
   ```
2. **In je browser:** `https://localhost:5555`
3. **Log in**: gebruikersnaam, wachtwoord, controleer dat de toggle op **Paper** staat, klik in
4. **Als er een 2FA-verzoek komt**, keur dat direct goed op je telefoon
5. **BELANGRIJKE, CRUCIALE STAP** (tweemaal bevestigd nodig op 24 aug 2026): een succesvolle **browserlogin** activeert NIET automatisch de **API-sessie** waar `auth_module.py` mee praat. Voer daarna ALTIJD expliciet deze aanroep uit:
   ```bash
   cd /opt/strategy
   python3 -c "
   from ibkr_web_api import _get_session, BASE_URL
   session = _get_session()
   response = session.post(f'{BASE_URL}/iserver/auth/ssodh/init', json={'compete': True, 'publish': True}, timeout=15)
   print(response.status_code)
   print(response.text)
   "
   ```
   Verwacht: `200` met `"authenticated":true` in de JSON-respons.
6. **Terug in je VPS-sessie, bevestig:**
   ```bash
   python3 -c "from auth_module import check_ibkr_authenticated; print(check_ibkr_authenticated())"
   ```
   Verwacht: `True`

---

## Belangrijke, al-vastgelegde valkuil (niet opnieuw tegenkomen)

**Gebruik NOOIT poort 5001 als lokale SSH-tunnelpoort** — die botst met IBeam's interne health-server en veroorzaakt een misleidende "sessie ongeldig"-melding terwijl de sessie feitelijk prima werkt. Gebruik altijd 5555 of een ander willekeurig nummer. Volledige uitleg staat in `auth_module.py`'s moduledocstring.

---

## Tweede valkuil (ontdekt 24 aug 2026): de Telegram-bot-service herladen geen gewijzigde code automatisch

**Symptoom:** `/check_ibkr` in Telegram bleef "VERLOPEN" tonen, terwijl een rechtstreekse terminal-check (`python3 -c "from auth_module import check_ibkr_authenticated; ..."`) al bevestigde `True` teruggaf.

**Oorzaak:** `tts-telegram-bot.service` draaide al 5 dagen onafgebroken, sinds ver vóór de `auth_module.py`-fixes van vandaag. Een lang lopend Python-proces leest een gewijzigd bestand niet automatisch opnieuw in.

**REGEL: herstart de service ALTIJD na het bijwerken van een bestand dat de bot gebruikt** (met name `auth_module.py`, `state_module.py`, `order_module.py`, of `telegram_bot.py` zelf):
```bash
systemctl restart tts-telegram-bot
```
Verifieer:
```bash
systemctl status tts-telegram-bot
```
(let op de "Active since"-tijd — die moet na je laatste code-wijziging liggen)

---

## Prioriteit

**Niet urgent.** Het systeem functioneert prima met de handmatige tunnel-route als vangnet — dit is een comfort-verbetering (volledige "zet-en-vergeet"-automatisering), geen blokkerend probleem. Pak dit op een moment dat er geen tijdsdruk is, zodat er rustig met de browser-ontwikkelaarstools geëxperimenteerd kan worden.

---

## Probleem 3 (ontdekt 24 aug 2026): `/check_ibkr` faalt structureel binnen de Telegram-bot-service, ook al werkt dezelfde code perfect in een interactieve SSH-sessie

**Symptoom:** `check_ibkr_authenticated()` geeft **altijd** `False` (met "geen bepalende regel ontvangen binnen Xs") wanneer aangeroepen vanuit de live-draaiende `tts-telegram-bot.service`, zelfs met een timeout van 60 seconden. Dezelfde functie, dezelfde code, rechtstreeks in een SSH-terminal aangeroepen, werkt **altijd** direct correct.

**Bevestigd gereproduceerd** door de check te draaien via `systemd-run` (simuleert een systemd-service-context zonder TTY):
```bash
systemd-run --uid=root --pipe --wait python3 -c "
import sys
sys.path.insert(0, '/opt/strategy')
from auth_module import check_ibkr_authenticated
print(check_ibkr_authenticated())
"
```
→ faalt consistent, ook bij 60s timeout.

**Vermoedelijke oorzaak (niet bevestigd):** het ontbreken van een gekoppelde TTY/terminal in een systemd-service-context beïnvloedt mogelijk hoe Xvfb/Selenium/Chromium (die IBeam intern gebruikt, ook voor `--check`) zich opstarten of hun output produceren — bijvoorbeeld via `pyvirtualdisplay`, of een input/output-aanname die een terminal veronderstelt.

**Praktische impact:** `/check_ibkr` en `/reauth_ibkr` in Telegram zijn op dit moment **onbetrouwbaar** (geven mogelijk altijd "verlopen" terug, ook als de sessie geldig is) — vertrouw voorlopig op een rechtstreekse terminal-check in plaats van de Telegram-commando's, tot dit is opgelost.

**Suggesties voor volgende keer:**
- Onderzoek of `ibeam_starter.py --check` zelf al een Xvfb/Selenium-sessie opstart (had dat niet zo hoeven zijn voor een simpele status-check) — mogelijk is er een lichtgewicht alternatief (rechtstreeks een HTTP-aanroep naar de Gateway's `/iserver/auth/status`-endpoint, zoals we handmatig deden met `_get_session()`, in plaats van het IBeam-subprocess te gebruiken voor de CHECK-functie specifiek)
- Dat zou zelfs een robuustere, snellere oplossing kunnen zijn dan het huidige subprocess-gebaseerde `check_ibkr_authenticated()`: vervang de hele functie door een rechtstreekse aanroep naar `ibkr_web_api.py`'s `_get_session()` + een GET naar `/iserver/auth/status`, zoals we handmatig deden toen we het poort-5001-probleem debugden. Dat vermijdt subprocess/Selenium/TTY-complicaties volledig.

**STATUS: OPGELOST (24 aug 2026, later diezelfde sessie).** `check_ibkr_authenticated()` is
herbouwd volgens exact de hierboven voorgestelde aanpak -- een directe HTTP GET naar
`/iserver/auth/status` via `ibkr_web_api._get_session()`, geen subprocess/Selenium meer.
Bevestigd werkend, ook binnen de Telegram-bot-service.

---

## Probleem 4 (onderzocht 25 aug 2026): Live/Paper-toggle-mechaniek -- gedeeltelijk doorgrond, nog niet definitief opgelost

**Context:** Probleem 1 (Live/Paper-mismatch bij `step_paper_toggle`) is deze dag verder
onderzocht via de browser-ontwikkelaarstools, zoals destijds gesuggereerd.

### Bevindingen (bevestigd via browserconsole)

- Het toggle-element is `<input type="checkbox" name="paperSwitch" id="toggle1">`
- **`checked: false` = Live** (de standaardstand bij het laden van de inlogpagina)
- **`checked: true` = Paper**
- Een **handmatige muisklik** op het element flipt de status correct (zowel de
  `checked`-property als de visuele weergave)
- Een **directe JavaScript-klik** (`document.getElementById('toggle1').click()`) via de
  browserconsole flipt de status **ook** correct -- dus de klik-mechaniek zelf is niet het
  probleem (dit ontkracht de eerdere hypothese dat JS-klikken door React niet correct
  zouden worden opgepikt)
- Gebruikersnaam/wachtwoord-velden blijven ingevuld staan na een toggle-klik (dus geen
  paginaherlaadprobleem)

### Waarom dit nog niet definitief getest kon worden

Om de daadwerkelijke Selenium-aangedreven `step_paper_toggle()`-flow te diagnosticeren
(met een toegevoegde diagnostische logregel die de `checked`-status direct na IBeam's
eigen klik uitleest), moest de VOLLEDIGE loginflow geforceerd worden -- dat vereist een
daadwerkelijk VERLOPEN onderliggende IBKR-sessie.

**Onverwachte ontdekking**: de "snelle REST-route" (`/iserver/auth/ssodh/init` na een
simpele `tickle`-check, zonder Selenium) bleek **meerdere keren op rij** te slagen, zelfs
na een expliciete `/logout`-aanroep vanuit onze eigen code. Dit suggereert dat de
onderliggende IBKR-sessie (op accountniveau, niet gebonden aan onze lokale cookie) langer
"warm" blijft dan gedacht -- mogelijk uren tot een dag, ongeacht hoe vaak wij lokaal
uitloggen.

**Praktische implicatie, mogelijk positief**: dit zou kunnen betekenen dat de
Selenium-route in de praktijk VEEL minder vaak nodig is dan we aannamen -- de meeste
her-authenticaties lukken mogelijk al via de snelle route. Het Live/Paper-toggle-probleem
zou dan vooral relevant zijn na een écht langdurig verlopen sessie (bijv. na een heel
weekend, of na een Gateway-herstart).

### Diagnostische code al klaarstaand voor de volgende keer

Er is al een diagnostische logregel toegevoegd aan
`/usr/local/lib/python3.12/dist-packages/ibeam/src/handlers/login_handler.py`, direct na
de toggle-klik in `step_paper_toggle()`:
```python
try:
    toggle_status = driver.execute_script("return document.getElementById('toggle1').checked;")
    _LOGGER.info(f'DIAGNOSE: toggle1.checked na klik = {toggle_status}')
except Exception as diag_error:
    _LOGGER.info(f'DIAGNOSE: kon toggle-status niet uitlezen: {diag_error}')
```

**Voor de volgende sessie**: wacht tot de sessie daadwerkelijk langere tijd (bijv. na een
weekend) niet gebruikt is, zodat de snelle route faalt en de Selenium-flow daadwerkelijk
wordt doorlopen. Zoek dan naar de "DIAGNOSE"-regel in het logbestand
(`/opt/ibkr/outputs/ibeam_log__<datum>.txt` of een handmatig omgeleid logbestand) om te
zien of `toggle1.checked` na IBeam's eigen klik daadwerkelijk `true` wordt. Als dat wél
`true` is maar de login alsnog faalt met de Live/Paper-mismatch-foutmelding, ligt het
probleem niet bij de klik zelf maar mogelijk bij een timing-race (de vorm wordt
verzonden vóórdat de serverzijde de nieuwe toggle-status heeft verwerkt) -- overweeg dan
een langere `time.sleep()` na de klik, vóór het opnieuw indienen van het formulier.
