#!/bin/bash
# Wrapper for HA shell_command — runs inside Docker container using HA's built-in Python
export INVERTER_URL="https://10.0.0.2"
export INVERTER_SN="OE012K01Z2610013"
export STATE_FILE="/ha-smartshift/.current_state.json"
# Load secrets from .env (not committed to git)
if [ -f /ha-smartshift/.env ]; then
  set -a; source /ha-smartshift/.env; set +a
fi
MODE="${1:-auto}"
/usr/local/bin/python3 /ha-smartshift/scripts/inverter_control.py \
  --mode "$MODE" >> /ha-smartshift/smartshift.log 2>&1
