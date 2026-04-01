#!/bin/bash
# watchdog.sh — Independent failsafe for ha-smartshift
# Runs via systemd timer OUTSIDE HA Docker container.
# If HA hasn't updated state in 15+ minutes, reset inverter to self_consumption.

set -euo pipefail

STATE_FILE="/home/bowen/ha-smartshift/.current_state.json"
LOG_FILE="/home/bowen/ha-smartshift/watchdog.log"
STALE_SECONDS=900  # 15 minutes
INVERTER_URL="${INVERTER_URL:-https://10.0.0.2}"
INVERTER_SN="${INVERTER_SN:-OE012K01Z2610013}"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG_FILE"
}

# 1. Check if HA container is running
if ! docker ps --format '{{.Names}}' | grep -q homeassistant; then
    log "ALERT: HA container not running! Resetting inverter to self_consumption."
    cd /home/bowen/ha-smartshift
    source .env 2>/dev/null || true
    uv run python scripts/inverter_control.py --mode self_consumption >> "$LOG_FILE" 2>&1
    exit 0
fi

# 2. Check state file staleness
if [ ! -f "$STATE_FILE" ]; then
    log "WARN: State file missing. HA may not have run yet."
    exit 0
fi

FILE_AGE=$(( $(date +%s) - $(stat -c %Y "$STATE_FILE") ))

if [ "$FILE_AGE" -gt "$STALE_SECONDS" ]; then
    log "ALERT: State file is ${FILE_AGE}s old (>${STALE_SECONDS}s). HA automation may be stuck."
    log "Resetting inverter to self_consumption as failsafe."
    cd /home/bowen/ha-smartshift
    source .env 2>/dev/null || true
    uv run python scripts/inverter_control.py --mode self_consumption >> "$LOG_FILE" 2>&1
else
    log "OK: HA alive, state ${FILE_AGE}s old."
fi
