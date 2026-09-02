#!/bin/bash
#
# check_ibkr_auth.sh — Touch & Turn Scalper, Dagelijkse IBKR-authenticatiecheck
# ================================================================================
# Wordt aangeroepen door cron, ruim vóór de handelsvensters (bijv. 08:00 CEST),
# zodat je de tijd hebt om een eventuele her-authenticatie goed te keuren op
# je telefoon voordat er om 15:15 getrade moet worden.
#
# Logt naar hetzelfde datumgestempelde logbestand als run_cycle.sh.

set -euo pipefail

# BELANGRIJKE FIX (21 aug 2026): cron leest /etc/environment niet
# automatisch -- dit verklaart waarom de 08:00-melding vandaag niet
# aankwam ("TELEGRAM_BOT_TOKEN of TELEGRAM_CHAT_ID ontbreekt").
set -a
source /etc/environment
set +a

STRATEGY_DIR="/opt/strategy"
LOG_DIR="$STRATEGY_DIR/logs"
DATE_STAMP=$(date +%Y-%m-%d)
LOG_FILE="$LOG_DIR/cycle_${DATE_STAMP}.log"

mkdir -p "$LOG_DIR"

echo "=== IBKR-authenticatiecheck gestart: $(date) ===" >> "$LOG_FILE"

cd "$STRATEGY_DIR"
python3 -c "from auth_module import run_daily_auth_check; print(run_daily_auth_check())" >> "$LOG_FILE" 2>&1

echo "=== IBKR-authenticatiecheck afgerond: $(date) ===" >> "$LOG_FILE"
