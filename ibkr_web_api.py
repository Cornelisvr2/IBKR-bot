"""
ibkr_web_api.py — Touch & Turn Scalper, Client Portal Web API Client

BELANGRIJKE ARCHITECTUURWIJZIGING (19 aug 2026): ib_async verwacht de
klassieke IB Gateway/TWS-socketverbinding (poort 4001/4002), maar wij
hebben de Client Portal Gateway draaien (poort 5000, REST/WebSocket-
API, geautomatiseerd via IBeam). Dit zijn twee verschillende IBKR-
producten met incompatibele protocollen -- een rechtstreekse
ib_async.IB().connect() naar poort 5000 geeft een TimeoutError,
bevestigd bij live testen.

Deze module praat rechtstreeks met de Client Portal Web API via HTTP
(requests), gebruikmakend van de reeds-geauthenticeerde sessie die de
Gateway lokaal bijhoudt (geen cookie-beheer nodig -- de Gateway is
single-session, elke lokale aanroep wordt behandeld als afkomstig van
de ingelogde gebruiker).

Kernfuncties:
    - resolve_conid(): symbool (bijv. "AAPL") -> IBKR contract-ID
    - get_historical_bars(): candledata voor een conid
    - get_account_id(): actief account-ID ophalen
    - place_bracket_order(): entry + TP + SL als één order-array

LET OP: de functies die daadwerkelijk HTTP-aanroepen doen zijn NIET
end-to-end getest tegen een live Gateway -- dat vereist een geldige
sessie en is gepland voor de volgende sessie. Wel getest: URL-opbouw,
payload-constructie en caching-logica, zie __main__.
"""

from __future__ import annotations

import logging
import time
import random
import threading
import requests
from datetime import datetime

logger = logging.getLogger("ibkr_web_api")

BASE_URL = "https://127.0.0.1:5000/v1/api"

# NIEUW (4 sep 2026, op verzoek: netwerkverkeer reguleren): begrenst
# het aantal ECHT GELIJKTIJDIGE aanvragen naar het history-eindpunt,
# ONGEACHT hoeveel symbolen main.py's Semaphore(5) toestaat -- elk
# symbool doet namelijk 2 losse aanvragen (dagcandles + 15-min-
# candles), dus 5 gelijktijdige symbolen konden tot 10 gelijktijdige
# HTTP-aanvragen opleveren, wat de daadwerkelijke, live geconstateerde
# oorzaak was van de aanhoudende 429-fouten. Dit is een
# threading.Semaphore (NIET asyncio.Semaphore) omdat de aanroepen via
# asyncio.to_thread() in echte OS-threads lopen, niet in de
# event loop zelf.
_HISTORY_REQUEST_THROTTLE = threading.Semaphore(2)

# Cache van symbool -> conid, zodat we niet bij elke aanroep opnieuw
# hoeven te zoeken. Simpele in-memory dict, geldig voor de duur van
# één procesrun (main.py-cyclus).
_conid_cache: dict[str, int] = {}


_session_cache = None


def _get_session():
    """
    Bouwt een requests.Session op met het zelfondertekende certificaat
    van de Gateway genegeerd (verify=False) -- consistent met hoe
    IBeam en onze eerdere curl-tests dit ook deden.

    KRITIEKE FIX (1 sep 2026): hergebruikt nu ÉÉN gedeelde sessie
    (module-level cache) in plaats van bij ELKE aanroep een gloednieuwe
    requests.Session() op te zetten -- dat laatste betekende een NIEUWE
    TCP/TLS-verbinding bij elke van de 10 aanroepplekken in dit bestand.
    Met onze asyncio.gather()-aanpak (tot 3 symbolen gelijktijdig, elk
    met meerdere data-aanroepen) leidde dat tot meerdere GELIJKTIJDIGE,
    verse verbindingen naar dezelfde lokale Gateway -- een plausibele
    (deel)verklaring voor de herhaalde 503 Service Unavailable-fouten
    die we eind augustus meerdere dagen op rij zagen.

    requests.Session-objecten zijn thread-safe voor gelijktijdig gebruik
    (de onderliggende urllib3-connectionpool regelt dit intern), dus dit
    is veilig te delen tussen de threads die asyncio.to_thread() gebruikt.
    """
    global _session_cache
    if _session_cache is None:
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        _session_cache = requests.Session()
        _session_cache.verify = False
        logger.info("Nieuwe, gedeelde IBKR-sessie aangemaakt (wordt hergebruikt voor alle aanroepen).")
    return _session_cache


