"""
main.py — Touch & Turn Scalper, Orchestratie

Brengt alle modules samen in de volgorde die de cron-jobs (Fase 6)
straks aanroepen:

    1. state_module   -- check of trading_enabled staat
    2. news_module     -- kies het aandeel van de dag
    3. data_module      -- haal dagcandles (ATR) en de openingscandle op
    4. atr_module        -- valideer of de openingscandle groot genoeg is
    5. entry_module        -- bepaal richting + Fibonacci-niveaus
    6. exit_module           -- bereken TP/SL/positiegrootte
    7. order_module            -- bouw en (in live-modus) verstuur de order

Dry-run modus (standaard): gebruikt voorbeelddata in plaats van een
live IBKR-verbinding, zodat de VOLLEDIGE keten hier al getest kan
worden -- inclusief hoe de modules op elkaar aansluiten. Alleen het
daadwerkelijke orderplaatsingsmoment wordt in dry-run overgeslagen.

Live-modus (--live vlag): vereist een geldige, geauthenticeerde
Client Portal Gateway-sessie (zie ibkr_web_api.py) -- NIET ib_async,
dat bleek te verbinden met het verkeerde IBKR-product (zie
ibkr_web_api.py's moduledocstring, ontdekt en opgelost op 19 aug
2026). Controleer eerst de sessie met:
    python3 -c "from auth_module import check_ibkr_authenticated; print(check_ibkr_authenticated())"

Gebruik:
    python3 main.py            # dry-run, met voorbeelddata
    python3 main.py --live     # live, verbindt met IBKR (nog niet getest)
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import datetime

from state_module import load_state
from news_module import select_symbol, select_top_symbols, select_top_symbols_from_watchlist, FALLBACK_WATCHLIST
from data_module import Candle, get_opening_candle, parse_bars_to_candles
from atr_module import calculate_atr, validate_opening_range
from entry_module import generate_entry_signal
from exit_module import calculate_exit_levels
from order_module import build_bracket_orders
from reversal_strategy_module import run_reversal_symbol_cycle

logger = logging.getLogger("main")

DEFAULT_CAPITAL = 2000.0  # Bewust vast bedrag, NIET gekoppeld aan het paper-accountsaldo (dat is
                           # vrijwel onbeperkt) -- simuleert het bedrag dat daadwerkelijk ingelegd
                           # zou worden bij een overstap naar live trading. Alle positiegrootte-,
                           # risico- (1%/trade) en dagstop-berekeningen (3%) gebruiken dit getal,
                           # ongeacht wat IBKR als paper-saldo toont.


def get_market_data_dry_run(symbol: str) -> tuple[list[Candle], Candle]:
    """
    Genereert voorbeeld-marktdata voor dry-run tests: 15 dagcandles
    (voor ATR) en een openingscandle met een duidelijke bullish bias,
    zodat de hele keten een concreet SHORT-signaal doorloopt.

    In live-modus wordt dit vervangen door twee aanroepen naar
    data_module.get_historical_candles() (één met bar_size="1 day",
    één met bar_size="15 mins").
    """
    daily_candles = []
    prijs = 100.0
    for i in range(15):
        daily_candles.append(Candle(
            timestamp=datetime(2026, 7, i + 1),
            open=prijs, high=prijs + 1.5, low=prijs - 1.5, close=prijs + 0.3,
            volume=10000,
        ))
        prijs += 0.3

    opening_candle = Candle(
        timestamp=datetime(2026, 8, 14, 9, 0),
        open=104.0, high=106.0, low=102.5, close=105.5, volume=15000,
    )
    return daily_candles, opening_candle


def get_market_data_live(symbol: str) -> tuple[list[Candle], Candle]:
    """
    Haalt echte marktdata op via de Client Portal Web API (zie
    data_module.get_historical_candles(), die op zijn beurt
    ibkr_web_api.py gebruikt -- niet ib_async, zie moduledocstrings
    van data_module.py en ibkr_web_api.py voor de uitleg waarom).

    LET OP: get_historical_candles() is live geverifieerd (19 aug
    2026, AAPL, 130 candles correct opgehaald). Deze functie zelf (de
    combinatie van dag- en 15-min candles voor één symbool) is nog
    niet als geheel getest, maar bouwt voort op een bevestigd werkend
    onderdeel.
    """
    from data_module import get_historical_candles

    daily_candles = get_historical_candles(symbol, duration="20d", bar_size="1d")
    intraday_candles = get_historical_candles(symbol, duration="1d", bar_size="15min")
    opening_candle = get_opening_candle(intraday_candles)

    return daily_candles, opening_candle


def run_symbol_cycle(symbol: str, capital: float, dry_run: bool) -> dict:
    """
    Voert de keten (marktdata -> ATR -> entry -> exit -> order) uit
    voor ÉÉN symbool. Wordt door run_cycle() tot 3 keer aangeroepen,
    één keer per gekozen aandeel -- elk symbool volledig onafhankelijk,
    zodat een mislukte ATR-validatie voor het ene aandeel de andere
    twee niet raakt.
    """
    if dry_run:
        daily_candles, opening_candle = get_market_data_dry_run(symbol)
    else:
        daily_candles, opening_candle = get_market_data_live(symbol)

    # KRITIEKE FIX (24 aug 2026): get_opening_candle() geeft sinds de
    # datumfilter-fix TERECHT None terug als er geen candles van
    # vandaag zijn (bijv. cyclus draait te vroeg t.o.v. marktopening,
    # of een onverwachte marktsluiting). Zonder deze check crasht
    # validate_opening_range() met een cryptische
    # "'NoneType' object has no attribute 'range'" -- live gebeurd
    # op 24 aug 2026, direct na het invoeren van de datumfilter-fix
    # in data_module.py.
    if opening_candle is None:
        reason = f"Geen openingscandle van vandaag beschikbaar voor {symbol} -- cyclus overgeslagen."
        logger.warning(reason)
        return {"status": "skipped", "symbol": symbol, "reason": reason}

    # NIEUW (9 sep 2026, dezelfde bugfix als eerder toegepast op de
    # (inmiddels weer verlaten) reversal-flow): de openingscandle-check
    # hierboven ving ALLEEN een mislukte 15-MINUTEN-fetch op -- de
    # DAG-candles (nodig voor ATR) hebben hun EIGEN, aparte aanvraag en
    # dus ook hun eigen kans om te mislukken (bv. bij 429-rate-limiet-
    # druk bij de volledige-watchlist-scan). Zonder deze check kon
    # calculate_atr() een lege lijst krijgen en een onopgevangen fout
    # gooien.
    if not daily_candles or len(daily_candles) < 15:
        reason = f"Onvoldoende dagcandles voor {symbol} ({len(daily_candles)} beschikbaar) -- cyclus overgeslagen."
        logger.warning(reason)
        return {"status": "skipped", "symbol": symbol, "reason": reason}

    atr = calculate_atr(daily_candles)
    validation = validate_opening_range(opening_candle, atr)
    if not validation["valid"]:
        reason = f"ATR-validatie mislukt voor {symbol}: {validation['reason']}"
        logger.warning(reason)
        return {"status": "skipped", "symbol": symbol, "reason": reason}

    signal = generate_entry_signal(opening_candle)
    if signal is None:
        reason = f"Neutrale openingscandle voor {symbol} -- geen signaal."
        logger.warning(reason)
        return {"status": "skipped", "symbol": symbol, "reason": reason}

    plan = calculate_exit_levels(signal, capital=capital)
    if plan.position_size * plan.entry_price < 5.0:
        reason = f"Positiewaarde te klein voor {symbol} met €{capital:.2f} kapitaal."
        logger.warning(reason)
        return {"status": "skipped", "symbol": symbol, "reason": reason}

    spec = build_bracket_orders(plan, symbol=symbol)

    if dry_run:
        logger.info(f"[DRY-RUN] Order zou geplaatst worden: {spec.reason}")
        return {"status": "dry_run_complete", "symbol": symbol, "order_spec": spec.to_dict()}

    # Losgekoppeld achtergrondproces starten i.p.v. hier te blokkeren --
    # execute_managed_trade() kan uren duren (wacht op fill, dan op
    # TP/SL), wat de cron-job (en daarmee de volgende cron-slot, via
    # de flock-vergrendeling in run_cycle.sh) zou blokkeren. Dit proces
    # overleeft het einde van main.py dankzij start_new_session=True.
    import subprocess

    log_path = f"/opt/strategy/logs/dispatch_{symbol}.log"
    with open(log_path, "a") as log_file:
        subprocess.Popen(
            [
                "python3", "/opt/strategy/execute_trade_standalone.py",
                "--symbol", symbol,
                "--action", spec.action,
                "--quantity", str(spec.quantity),
                "--entry-price", str(spec.entry_price),
                "--take-profit", str(spec.take_profit),
                "--stop-loss", str(spec.stop_loss),
                "--oca-group", spec.oca_group,
            ],
            stdout=log_file, stderr=subprocess.STDOUT,
            start_new_session=True,  # loskoppelen van deze process-groep
        )

    logger.info(f"Trade voor {symbol} gedispatcht naar losgekoppeld proces.")
    return {"status": "trade_dispatched", "symbol": symbol, "reason": spec.reason}


def run_cycle(capital: float = None, dry_run: bool = True, max_trades: int = 3) -> dict:
    """
    Voert één volledige cyclus uit: kiest tot `max_trades` verschillende
    aandelen op basis van nieuws, en draait voor elk GELIJKTIJDIG de
    volledige keten (via run_symbol_cycle), met asyncio.gather() en
    to_thread() -- elk symbool pollt onafhankelijk op zijn eigen
    thread, in plaats van serieel te wachten (belangrijk omdat
    execute_managed_trade() blokkerend is en tot uren kan duren).

    Elke trade riskeert `capital * risk_pct` (standaard 1%) -- bij 3
    gelijktijdige trades dus maximaal 3% totale blootstelling, wat
    overeenkomt met de daglimiet uit het risicokader (Fase 9).

    COMPOUNDING (22 aug 2026): als `capital` niet expliciet is
    meegegeven, wordt het gesimuleerde, doorlopende saldo opgehaald
    (state_module.get_simulated_balance()) in plaats van een vast
    bedrag -- winst/verlies van eerdere trades wordt zo herbelegd.
    Geef `capital` alleen expliciet mee voor losse tests met een vast
    bedrag.

    Returns:
        Een dict met de status per symbool, plus een samenvatting.
    """
    logger.info(f"=== Start cyclus ({'DRY-RUN' if dry_run else 'LIVE'}) ===")

    # PER-STRATEGIE SALDI (9 sep 2026): TTS en QFS draaiden tot nu toe
    # BEIDE op hetzelfde `allocated_capital`-getal binnen één cyclus --
    # ook al waren het twee te vergelijken strategieën, ze deelden
    # feitelijk nog steeds één pot. Nu haalt elke strategie zijn EIGEN
    # onafhankelijke compounding-saldo op (get_strategy_balance), zodat
    # een verlies bij TTS de positiegrootte van QFS niet meer raakt en
    # omgekeerd. Het `capital`-argument van deze functie is nog puur
    # voor losse tests met een vast bedrag; in de normale (cron-)flow
    # wordt het genegeerd ten gunste van de per-strategie saldi.
    from state_module import get_strategy_balance
    strategie_saldi = {"TTS": get_strategy_balance("TTS"), "QFS": get_strategy_balance("QFS")}
    if capital is not None:
        strategie_saldi = {"TTS": capital, "QFS": capital}
    logger.info(f"Saldi per strategie: TTS €{strategie_saldi['TTS']:.2f} · QFS €{strategie_saldi['QFS']:.2f}")

    state = load_state()
    if not state["trading_enabled"]:
        reason = "Trading staat gepauzeerd (via /stop_trading) -- cyclus overgeslagen."
        logger.warning(reason)
        return {"status": "skipped", "reason": reason}

    # Circuit breakers (VIX + 3%-dagstop) -- alleen zinvol in live-modus,
    # want de VIX-check vereist een live Gateway-sessie. In dry-run
    # slaan we dit over zodat je de rest van de keten kunt blijven
    # testen zonder IBKR-afhankelijkheid.
    #
    # De 3%-dagstop is bewust een GLOBALE, gedeelde veiligheidsrem (niet
    # per strategie) -- daarom wordt hier de SOM van beide saldi als
    # referentie gebruikt, ook al compounden ze verder onafhankelijk.
    if not dry_run:
        from risk_module import check_circuit_breakers, get_allocated_capital

        totaal_referentie = strategie_saldi["TTS"] + strategie_saldi["QFS"]
        breaker_result = check_circuit_breakers(capital=totaal_referentie)
        if not breaker_result["safe_to_trade"]:
            logger.warning(f"Circuit breaker actief: {breaker_result['reason']}")
            return {"status": "circuit_breaker_triggered", "reason": breaker_result["reason"]}

        # KRITIEKE FIX (22 aug 2026): de VIX-gebaseerde glijdende-schaal-
        # allocatie (risk_module.get_dynamic_allocation) werd berekend
        # en getest, maar NOOIT daadwerkelijk toegepast op de
        # positiegrootte -- de scalper gebruikte tot nu toe altijd het
        # volledige kapitaal, ongeacht de VIX-stand. Nu wel -- en sinds
        # 9 sep 2026 toegepast op ELK van de twee EIGEN saldi apart
        # (dezelfde VIX-drempel geldt voor beide, elk op zijn eigen basis).
        for code in ("TTS", "QFS"):
            allocation = get_allocated_capital(strategie_saldi[code], "scalper")
            strategie_saldi[code] = allocation["allocated_capital"]
            logger.info(
                f"VIX-allocatie toegepast op {code}: €{strategie_saldi[code]:.2f} van €{get_strategy_balance(code):.2f} "
                f"({allocation['allocation_pct']*100:.0f}%, VIX {allocation['vix']})"
            )

        if strategie_saldi["TTS"] <= 0 and strategie_saldi["QFS"] <= 0:
            reason = "Geen kapitaal toegewezen aan TTS of QFS (VIX-allocatie 0%) -- cyclus overgeslagen."
            logger.info(reason)
            return {"status": "skipped", "reason": reason}

    # Kies tot max_trades verschillende symbolen op basis van nieuws.
    # AANGEPAST (2 sep 2026, op verzoek): niet langer de nieuws-top-3,
    # maar de VOLLEDIGE watchlist (26 aandelen) -- elk aandeel dat zelf
    # de ATR-validatie (stap 2) haalt, wordt doorgestuurd naar de
    # bewakingslus (stap 3). Bewust GEEN limiet op het aantal
    # gelijktijdige posities op dit moment -- eerst observeren wat er
    # in de praktijk gebeurt, zoals besproken.
    chosen = [(symbol, "volledige-watchlist-scan") for symbol in FALLBACK_WATCHLIST]

    if not chosen:
        reason = "Geen symbolen gekozen (ook vangnet-lijst leverde niets op)."
        logger.error(reason)
        return {"status": "skipped", "reason": reason}

    # NIEUW (9 sep 2026, op verzoek): BEIDE strategieën tegelijk laten
    # draaien op ALLE gekozen aandelen, om ze onderling te kunnen
    # vergelijken -- de Fibonacci-flow (run_symbol_cycle, OCA-prefix
    # "TTS_") en de bevestigingsflow (run_reversal_symbol_cycle,
    # OCA-prefix "QFS_") zijn al los van elkaar herkenbaar in de
    # journal, dus geen aanpassing nodig aan de CSV-structuur zelf om
    # ze achteraf te kunnen scheiden. Bewust GEEN limiet en GEEN
    # opsplitsing van de watchlist (op uitdrukkelijk verzoek) -- meer
    # berichten en eventuele fouten spotten weegt zwaarder dan minder
    # netwerkverkeer.
    taken_lijst = []
    for symbool, reden in chosen:
        if strategie_saldi["TTS"] > 0:
            taken_lijst.append((symbool, reden, "TTS"))
        if strategie_saldi["QFS"] > 0:
            taken_lijst.append((symbool, reden, "QFS"))

    # NIEUW (2 sep 2026, bugfix): bij de volledige-watchlist-scan (26
    # symbolen) veroorzaakte het gelijktijdig starten van ALLE taken
    # (asyncio.gather zonder limiet) 429-fouten ("Too Many Requests")
    # bij IBKR -- 6 van de 26 aandelen misten hierdoor hun openingscandle
    # NIET vanwege de strategie zelf, maar vanwege overbelasting van onze
    # eigen aanvragen. Een Semaphore beperkt nu het aantal GELIJKTIJDIGE
    # symbool-verwerkingen tot 5 tegelijk -- de overige wachten netjes
    # in de rij, net zo lang tot er weer ruimte is.
    semafoor = asyncio.Semaphore(5)

    async def _run_symbol_async(symbol: str, news_reason: str, strategie: str):
        async with semafoor:
            # NIEUW (2 sep 2026, aanvullende bugfix): een kleine,
            # willekeurige opstartvertraging (0-3s) VOORDAT dit symbool
            # zijn eerste IBKR-aanvraag doet -- voorkomt dat de eerste
            # batch van 5 (vrijgegeven door de Semaphore) allemaal
            # EXACT tegelijk hun eerste aanvraag doen, wat het
            # "kudde-effect" bij de rate-limiet (429) live meetekende
            # veroorzaakte, zelfs met de latere jitter op de
            # retry-wachttijd in ibkr_web_api.py.
            await asyncio.sleep(random.uniform(0, 3))
            logger.info(f"--- Symbool: {symbol} ({news_reason}) [{strategie}] ---")
            try:
                if strategie == "TTS":
                    resultaat = await asyncio.to_thread(run_symbol_cycle, symbol, strategie_saldi["TTS"], dry_run)
                else:
                    resultaat = await asyncio.to_thread(run_reversal_symbol_cycle, symbol, strategie_saldi["QFS"], dry_run)
                resultaat["strategie"] = strategie
                return resultaat
            except Exception as e:
                logger.error(f"Onverwachte fout bij {symbol} ({strategie}): {e}")
                return {"status": "error", "symbol": symbol, "strategie": strategie, "reason": str(e)}

    async def _run_all_symbols():
        tasks = [_run_symbol_async(symbool, reden, strategie) for symbool, reden, strategie in taken_lijst]
        return await asyncio.gather(*tasks, return_exceptions=True)

    # asyncio.run() start een nieuwe event loop -- werkt voor de
    # gebruikelijke aanroep vanuit cron/losse scripts. Als run_cycle()
    # ooit vanuit een omgeving met een AL actieve event loop wordt
    # aangeroepen (bijv. binnen een andere async-context), moet dit
    # aangepast worden naar asyncio.get_event_loop().run_until_complete()
    # of vergelijkbaar -- niet getest in dat scenario.
    raw_results = asyncio.run(_run_all_symbols())

    results = []
    for (symbool, _, strategie), r in zip(taken_lijst, raw_results):
        if isinstance(r, Exception):
            logger.error(f"Onafgevangen fout bij {symbool} ({strategie}): {r}")
            results.append({"status": "error", "symbol": symbool, "strategie": strategie, "reason": str(r)})
        else:
            results.append(r)

    # BUGFIX (2 sep 2026): "dispatched" (de status die run_reversal_
    # symbol_cycle() teruggeeft bij een geslaagde live-dispatch)
    # ontbrak in deze lijst -- de eerdere live-run toonde daardoor
    # "0/3 trades uitgevoerd" terwijl CRM in werkelijkheid wél
    # succesvol gedispatcht was. "trade_dispatched" blijft staan voor
    # de OUDE (Fibonacci-gebaseerde) flow in run_symbol_cycle().
    executed = [r for r in results if r["status"] in ("dry_run_complete", "trade_complete", "trade_dispatched", "dispatched")]
    logger.info(f"=== Cyclus afgerond: {len(executed)}/{len(taken_lijst)} trades uitgevoerd (TTS + QFS samen) ===")

    # NIEUW (3 sep 2026, op verzoek): één samenvattend Telegram-bericht
    # met ALLE vandaag gekwalificeerde aandelen (manipulatie-candle
    # bevestigd, bewaking gestart), verstuurd zodra de HELE cyclus
    # klaar is met de ATR-check-fase -- bewust niet per aandeel apart
    # (zou bij 20+ kandidaten te veel ruis geven), en bewust NA afloop
    # i.p.v. live bijgewerkt (de ATR-checks lopen dankzij de Semaphore
    # + jitter toch niet allemaal exact gelijktijdig, maar zijn na een
    # paar minuten altijd wel allemaal afgerond).
    # NIEUW (3 sep 2026, op verzoek; uitgebreid 9 sep 2026 voor de
    # A/B-vergelijking TTS vs QFS): één samenvattend Telegram-bericht
    # met ALLE vandaag gekwalificeerde aandelen, PER STRATEGIE apart
    # weergegeven -- bewust niet per aandeel apart (zou bij 20+
    # kandidaten x 2 strategieën te veel ruis geven), en bewust NA
    # afloop i.p.v. live bijgewerkt.
    if not dry_run and executed:
        qfs_gedispatcht = [r for r in results if r["status"] == "dispatched"]
        tts_gedispatcht = [r for r in results if r["status"] == "trade_dispatched"]

        if qfs_gedispatcht or tts_gedispatcht:
            secties = []
            if tts_gedispatcht:
                regels = [f"• {r['symbol']} ({r.get('action', r.get('direction', '?'))})" for r in tts_gedispatcht]
                secties.append(f"TTS (Touch & Turn, {len(tts_gedispatcht)}x):\n" + "\n".join(regels))
            if qfs_gedispatcht:
                regels = [f"• {r['symbol']} ({r.get('direction', '?')})" for r in qfs_gedispatcht]
                secties.append(f"QFS (Quick Flip, {len(qfs_gedispatcht)}x, bewaking tot 17:00):\n" + "\n".join(regels))

            samenvatting = "🔍 Vandaag gekwalificeerd:\n\n" + "\n\n".join(secties)
            try:
                from telegram_notify import send_telegram_message
                send_telegram_message(samenvatting)
            except Exception as e:
                logger.error(f"Kon samenvattende kwalificatie-melding niet versturen: {e}")

    return {"status": "cycle_complete", "trade_count": len(executed), "results": results}


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    if "--live" in sys.argv:
        # LET OP: nog niet end-to-end getest als volledige cyclus.
        # Vereist een geldige, geauthenticeerde Client Portal Gateway-
        # sessie (zie ibkr_web_api.py) -- geen ib_async-verbinding
        # meer nodig (dat verwachtte het verkeerde IBKR-product, zie
        # ibkr_web_api.py's moduledocstring). Controleer eerst:
        #   python3 -c "from auth_module import check_ibkr_authenticated; print(check_ibkr_authenticated())"
        result = run_cycle(dry_run=False)
        print(f"Resultaat: {result}")
    else:
        # Dry-run: volledige keten testen met voorbeelddata, geen IBKR nodig.
        result = run_cycle(dry_run=True)
        print(f"\nCyclus-resultaat: {result['status']}, {result.get('trade_count', 0)} trade(s)")
        for r in result.get("results", []):
            print(f"  {r['symbol']}: {r['status']} -- {r.get('reason', r.get('order_spec', {}).get('reason', ''))}")
