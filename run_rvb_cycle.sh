#!/bin/bash
#
# run_rvb_cycle.sh — Relative Volume Breakout, Cron-wrapper
# =========================================================
# Zelfde opzet als run_cycle.sh (flock tegen overlappende runs, dag-
# gestempeld logbestand, /etc/environment inladen voor Telegram e.d.),
# maar met twee taken:
#
#   baseline : één keer per dag vóór opening -- bouwt de tijdstip-
#              gematchte volume-baseline (rvb_baseline_builder.py)
#   scan     : elke 5 minuten van 16:30 tot 21:55 -- rvb_scan.py
#
# Crontab (CEST, ma-vr):
#   0   15  * * 1-5  /opt/strategy/run_rvb_cycle.sh baseline
#   */5 16-21 * * 1-5  /opt/strategy/run_rvb_cycle.sh scan          # dry-run
#   */5 16-21 * * 1-5  /opt/strategy/run_rvb_cycle.sh scan --live   # live
#
# De scan vóór 16:30 (16:00-16:25) doet zelf niets (ORB nog niet compleet)
# en kost alleen een sessie-check -- bewust zo gelaten, eenvoudiger dan
# een cron-expressie die exact om 16:35 begint.

set -euo pipefail
set -a
source /etc/environment
set +a

STRATEGY_DIR="/opt/strategy"
LOG_DIR="$STRATEGY_DIR/logs"
DATE_STAMP=$(date +%Y-%m-%d)
mkdir -p "$LOG_DIR" "$STRATEGY_DIR/data"

TASK="${1:-scan}"
MODE_ARG="${2:-}"   # leeg = dry-run, "--live" = live (alleen relevant voor scan)

case "$TASK" in
  baseline)
    LOCK_FILE="/tmp/rvb_baseline.lock"
    LOG_FILE="$LOG_DIR/rvb_baseline_${DATE_STAMP}.log"
    CMD="python3 rvb_baseline_builder.py"
    ;;
  scan)
    LOCK_FILE="/tmp/rvb_scan.lock"
    LOG_FILE="$LOG_DIR/rvb_scan_${DATE_STAMP}.log"
    CMD="python3 rvb_scan.py $MODE_ARG"
    ;;
  *)
    echo "Gebruik: $0 {baseline|scan} [--live]" >&2; exit 2 ;;
esac

(
    flock -n 200 || { echo "$(date): vorige RVB-$TASK draait nog -- overgeslagen." >> "$LOG_FILE"; exit 1; }
    echo "=== RVB $TASK gestart: $(date) (modus: ${MODE_ARG:-dry-run}) ===" >> "$LOG_FILE"
    cd "$STRATEGY_DIR"
    $CMD >> "$LOG_FILE" 2>&1
    echo "=== RVB $TASK afgerond: $(date) ===" >> "$LOG_FILE"
) 200>"$LOCK_FILE"