def tickle() -> bool:
    """
    Houdt de sessie actief (voorkomt automatisch verlopen) en is
    tegelijk een simpele check of de Gateway reageert. Zelfde
    endpoint dat IBeam zelf ook gebruikt.
    """
    session = _get_session()
    try:
        response = session.post(f"{BASE_URL}/tickle", timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Tickle mislukt: {e}")
        return False


def resolve_conid(symbol: str, exchange: str = "SMART", sec_type: str = "STK") -> int | None:
    """
    Zoekt het IBKR contract-ID (conid) op voor een symbool.
    Resultaat wordt gecached (per symbool+sec_type combinatie) zodat
    herhaalde aanroepen binnen dezelfde procesrun geen extra
    API-calls kosten.

    De /iserver/secdef/search respons geeft per bedrijf één entry met
    een conid op het HOOFDNIVEAU (dit is de "primaire" notering, vaak
    de VS-beurs voor Amerikaanse aandelen) en een geneste 'sections'-
    lijst met alle beschikbare instrumenttypes (STK/OPT/WAR/etc.) --
    de sections zelf hebben meestal GEEN eigen conid, dat hoort bij
    het hoofdniveau. We nemen daarom het eerste resultaat dat een
    sectie van het gevraagde type bevat.

    Args:
        symbol: bijv. "AAPL" (aandeel) of "VIX" (index)
        sec_type: "STK" voor aandelen (standaard), "IND" voor indices
                  zoals VIX -- nodig omdat VIX geen aandeel is en dus
                  nooit een STK-sectie heeft.

    Live geverifieerd op 19 aug 2026: AAPL -> conid 265598 (NASDAQ,
    STK), met latere resultaten voor buitenlandse noteringen (TSE,
    MEXI, EBS) die correct worden overgeslagen door deze eerste-match-
    logica. VIX/IND-opzoeken zelf nog niet live getest.
    """
    cache_key = f"{symbol}:{sec_type}"
    if cache_key in _conid_cache:
        return _conid_cache[cache_key]

    session = _get_session()
    try:
        response = session.get(
            f"{BASE_URL}/iserver/secdef/search",
            params={"symbol": symbol}, timeout=15,
        )
        response.raise_for_status()
        results = response.json()

        if not results:
            logger.error(f"Geen conid gevonden voor {symbol}")
            return None

        for item in results:
            sections = item.get("sections", [])
            has_type = any(s.get("secType") == sec_type for s in sections)
            if has_type and "conid" in item:
                conid = int(item["conid"])
                _conid_cache[cache_key] = conid
                logger.info(f"Conid voor {symbol} ({sec_type}): {conid} ({item.get('description', '?')})")
                return conid

        logger.error(f"Geen {sec_type}-resultaat gevonden voor {symbol} in: {results}")
        return None
    except Exception as e:
        logger.error(f"Kon conid niet opzoeken voor {symbol}: {e}")
        return None


def get_account_id() -> str | None:
    """
    Haalt het actieve account-ID op (nodig voor order-plaatsing).

    LET OP: nog niet live getest.
    """
    session = _get_session()
    try:
        response = session.get(f"{BASE_URL}/iserver/accounts", timeout=10)
        response.raise_for_status()
        data = response.json()
        accounts = data.get("accounts", [])
        if not accounts:
            logger.error("Geen accounts gevonden in /iserver/accounts respons.")
            return None
        return accounts[0]
    except Exception as e:
        logger.error(f"Kon account-ID niet ophalen: {e}")
        return None


def get_historical_bars(conid: int, period: str = "5d", bar: str = "15min", max_retries: int = 3) -> list[dict]:
    """
    Haalt historische candledata op voor een conid.

    Args:
        conid: IBKR contract-ID (via resolve_conid())
        period: hoeveel historie, bijv. "5d", "1y"
        bar: candle-grootte, bijv. "15min", "1d"
        max_retries: aantal pogingen bij een TIJDELIJKE serverfout
                     (503/502/504) vóór definitief opgeven.

    Returns:
        Lijst van dicts met o.oa. 't' (timestamp), 'o','h','l','c','v'
        -- de rauwe vorm van de Web API, om te zetten naar onze eigen
        Candle-dataclass in data_module.py.

    TOEVOEGING (26 aug 2026): retry-logica voor tijdelijke IBKR-
    serverfouten (503 Service Unavailable, en de vergelijkbare 502/504).
    Live ontdekt op 26 aug 2026: meerdere 503-fouten op rij zorgden
    ervoor dat 2 van de 3 mogelijke trades die dag werden overgeslagen,
    puur door een kortstondige serverhapering -- een korte, herhaalde
    poging (met 3 seconden pauze ertussen) lost dit soort tijdelijke
    fouten vaak vanzelf op, zonder de cyclus onnodig te vertragen bij
    een structureel probleem (LET OP: bij een NIET-tijdelijke fout,
    zoals 401 Unauthorized, wordt NIET herhaald -- dat zou alleen de
    achterliggende oorzaak verbergen in plaats van oplossen).

    LET OP: nog niet live getest. Een bekende eigenaardigheid van deze
    API (te verifiëren): soms moet eerst een marketdata-snapshot
    worden opgevraagd om de datastroom te "primen" voordat historische
    data beschikbaar is -- zie get_market_data_snapshot() hieronder,
    aan te roepen vóór get_historical_bars() als deze leeg terugkomt.
    """
    session = _get_session()
    TIJDELIJKE_FOUTCODES = (502, 503, 504)
    # NIEUW (2 sep 2026, bugfix): 429 (Too Many Requests) is een ECHTE
    # rate-limiet van IBKR's Web API -- ontdekt bij de overstap naar de
    # volledige-watchlist-scan (26 symbolen i.p.v. 3), waarbij meerdere
    # gelijktijdige symbolen (ook al beperkt tot 5 tegelijk via een
    # Semaphore in main.py) toch de limiet raakten. In tegenstelling tot
    # 502/503/504 (server tijdelijk overbelast, snel weer beschikbaar)
    # betekent een 429 letterlijk "u gaat te snel" -- een even korte
    # pauze (3s) zou dezelfde limiet direct weer kunnen raken, dus een
    # LANGERE pauze (8s) vóór een nieuwe poging.
    RATE_LIMIT_FOUTCODE = 429
    RATE_LIMIT_WACHTTIJD_MIN = 6
    RATE_LIMIT_WACHTTIJD_MAX = 14

    for poging in range(1, max_retries + 1):
        try:
            # NIEUW (4 sep 2026): begrenst echte gelijktijdigheid naar
            # het history-eindpunt tot 2, ongeacht hoeveel symbool-
            # processen main.py's Semaphore(5) toestaat.
            with _HISTORY_REQUEST_THROTTLE:
                response = session.get(
                    f"{BASE_URL}/iserver/marketdata/history",
                    params={"conid": conid, "period": period, "bar": bar},
                    timeout=20,
                )
            response.raise_for_status()
            data = response.json()
            bars = data.get("data", [])
            logger.info(f"{len(bars)} candles opgehaald voor conid {conid}" + (f" (poging {poging})" if poging > 1 else ""))
            return bars
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response is not None else None
            if status_code in TIJDELIJKE_FOUTCODES and poging < max_retries:
                logger.warning(f"Tijdelijke serverfout ({status_code}) bij conid {conid}, poging {poging}/{max_retries} -- opnieuw proberen na 3s.")
                time.sleep(3)
                continue
            if status_code == RATE_LIMIT_FOUTCODE and poging < max_retries:
                # NIEUW (2 sep 2026, aanvullende bugfix): een VASTE
                # wachttijd (was: altijd exact 8s) zorgde voor een
                # "kudde-effect" -- meerdere symbolen die gelijktijdig
                # tegen de rate-limiet aanliepen, wachtten allemaal
                # EXACT even lang, en probeerden dan weer EXACT
                # tegelijk opnieuw, waardoor ze elkaar herhaaldelijk in
                # de weg bleven zitten (live waargenomen: dezelfde 4-5
                # conids botsten meerdere pogingen op rij). Een
                # willekeurige wachttijd (6-14s) spreidt de nieuwe
                # pogingen uit elkaar, zodat ze elkaar niet steeds
                # opnieuw synchroon blijven raken.
                wachttijd = random.uniform(RATE_LIMIT_WACHTTIJD_MIN, RATE_LIMIT_WACHTTIJD_MAX)
                logger.warning(f"Rate-limiet (429) bij conid {conid}, poging {poging}/{max_retries} -- opnieuw proberen na {wachttijd:.1f}s.")
                time.sleep(wachttijd)
                continue
            logger.error(f"Kon historische data niet ophalen voor conid {conid}: {e}")
            return []
        except Exception as e:
            logger.error(f"Kon historische data niet ophalen voor conid {conid}: {e}")
            return []

    return []


def get_market_data_snapshot(conid: int) -> dict:
    """
    Vraagt een marketdata-snapshot op -- kan nodig zijn om de
    datastroom te initialiseren vóór get_historical_bars() bruikbare
    data teruggeeft (bekende eigenaardigheid van deze API, te
    bevestigen bij live testen).
    """
    session = _get_session()
    try:
        response = session.get(
            f"{BASE_URL}/iserver/marketdata/snapshot",
            params={"conids": str(conid), "fields": "31,84,86"},
            timeout=15,
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Kon snapshot niet ophalen voor conid {conid}: {e}")
        return {}


def build_single_order_payload(
    conid: int, account_id: str, action: str, quantity: float,
    order_type: str = "LMT", price: float = None, aux_price: float = None,
    coid: str = None, trailing_amt: float = None,
) -> dict:
    """
    Bouwt de payload op voor ÉÉN losse, ongekoppelde order -- dit is
    het formaat dat bij live testen (19 aug 2026) betrouwbaar werkte,
    in tegenstelling tot de parent/child-bracketstructuur (die
    herhaaldelijk faalde met "Invalid order price fields" /
    "Parent order ... is no more existed").

    We beheren OCO-gedrag (entry -> wacht op fill -> plaats TP/SL ->
    annuleer de ander bij fill) daarom zelf in order_module.py, in
    plaats van te vertrouwen op IBKR's ingebouwde parentId-koppeling.

    BELANGRIJKE ONTDEKKING (19 aug 2026): voor STP-orders (stop-loss)
    verwacht deze API het veld 'price', NIET 'auxPrice' zoals de
    klassieke TWS API-conventie zou doen -- 'auxPrice' bij een STP-
    order gaf structureel "Invalid order price fields" terug, ongeacht
    andere velden (secType, tif, side, outsideRTH allemaal getest en
    uitgesloten als oorzaak). Met 'price' i.p.v. 'auxPrice' werkte de
    order meteen correct (bevestigd: order_id ontvangen, PreSubmitted).
    Om verwarring te voorkomen accepteert deze functie nog steeds het
    aux_price-argument (voor leesbaarheid bij de aanroeper -- "dit is
    de stop-triggerprijs"), maar zet het intern om naar het 'price'-
    veld in de payload.

    NIEUWE ONTDEKKING (21 aug 2026, voor VIX Rider): een TRAIL-order
    (meebewegende stop-loss) vereist de velden 'trailingAmt' (het
    bedrag waarmee de stop meebeweegt, in dollars/euro's) EN
    'trailingType': 'amt' -- zonder deze velden geeft de API dezelfde
    "Invalid order price fields"-fout als we destijds bij STP zagen.
    Live bevestigd: order geplaatst en PreSubmitted-status ontvangen
    met trailing_amt=5.00. Een percentage-gebaseerde variant
    ('trailingType': 'pct') is NIET getest -- alleen het absolute-
    bedrag-formaat is bevestigd werkend.

    Pure payload-constructie, geen HTTP-aanroep -- volledig testbaar.
    """
    order = {
        "acctId": account_id,
        "conid": conid,
        "secType": "STK",
        "orderType": order_type,
        "side": action,
        "quantity": quantity,
        "tif": "DAY",
    }
    is_fractional = quantity != int(quantity)

    if order_type not in ("STP", "TRAIL") and not is_fractional:
        # STP- en TRAIL-orders ondersteunen outsideRTH niet -- voor STP
        # live bevestigd foutmelding: "invalid order attribute : Outside
        # Regular Trading Hours". Voor TRAIL nog niet expliciet los
        # getest, maar uit voorzorg hetzelfde patroon aangehouden totdat
        # het tegendeel blijkt.
        #
        # ONTDEKKING (21 aug 2026): fractionele hoeveelheden (bijv. 0.5)
        # ondersteunen outsideRTH OOK niet -- live bevestigde foutmelding:
        # "Outside RTH/PreOpen RTH is not supported for fractional
        # orders." Vandaar de is_fractional-check hierboven.
        order["outsideRTH"] = True

    if order_type == "STP" and aux_price is not None:
        # Ontdekt bij live testen: STP-orders gebruiken 'price', niet 'auxPrice'.
        # BELANGRIJKE FIX (21 aug 2026): afronden op 2 decimalen -- een
        # prijs met meer decimalen (bijv. 481.30474, ontstaan uit
        # fractionele-positiegrootte-berekeningen) gaf live de fout
        # "does not conform to the minimum price variation of 0.01",
        # waardoor een take-profit order faalde en de stop-loss
        # daardoor NOOIT geplaatst werd -- een tijdelijk onbeschermde
        # live positie tot gevolg. Vandaar deze afronding voor ALLE
        # prijsvelden hieronder, niet alleen deze.
        order["price"] = round(aux_price, 2)
    elif order_type == "TRAIL" and trailing_amt is not None:
        order["trailingAmt"] = round(trailing_amt, 2)
        order["trailingType"] = "amt"
    else:
        if price is not None:
            order["price"] = round(price, 2)
        if aux_price is not None:
            order["auxPrice"] = round(aux_price, 2)

    if coid:
        order["cOID"] = coid
    return order


def confirm_order_reply(reply_id: str) -> dict:
    """
    Bevestigt een order-'question' (bijv. een prijswaarschuwing) via
    de /iserver/reply/{id} endpoint. IBKR's Web API geeft soms een
    question-object terug in plaats van direct de order te plaatsen --
    ontdekt bij live testen op 19 aug 2026 (melding: "price exceeds
    the Percentage constraint of 3%").
    """
    session = _get_session()
    try:
        response = session.post(
            f"{BASE_URL}/iserver/reply/{reply_id}",
            json={"confirmed": True}, timeout=15,
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Kon reply {reply_id} niet bevestigen: {e}")
        return {"error": str(e)}


def place_single_order(
    conid: int, account_id: str, action: str, quantity: float,
    order_type: str = "LMT", price: float = None, aux_price: float = None,
    coid: str = None, max_confirmations: int = 3, trailing_amt: float = None,
) -> dict:
    """
    Plaatst ÉÉN losse order via de Web API en geeft het order_id
    terug (of een foutmelding). Betrouwbaar bevestigd bij live testen
    -- zie build_single_order_payload() voor de achtergrond.

    Handelt automatisch IBKR's "question"-bevestigingen af (bijv. een
    prijswaarschuwing als de order >3% van de marktprijs afwijkt) door
    de vraag te bevestigen en de order opnieuw te versturen, tot
    max_confirmations keer -- voorkomt dat een order blijft hangen als
    "vraag" zonder ooit daadwerkelijk geplaatst te worden.

    LET OP: dit bevestigt automatisch ALLE vragen die de API stelt,
    inclusief prijswaarschuwingen. Voor een strategie die op
    marktprijs-nabije Fibonacci-niveaus handelt zou dit zelden moeten
    triggeren, maar controleer dit gedrag bij live gebruik -- een
    verkeerd geconfigureerde prijs zou zo alsnog geplaatst kunnen
    worden ondanks de waarschuwing.
    """
    session = _get_session()
    payload = build_single_order_payload(
        conid, account_id, action, quantity, order_type, price, aux_price, coid, trailing_amt
    )

    try:
        response = session.post(
            f"{BASE_URL}/iserver/account/{account_id}/orders",
            json={"orders": [payload]}, timeout=20,
        )
        response.raise_for_status()
        result = response.json()

        confirmations = 0
        while confirmations < max_confirmations:
            question = None
            if isinstance(result, dict) and "id" in result and "message" in result:
                question = result
            elif isinstance(result, list) and len(result) == 1 and "id" in result[0] and "message" in result[0] and "order_status" not in result[0]:
                question = result[0]

            if question is None:
                break

            logger.warning(f"Order-bevestiging vereist: {question.get('message')}")
            result = confirm_order_reply(question["id"])
            confirmations += 1

        logger.info(f"Order geplaatst: {result}")

        if isinstance(result, list) and result:
            entry = result[0]
            if entry.get("order_status") == "Failed" or entry.get("order_id") == "-1":
                return {"error": entry.get("text", "Order geweigerd."), "raw": entry}
            return {"order_id": entry.get("order_id"), "status": entry.get("order_status"), "raw": entry}

        return {"error": "Onverwacht antwoordformaat (mogelijk onopgeloste vraag).", "raw": result}
    except Exception as e:
        logger.error(f"Kon order niet plaatsen: {e}")
        return {"error": str(e)}


def cancel_order(account_id: str, order_id: str) -> dict:
    """Annuleert een order via de Web API."""
    session = _get_session()
    try:
        response = session.delete(
            f"{BASE_URL}/iserver/account/{account_id}/order/{order_id}", timeout=10
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Kon order {order_id} niet annuleren: {e}")
        return {"error": str(e)}


def get_order_status(order_id: str) -> dict:
    """Vraagt de status van een specifieke order op."""
    session = _get_session()
    try:
        response = session.get(f"{BASE_URL}/iserver/account/order/status/{order_id}", timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Kon orderstatus niet ophalen voor {order_id}: {e}")
        return {"error": str(e)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Scenario 1: conid-caching (geen live call, alleen de dict-logica)
    _conid_cache["AAPL:STK"] = 265598
    print(f"Scenario 1 (conid-cache): AAPL -> {_conid_cache.get('AAPL:STK')}")
    print(f"Nog niet gecached symbool: {_conid_cache.get('MSFT:STK')}")

    print("\n--- Live tests vereisen een geauthenticeerde Gateway-sessie: ---")
    print("  python3 -c \"from ibkr_web_api import tickle; print(tickle())\"")
    print("  python3 -c \"from ibkr_web_api import resolve_conid; print(resolve_conid('AAPL'))\"")

    # Scenario 2: losse-order payload (het formaat dat live betrouwbaar werkte)
    payload = build_single_order_payload(
        conid=265598, account_id="DU1234567", action="BUY", quantity=1,
        order_type="LMT", price=280.0, coid="p12345",
    )
    print(f"\nScenario 2 (losse LMT-order): {payload}")

    payload = build_single_order_payload(
        conid=265598, account_id="DU1234567", action="SELL", quantity=1,
        order_type="STP", aux_price=270.0,
    )
    print(f"Scenario 3 (losse STP-order, geen cOID): {payload}")
    assert "price" in payload and payload["price"] == 270.0, "STP moet 'price' bevatten, niet 'auxPrice'!"
    assert "auxPrice" not in payload, "STP mag GEEN 'auxPrice' bevatten -- live bevestigd dat dit een 'Invalid order price fields'-fout geeft!"
    print("(geverifieerd: STP-order gebruikt 'price', niet 'auxPrice' -- live bevestigd 19 aug 2026)")

    # Scenario 3b: TRAIL-order payload (voor VIX Rider's meebewegende stop)
    payload = build_single_order_payload(
        conid=265598, account_id="DU1234567", action="SELL", quantity=1,
        order_type="TRAIL", trailing_amt=5.00,
    )
    print(f"Scenario 3b (losse TRAIL-order): {payload}")
    assert payload.get("trailingAmt") == 5.00, "TRAIL moet 'trailingAmt' bevatten!"
    assert payload.get("trailingType") == "amt", "TRAIL moet 'trailingType': 'amt' bevatten!"
    assert "outsideRTH" not in payload, "TRAIL mag geen outsideRTH bevatten, zelfde patroon als STP!"
    print("(geverifieerd: TRAIL-order gebruikt trailingAmt + trailingType='amt' -- live bevestigd 21 aug 2026)")

    # Scenario 3c: fractionele order mag GEEN outsideRTH bevatten
    payload = build_single_order_payload(
        conid=265598, account_id="DU1234567", action="BUY", quantity=0.5,
        order_type="LMT", price=280.0,
    )
    print(f"Scenario 3c (fractionele order): {payload}")
    assert "outsideRTH" not in payload, "Fractionele orders mogen GEEN outsideRTH bevatten -- live bevestigd dat dit een fout geeft!"
    print("(geverifieerd: fractionele order bevat geen outsideRTH -- live bevestigd 21 aug 2026)")

    # Scenario 3d: hele-getal-hoeveelheid behoudt WEL outsideRTH (normale LMT)
    payload = build_single_order_payload(
        conid=265598, account_id="DU1234567", action="BUY", quantity=1,
        order_type="LMT", price=280.0,
    )
    assert payload.get("outsideRTH") is True, "Hele-getal LMT-orders moeten WEL outsideRTH behouden!"
    print("(geverifieerd: hele-getal-orders behouden outsideRTH zoals voorheen)")

    # Scenario 3e: prijsafronding -- de exacte situatie die op 21 aug
    # 2026 live faalde ("does not conform to the minimum price
    # variation of 0.01") met een 5-decimalen-prijs uit een
    # fractionele-positiegrootte-berekening.
    payload = build_single_order_payload(
        conid=272093, account_id="DU1234567", action="SELL", quantity=25.7278,
        order_type="LMT", price=481.30474,
    )
    print(f"Scenario 3e (niet-afgeronde prijs, LMT): {payload}")
    assert payload["price"] == 481.30, f"Prijs moet afgerond zijn naar 481.30, kreeg {payload['price']}"
    print("(geverifieerd: prijs afgerond naar 2 decimalen -- voorkomt de live-fout van 21 aug 2026)")

    payload = build_single_order_payload(
        conid=272093, account_id="DU1234567", action="SELL", quantity=25.7278,
        order_type="STP", aux_price=478.97263,
    )
    assert payload["price"] == 478.97, f"STP-prijs moet afgerond zijn naar 478.97, kreeg {payload['price']}"
    print(f"Scenario 3f (niet-afgeronde prijs, STP): {payload}")
    print("(geverifieerd: STP-prijs ook correct afgerond)")

    # Scenario 4: echte AAPL-respons parsing (geverifieerd 19 aug 2026)
    echte_aapl_respons = [
        {"conid": "265598", "companyHeader": "APPLE INC - NASDAQ", "companyName": "APPLE INC",
         "symbol": "AAPL", "description": "NASDAQ",
         "sections": [{"secType": "STK"}, {"secType": "OPT"}, {"secType": "WAR"},
                      {"secType": "IOPT"}, {"secType": "CFD"}, {"secType": "BAG"}]},
        {"conid": "532640894", "companyHeader": "APPLE INC-CDR - TSE", "companyName": "APPLE INC-CDR",
         "symbol": "AAPL", "description": "TSE",
         "sections": [{"secType": "STK"}, {"secType": "OPT"}, {"secType": "BAG"}]},
    ]

    def _test_parse(results):
        for item in results:
            sections = item.get("sections", [])
            has_stk = any(s.get("secType") == "STK" for s in sections)
            if has_stk and "conid" in item:
                return int(item["conid"]), item.get("description", "?")
        return None, None

    conid, beurs = _test_parse(echte_aapl_respons)
    print(f"\nScenario 4 (echte AAPL-respons): conid={conid}, beurs={beurs}")
    print("(verwacht: conid=265598, beurs=NASDAQ -- niet de TSE-notering)")
