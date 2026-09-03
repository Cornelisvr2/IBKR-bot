"""
reversal_strategy_module.py

DEEL 3 -- brengt de eerdere twee onderdelen samen tot de volledige,
CORRECTE "Quick Flip Scalper"-strategie (video-transcriptie, 1 sep 2026):

  Stap 1 (ongewijzigd, bestaand): openingsrange vaststellen
          (data_module.get_opening_candle)
  Stap 2 (ongewijzigd, bestaand): ATR-validatie -- is dit een
          manipulatie-candle? (atr_module.validate_opening_range)
  Stap 3 (NIEUW, dit bestand + reversal_monitor_module.py): wacht op
          een bevestigd omkeerpatroon op het 5-minuten-timeframe,
          BUITEN de range, binnen de 90-minuten-deadline

BELANGRIJKSTE VERSCHILLEN met de oude, Fibonacci-gebaseerde aanpak:
- Entry: niet langer een passieve limietorder op de rand van de range,
  maar een ACTIEVE, BEVESTIGDE entry op het omkeerpatroon-triggerniveau
- Take-profit: de TEGENOVERLIGGENDE RAND VAN DE VOLLEDIGE OPENINGSRANGE
  (video: "the box that we drew ... gives us two ... target profit
  levels"), NIET meer het 38,2%-Fibonacci-niveau
- Stop-loss: STRUCTUURGEBASEERD, afgeleid van het omkeerpatroon zelf
  (ReversalSignal.stop_loss_price), NIET meer een vaste 50%-fractie
  van de TP-afstand

Positiegrootte-berekening (1% risico, max-positiewaarde-cap) is
ONGEWIJZIGD overgenomen uit exit_module.py -- dat mechanisme was al
correct en hoeft niet opnieuw uitgevonden te worden.
"""

import logging

from data_module import get_opening_candle, get_historical_candles
from atr_module import calculate_atr, validate_opening_range

logger = logging.getLogger("reversal_strategy_module")

RISK_PER_TRADE_PCT = 0.01       # 1%, zelfde als exit_module.py
MAX_POSITION_VALUE_PCT = 0.50   # zelfde cap als exit_module.py
# LET OP: de R/R-verhouding is nu VARIABEL (structuurgebaseerde SL,
# box-gebaseerde TP) -- geen vaste 2:1-constante meer, in tegenstelling
# tot de oude, Fibonacci-gebaseerde strategie.


def calculate_reversal_position_size(entry_price: float, stop_loss: float,
                                        capital: float, direction: str,
                                        risk_pct: float = RISK_PER_TRADE_PCT,
                                        max_position_value_pct: float = MAX_POSITION_VALUE_PCT) -> tuple[float, float, bool]:
    """
    Zelfde risicobeheer-mechanisme als exit_module.calculate_exit_levels()
    (1% risico per trade, begrensd door een max-positiewaarde-cap) --
    hier apart, want de SL-afstand is nu STRUCTUURGEBASEERD (variabel per
    trade, afhankelijk van het gevonden omkeerpatroon) i.p.v. een vaste
    formule-fractie van de TP-afstand.

    KRITIEKE FIX (1 sep 2026, gevonden bij een vierde hertoetsing tegen
    de video): controleert nu EXPLICIET dat de stop-loss aan de juiste
    KANT van de entry-prijs ligt (onder de entry bij LONG, erboven bij
    SHORT) -- de eerdere check (abs(entry_price - stop_loss) <= 0) ving
    dit NIET op, want een absolute waarde is altijd positief, ongeacht
    de volgorde. Bij een ongewone koerssprong binnen de bevestigingscandle
    (confirmation_candle.open kan in theorie aan de verkeerde kant van
    hamer_candle.low/high liggen) zou de vorige code een STRUCTUREEL
    ONMOGELIJKE trade hebben doorgezet -- bijvoorbeeld een LONG-positie
    met de stop-loss BOVEN de entry in plaats van eronder.

    Geeft (position_size, risk_amount, capped_by_max_value) terug.

    Raises:
        ValueError: als de SL aan de verkeerde kant van de entry ligt,
                    of als de SL-afstand 0 is.
    """
    if direction == "LONG" and stop_loss >= entry_price:
        raise ValueError(
            f"Ongeldige LONG-trade: stop-loss ({stop_loss:.4f}) ligt niet ONDER "
            f"de entry-prijs ({entry_price:.4f}) -- vermoedelijk een koerssprong "
            f"binnen de bevestigingscandle. Trade wordt overgeslagen."
        )
    if direction == "SHORT" and stop_loss <= entry_price:
        raise ValueError(
            f"Ongeldige SHORT-trade: stop-loss ({stop_loss:.4f}) ligt niet BOVEN "
            f"de entry-prijs ({entry_price:.4f}) -- vermoedelijk een koerssprong "
            f"binnen de bevestigingscandle. Trade wordt overgeslagen."
        )

    sl_distance = abs(entry_price - stop_loss)
    if sl_distance <= 0:
        raise ValueError("Stop-loss-afstand is 0 -- geen geldige trade mogelijk.")

    risk_amount = capital * risk_pct
    position_size_from_risk = round(risk_amount / sl_distance, 4)

    max_position_value = capital * max_position_value_pct
    position_size_from_max_value = round(max_position_value / entry_price, 4) if entry_price > 0 else 0

    capped_by_max_value = position_size_from_max_value < position_size_from_risk
    position_size = min(position_size_from_risk, position_size_from_max_value)

    return position_size, risk_amount, capped_by_max_value


