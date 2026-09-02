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
        send_telegram_message(message)
        logger.warning("VIX onbekend -- rapport verstuurd met waarschuwing.")
        return {"vix": None}

    allocation = get_dynamic_allocation(vix)
    scalper_capital = total_capital * allocation["scalper_pct"]
    vix_rider_capital = total_capital * allocation["macro_panic_pct"]

    if allocation["scalper_pct"] == 1.0:
        besluit = "Alleen Touch & Turn Scalper handelt vandaag (lage volatiliteit)."
    elif allocation["macro_panic_pct"] == 1.0:
        besluit = "Alleen VIX Rider handelt vandaag (hoge volatiliteit)."
    else:
        besluit = "Beide strategieën handelen vandaag, met verdeeld kapitaal."

    message = (
        f"📊 Dagelijks VIX-rapport\n\n"
        f"VIX: {vix:.2f}\n\n"
        f"{besluit}\n\n"
        f"Touch & Turn Scalper: {allocation['scalper_pct']*100:.0f}% "
        f"(€{scalper_capital:,.2f})\n"
        f"VIX Rider: {allocation['macro_panic_pct']*100:.0f}% "
        f"(€{vix_rider_capital:,.2f})"
    )
    send_telegram_message(message)
    logger.info(f"Dagelijks VIX-rapport verstuurd: VIX={vix:.2f}, {besluit}")

    return {"vix": vix, **allocation}


if __name__ == "__main__":
    result = send_daily_vix_report()
    print(result)
