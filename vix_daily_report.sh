#!/bin/bash
#
# vix_daily_report.sh — Cron-wrapper voor het dagelijkse VIX-rapport
# ====================================================================
# Laadt eerst de omgevingsvariabelen (cron leest /etc/environment niet
# automatisch in -- zie run_cycle.sh voor de volledige uitleg), en
# roept dan het Python-rapportagescript aan.

set -euo pipefail

set -a
source /etc/environment
set +a

cd /opt/strategy
python3 vix_daily_report.py
