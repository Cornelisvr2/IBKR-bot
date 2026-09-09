#!/bin/bash
#
# run_vwap_bounce_cycle.sh — VWAP Dynamic Bounce, Cron-wrapper
# ==============================================================
# Zelfde opzet als run_rvb_cycle.sh (flock tegen overlappende runs,
# dag-gestempeld logbestand, /etc/environment inladen), maar zonder
# baseline-taak -- VDB heeft geen historische volume-baseline nodig,
# de VWAP wordt elke scan vers herberekend uit de candles van vandaag.
#
# Crontab (CEST, ma-vr):
#   */5 16-21 * * 1-5  /opt/strategy/run_vwap_bounce_cycle.sh          # dry-run
#   */5 16-21 * * 1-5  /opt/strategy/run_vwap_bounce_cycle.sh --live   # live
#
# De scan vóór 16:20 doet zelf niets (nog niet genoeg trend-
# geschiedenis) en kost alleen een sessie-check.

set -euo pipefail
set -a
source /etc/environment
set +a

STRATEGY_DIR="/opt/strategy"
LOG_DIR="$STRATEGY_DIR/logs"
DATE_STAMP=$(date +%Y-%m-%d)
mkdir -p "$LOG_DIR" "$STRATEGY_DIR/data"

MODE_ARG="${1:-}"   # leeg = dry-run, "--live" = live
LOCK_FILE="/tmp/vdb_scan.lock"
LOG_FILE="$LOG_DIR/vdb_scan_${DATE_STAMP}.log"

(
    flock -n 200 || { echo "$(date): vorige VDB-scan draait nog -- overgeslagen." >> "$LOG_FILE"; exit 1; }
    echo "=== VDB scan gestart: $(date) (modus: ${MODE_ARG:-dry-run}) ===" >> "$LOG_FILE"
    cd "$STRATEGY_DIR"
    python3 vwap_bounce_scan.py $MODE_ARG >> "$LOG_FILE" 2>&1
    echo "=== VDB scan afgerond: $(date) ===" >> "$LOG_FILE"
) 200>"$LOCK_FILE"