def run_reversal_symbol_cycle(symbol: str, capital: float, dry_run: bool) -> dict:
    """
    Vervangt run_symbol_cycle() (main.py) -- voert de VOLLEDIGE, correcte
    3-stappen-strategie uit voor één symbool: box -> ATR-bevestiging ->
    (BLOKKEREND) wachten op omkeerpatroon -> order plaatsen.

    LET OP: dit is een LANGLOPENDE, BLOKKERENDE functie (kan tot 75
    minuten duren, tot FORCED_CLOSE_TIME) -- zelfde ontwerp-overweging
    als execute_managed_trade() in order_module.py. Moet daarom, net als
    de bestaande flow, gedispatcht worden naar een losgekoppeld
    achtergrondproces per symbool, niet direct in de hoofd-cyclus
    aangeroepen worden.
    """
    daily_candles = get_historical_candles(symbol, duration="20d", bar_size="1d")
    intraday_candles = get_historical_candles(symbol, duration="1d", bar_size="15min")
    opening_candle = get_opening_candle(intraday_candles)

    if opening_candle is None:
        reason = f"Geen openingscandle van vandaag beschikbaar voor {symbol} -- cyclus overgeslagen."
        logger.warning(reason)
        return {"status": "skipped", "symbol": symbol, "reason": reason}

    # NIEUW (3 sep 2026, bugfix): de openingscandle-check hierboven ving
    # ALLEEN een mislukte 15-MINUTEN-fetch op -- de DAG-candles (nodig
    # voor de ATR-berekening) hebben hun EIGEN, aparte HTTP-aanroep en
    # dus ook hun EIGEN kans om te mislukken (bv. bij aanhoudende
    # 429-druk, na uitputting van alle retry-pogingen). Zonder deze
    # check kon calculate_atr() een LEGE lijst krijgen en een
    # onopgevangen fout gooien -- live gebeurd bij XOM/MA/CVX (3 sep
    # 2026) tijdens een zware-belasting-test met alle 26 symbolen.
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

    box_high = opening_candle.high
    box_low = opening_candle.low

    # Richting exact zoals de video: groene (bullish) opening -> SHORT
    # verwacht (omkeer boven de box), rode (bearish) opening -> LONG
    # verwacht (omkeer onder de box) -- zelfde richtingslogica als de
    # bestaande entry_module.py, nu toegepast op STAP 3 i.p.v. een
    # directe limietorder.
    if opening_candle.is_bullish:
        expected_direction = "SHORT"
    elif opening_candle.is_bearish:
        expected_direction = "LONG"
    else:
        reason = f"Neutrale openingscandle voor {symbol} -- geen signaal."
        logger.warning(reason)
        return {"status": "skipped", "symbol": symbol, "reason": reason}

    logger.info(
        f"{symbol}: box=[{box_low:.2f}, {box_high:.2f}], "
        f"{'bullish' if opening_candle.is_bullish else 'bearish'} opening -> "
        f"{expected_direction} verwacht"
    )

    if dry_run:
        logger.info(
            f"[DRY-RUN] Zou wachten op omkeerpatroon voor {symbol} "
            f"(box=[{box_low:.2f}, {box_high:.2f}], richting={expected_direction})"
        )
        return {
            "status": "dry_run_complete", "symbol": symbol,
            "box_high": box_high, "box_low": box_low, "direction": expected_direction,
        }

    # KRITIEK (1 sep 2026, zelfde reden als de bestaande execute_trade_
    # standalone.py-dispatch): stap 3 (wachten op een bevestigd
    # omkeerpatroon) kan tot 75 minuten duren. Dit MOET dus, net als de
    # bestaande flow, gedispatcht worden naar een losgekoppeld
    # achtergrondproces -- ANDERS blijft de cron-job zelf tot 75
    # minuten hangen, wat de flock-vergrendeling in run_cycle.sh tot
    # 75 minuten zou vasthouden en de volgende cron-slot zou blokkeren.
    #
    # Belangrijk verschil met de OUDE dispatch: daar was de volledige
    # order-spec (entry/TP/SL) al bekend vóór het dispatchen. Hier is
    # dat nog NIET het geval -- die komen pas uit stap 3 zelf, dus het
    # losgekoppelde proces (execute_reversal_trade_standalone.py) voert
    # ZOWEL de bewaking ALS de trade-uitvoering uit, i.p.v. alleen de
    # laatste stap zoals bij de oude flow.
    import subprocess
    log_path = f"/opt/strategy/logs/dispatch_reversal_{symbol}.log"
    with open(log_path, "a") as log_file:
        subprocess.Popen(
            [
                "python3", "/opt/strategy/execute_reversal_trade_standalone.py",
                "--symbol", symbol,
                "--box-high", str(box_high),
                "--box-low", str(box_low),
                "--direction", expected_direction,
                "--capital", str(capital),
            ],
            stdout=log_file, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    logger.info(f"Reversal-bewaking + trade voor {symbol} gedispatcht naar losgekoppeld proces.")
    return {"status": "dispatched", "symbol": symbol, "direction": expected_direction}


if __name__ == "__main__":
    print("Dit bestand bevat alleen orkestratie-logica die live IBKR-data")
    print("en de volledige bestaande module-set (order_module.py etc.) vereist.")
    print("Losstaand testen: zie reversal_pattern_module.py en")
    print("reversal_monitor_module.py voor de geïsoleerde, wél losstaand")
    print("testbare onderdelen (patroonherkenning + bewakingslus-logica).")
    print()
    print("Syntax-check:")
    import ast
    with open(__file__) as f:
        ast.parse(f.read())
    print("OK -- geen syntaxfouten.")
