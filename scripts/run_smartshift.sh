#!/bin/bash
# Wrapper for HA shell_command — runs inside Docker container using HA's built-in Python
export INVERTER_URL="https://10.0.0.2"
export INVERTER_SN="OE012K01Z2610013"
export STATE_FILE="/ha-smartshift/.current_state.json"
MODE="${1:-auto}"
/usr/local/bin/python3 /ha-smartshift/scripts/inverter_control.py \
  --mode "$MODE" >> /ha-smartshift/smartshift.log 2>&1
