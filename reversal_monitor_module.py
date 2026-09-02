"""
reversal_monitor_module.py

STAP 3 van de "Quick Flip Scalper"-strategie (zie video-transcriptie,
1 sep 2026): NA het bevestigen van de openingsrange (stap 1+2, ongewijzigd
in entry_module.py/atr_module.py), wacht dit deel actief op een 5-minuten-
candle die BUITEN de range sluit MET een geldig omkeerpatroon (hamer,
inverted hamer, of engulfing -- zie reversal_pattern_module.py).

Dit vervangt de oude aanpak (direct een passieve limietorder plaatsen op
de rand van de range, zonder bevestiging) -- de kern-tekortkoming die de
zwakke, live gemeten winratio (25% over 8 trades) waarschijnlijk
verklaart.

Blokkerende, synchrone functie (zelfde patroon als wait_for_entry_fill()
in order_module.py) -- bedoeld om te draaien in het reeds-bestaande,
losgekoppelde per-symbool-proces (zie main.py's "Trade voor SYMBOOL
gedispatcht naar losgekoppeld proces").
"""

import logging
import time
import json
import os
from datetime import datetime, timedelta, time as dt_time
from typing import Optional

from data_module import get_historical_candles, Candle
from reversal_pattern_module import (
    check_engulfing_signal, check_hamer_setup, check_hamer_confirmation, ReversalSignal,
)

logger = logging.getLogger("reversal_monitor_module")

POLL_INTERVAL_SECONDS = 60

# NIEUW (2 sep 2026, op verzoek: live dashboard) -- elke poll wordt de
# actuele status van de bewaking naar een klein JSON-bestand geschreven,
# zodat een los dashboard (dashboard_server.py) kan tonen wat de bot
# precies binnenkrijgt en beslist, zonder dat dashboard zelf iets van de
# handelslogica hoeft te weten.
MONITOR_STATE_DIR = "/opt/strategy/logs"


