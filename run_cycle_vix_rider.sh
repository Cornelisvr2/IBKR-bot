#!/bin/bash
#
# run_cycle_vix_rider.sh — VIX Rider, Cron-wrapper
# ==================================================
# Wordt EENMAAL per dag aangeroepen door cron, vlak vóór marktopening
# (bijv. 15:29 CEST). In tegenstelling tot run_cycle.sh (de scalper,
# die tweemaal per dag kort draait) start dit script vix_rider_main.py,
# dat ZELF een doorlopende monitoringlus draait tot de afkaptijd
# (standaard 17:30 CEST) -- dus dit script blijft actief gedurende
# die hele periode, niet slechts enkele seconden.
#
# Gebruik flock op dezelfde manier als run_cycle.sh, als bescherming
# tegen een per ongeluk dubbele trigger (niet omdat er meerdere
# geplande cron-momenten zijn zoals bij de scalper).

set -euo pipefail

# BELANGRIJKE FIX (21 aug 2026): cron leest /etc/environment niet
# automatisch -- zie run_cycle.sh voor de volledige uitleg.
set -a
source /etc/environment
set +a

STRATEGY_DIR="/opt/strategy"
LOG_DIR="$STRATEGY_DIR/logs"
LOCK_FILE="/tmp/tts_vix_rider_run.lock"
DATE_STAMP=$(date +%Y-%m-%d)
LOG_FILE="$LOG_DIR/vix_rider_cycle_${DATE_STAMP}.log"

mkdir -p "$LOG_DIR"

MODE_ARG="${1:-}"  # leeg = dry-run, "--live" = live

(
    flock -n 200 || { echo "$(date): vorige VIX Rider-cyclus draait nog -- deze run overgeslagen." >> "$LOG_FILE"; exit 1; }

    echo "=== VIX Rider cyclus gestart: $(date) (modus: ${MODE_ARG:-dry-run}) ===" >> "$LOG_FILE"

    cd "$STRATEGY_DIR"
    python3 vix_rider_main.py $MODE_ARG >> "$LOG_FILE" 2>&1

    echo "=== VIX Rider cyclus afgerond: $(date) ===" >> "$LOG_FILE"

) 200>"$LOCK_FILE"
