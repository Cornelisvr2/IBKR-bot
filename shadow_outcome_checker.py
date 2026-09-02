"""
shadow_outcome_checker.py — Hypothetische uitkomst van schaduw-signalen

Draait NA marktsluiting (bijv. 22:10 CEST). Leest alle signalen die
shadow_scan.py die dag heeft gelogd, haalt voor elk symbool de
candles van de rest van de handelsdag op, en bepaalt of de
hypothetische trade de take-profit of stop-loss zou hebben geraakt --
puur ter analyse, er wordt nooit daadwerkelijk gehandeld.

Bepalingslogica (vereenvoudigd, zie kanttekening in de docstring van
check_outcome_for_signal hieronder voor de beperkingen):
    1. Wacht tot de prijs de ENTRY-prijs bereikt (limietorder-simulatie)
    2. Vanaf dat punt: welke wordt eerst geraakt, TP of SL?
    3. Als de entry nooit bereikt wordt: "entry_never_reached"
    4. Als entry wel geraakt wordt maar geen van beide exits vóór
       sluiting: "still_open_at_close"

Gebruik (via cron, na marktsluiting):
    python3 shadow_outcome_checker.py
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from data_module import get_historical_candles
from shadow_journal_module import read_todays_signals, log_shadow_outcome

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("shadow_outcome_checker")

# IBKR-TARIEVEN VOOR NEDERLAND (bevestigd door gebruiker via de
# officiële prijspagina op 25 aug 2026) -- FRACTIONELE AANDELEN,
# aangezien deze strategie vrijwel altijd fractionele posities
# gebruikt (bijv. 2,8443 GOOGL, 52,0955 NVDA):
#
#   Fixed - IB SmartRouting: 0,05% van de handelswaarde
#   Minimum per order (fractionele aandelen): EUR 1,25
#
# Dit vervangt de eerdere, incorrecte aanname (een Amerikaanse
# per-aandeel-tabel) -- de Nederlandse tabel werkt met een PERCENTAGE
# van de handelswaarde, niet een vast bedrag per aandeel.
FEE_PERCENTAGE = 0.0005  # 0,05% van de handelswaarde
MIN_FEE_PER_ORDER = 1.25  # EUR, minimum voor fractionele aandelen (Fixed-tarief)


def calculate_order_fee(quantity: float, price: float) -> float:
    """
    Berekent de commissie voor één order volgens IBKR's Nederlandse
    Fixed-tarief voor fractionele aandelen: het hoogste van
    (handelswaarde x 0,05%) of het minimumbedrag van EUR 1,25.

    Args:
        quantity: aantal (fractionele) aandelen
        price: prijs per aandeel -- NODIG in dit tarievenmodel, in
               tegenstelling tot een puur per-aandeel-tarief, omdat de
               fee hier een PERCENTAGE van de handelswaarde is.

    BELANGRIJKE OBSERVATIE: bij de meeste van onze trades (handelswaarde
    vaak rond de €500-1.000, gezien de 50%-max-positiewaarde-limiet op
    €2.000 kapitaal) is 0,05% van de handelswaarde (€0,25-0,50) nog
    steeds ONDER het minimum van €1,25 -- dus ook hier is het minimum
    per order vrijwel altijd de daadwerkelijk bepalende factor.
    """
    handelswaarde = quantity * price
    return max(handelswaarde * FEE_PERCENTAGE, MIN_FEE_PER_ORDER)


def check_outcome_for_signal(signal: dict, candles_na_opening: list) -> dict:
    """
    Bepaalt de hypothetische uitkomst van één gelogd signaal, op basis
    van de candles die NA de openingscandle plaatsvonden.

    BEPERKING (belangrijk, eerlijk te vermelden): als zowel TP als SL
    binnen DEZELFDE candle liggen, kunnen we op basis van alleen
    High/Low/Close niet met zekerheid zeggen welke van de twee EERST
    geraakt werd binnen die 15 minuten (daarvoor zou tick-data nodig
    zijn, wat we niet hebben). In dat geval markeren we het resultaat
    als "ambigu_zelfde_candle" -- een eerlijke erkenning van deze
    beperking, in plaats van een gok te presenteren als zekerheid.

    Args:
        signal: dict met "direction", "entry_price", "take_profit",
                "stop_loss", "quantity" (nodig voor de fee-berekening
                relatief aan de PnL in euro's, niet alleen procentueel)

    Returns:
        dict met "outcome", "exit_price_estimate", "pnl_gross" (vóór
        fees), "pnl_net" (na aftrek van 2x FEE_PER_ORDER_ESTIMATE --
        entry + exit).
    """
    direction = signal["direction"]
    entry_price = float(signal["entry_price"])
    take_profit = float(signal["take_profit"])
    stop_loss = float(signal["stop_loss"])
    quantity = float(signal.get("quantity", 0)) if signal.get("quantity") not in (None, "") else 0.0

    entry_bereikt = False

    def _bereken_resultaat(uitkomst: str, exit_price: float | None) -> dict:
        """Berekent bruto en netto (na fees) PnL voor een gegeven uitkomst."""
        pnl_gross = None
        pnl_net = None
        if exit_price is not None and quantity > 0:
            if direction == "LONG":
                pnl_gross = (exit_price - entry_price) * quantity
            else:
                pnl_gross = (entry_price - exit_price) * quantity
            entry_fee = calculate_order_fee(quantity, entry_price)
            exit_fee = calculate_order_fee(quantity, exit_price)
            pnl_net = pnl_gross - entry_fee - exit_fee
        return {
            "outcome": uitkomst, "exit_price_estimate": exit_price,
            "pnl_gross": pnl_gross, "pnl_net": pnl_net,
        }

    for candle in candles_na_opening:
        if not entry_bereikt:
            if direction == "SHORT" and candle.high >= entry_price:
                entry_bereikt = True
            elif direction == "LONG" and candle.low <= entry_price:
                entry_bereikt = True
            if not entry_bereikt:
                continue

        # Vanaf hier: entry is (in deze of een eerdere candle) bereikt.
        tp_geraakt = (candle.low <= take_profit) if direction == "SHORT" else (candle.high >= take_profit)
        sl_geraakt = (candle.high >= stop_loss) if direction == "SHORT" else (candle.low <= stop_loss)

        if tp_geraakt and sl_geraakt:
            return _bereken_resultaat("ambigu_zelfde_candle", None)
        if tp_geraakt:
            return _bereken_resultaat("take_profit_hit", take_profit)
        if sl_geraakt:
            return _bereken_resultaat("stop_loss_hit", stop_loss)

    if not entry_bereikt:
        return _bereken_resultaat("entry_never_reached", None)

    return _bereken_resultaat(
        "still_open_at_90min_cutoff",
        candles_na_opening[-1].close if candles_na_opening else None,
    )


def run_outcome_check(date_str: str = None) -> dict:
    """
    Leest alle signalen van vandaag (of de opgegeven datum), en bepaalt
    voor elk de hypothetische uitkomst.

    KRITIEKE FIX (26 aug 2026): beperkt de candles nu tot uiterlijk
    90 minuten na marktopening (17:00 CEST) -- CONSISTENT met de live
    90-minuten-geforceerde-sluitingsregel die diezelfde dag aan
    order_module.py is toegevoegd. Zonder deze afkap zou de schaduw-
    scan een ONEERLIJKE vergelijking opleveren: hypothetische signalen
    zouden de hele handelsdag de tijd krijgen om TP/SL te raken, terwijl
    live trades na 90 minuten al geforceerd sluiten. Voor een geldige
    A/B-vergelijking (nieuws-selectie vs. brede ATR-scan) moet de
    schaduw-simulatie EXACT hetzelfde regime volgen als live trading.
    """
    from order_module import MARKET_OPEN_TIME, FORCED_CLOSE_MINUTES_AFTER_OPEN

    if date_str is None:
        date_str = datetime.now(timezone.utc).date().isoformat()

    signalen = read_todays_signals(date_str)
    logger.info(f"{len(signalen)} schaduw-signalen gevonden voor {date_str}.")

    resultaten = []
    for signaal in signalen:
        symbol = signaal["symbol"]
        try:
            alle_candles = get_historical_candles(symbol, duration="1d", bar_size="15min")
            # Neem alleen de candles NA de openingscandle (de eerste
            # candle van de dag) -- de openingscandle zelf leverde het
            # signaal op, dus de uitkomst moet op de candles DAARNA
            # gebaseerd zijn. BEPERK bovendien tot 90 minuten na
            # marktopening, consistent met de live geforceerde sluiting.
            candles_na_opening_alles = alle_candles[1:] if len(alle_candles) > 1 else []
            open_minuten = MARKET_OPEN_TIME.hour * 60 + MARKET_OPEN_TIME.minute
            afkap_minuten = open_minuten + FORCED_CLOSE_MINUTES_AFTER_OPEN
            candles_na_opening = [
                c for c in candles_na_opening_alles
                if (c.timestamp.hour * 60 + c.timestamp.minute) < afkap_minuten
            ]

            uitkomst = check_outcome_for_signal(signaal, candles_na_opening)
            log_shadow_outcome({
                "date": date_str, "symbol": symbol,
                "outcome": uitkomst["outcome"],
                "exit_price_estimate": uitkomst["exit_price_estimate"],
                "pnl_gross": uitkomst.get("pnl_gross"),
                "pnl_net": uitkomst.get("pnl_net"),
            })
            resultaten.append({"symbol": symbol, **uitkomst})
        except Exception as e:
            logger.error(f"Kon uitkomst niet bepalen voor {symbol}: {e}")
            resultaten.append({"symbol": symbol, "outcome": "error", "reason": str(e)})

    return {"date": date_str, "count": len(resultaten), "results": resultaten}


if __name__ == "__main__":
    from data_module import Candle

    # Scenario 1: SHORT-signaal, TP geraakt in een latere candle
    signaal = {"direction": "SHORT", "entry_price": 100.0, "take_profit": 98.0, "stop_loss": 101.0, "quantity": 10.0}
    candles = [
        Candle(timestamp=datetime.now(), open=99.0, high=99.5, low=98.5, close=99.2, volume=1000),  # entry nog niet bereikt
        Candle(timestamp=datetime.now(), open=99.2, high=100.5, low=99.0, close=100.0, volume=1000),  # entry bereikt (high >= 100)
        Candle(timestamp=datetime.now(), open=100.0, high=100.2, low=97.5, close=97.8, volume=1000),  # TP geraakt (low <= 98)
    ]
    resultaat = check_outcome_for_signal(signaal, candles)
    print(f"Scenario 1 (SHORT, TP geraakt): {resultaat}")
    entry_fee_s1 = calculate_order_fee(10.0, 100.0)
    exit_fee_s1 = calculate_order_fee(10.0, 98.0)
    print(f"(verwacht: take_profit_hit, pnl_gross=20.00, pnl_net={20.0 - entry_fee_s1 - exit_fee_s1:.2f} "
          f"(entry-fee €{entry_fee_s1:.2f} + exit-fee €{exit_fee_s1:.2f}, beide op het minimum))")

    # Scenario 2: LONG-signaal, SL geraakt
    signaal2 = {"direction": "LONG", "entry_price": 50.0, "take_profit": 52.0, "stop_loss": 49.0, "quantity": 20.0}
    candles2 = [
        Candle(timestamp=datetime.now(), open=50.5, high=50.8, low=49.9, close=50.2, volume=1000),  # entry bereikt (low <= 50)
        Candle(timestamp=datetime.now(), open=50.2, high=50.3, low=48.5, close=48.8, volume=1000),  # SL geraakt (low <= 49)
    ]
    resultaat2 = check_outcome_for_signal(signaal2, candles2)
    print(f"\nScenario 2 (LONG, SL geraakt): {resultaat2}")
    entry_fee_verwacht = calculate_order_fee(20.0, 50.0)
    exit_fee_verwacht = calculate_order_fee(20.0, 49.0)
    print(f"(verwacht: stop_loss_hit, pnl_gross=-20.00, pnl_net={-20.0 - entry_fee_verwacht - exit_fee_verwacht:.2f} "
          f"(entry-fee €{entry_fee_verwacht:.2f} + exit-fee €{exit_fee_verwacht:.2f}))")

    # Scenario 2b: KLEINE, realistische positiegrootte (zoals vandaag's
    # GOOGL-trade: 2,8443 aandelen @ ~€348) -- het MINIMUM per order is
    # hier de bepalende factor, niet het percentage-tarief.
    print(f"\nScenario 2b (fee-berekening bij realistische GOOGL-achtige positie):")
    googl_fee = calculate_order_fee(2.8443, 348.06)
    handelswaarde = 2.8443 * 348.06
    print(f"  Handelswaarde: €{handelswaarde:.2f}, 0,05% daarvan = €{handelswaarde * FEE_PERCENTAGE:.4f}")
    print(f"  calculate_order_fee(2.8443, 348.06) = €{googl_fee:.4f}")
    print(f"  (verwacht: €{MIN_FEE_PER_ORDER:.2f} -- het minimum, want 0,05% van de handelswaarde ligt daaronder)")

    # Scenario 2c: grotere positiewaarde, waar het percentage WEL bepalend wordt
    grote_fee = calculate_order_fee(100, 500.0)
    grote_handelswaarde = 100 * 500.0
    print(f"\n  calculate_order_fee(100 aandelen @ €500) = €{grote_fee:.4f}")
    print(f"  (verwacht: €{grote_handelswaarde * FEE_PERCENTAGE:.2f} -- hier IS het percentage bepalend, ligt boven het minimum)")

    # Scenario 3: entry nooit bereikt
    signaal3 = {"direction": "SHORT", "entry_price": 200.0, "take_profit": 198.0, "stop_loss": 201.0}
    candles3 = [
        Candle(timestamp=datetime.now(), open=190.0, high=191.0, low=189.0, close=190.5, volume=1000),
    ]
    resultaat3 = check_outcome_for_signal(signaal3, candles3)
    print(f"\nScenario 3 (entry nooit bereikt): {resultaat3}")
    print("(verwacht: entry_never_reached)")

    # Scenario 4: ambigu -- TP en SL in dezelfde candle
    signaal4 = {"direction": "LONG", "entry_price": 50.0, "take_profit": 52.0, "stop_loss": 49.0}
    candles4 = [
        Candle(timestamp=datetime.now(), open=50.5, high=52.5, low=48.5, close=50.0, volume=1000),  # entry EN TP EN SL allemaal in 1 candle
    ]
    resultaat4 = check_outcome_for_signal(signaal4, candles4)
    print(f"\nScenario 4 (ambigu, zelfde candle): {resultaat4}")
    print("(verwacht: ambigu_zelfde_candle)")

    # Scenario 5: KRITIEKE FIX-VERIFICATIE (26 aug 2026) -- entry bereikt,
    # maar TP/SL niet geraakt binnen de meegegeven candles (simuleert de
    # 90-minuten-afkap) -- moet nu de LAATSTE slotkoers als schatting
    # gebruiken, consistent met wat de live 90-min-geforceerde-sluiting
    # daadwerkelijk zou doen.
    signaal5 = {"direction": "LONG", "entry_price": 50.0, "take_profit": 55.0, "stop_loss": 45.0, "quantity": 10.0}
    candles5 = [
        Candle(timestamp=datetime.now(), open=50.5, high=50.8, low=49.9, close=50.2, volume=1000),  # entry bereikt
        Candle(timestamp=datetime.now(), open=50.2, high=51.0, low=50.0, close=50.7, volume=1000),  # geen TP/SL, laatste candle binnen de afkap
    ]
    resultaat5 = check_outcome_for_signal(signaal5, candles5)
    print(f"\nScenario 5 (90-min-afkap, geen TP/SL geraakt): {resultaat5}")
    print("(verwacht: still_open_at_90min_cutoff, exit_price_estimate=50.7 (laatste slotkoers), pnl_gross=7.00 (10x(50.7-50)))")
