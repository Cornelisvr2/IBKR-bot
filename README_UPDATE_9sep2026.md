# Update 9 sep 2026 — RVB-strategie + dagrapport-dashboard

Uitpakken in de root van de repo (overschrijft order_module.py, journal_module.py
en dashboard_server.py; de rest is nieuw):

    cd ~/IBKR-bot            # lokale clone
    unzip -o ibkr-bot-update-9sep2026.zip
    git add -A && git commit -m "RVB-strategie + dagrapport-dashboard (HTTPS)" && git push

Op de VPS daarna: zie DASHBOARD_DEPLOY.md (stap 0 = eerst `git status`).

Bestanden:
  order_module.py                    gewijzigd — forced_close_time-parameter, spec-velden strategy/box, journal-velden, grafiek
  journal_module.py                  gewijzigd — 8 nieuwe kolommen + automatische header-migratie
  dashboard_server.py                vervangen — dagrapport TTS/QFS/RVB
  rvb_strategy_module.py             nieuw — RVB-logica (zelftest: python3 rvb_strategy_module.py)
  rvb_baseline_builder.py            nieuw — dagelijkse volume-baseline (cron 15:00)
  rvb_scan.py                        nieuw — 5-min-scanner (cron */5 16-21)
  execute_rvb_trade_standalone.py    nieuw — losgekoppeld tradeproces
  run_rvb_cycle.sh                   nieuw — cron-wrapper (crontab-regels in de header)
  ibkr-dashboard.service             nieuw — systemd-unit
  Caddyfile.ibkr                     nieuw — HTTPS + basic-auth
  DASHBOARD_DEPLOY.md                nieuw — uitrolstappen
