#!/bin/bash
#
# run_cycle.sh — Touch & Turn Scalper, Cron-wrapper
# ==================================================
# Wordt aangeroepen door cron (zie crontab -e) op de vaste tijden
# rond markt­opening. Twee taken die main.py zelf niet doet:
#
#   1. Voorkomt overlappende runs via flock -- als een vorige cyclus
#      onverwacht lang duurt, wordt een nieuwe run overgeslagen in
#      plaats van gelijktijdig te draaien (wat tot dubbele orders zou
#      kunnen leiden).
#   2. Logt de output naar een apart, datumgestempeld logbestand in
#      /opt/strategy/logs/, zodat je per dag kunt terugzoeken wat er
#      gebeurd is.
#
# Gebruik (in crontab):
#   /opt/strategy/run_cycle.sh          # dry-run (standaard, veilig)
#   /opt/strategy/run_cycle.sh --live   # live, verstuurt echte orders
#
# LET OP: laat dit op --live staan totdat je zelf bewust hebt
# geverifieerd dat de IBKR-verbinding en paper-trading-modus correct
# werken (zie Fase 7 van het implementatieplan).

set -euo pipefail

# BELANGRIJKE FIX (21 aug 2026): cron gebruikt een eigen, minimale
# omgeving en leest /etc/environment NIET automatisch in -- dit werd
# ontdekt doordat de Telegram-meldingen ontbraken bij live-getriggerde
# trades. 'set -a' zorgt dat alle variabelen die hierna worden
# gedefinieerd automatisch GEËXPORTEERD worden, zodat ze ook
# doorgegeven worden aan python3 main.py EN aan de losgekoppelde
# subprocessen die main.py op zijn beurt start.
set -a
source /etc/environment
set +a

STRATEGY_DIR="/opt/strategy"
LOG_DIR="$STRATEGY_DIR/logs"
LOCK_FILE="/tmp/tts_run_cycle.lock"
DATE_STAMP=$(date +%Y-%m-%d)
LOG_FILE="$LOG_DIR/cycle_${DATE_STAMP}.log"

mkdir -p "$LOG_DIR"

MODE_ARG="${1:-}"  # leeg = dry-run, "--live" = live

(
    flock -n 200 || { echo "$(date): vorige cyclus draait nog -- deze run overgeslagen." >> "$LOG_FILE"; exit 1; }

    echo "=== Cyclus gestart: $(date) (modus: ${MODE_ARG:-dry-run}) ===" >> "$LOG_FILE"

    cd "$STRATEGY_DIR"
    python3 main.py $MODE_ARG >> "$LOG_FILE" 2>&1

    echo "=== Cyclus afgerond: $(date) ===" >> "$LOG_FILE"

) 200>"$LOCK_FILE"
