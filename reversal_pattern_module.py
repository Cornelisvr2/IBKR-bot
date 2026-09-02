"""
reversal_pattern_module.py

Herkenning van de twee omkeer-candlestick-patronen die de "Quick Flip
Scalper" (a.k.a. Touch & Turn) gebruikt in STAP 3 van de strategie
(zie de video-transcriptie, 1 sep 2026): pas NADAT de openingsrange is
vastgesteld (stap 1) en bevestigd als manipulatie-candle (stap 2), wordt
er op een lager timeframe (5 minuten) gewacht op een van deze twee
patronen, BUITEN de openingsrange, voordat er daadwerkelijk wordt
ingestapt.

Dit is de KERN van de strategie die in de eerdere, Fibonacci-38,2%-
gebaseerde implementatie volledig ontbrak -- die plaatste een passieve
limietorder direct op de rand van de openingsrange, zonder op enige
bevestiging te wachten. Vandaar de zwakke, live gemeten winratio (25%
over 8 trades): niet de kern-theorie was verkeerd, de implementatie was
onvolledig.

Twee patronen, elk met een bullish- en bearish-variant:
1. Hamer / Inverted hamer -- kleine romp, lange lont aan één kant
2. Bullish/Bearish Engulfing -- de romp van de huidige candle omvat
   volledig de romp van de vorige candle, in tegengestelde richting
"""

from dataclasses import dataclass
from typing import Optional

from data_module import Candle


# Standaard technische-analyse-definitie: de lont moet minstens dit
# veelvoud van de romp-grootte zijn om als "significant" te gelden.
MIN_WICK_TO_BODY_RATIO = 2.0

# De romp moet zich in dit bovenste/onderste deel van de totale
# candle-range bevinden (bv. 0.33 = bovenste/onderste derde).
MAX_BODY_POSITION_FRACTION = 0.33


@dataclass
class ReversalSignal:
    pattern_type: str      # "hamer", "inverted_hamer", "bullish_engulfing", "bearish_engulfing"
    direction: str          # "LONG" of "SHORT" -- de richting die dit patroon suggereert
    trigger_price: float    # het prijsniveau waarop de entry-order geplaatst moet worden
    stop_loss_price: float  # structuurgebaseerde SL, afgeleid van het patroon zelf


def _candle_body_size(candle: Candle) -> float:
    return abs(candle.close - candle.open)


def is_hamer(candle: Candle) -> bool:
    """
    Hamer-patroon (bullish omkeer): kleine romp bovenin de range, lange
    lont ONDER de romp (minstens 2x de rompgrootte), nauwelijks lont
    erboven. Duidt op afwijzing van lagere prijzen -- grote kopers
    stapten in tijdens de dip.

    LET OP: dit controleert ALLEEN de VORM van de candle zelf. De video
    vereist EXPLICIET dat dit patroon "must come after a clear red
    negative price movement" -- die voorwaarde wordt HIER niet
    gecontroleerd, maar in check_hamer_setup() hieronder, die de
    voorafgaande candle meeneemt.
    """
    body = _candle_body_size(candle)
    total_range = candle.range
    if total_range <= 0 or body <= 0:
        return False

    body_top = max(candle.open, candle.close)
    body_bottom = min(candle.open, candle.close)
    lower_wick = body_bottom - candle.low
    upper_wick = candle.high - body_top

    romp_bovenin = (body_bottom - candle.low) / total_range >= (1 - MAX_BODY_POSITION_FRACTION)
    lange_onderste_lont = lower_wick >= MIN_WICK_TO_BODY_RATIO * body
    korte_bovenste_lont = upper_wick <= body

    return romp_bovenin and lange_onderste_lont and korte_bovenste_lont


def is_inverted_hamer(candle: Candle) -> bool:
    """
    Inverted-hamer-patroon (bearish omkeer): kleine romp onderin de
    range, lange lont BOVEN de romp, nauwelijks lont eronder. Duidt op
    afwijzing van hogere prijzen.

    Zelfde kanttekening als is_hamer(): alleen de VORM wordt hier
    gecontroleerd, de "clear positive green movement"-voorwaarde zit
    in check_hamer_setup().
    """
    body = _candle_body_size(candle)
    total_range = candle.range
    if total_range <= 0 or body <= 0:
        return False

    body_top = max(candle.open, candle.close)
    body_bottom = min(candle.open, candle.close)
    lower_wick = body_bottom - candle.low
    upper_wick = candle.high - body_top

    romp_onderin = (candle.high - body_top) / total_range >= (1 - MAX_BODY_POSITION_FRACTION)
    lange_bovenste_lont = upper_wick >= MIN_WICK_TO_BODY_RATIO * body
    korte_onderste_lont = lower_wick <= body

    return romp_onderin and lange_bovenste_lont and korte_onderste_lont


