# Update 9 sep 2026 (2) — meldingen routeren

telegram_notify.py   gewijzigd — send_telegram_message() routeert op niveau:
                      🚨/❌ = urgent -> Telegram + logs/events.jsonl
                      ⚠️   = let op -> alleen events.jsonl
                      rest = info   -> alleen events.jsonl
                      Omgevingsvariabele TELEGRAM_LEVEL=urgent|warning|all (standaard urgent).
vix_daily_report.py  gewijzigd — dagrapport gaat bewust wél altijd naar Telegram (urgent=True).
order_module.py      gewijzigd — "SL-order mislukt, alleen TP actief" is nu urgent (positie zonder stop).
dashboard_server.py  gewijzigd — sectie "Meldingen" per dag (urgent bovenaan, info inklapbaar).

Uitrollen op de VPS:
    cd /opt/strategy && git pull && systemctl restart ibkr-dashboard
    systemctl list-units | grep -i telegram     # naam van de bot-service
    systemctl restart <die-service>
