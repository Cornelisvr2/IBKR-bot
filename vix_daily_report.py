"""
vix_daily_report.py — Dagelijks VIX-rapport via Telegram

Stuurt, vóórdat het handelen begint, één overzichtelijk bericht met:
    - de actuele VIX-waarde
    - de kapitaalverdeling tussen Touch & Turn Scalper en VIX Rider
      voor die dag (via risk_module.get_dynamic_allocation())

Bedoeld om via cron te draaien vlak vóór de eerste handelsmomenten
(scalper om 15:15, VIX Rider om 15:29 CEST) -- bijv. om 15:10 CEST.

Dit is puur INFORMATIEF: het bericht zelf beïnvloedt niets, de
daadwerkelijke allocatie wordt apart (opnieuw) berekend door main.py
en vix_rider_main.py op hun eigen moment. Een kleine kans op een
lichte afwijking tussen dit rapport en de daadwerkelijke uitvoering
is daarom mogelijk als de VIX in de tussenliggende minuten verandert
-- dat is een bewuste, kleine imperfectie, geen bug.

Gebruik:
    python3 vix_daily_report.py
"""

from __future__ import annotations

import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vix_daily_report")


def send_daily_vix_report(total_capital: float = 2000.0) -> dict:
    """
    Haalt de actuele VIX op, berekent de kapitaalverdeling, en
    verstuurt een samenvattend Telegram-bericht.

    Returns:
        dict met de berekende allocatie, voor logging-doeleinden.
    """
    from risk_module import get_current_vix, get_dynamic_allocation
    from telegram_notify import send_telegram_message

    vix = get_current_vix()

    if vix is None:
        message = (
            "⚠️ Dagelijks VIX-rapport: kon de VIX-waarde niet ophalen. "
            "Beide strategieën passen hun eigen veilige-fallback toe "
            "(geen trades bij onbekende VIX)."
        )
        send_telegram_message(urgent=True, text=message)
        logger.warning("VIX onbekend -- rapport verstuurd met waarschuwing.")
        return {"vix": None}

    allocation = get_dynamic_allocation(vix)
    scalper_capital = total_capital * allocation["scalper_pct"]
    vix_rider_capital = total_capital * allocation["macro_panic_pct"]

    # VERWIJDERD (9 sep 2026, op verzoek): de regel "Alleen Touch & Turn
    # Scalper handelt vandaag" (en de "Alleen VIX Rider"/"Beide
    # strategieën"-varianten) was ACHTERHAALD -- die klopte alleen toen
    # TTS en VIX Rider de enige twee strategieën waren. Sinds QFS, RVB
    # en VDB zijn toegevoegd (die ONAFHANKELIJK van deze VIX-schaal
    # handelen, met hun eigen saldo) suggereerde die zin ten onrechte
    # dat er die dag maar één strategie actief zou zijn.

    message = (
        f"📊 Dagelijks VIX-rapport\n\n"
        f"VIX: {vix:.2f}\n\n"
        f"Scalpers (TTS + QFS): {allocation['scalper_pct']*100:.0f}% "
        f"(€{scalper_capital:,.2f})\n"
        f"VIX Rider: {allocation['macro_panic_pct']*100:.0f}% "
        f"(€{vix_rider_capital:,.2f})"
    )
    send_telegram_message(urgent=True, text=message)
    logger.info(f"Dagelijks VIX-rapport verstuurd: VIX={vix:.2f}, scalper_pct={allocation['scalper_pct']}")

    # NIEUW (9 sep 2026, op verzoek): de VIX-waarde ook wegschrijven naar
    # een logbestand, zodat het dashboard 'm kan tonen zonder zelf een
    # live IBKR-aanroep te hoeven doen (dashboard_server.py is bewust
    # een pure log-lezer, zie de moduledocstring daar).
    _log_vix_report(vix, allocation)

    return {"vix": vix, **allocation}


def _log_vix_report(vix: float, allocation: dict) -> None:
    import json
    import os
    from datetime import datetime

    path = os.environ.get("VIX_REPORT_LOG", "/opt/strategy/logs/vix_daily.jsonl")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps({
                "time": datetime.now().isoformat(timespec="seconds"),
                "vix": vix,
                "scalper_pct": allocation["scalper_pct"],
                "macro_panic_pct": allocation["macro_panic_pct"],
            }) + "\n")
    except Exception as e:
        logger.warning(f"Kon VIX-rapport niet naar logbestand schrijven: {e}")


if __name__ == "__main__":
    result = send_daily_vix_report()
    print(result)