def check_hamer_setup(trend_candle: Candle, hamer_candle: Candle,
                        box_high: float, box_low: float,
                        expected_direction: str) -> bool:
    """
    NIEUW (1 sep 2026, bugfix na hertoetsing tegen de video-transcriptie)
    -- controleert de VOLLEDIGE hamer/inverted-hamer-voorwaarde, inclusief
    de voorheen ONTBREKENDE "duidelijke voorafgaande beweging"-eis:

        "This must come after a clear red negative price movement"
        (hamer, video, ~9:10)
        "Would be something that comes after a clear positive green
        movement" (inverted hamer, video, ~10:06)

    trend_candle: de candle DIRECT voorafgaand aan hamer_candle -- moet
    zelf al in de verwachte richting bewegen (rood voor een hamer/LONG-
    setup, groen voor een inverted-hamer/SHORT-setup) als (vereenvoudigde,
    maar directe) bevestiging van een "duidelijke" voorafgaande beweging.

    Geeft True terug als dit een GELDIGE, wachtende hamer-opstelling is
    (nog GEEN definitief signaal -- zie check_hamer_confirmation()
    hieronder voor de bevestigingsstap).
    """
    if expected_direction == "LONG":
        if hamer_candle.low >= box_low:
            return False  # nog niet buiten (onder) de box
        if not trend_candle.is_bearish:
            return False  # geen duidelijke voorafgaande rode beweging
        return is_hamer(hamer_candle)

    elif expected_direction == "SHORT":
        if hamer_candle.high <= box_high:
            return False
        if not trend_candle.is_bullish:
            return False
        return is_inverted_hamer(hamer_candle)

    else:
        raise ValueError(f"Onbekende expected_direction: {expected_direction!r}")


# Kleine veiligheidsmarge op de structuurgebaseerde SL van hamer/inverted-
# hamer-patronen -- video: "the stop loss would be put SLIGHTLY above the
# high" (inverted hamer). Geen concreet bedrag genoemd in de bron, dus
# hier ingevuld als 0,1% van de hamer-candle's eigen range -- een kleine,
# evenredig-schalende marge i.p.v. een vast bedrag (dat bij zeer
# goedkope/dure aandelen disproportioneel zou zijn).
HAMER_SL_BUFFER_FRACTION = 0.001


def check_hamer_confirmation(hamer_candle: Candle, confirmation_candle: Candle,
                                expected_direction: str) -> Optional[ReversalSignal]:
    """
    NIEUW (1 sep 2026, bugfix na hertoetsing tegen de video-transcriptie)
    -- implementeert de EXACTE entry-mechaniek die de video beschrijft
    voor hamer/inverted-hamer (a la "wait for the break of the candle
    and ... enter the trade in the opening of the next candle"), in
    plaats van de eerdere, te simpele aanname (entry direct op het
    high/low-niveau van de hamer zelf).

    Wordt aangeroepen op de candle DIRECT NA een geldige hamer-opstelling
    (bevestigd via check_hamer_setup()). Geeft alleen een signaal terug
    als deze candle de hamer daadwerkelijk "doorbreekt" (bevestigt) --
    anders is de opstelling ongeldig en wordt None geretourneerd (de
    aanroeper moet dan opnieuw op zoek naar een verse opstelling).

    Stop-loss: de low (LONG) of high (SHORT) van de HAMER-candle zelf,
    MET een kleine buffer (HAMER_SL_BUFFER_FRACTION) -- exact zoals de
    video: "the stop loss would be put SLIGHTLY above the high" (voor
    inverted hamer; symmetrisch toegepast voor de hamer/LONG-kant).
    """
    if expected_direction == "LONG":
        if confirmation_candle.high <= hamer_candle.high:
            return None  # geen "break" -- opstelling ongeldig
        buffer = hamer_candle.range * HAMER_SL_BUFFER_FRACTION
        return ReversalSignal(
            pattern_type="hamer", direction="LONG",
            trigger_price=confirmation_candle.open,  # video: "enter ... in the opening of the next candle"
            stop_loss_price=hamer_candle.low - buffer,  # "slightly below" de low
        )

    elif expected_direction == "SHORT":
        if confirmation_candle.low >= hamer_candle.low:
            return None
        buffer = hamer_candle.range * HAMER_SL_BUFFER_FRACTION
        return ReversalSignal(
            pattern_type="inverted_hamer", direction="SHORT",
            trigger_price=confirmation_candle.open,
            stop_loss_price=hamer_candle.high + buffer,  # video: "slightly above" de high
        )

    else:
        raise ValueError(f"Onbekende expected_direction: {expected_direction!r}")