def _schrijf_monitor_status(symbol: str, box_high: float, box_low: float,
                               expected_direction: str, deadline: dt_time,
                               candles_vandaag: list, status: str,
                               wachtende_hamer: Optional[Candle] = None,
                               laatste_signaal: Optional[ReversalSignal] = None):
    """
    Schrijft de actuele bewakingsstatus naar een JSON-bestand, puur voor
    het live-dashboard -- heeft GEEN invloed op de handelslogica zelf
    (best-effort: een schrijffout hier mag de bewaking nooit blokkeren).
    """
    try:
        data = {
            "symbol": symbol,
            "direction": expected_direction,
            "box_high": box_high,
            "box_low": box_low,
            "deadline": deadline.strftime("%H:%M:%S"),
            "last_poll_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": status,
            "candles_today": [
                {
                    "timestamp": c.timestamp.strftime("%H:%M:%S"),
                    "open": c.open, "high": c.high, "low": c.low, "close": c.close,
                    "is_bullish": c.is_bullish,
                }
                for c in candles_vandaag
            ],
            "wachtende_hamer": (
                {
                    "timestamp": wachtende_hamer.timestamp.strftime("%H:%M:%S"),
                    "high": wachtende_hamer.high, "low": wachtende_hamer.low,
                } if wachtende_hamer is not None else None
            ),
            "laatste_signaal": (
                {
                    "pattern_type": laatste_signaal.pattern_type,
                    "trigger_price": laatste_signaal.trigger_price,
                    "stop_loss_price": laatste_signaal.stop_loss_price,
                } if laatste_signaal is not None else None
            ),
        }
        pad = os.path.join(MONITOR_STATE_DIR, f"monitor_state_{symbol}.json")
        with open(pad, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.debug(f"{symbol}: kon dashboard-status niet schrijven (niet kritiek): {e}")

# NIEUW (1 sep 2026, bugfix na verse code-review): een 5-minuten-candle
# beschouwen we pas als DEFINITIEF GESLOTEN als er minstens dit veel tijd
# verstreken is sinds het BEGIN van die candle (5 min bar-duur + een
# kleine buffer voor eventuele vertraging van de databron). Zonder dit
# zouden we een NOG LOPENDE candle (met mogelijk nog veranderende high/
# low/close) kunnen markeren als "al gecontroleerd" -- en die daarna
# NOOIT meer met de uiteindelijke, definitieve waarden herevalueren,
# omdat de timestamp dan al in reeds_gecontroleerde_timestamps staat.
CANDLE_SLUIT_BUFFER_SECONDS = 20
CANDLE_DUUR_MINUTEN = 5


def wait_for_reversal_signal(symbol: str, box_high: float, box_low: float,
                                expected_direction: str,
                                deadline: dt_time,
                                poll_interval_seconds: float = POLL_INTERVAL_SECONDS) -> Optional[ReversalSignal]:
    """
    Wacht op 5-minuten-candles tot er een geldig omkeerpatroon verschijnt
    BUITEN de opgegeven openingsrange (box_high/box_low), of tot de
    meegegeven deadline (de bestaande 90-minuten-regel, FORCED_CLOSE_TIME
    uit order_module.py) bereikt wordt.

    expected_direction: "LONG" of "SHORT", bepaald door de kleur van de
    ORIGINELE 15-minuten-openingscandle (rood -> LONG verwacht onder de
    range, groen -> SHORT verwacht boven de range).

    Geeft het gevonden ReversalSignal terug, of None als de deadline
    verstrijkt zonder geldig signaal (= "geen trade vandaag voor dit
    symbool").
    """
    reeds_gecontroleerde_timestamps: set = set()
    vorige_candle: Optional[Candle] = None
    # NIEUW (1 sep 2026, bugfix): een hamer/inverted-hamer-opstelling is
    # nu een 2-STAPS-proces (eerst de opstelling zelf, dan wachten op de
    # bevestiging door de DAAROPVOLGENDE candle -- zie de uitgebreide
    # toelichting in reversal_pattern_module.check_hamer_confirmation()).
    # Deze variabele onthoudt een "wachtende" opstelling tussen twee
    # opeenvolgende polls in.
    wachtende_hamer_candle: Optional[Candle] = None

    logger.info(
        f"{symbol}: bewaking gestart voor omkeerpatroon ({expected_direction} verwacht, "
        f"box=[{box_low:.2f}, {box_high:.2f}], deadline={deadline})"
    )

    while datetime.now().time() < deadline:
        try:
            # KRITIEKE FIX (2 sep 2026, LIVE gevonden op de eerste
            # live-run): period="1d" bleek een REËLE beperking van
            # IBKR's Web API te hebben bij de combinatie met bar="5min"
            # -- gaf herhaaldelijk NIET alle al-gesloten candles van de
            # lopende handelsdag terug (bleef "vastzitten" op 2 candles,
            # ook toen er volgens de klok al 5+ hadden moeten bestaan).
            # Empirisch bevestigd: dezelfde onderliggende candles WERDEN
            # wel teruggegeven met period="2d" (inclusief de candles van
            # gisteren, die we hieronder toch al uitfilteren op datum).
            verse_candles = get_historical_candles(symbol, duration="2d", bar_size="5min")
        except Exception as e:
            logger.warning(f"{symbol}: kon geen verse 5-min-candles ophalen ({e}) -- volgende poging over {poll_interval_seconds}s.")
            time.sleep(poll_interval_seconds)
            continue

        vandaag_candles = [c for c in verse_candles if c.timestamp.date() == datetime.now().date()]
        vandaag_candles.sort(key=lambda c: c.timestamp)

        # NIEUW (1 sep 2026, bugfix): sluit candles uit die NOG NIET
        # definitief gesloten kunnen zijn (zie toelichting bij de
        # constanten hierboven) -- verwerk alleen candles waarvan het
        # BEGIN al minstens CANDLE_DUUR_MINUTEN + buffer geleden is.
        definitief_gesloten_candles = [
            c for c in vandaag_candles
            if datetime.now() >= c.timestamp + timedelta(minutes=CANDLE_DUUR_MINUTEN, seconds=CANDLE_SLUIT_BUFFER_SECONDS)
        ]

        for candle in definitief_gesloten_candles:
            if candle.timestamp in reeds_gecontroleerde_timestamps:
                continue
            reeds_gecontroleerde_timestamps.add(candle.timestamp)

            if vorige_candle is None:
                vorige_candle = candle
                continue

            # STAP A: is er een hamer-opstelling die op DEZE candle wacht
            # op bevestiging (de "break")?
            if wachtende_hamer_candle is not None:
                signaal = check_hamer_confirmation(wachtende_hamer_candle, candle, expected_direction)
                if signaal is not None:
                    logger.info(
                        f"{symbol}: hamer-patroon BEVESTIGD -- {signaal.pattern_type}, "
                        f"trigger={signaal.trigger_price:.2f} (open van bevestigingscandle), "
                        f"SL={signaal.stop_loss_price:.2f}"
                    )
                    _schrijf_monitor_status(symbol, box_high, box_low, expected_direction, deadline,
                                              definitief_gesloten_candles, "bevestigd", laatste_signaal=signaal)
                    return signaal
                else:
                    logger.info(f"{symbol}: hamer-opstelling NIET bevestigd (geen break) -- opstelling vervalt.")
                    wachtende_hamer_candle = None
                    # Val door naar STAP B: deze candle kan zelf ook weer
                    # een NIEUWE opstelling of engulfing-patroon vormen.

            # STAP B: engulfing-check (1-staps, ongewijzigd) of een NIEUWE
            # hamer-opstelling starten (2-staps, wacht op de VOLGENDE candle).
            if wachtende_hamer_candle is None:
                engulfing_signaal = check_engulfing_signal(
                    vorige_candle, candle, box_high=box_high, box_low=box_low,
                    expected_direction=expected_direction,
                )
                if engulfing_signaal is not None:
                    logger.info(
                        f"{symbol}: omkeerpatroon gevonden -- {engulfing_signaal.pattern_type}, "
                        f"trigger={engulfing_signaal.trigger_price:.2f}, SL={engulfing_signaal.stop_loss_price:.2f}"
                    )
                    _schrijf_monitor_status(symbol, box_high, box_low, expected_direction, deadline,
                                              definitief_gesloten_candles, "bevestigd", laatste_signaal=engulfing_signaal)
                    return engulfing_signaal

                if check_hamer_setup(vorige_candle, candle, box_high=box_high, box_low=box_low,
                                       expected_direction=expected_direction):
                    logger.info(
                        f"{symbol}: geldige hamer-opstelling gevonden (candle @ {candle.timestamp}) -- "
                        f"wacht op bevestiging door de volgende candle."
                    )
                    wachtende_hamer_candle = candle

            vorige_candle = candle

        _schrijf_monitor_status(
            symbol, box_high, box_low, expected_direction, deadline,
            definitief_gesloten_candles,
            "hamer_setup_gevonden" if wachtende_hamer_candle is not None else "wachten",
            wachtende_hamer=wachtende_hamer_candle,
        )
        time.sleep(poll_interval_seconds)

    logger.info(f"{symbol}: deadline ({deadline}) bereikt zonder geldig omkeerpatroon -- geen trade vandaag.")
    _schrijf_monitor_status(symbol, box_high, box_low, expected_direction, deadline, [], "verlopen")
    return None


if __name__ == "__main__":
    from datetime import timedelta

    # FIX (2 sep 2026): een vaste "hour=15, minute=45" kon in de TOEKOMST
    # liggen t.o.v. het daadwerkelijke moment waarop de test draait
    # (empirisch gevonden: veroorzaakte valse test-mislukkingen doordat
    # de nieuwe "definitief gesloten"-filter deze candles dan terecht
    # afwees als "nog niet gesloten"). Nu veilig verankerd op 1 uur in
    # het VERLEDEN t.o.v. het daadwerkelijke testmoment, ongeacht hoe
    # laat het is wanneer dit script draait.
    basis_tijd = datetime.now() - timedelta(hours=1)

    def maak_candle(minuten_na_open, o, h, l, c):
        return Candle(
            timestamp=basis_tijd + timedelta(minutes=minuten_na_open),
            open=o, high=h, low=l, close=c, volume=1000,
        )

    poll_resultaten = [
        [maak_candle(0, 176.5, 177.0, 175.0, 175.15)],
        [maak_candle(0, 176.5, 177.0, 175.0, 175.15),
         maak_candle(5, 175.0, 176.8, 174.5, 176.6)],
    ]
    poll_index = {"i": 0}

    def gesimuleerde_get_historical_candles(symbol, duration="1d", bar_size="5min"):
        i = min(poll_index["i"], len(poll_resultaten) - 1)
        poll_index["i"] += 1
        return poll_resultaten[i]

    # Patch de functie in HET EIGEN, lopende namespace (globals()) --
    # NIET via een her-import van de eigen module (dat zou, wanneer dit
    # bestand als __main__ draait, een TWEEDE, aparte modulekopie laden
    # met zijn eigen, ongepatchte get_historical_candles -- een bekende
    # Python-valkuil, empirisch gevonden na een eerste, mislukte poging).
    globals()["get_historical_candles"] = gesimuleerde_get_historical_candles

    korte_deadline = (datetime.now() + timedelta(seconds=3)).time()
    resultaat = wait_for_reversal_signal(
        "TESTSYM", box_high=182.0, box_low=176.0,
        expected_direction="LONG", deadline=korte_deadline,
        poll_interval_seconds=0.1,
    )
    print(f"\nResultaat (engulfing-scenario): {resultaat}")
    print("(verwacht: een ReversalSignal met pattern_type='bullish_engulfing', direction='LONG')")

    print("\n\n=== Tweede test: de NIEUWE 2-staps-hamer-flow (bugfix 1 sep 2026) ===")
    poll_resultaten_hamer = [
        # Poll 1: alleen de eerste candle (nog geen "vorige" beschikbaar)
        [maak_candle(0, 178.0, 178.2, 175.5, 175.8)],  # duidelijk rood -- de "trend"-candle
        # Poll 2: hamer-kandidaat verschijnt (buiten box, hamer-vorm)
        [maak_candle(0, 178.0, 178.2, 175.5, 175.8),
         maak_candle(5, 175.2, 175.4, 170.0, 175.3)],  # hamer-vorm, low=170 (buiten box_low=176)
        # Poll 3: bevestigingscandle breekt de hamer's high (175.4)
        [maak_candle(0, 178.0, 178.2, 175.5, 175.8),
         maak_candle(5, 175.2, 175.4, 170.0, 175.3),
         maak_candle(10, 175.6, 176.0, 175.3, 175.9)],  # high=176.0 > hamer.high=175.4 -> bevestigd
    ]
    poll_index_hamer = {"i": 0}

    def gesimuleerde_get_historical_candles_hamer(symbol, duration="1d", bar_size="5min"):
        i = min(poll_index_hamer["i"], len(poll_resultaten_hamer) - 1)
        poll_index_hamer["i"] += 1
        return poll_resultaten_hamer[i]

    globals()["get_historical_candles"] = gesimuleerde_get_historical_candles_hamer

    korte_deadline_2 = (datetime.now() + timedelta(seconds=3)).time()
    resultaat_hamer = wait_for_reversal_signal(
        "TESTSYM2", box_high=182.0, box_low=176.0,
        expected_direction="LONG", deadline=korte_deadline_2,
        poll_interval_seconds=0.1,
    )
    print(f"\nResultaat (hamer-scenario): {resultaat_hamer}")
    print("(verwacht: hamer, LONG, trigger=175.6 (open van bevestigingscandle), SL=170.0 (low van de hamer))")

    print("\n\n=== Derde test: NIEUWE 'definitief gesloten'-filter (bugfix 1 sep 2026) ===")
    # Simuleer een candle die NET is aangemaakt (timestamp = NU) -- deze
    # mag NOG NIET verwerkt worden, want de candle-periode (5 min) is
    # nog niet voorbij. Gebruik een NOG-NIET-BESTAAND signaal (een
    # duidelijke bullish engulfing) dat WEL zou triggeren als de filter
    # het TEN ONRECHTE als "gesloten" zou behandelen.
    nu = datetime.now()
    te_recente_candles = [
        Candle(timestamp=nu - timedelta(minutes=20), open=176.5, high=177.0, low=175.0, close=175.15, volume=1000),
        Candle(timestamp=nu, open=175.0, high=176.8, low=174.5, close=176.6, volume=1000),  # NET aangemaakt -- nog "lopend"
    ]

    def gesimuleerde_get_historical_candles_recent(symbol, duration="1d", bar_size="5min"):
        return te_recente_candles

    globals()["get_historical_candles"] = gesimuleerde_get_historical_candles_recent

    korte_deadline_3 = (datetime.now() + timedelta(seconds=2)).time()
    resultaat_recent = wait_for_reversal_signal(
        "TESTSYM3", box_high=182.0, box_low=176.0,
        expected_direction="LONG", deadline=korte_deadline_3,
        poll_interval_seconds=0.1,
    )
    print(f"Resultaat (nog-lopende candle): {resultaat_recent}")
    print("(verwacht: None -- de candle van 'nu' is nog niet definitief gesloten, ondanks dat de VORM al een geldig engulfing-patroon zou zijn)")
