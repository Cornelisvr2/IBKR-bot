"""
risk_module.py — Touch & Turn Scalper, Circuit Breakers

Twee onafhankelijke veiligheidschecks, uitgevoerd vóór elke cyclus in
main.py, die samen bepalen of er die dag nog nieuwe trades geopend
mogen worden:

    1. VIX-check: geen nieuwe trades als de VIX boven een drempel
       staat (standaard 30) -- een hoge VIX duidt op abnormale
       marktvolatiliteit, waarbij de mean-reversion-aanname van deze
       strategie minder betrouwbaar is.
    2. 3%-dagstop: geen nieuwe trades meer als het gerealiseerde
       verlies vandaag al 3% van het kapitaal heeft geraakt.

BELANGRIJKE BEPERKING: de dagstop telt alleen GEREALISEERDE verliezen
(gesloten trades, via state_module's trade_log). Ongerealiseerde
verliezen van nog OPENSTAANDE posities tellen niet mee -- dat vereist
continue live-prijsmonitoring van open posities, wat nog niet gebouwd
is.

Gebruik:
    from risk_module import check_circuit_breakers
    result = check_circuit_breakers(capital=1000.0)
    if not result["safe_to_trade"]:
        print(result["reason"])
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger("risk_module")

VIX_SYMBOL = "VIX"
DEFAULT_VIX_THRESHOLD = 30.0
DEFAULT_DAILY_LOSS_PCT = 0.03  # 3%


def get_current_vix() -> float | None:
    """
    Haalt de actuele VIX-waarde op via de Client Portal Web API.

    VIX is een INDEX (secType "IND"), geen aandeel -- vandaar de
    expliciete sec_type="IND" bij het conid-opzoeken.

    Returns:
        De laatste slotkoers van VIX, of None als het niet lukt op te
        halen.
    """
    from ibkr_web_api import resolve_conid, get_historical_bars

    conid = resolve_conid(VIX_SYMBOL, sec_type="IND")
    if conid is None:
        logger.error("Kon geen conid vinden voor VIX -- VIX-check kan niet worden uitgevoerd.")
        return None

    bars = get_historical_bars(conid, period="1d", bar="15min")
    if not bars:
        logger.error("Geen VIX-data ontvangen -- VIX-check kan niet worden uitgevoerd.")
        return None

    try:
        return float(bars[-1]["c"])
    except (KeyError, IndexError, ValueError, TypeError) as e:
        logger.error(f"Kon VIX-waarde niet parsen uit laatste bar: {e}")
        return None


def get_dynamic_allocation(
    vix: float, low_vix: float = 20.0, high_vix: float = 30.0
) -> dict:
    """
    Berekent een glijdende kapitaalverdeling tussen de Touch & Turn
    Scalper en de Macro Panic/VIX Rider-strategie, op basis van de
    actuele VIX.

        VIX <= low_vix   -> Scalper 100% / Macro Panic 0%
        VIX >= high_vix  -> Scalper 0%   / Macro Panic 100%
        Daartussen       -> lineaire interpolatie

    Returns:
        dict met "scalper_pct" en "macro_panic_pct" (beide 0.0-1.0,
        samen altijd exact 1.0), plus "vix" ter referentie.
    """
    if vix <= low_vix:
        scalper_pct = 1.0
    elif vix >= high_vix:
        scalper_pct = 0.0
    else:
        scalper_pct = 1.0 - ((vix - low_vix) / (high_vix - low_vix))

    macro_panic_pct = 1.0 - scalper_pct

    return {
        "vix": vix,
        "scalper_pct": round(scalper_pct, 4),
        "macro_panic_pct": round(macro_panic_pct, 4),
    }


def get_allocated_capital(total_capital: float, strategy: str) -> dict:
    """
    Haalt de actuele VIX op en berekent hoeveel van het totale kapitaal
    een specifieke strategie op dit moment mag gebruiken.

    Args:
        total_capital: het volledige beschikbare kapitaal (bijv. €2000)
        strategy: "scalper" of "macro_panic"

    Returns:
        dict met "allocated_capital" (float), "allocation_pct" (float),
        "vix" (float of None).
    """
    vix = get_current_vix()

    if vix is None:
        logger.warning("VIX onbekend -- geen kapitaal toegewezen aan welke strategie dan ook.")
        return {"allocated_capital": 0.0, "allocation_pct": 0.0, "vix": None}

    allocation = get_dynamic_allocation(vix)
    pct_key = "scalper_pct" if strategy == "scalper" else "macro_panic_pct"
    pct = allocation[pct_key]

    return {
        "allocated_capital": round(total_capital * pct, 2),
        "allocation_pct": pct,
        "vix": vix,
    }


def check_vix_threshold(threshold: float = DEFAULT_VIX_THRESHOLD) -> dict:
    """
    Toetst of de actuele VIX onder de drempel staat.

    Returns:
        dict met "safe" (bool), "vix" (float of None), "reason" (str).
    """
    vix = get_current_vix()

    if vix is None:
        reason = "VIX-waarde kon niet worden opgehaald -- veiligheidshalve geen nieuwe trades."
        logger.warning(reason)
        return {"safe": False, "vix": None, "reason": reason}

    if vix > threshold:
        reason = f"VIX ({vix:.2f}) boven drempel ({threshold}) -- geen nieuwe trades."
        logger.warning(reason)
        return {"safe": False, "vix": vix, "reason": reason}

    reason = f"VIX ({vix:.2f}) binnen drempel ({threshold})."
    logger.info(reason)
    return {"safe": True, "vix": vix, "reason": reason}


def check_daily_loss_limit(capital: float, max_loss_pct: float = DEFAULT_DAILY_LOSS_PCT) -> dict:
    """
    Toetst of het gerealiseerde verlies van VANDAAG de daglimiet nog
    niet heeft geraakt.

    Returns:
        dict met "safe" (bool), "realized_pnl_today" (float),
        "limit" (float), "reason" (str).
    """
    from state_module import load_state

    state = load_state()
    today = datetime.now(timezone.utc).date().isoformat()

    trades_today = [t for t in state.get("trade_log", []) if t.get("date") == today]
    realized_pnl_today = sum(t.get("pnl", 0) for t in trades_today)

    loss_limit = -abs(capital * max_loss_pct)

    if realized_pnl_today <= loss_limit:
        reason = (
            f"Dagverlies (€{realized_pnl_today:.2f}) heeft de limiet "
            f"(€{loss_limit:.2f}, {max_loss_pct*100:.0f}% van €{capital:.2f}) geraakt -- "
            f"geen nieuwe trades vandaag."
        )
        logger.warning(reason)
        return {"safe": False, "realized_pnl_today": realized_pnl_today, "limit": loss_limit, "reason": reason}

    reason = f"Dagresultaat (€{realized_pnl_today:.2f}) binnen de limiet (€{loss_limit:.2f})."
    logger.info(reason)
    return {"safe": True, "realized_pnl_today": realized_pnl_today, "limit": loss_limit, "reason": reason}


def check_circuit_breakers(
    capital: float, vix_threshold: float = DEFAULT_VIX_THRESHOLD,
    max_daily_loss_pct: float = DEFAULT_DAILY_LOSS_PCT,
) -> dict:
    """
    Voert beide circuit breakers uit.

    Returns:
        dict met "safe_to_trade" (bool), "reason" (str, gecombineerd),
        "vix_check" en "daily_loss_check" (losse sub-resultaten).
    """
    vix_result = check_vix_threshold(vix_threshold)
    daily_loss_result = check_daily_loss_limit(capital, max_daily_loss_pct)

    safe_to_trade = vix_result["safe"] and daily_loss_result["safe"]

    reasons = []
    if not vix_result["safe"]:
        reasons.append(vix_result["reason"])
    if not daily_loss_result["safe"]:
        reasons.append(daily_loss_result["reason"])

    reason = " | ".join(reasons) if reasons else "Beide circuit breakers OK."

    return {
        "safe_to_trade": safe_to_trade,
        "reason": reason,
        "vix_check": vix_result,
        "daily_loss_check": daily_loss_result,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import tempfile
    import os
    import state_module

    with tempfile.TemporaryDirectory() as tmp_dir:
        state_module.STATE_FILE_PATH = os.path.join(tmp_dir, "state.json")

        result = check_daily_loss_limit(capital=1000.0)
        print(f"Scenario 1 (geen trades vandaag): safe={result['safe']}, pnl={result['realized_pnl_today']}")

        from state_module import add_trade_to_log
        add_trade_to_log({"symbol": "AAPL", "pnl": -10.0})
        add_trade_to_log({"symbol": "MSFT", "pnl": 5.0})
        result = check_daily_loss_limit(capital=1000.0)
        print(f"Scenario 2 (klein verlies, -€5 totaal): safe={result['safe']}, pnl={result['realized_pnl_today']}")

        add_trade_to_log({"symbol": "NVDA", "pnl": -30.0})
        result = check_daily_loss_limit(capital=1000.0)
        print(f"Scenario 3 (limiet geraakt, -€35 totaal): safe={result['safe']}, pnl={result['realized_pnl_today']}")
        print(f"   Reden: {result['reason']}")

        state_module.STATE_FILE_PATH = os.path.join(tmp_dir, "state2.json")
        add_trade_to_log({"symbol": "OLD", "pnl": -500.0, "date": "2020-01-01"})
        result = check_daily_loss_limit(capital=1000.0)
        print(f"\nScenario 4 (oude trade, andere datum): safe={result['safe']}, pnl={result['realized_pnl_today']} (verwacht: 0.0, want -500 was gisteren)")

    print("\n--- Dry-run tests klaar (VIX vereist live Gateway, niet getest hier). ---")
    print("Live test: python3 -c \"from risk_module import check_vix_threshold; print(check_vix_threshold())\"")

    for test_vix in [12, 20, 22.5, 25, 27.5, 30, 35]:
        result = get_dynamic_allocation(test_vix)
        print(
            f"VIX {test_vix:5.1f} -> Scalper {result['scalper_pct']*100:5.1f}% "
            f"/ Macro Panic {result['macro_panic_pct']*100:5.1f}%"
        )

    for test_vix in [5, 15, 22, 28, 40]:
        result = get_dynamic_allocation(test_vix)
        totaal = result["scalper_pct"] + result["macro_panic_pct"]
        assert abs(totaal - 1.0) < 0.0001, f"Som klopt niet bij VIX {test_vix}: {totaal}"
    print("\n(geverifieerd: scalper_pct + macro_panic_pct = 1.0 bij alle geteste VIX-waarden)")

    allocation = get_dynamic_allocation(25.0)
    toegewezen = 2000.0 * allocation["scalper_pct"]
    print(f"\nScenario 11 (VIX 25, kapitaal €2000): scalper krijgt €{toegewezen:.2f}")
    print("(verwacht: €1000.00, want VIX 25 = precies het midden van de 20-30 bandbreedte)")