def is_bullish_engulfing(previous: Candle, current: Candle) -> bool:
    """
    Bullish engulfing: de huidige (groene) candle's romp omvat volledig
    de vorige (rode) candle's romp. Duidt op een sterke koopgolf die de
    voorgaande verkoopdruk volledig heeft overschaduwd.
    """
    if not (previous.is_bearish and current.is_bullish):
        return False
    return current.open <= previous.close and current.close >= previous.open


def is_bearish_engulfing(previous: Candle, current: Candle) -> bool:
    """
    Bearish engulfing: de huidige (rode) candle's romp omvat volledig
    de vorige (groene) candle's romp.
    """
    if not (previous.is_bullish and current.is_bearish):
        return False
    return current.open >= previous.close and current.close <= previous.open


def check_engulfing_signal(previous: Candle, current: Candle,
                              box_high: float, box_low: float,
                              expected_direction: str) -> Optional[ReversalSignal]:
    """
    HERNOEMD/VERSMALD (1 sep 2026, na bugfix) van de eerdere
    check_for_reversal_signal() -- behandelt nu UITSLUITEND de
    engulfing-patronen, die (anders dan hamer/inverted-hamer) GEEN
    aparte bevestigingsstap nodig hebben: de video specificeert de
    entry direct bij het herkennen van het patroon zelf ("I like to
    set the entry level already at the high of the previous candle").

    Voor hamer/inverted-hamer: zie check_hamer_setup() +
    check_hamer_confirmation() hierboven -- die vereisen een 2-staps-
    proces (wachten op bevestiging door de daaropvolgende candle) en
    passen niet in deze eenvoudige, 1-staps-signatuur.
    """
    if expected_direction == "LONG":
        if current.low >= box_low:
            return None
        if is_bullish_engulfing(previous, current):
            return ReversalSignal(
                pattern_type="bullish_engulfing", direction="LONG",
                trigger_price=previous.high,
                stop_loss_price=current.low,
            )
        return None

    elif expected_direction == "SHORT":
        if current.high <= box_high:
            return None
        if is_bearish_engulfing(previous, current):
            return ReversalSignal(
                pattern_type="bearish_engulfing", direction="SHORT",
                trigger_price=previous.low,
                stop_loss_price=current.high,
            )
        return None

    else:
        raise ValueError(f"Onbekende expected_direction: {expected_direction!r} (verwacht 'LONG' of 'SHORT')")


