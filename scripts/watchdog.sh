#!/bin/bash
# watchdog.sh — Independent failsafe for ha-smartshift
# Runs via systemd timer OUTSIDE HA Docker container.
# If HA hasn't updated state in 15+ minutes, reset inverter to self_consumption
# AND notify Bowen via Telegram.

set -euo pipefail

STATE_FILE="/ha-smartshift/.current_state.json"
LOG_FILE="/ha-smartshift/watchdog.log"
ALERT_LOCKFILE="/tmp/smartshift-watchdog-alerted"
STALE_SECONDS=900  # 15 minutes

# Telegram notification (via OpenClaw CLI or curl)
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-2069029798}"

notify() {
    local msg="$1"
    echo "$msg" >> "$LOG_FILE"
    
    # Try OpenClaw message tool first (if available)
    if command -v openclaw &>/dev/null; then
        openclaw message send --channel telegram --to "$TELEGRAM_CHAT_ID" --message "$msg" 2>/dev/null && return 0
    fi
    
    # Fallback: direct Telegram API
    if [ -n "$TELEGRAM_BOT_TOKEN" ]; then
        curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d chat_id="$TELEGRAM_CHAT_ID" \
            -d text="$msg" \
            -d parse_mode="Markdown" >/dev/null 2>&1
    fi
}

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG_FILE"
}

reset_inverter() {
    cd /ha-smartshift
    source .env 2>/dev/null || true
    uv run python scripts/inverter_control.py --mode self_consumption >> "$LOG_FILE" 2>&1
}

# 1. Check if HA container is running
if ! docker ps --format '{{.Names}}' | grep -q homeassistant; then
    log "ALERT: HA container not running!"
    reset_inverter
    
    # Only notify once per outage (avoid spam)
    if [ ! -f "$ALERT_LOCKFILE" ]; then
        notify "🚨 *SmartShift Watchdog Alert*
HA container is DOWN. Inverter reset to self\_consumption.
Battery will passively cover home load. No grid export until HA is restored.
Time: $(date '+%Y-%m-%d %H:%M AEDT')"
        touch "$ALERT_LOCKFILE"
    fi
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
    reset_inverter
    
    if [ ! -f "$ALERT_LOCKFILE" ]; then
        notify "⚠️ *SmartShift Watchdog Alert*
HA automation stale (${FILE_AGE}s since last update). Inverter reset to self\_consumption.
Check HA container and smartshift automation.
Time: $(date '+%Y-%m-%d %H:%M AEDT')"
        touch "$ALERT_LOCKFILE"
    fi
else
    log "OK: HA alive, state ${FILE_AGE}s old."
    # Clear alert lockfile when recovered
    if [ -f "$ALERT_LOCKFILE" ]; then
        notify "✅ *SmartShift Recovered*
HA automation back online. Normal operation resumed.
Time: $(date '+%Y-%m-%d %H:%M AEDT')"
        rm -f "$ALERT_LOCKFILE"
    fi
fi
