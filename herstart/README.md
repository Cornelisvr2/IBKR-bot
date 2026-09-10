# IBKR-bot — stilgezet op 10 september 2026

## Waarom
Alle vijf strategieën zijn op 10 sep 2026 gebacktest op 240 handelsdagen × 23 aandelen
(Alpaca SIP 5-min historie, exacte botlogica, zie logs/backtest/):

| Strategie      | Bruto per trade | Opmerking |
|----------------|-----------------|-----------|
| TTS            | +0,10R          | kleurregel openingscandle voorspelt niets (≈50/50) |
| QFS 5-min      |  0,00R          | 1.015 trades |
| QFS 15-min     | +0,06R          | oudste helft ≈ 0; NFLX/AMD-uitschieters = selectie-effect |
| RVB            | −0,02R          | 88% van de trades eindigt op 21:55-sluiting; volumefilter doet niets |
| VDB            | +0,03R          | 2.622 trades |

Geen enkele strategie heeft een randje dat €2,50 fees per trade overleeft; de meeste
hebben ook zonder fees geen randje. Daarom stilgezet — niet wegens bugs.

## Wat er die dag ook gevonden is
- De IBKR-feed op het paper-account is vertraagd: om 15:46 kreeg de bot een halve
  openingscandle (soms met de verkeerde kleur). Alle journal-resultaten van vóór
  11 sep 2026 zijn daardoor onbruikbaar.
- Oplossing: Alpaca (gratis paper-account) als realtime databron via alpaca_data.py,
  schakelaar DATA_PROVIDER=alpaca + ALPACA_API_KEY/SECRET in /etc/environment.
  IBKR bleef broker.
- Positiecap €1.000 maakt "1% risico" in de praktijk 0,1%; fees zijn daardoor 0,7R.

## Onderzoekspijplijn (het waardevolste dat overblijft)
- backtest_boxrand.py  — touches van de 15-min boxrand: fade vs breakout, filters
- backtest_qfs.py      — exacte QFS-logica (reversal_pattern_module) op 5- of 15-min
- backtest_rvb_vdb.py  — exacte RVB/VDB-logica
Data wordt gecachet in data/backtest/ (Alpaca, feed=sip, gratis voor >15 min oud).
Een nieuwe hypothese is in minuten te toetsen op jaren historie: eerst hier, dan pas
in de bot.

## Opnieuw opstarten
1. /etc/environment: ALPACA_API_KEY, ALPACA_API_SECRET, DATA_PROVIDER=alpaca
   (plus de bestaande IBKR/Telegram-variabelen).
2. systemd: cp herstart/*.service /etc/systemd/system/ && systemctl daemon-reload
   && systemctl enable --now ibkr-gateway ibkr-dashboard tts-telegram-bot
3. Gateway inloggen (auth_module / Selenium-login), controleren met
   python3 -c "from box_guard import fetch_last_price; print(fetch_last_price('AAPL'))"
   -> status 'R' verwacht.
4. Cron: crontab herstart/crontab.txt  (de regels met "# UIT 2026-09-10:" weer
   ontdoen van dat voorvoegsel).
5. Eerst dry-run: python3 main.py  (state.json wordt aangemaakt).