if __name__ == "__main__":
    from datetime import datetime

    def maak_candle(o, h, l, c):
        return Candle(timestamp=datetime.now(), open=o, high=h, low=l, close=c, volume=1000)

    print("--- Hamer-patroon-tests ---")
    # Duidelijke hamer: romp bovenin (99-100), lange onderste lont (95-99), geen bovenste lont
    duidelijke_hamer = maak_candle(o=99.5, h=100.0, l=95.0, c=99.8)
    print(f"Duidelijke hamer: {is_hamer(duidelijke_hamer)} (verwacht: True)")

    # Geen hamer: romp in het midden, geen significante lont
    geen_hamer = maak_candle(o=98.0, h=100.0, l=96.0, c=99.0)
    print(f"Normale candle (geen hamer): {is_hamer(geen_hamer)} (verwacht: False)")

    print("\n--- Inverted-hamer-tests ---")
    duidelijke_inv_hamer = maak_candle(o=100.2, h=105.0, l=100.0, c=100.5)
    print(f"Duidelijke inverted hamer: {is_inverted_hamer(duidelijke_inv_hamer)} (verwacht: True)")

    print("\n--- Engulfing-tests ---")
    vorige_rood = maak_candle(o=100.0, h=100.5, l=98.0, c=98.5)
    huidige_groen_engulfing = maak_candle(o=98.0, h=101.5, l=97.5, c=101.0)
    print(f"Bullish engulfing: {is_bullish_engulfing(vorige_rood, huidige_groen_engulfing)} (verwacht: True)")

    vorige_groen = maak_candle(o=98.0, h=100.5, l=97.5, c=100.0)
    huidige_rood_engulfing = maak_candle(o=100.5, h=101.0, l=96.5, c=97.0)
    print(f"Bearish engulfing: {is_bearish_engulfing(vorige_groen, huidige_rood_engulfing)} (verwacht: True)")

    print("\n--- check_engulfing_signal integratietest ---")
    # Scenario: rode openingscandle (LONG verwacht), box_low=176, box_high=182
    # Candle die buiten (onder) de box zakt en dan een bullish engulfing vormt
    vorige = maak_candle(o=176.5, h=177.0, l=175.0, c=175.15)  # rood, binnen/rand van box
    huidige = maak_candle(o=175.0, h=176.8, l=174.5, c=176.6)  # groen, engulft de vorige volledig
    signaal = check_engulfing_signal(vorige, huidige, box_high=182.0, box_low=176.0, expected_direction="LONG")
    print(f"Signaal gevonden: {signaal}")
    print("(verwacht: bullish_engulfing, LONG, trigger=177.0 (high van vorige), SL=174.5 (low van huidige))")

    print("\n--- NIEUW: check_hamer_setup + check_hamer_confirmation (2-staps-tests, bugfix 1 sep 2026) ---")
    # Scenario A: GEEN duidelijke voorafgaande rode beweging -> setup moet AFGEWEZEN worden
    trend_niet_rood = maak_candle(o=175.0, h=175.5, l=174.8, c=175.3)  # groen, geen duidelijke daling
    hamer_kandidaat = maak_candle(o=175.2, h=175.4, l=170.0, c=175.3)  # zou een geldige hamer-VORM zijn
    setup_ongeldig = check_hamer_setup(trend_niet_rood, hamer_kandidaat, box_high=182.0, box_low=176.0, expected_direction="LONG")
    print(f"Setup zonder duidelijke voorafgaande beweging: {setup_ongeldig} (verwacht: False -- dit was de gevonden bug)")

    # Scenario B: WEL een duidelijke voorafgaande rode beweging -> setup moet GELDIG zijn
    trend_wel_rood = maak_candle(o=178.0, h=178.2, l=175.5, c=175.8)  # duidelijk rood/dalend
    setup_geldig = check_hamer_setup(trend_wel_rood, hamer_kandidaat, box_high=182.0, box_low=176.0, expected_direction="LONG")
    print(f"Setup MET duidelijke voorafgaande beweging: {setup_geldig} (verwacht: True)")

    # Bevestiging: de daaropvolgende candle breekt de hamer's high -> signaal, entry op de OPEN van die candle
    bevestigingscandle_breekt = maak_candle(o=175.6, h=176.0, l=175.3, c=175.9)  # high (176.0) > hamer.high (175.4)
    bevestigd_signaal = check_hamer_confirmation(hamer_kandidaat, bevestigingscandle_breekt, expected_direction="LONG")
    print(f"Bevestigingssignaal: {bevestigd_signaal}")
    verwachte_sl = 170.0 - (hamer_kandidaat.range * HAMER_SL_BUFFER_FRACTION)
    print(f"(verwacht: hamer, LONG, trigger=175.6 (OPEN van de bevestigingscandle), SL={verwachte_sl:.4f} (low van de hamer MET buffer))")

    # Geen bevestiging: de daaropvolgende candle breekt de hamer NIET -> geen signaal
    bevestigingscandle_breekt_niet = maak_candle(o=175.3, h=175.35, l=175.0, c=175.1)  # high blijft onder hamer.high
    geen_bevestiging = check_hamer_confirmation(hamer_kandidaat, bevestigingscandle_breekt_niet, expected_direction="LONG")
    print(f"Geen bevestiging: {geen_bevestiging} (verwacht: None)")
