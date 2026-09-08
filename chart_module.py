"""
chart_module.py

Genereert na afloop van een trade een koersgrafiek die laat zien:
- Hoe de koers zich ontwikkelde tijdens (en kort na) de trade
- De box-grenzen (openingsrange)
- De entry-, take-profit-, en stop-loss-niveaus
- Het daadwerkelijke exit-punt

Bedoeld om samen met de bestaande Telegram-tekstmelding te versturen
(via telegram_notify.send_telegram_photo()), zodat je in één oogopslag
kunt zien OF de SL/TP-niveaus goed gekozen waren, en HOE de koers zich
daarna daadwerkelijk gedroeg.

Gebruikt matplotlib met de "Agg"-backend (geen scherm nodig -- dit
draait op een headless server).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

import matplotlib
matplotlib.use("Agg")  # geen display nodig, puur bestanden wegschrijven
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

logger = logging.getLogger("chart_module")

CHART_OUTPUT_DIR = "/opt/strategy/logs/charts"


def genereer_trade_grafiek(
    symbol: str,
    candles: list,
    box_high: float,
    box_low: float,
    entry_price: float,
    take_profit: float,
    stop_loss: float,
    exit_price: float | None,
    direction: str,
    result: str,
    entry_time=None,
) -> str | None:
    """
    Bouwt een lijngrafiek (close-prijzen van de meegegeven candles) met
    horizontale niveaus voor box/entry/TP/SL, en een markering op het
    exit-punt. Schrijft weg als PNG en geeft het bestandspad terug (of
    None bij een fout -- een mislukte grafiek mag nooit de rest van de
    trade-afhandeling blokkeren).

    Args:
        candles: lijst van Candle-objecten (data_module.Candle),
                 idealiter vanaf de openingscandle t/m het moment van
                 sluiten (of de huidige tijd, als de trade nog loopt).
        exit_price: None als de trade nog niet gesloten is (dan wordt
                    er geen exit-marker getekend).
        direction: "LONG" of "SHORT" -- bepaalt de kleur/interpretatie.
        result: bv. "take_profit_hit", "stop_loss_hit",
                "forced_close_90min" -- gebruikt voor de titel/kleur.
        entry_time: NIEUW (8 sep 2026, bugfix op verzoek) -- het exacte
                    tijdstip van instappen. Zonder dit werd entry_price
                    alleen als een VLAKKE LIJN over de hele dag getoond
                    -- daardoor was nergens te zien OP WELK MOMENT de
                    daadwerkelijke entry plaatsvond, wat het onmogelijk
                    maakte om visueel te beoordelen of de entry
                    daadwerkelijk samenviel met een herkenbaar
                    omkeerpatroon. Optioneel (None) voor
                    achterwaartse compatibiliteit -- dan wordt alleen
                    de vlakke lijn getoond, zoals voorheen.
    """
    try:
        os.makedirs(CHART_OUTPUT_DIR, exist_ok=True)

        if not candles:
            logger.warning(f"{symbol}: geen candles meegegeven, grafiek overgeslagen.")
            return None

        tijden = [c.timestamp for c in candles]
        prijzen = [c.close for c in candles]

        fig, ax = plt.subplots(figsize=(10, 6))

        # Prijslijn
        ax.plot(tijden, prijzen, color="#4ecdc4", linewidth=1.5, label="Koers (close)", zorder=3)

        # Box-grenzen (openingsrange)
        ax.axhline(box_high, color="#888888", linestyle=":", linewidth=1, label=f"Box high ({box_high:.2f})")
        ax.axhline(box_low, color="#888888", linestyle=":", linewidth=1, label=f"Box low ({box_low:.2f})")
        ax.axhspan(box_low, box_high, color="#888888", alpha=0.08)

        # Entry -- vlakke referentielijn (blijft, handig om het niveau
        # door de hele dag te kunnen volgen)
        ax.axhline(entry_price, color="#ffd166", linestyle="-", linewidth=1.5, label=f"Entry ({entry_price:.2f})")

        # NIEUW: apart, duidelijk zichtbaar markeringspunt OP het
        # exacte instapmoment, indien bekend -- dit is het daadwerkelijke
        # antwoord op "waar precies stapten we in", i.p.v. alleen het
        # prijsniveau over de hele dag.
        if entry_time is not None:
            ax.scatter([entry_time], [entry_price], color="#ffd166", s=140, zorder=6,
                       marker="^", edgecolors="black", linewidths=1, label=f"Entry-moment ({entry_time.strftime('%H:%M')})")

        # Take-profit en stop-loss
        tp_kleur = "#06d6a0"
        sl_kleur = "#ef476f"
        ax.axhline(take_profit, color=tp_kleur, linestyle="--", linewidth=1.5, label=f"TP ({take_profit:.2f})")
        ax.axhline(stop_loss, color=sl_kleur, linestyle="--", linewidth=1.5, label=f"SL ({stop_loss:.2f})")

        # Exit-marker, indien bekend
        if exit_price is not None and tijden:
            exit_kleur = tp_kleur if result == "take_profit_hit" else (
                sl_kleur if result == "stop_loss_hit" else "#ffd166"
            )
            ax.scatter([tijden[-1]], [exit_price], color=exit_kleur, s=100, zorder=5,
                       marker="X", label=f"Exit ({exit_price:.2f})")

        richting_pijl = "↑ LONG" if direction == "LONG" else "↓ SHORT"
        resultaat_label = {
            "take_profit_hit": "TP geraakt", "stop_loss_hit": "SL geraakt",
            "forced_close_90min": "Geforceerd gesloten (90 min)",
        }.get(result, result)

        ax.set_title(f"{symbol} {richting_pijl} — {resultaat_label}", fontsize=13, fontweight="bold")
        ax.set_xlabel("Tijd")
        ax.set_ylabel("Prijs")
        ax.legend(loc="best", fontsize=8, framealpha=0.9)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        fig.autofmt_xdate()
        ax.grid(True, alpha=0.2)

        bestandsnaam = f"{symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        pad = os.path.join(CHART_OUTPUT_DIR, bestandsnaam)
        fig.tight_layout()
        fig.savefig(pad, dpi=120)
        plt.close(fig)

        logger.info(f"{symbol}: trade-grafiek weggeschreven naar {pad}")
        return pad

    except Exception as e:
        logger.error(f"{symbol}: kon trade-grafiek niet genereren: {e}")
        return None


if __name__ == "__main__":
    # Losstaande test met synthetische data -- geen live IBKR-
    # verbinding nodig, verifieert alleen dat de grafiek-generatie zelf
    # werkt en een geldig PNG-bestand oplevert.
    from dataclasses import dataclass
    from datetime import timedelta

    @dataclass
    class TestCandle:
        timestamp: datetime
        open: float
        high: float
        low: float
        close: float
        volume: float

    basis_tijd = datetime.now().replace(hour=15, minute=30, second=0, microsecond=0)
    testcandles = []
    prijs = 259.0
    import random
    random.seed(42)
    for i in range(20):
        beweging = random.uniform(-1.5, 2.0)
        prijs += beweging
        testcandles.append(TestCandle(
            timestamp=basis_tijd + timedelta(minutes=5 * i),
            open=prijs, high=prijs + 0.5, low=prijs - 0.5, close=prijs, volume=1000,
        ))

    pad = genereer_trade_grafiek(
        symbol="TESTCRM",
        candles=testcandles,
        box_high=264.55, box_low=259.00,
        entry_price=259.50, take_profit=264.00, stop_loss=257.00,
        exit_price=testcandles[-1].close,
        direction="SHORT", result="take_profit_hit",
        entry_time=testcandles[5].timestamp,
    )
    print(f"Grafiek gegenereerd: {pad}")
    print(f"Bestand bestaat: {os.path.exists(pad) if pad else False}")
    if pad:
        print(f"Bestandsgrootte: {os.path.getsize(pad)} bytes")
