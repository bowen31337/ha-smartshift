#!/usr/bin/env python3
"""
ha-smartshift: Battery charge/discharge automation using spot prices.

Inverter: AISWEI ASW12kH-T3 (Solplanet)
API: https://<INVERTER_IP> (self-signed cert)
Price source: Amber Electric API (or AEMO nemweb fallback)

Usage:
    uv run python scripts/inverter_control.py [--dry-run] [--mode {auto,charge,discharge,self_consumption}]
"""
import argparse
import json
import logging
import os
import sys
import io
import zipfile
import re
import urllib.request
import urllib.error
import ssl
import time
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
INVERTER_URL = os.environ.get("INVERTER_URL", "https://192.168.1.100")
INVERTER_SN = os.environ.get("INVERTER_SN", "")
AMBER_API_KEY = os.environ.get("AMBER_API_KEY", "")
AMBER_STATE = os.environ.get("AMBER_STATE", "NSW")

# Decision thresholds (cents/kWh)
CHARGE_THRESHOLD = float(os.environ.get("CHARGE_THRESHOLD", "5"))
DISCHARGE_THRESHOLD = float(os.environ.get("DISCHARGE_THRESHOLD", "25"))

# Safety limits
SOC_MIN = float(os.environ.get("SOC_MIN", "20"))   # Never discharge below
SOC_MAX = float(os.environ.get("SOC_MAX", "95"))   # Never charge above

# Battery config (read from inverter, these are fallback defaults)
BATTERY_MUF = 5
BATTERY_MOD = 9
BATTERY_NUM = 3
CHARGE_MAX = 100
DISCHARGE_MAX = 10

# ─── SSL context (skip verify for self-signed cert) ───────────────────────────
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


# ─── Spot price functions ──────────────────────────────────────────────────────

def get_spot_price_amber() -> float | None:
    """Fetch current spot price from Amber Electric API. Returns c/kWh or None."""
    if not AMBER_API_KEY:
        return None
    url = f"https://api.amber.com.au/v1/prices/current?next=0&previous=0&state={AMBER_STATE}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {AMBER_API_KEY}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            # Amber returns list of intervals, find 'general' type current interval
            for item in data:
                if item.get("type") == "CurrentInterval":
                    # perKwh is in c/kWh
                    price = float(item["perKwh"])
                    log.info(f"Amber spot price: {price:.2f} c/kWh")
                    return price
    except Exception as e:
        log.warning(f"Amber API error: {e}")
    return None


def get_spot_price_aemo_nemweb() -> float | None:
    """
    Fetch NSW spot price from AEMO nemweb public TradingIS CSV.
    Returns price in c/kWh (converted from $/MWh by dividing by 10) or None on error.

    File format: D,TRADING,PRICE,3,<date>,<runno>,<regionid>,<periodid>,<rrp>,...
    Example: D,TRADING,PRICE,3,"2026/03/31 05:45:00",1,NSW1,69,86.89,...
    """
    try:
        index_url = "https://nemweb.com.au/Reports/Current/TradingIS_Reports/"
        req = urllib.request.Request(
            index_url,
            headers={"User-Agent": "Mozilla/5.0 ha-smartshift/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        # Find most recent PUBLIC_TRADINGIS zip file
        matches = re.findall(r'PUBLIC_TRADINGIS_(\d+)_\d+\.zip', html)
        if not matches:
            log.warning("AEMO: No PUBLIC_TRADINGIS files found in listing")
            return None

        latest_ts = sorted(matches)[-1]
        # Get full filename including unique ID
        full_match = re.findall(rf'(PUBLIC_TRADINGIS_{latest_ts}_\d+\.zip)', html)
        if not full_match:
            return None
        filename = full_match[-1]
        zip_url = f"https://nemweb.com.au/Reports/Current/TradingIS_Reports/{filename}"

        log.info(f"AEMO: fetching {filename}")
        zip_req = urllib.request.Request(
            zip_url,
            headers={"User-Agent": "Mozilla/5.0 ha-smartshift/1.0"},
        )
        with urllib.request.urlopen(zip_req, timeout=15) as resp:
            zip_data = resp.read()

        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            csv_name = zf.namelist()[0]
            csv_content = zf.read(csv_name).decode("utf-8", errors="replace")

        # Parse TradingIS PRICE rows for NSW1
        # Format: D,TRADING,PRICE,3,<date>,<runno>,<regionid>,<periodid>,<rrp>,...
        # The RRP (Regional Reference Price) is in column index 8 (0-based)
        price_mwh = None
        for line in csv_content.splitlines():
            parts = [p.strip().strip('"') for p in line.split(",")]
            if (
                len(parts) >= 9
                and parts[0] == "D"
                and parts[1] == "TRADING"
                and parts[2] == "PRICE"
                and parts[6].upper() == "NSW1"
            ):
                try:
                    price_mwh = float(parts[8])  # RRP column
                except (ValueError, IndexError):
                    pass

        if price_mwh is not None:
            # Convert $/MWh to c/kWh: $/MWh ÷ 10 = c/kWh
            price_ckwh = price_mwh / 10.0
            log.info(f"AEMO NSW1 spot: {price_mwh:.2f} $/MWh = {price_ckwh:.2f} c/kWh")
            return price_ckwh

        log.warning("AEMO: Could not parse NSW1 price from CSV")
        return None

    except Exception as e:
        log.warning(f"AEMO nemweb error: {e}")
        return None


def get_spot_price() -> float:
    """
    Get current NSW spot price in c/kWh.
    Priority: Amber API → AEMO nemweb → raises RuntimeError.
    """
    # 1. Try Amber
    price = get_spot_price_amber()
    if price is not None:
        return price

    # 2. Try AEMO nemweb
    price = get_spot_price_aemo_nemweb()
    if price is not None:
        return price

    raise RuntimeError(
        "Could not fetch spot price from any source. "
        "Set AMBER_API_KEY env var for Amber Electric API access."
    )


# ─── Battery state ────────────────────────────────────────────────────────────

def get_battery_state() -> dict:
    """
    Query inverter for battery state.
    Returns dict with: soc (%), power (W, positive=charging), grid (W)
    """
    url = f"{INVERTER_URL}/getdevdata.cgi?device=4&sn={INVERTER_SN}"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=10) as resp:
            data = json.loads(resp.read())
            soc = int(data.get("soc", 0))
            pb = int(data.get("pb", 0))  # Battery power in W (negative=charging, positive=discharging)
            log.info(f"Battery: SoC={soc}%, Power={pb}W")
            return {"soc": soc, "power": pb, "raw": data}
    except Exception as e:
        log.error(f"Failed to get battery state: {e}")
        return {"soc": 50, "power": 0, "raw": {}, "error": str(e)}


def get_battery_config() -> dict:
    """
    Query inverter for battery configuration (work mode, muf, mod, num, etc.)
    """
    url = f"{INVERTER_URL}/getdefine.cgi"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=10) as resp:
            data = json.loads(resp.read())
            return data
    except Exception as e:
        log.warning(f"Failed to get battery config: {e}")
        return {}


# ─── Work mode control ────────────────────────────────────────────────────────

# Work mode codes based on Solplanet client.py analysis:
# mod_r: 2 = Self-consumption
#         4 = Custom/manual
#         5 = Time-of-use / TOU
# For direct charge/discharge we use Time-of-use with appropriate schedule
# or rely on mod_r=2 (self-consumption) as base

WORK_MODE_MAP = {
    "self_consumption": 2,
    "charge": 5,          # Force charge via TOU override
    "discharge": 5,       # Force discharge via TOU override
}


def _post_inverter(payload: dict) -> dict:
    """POST JSON payload to /setting.cgi. Returns response dict."""
    url = f"{INVERTER_URL}/setting.cgi"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, context=_ssl_ctx, timeout=10) as resp:
        return json.loads(resp.read())


def set_work_mode(mode: str, dry_run: bool = False) -> bool:
    """
    Set inverter battery work mode.
    mode: 'self_consumption' | 'charge' | 'discharge'
    Returns True on success.
    """
    if mode not in WORK_MODE_MAP:
        raise ValueError(f"Unknown mode: {mode}. Valid: {list(WORK_MODE_MAP)}")

    mod_r = WORK_MODE_MAP[mode]

    # Build base battery payload
    payload = {
        "action": "setbattery",
        "device": 4,
        "value": {
            "type": 1,
            "mod_r": mod_r,
            "sn": INVERTER_SN,
            "discharge_max": DISCHARGE_MAX,
            "charge_max": CHARGE_MAX,
            "muf": BATTERY_MUF,
            "mod": BATTERY_MOD,
            "num": BATTERY_NUM,
        },
    }

    if dry_run:
        log.info(f"[DRY RUN] Would set mode={mode} (mod_r={mod_r}): {json.dumps(payload)}")
        return True

    log.info(f"Setting battery mode: {mode} (mod_r={mod_r})")
    try:
        resp = _post_inverter(payload)
        if resp.get("dat") == "ok":
            log.info(f"Battery mode set to {mode} ✓")
            return True
        else:
            log.error(f"Unexpected response: {resp}")
            return False
    except Exception as e:
        log.error(f"Failed to set work mode: {e}")
        return False


# ─── Decision logic ───────────────────────────────────────────────────────────

def decide_action(spot_price: float, soc: int) -> str:
    """
    Decide battery action based on spot price and SoC.

    Rules:
    - spot < CHARGE_THRESHOLD (5c) AND soc < SOC_MAX (95%) → charge
    - spot > DISCHARGE_THRESHOLD (25c) AND soc > SOC_MIN (20%) → discharge
    - else → self_consumption

    Safety hard limits:
    - NEVER discharge below SOC_MIN
    - NEVER charge above SOC_MAX
    """
    if spot_price < CHARGE_THRESHOLD and soc < SOC_MAX:
        return "charge"
    elif spot_price > DISCHARGE_THRESHOLD and soc > SOC_MIN:
        return "discharge"
    else:
        return "self_consumption"


def save_state(action: str, spot_price: float, soc: int) -> None:
    """Save current state to a JSON file for HA sensor pickup."""
    state_file = os.environ.get(
        "STATE_FILE",
        "/home/bowen/ha-smartshift/.current_state.json"
    )
    state = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "spot_price_ckwh": round(spot_price, 3),
        "soc_pct": soc,
        "thresholds": {
            "charge_below": CHARGE_THRESHOLD,
            "discharge_above": DISCHARGE_THRESHOLD,
            "soc_min": SOC_MIN,
            "soc_max": SOC_MAX,
        },
    }
    try:
        with open(state_file, "w") as f:
            json.dump(state, f, indent=2)
        log.info(f"State saved to {state_file}")
    except Exception as e:
        log.warning(f"Could not save state: {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def run(args) -> int:
    """Main control loop. Returns exit code."""
    log.info("=" * 60)
    log.info("ha-smartshift inverter control")
    log.info("=" * 60)

    # Get battery state
    battery = get_battery_state()
    soc = battery["soc"]
    power = battery["power"]

    # Get spot price
    try:
        spot_price = get_spot_price()
    except RuntimeError as e:
        log.error(str(e))
        return 1

    # Decide action
    if args.mode == "auto":
        action = decide_action(spot_price, soc)
        log.info(
            f"Decision: spot={spot_price:.2f}c/kWh, SoC={soc}% → {action}"
        )
    else:
        action = args.mode
        # Safety check even for manual overrides
        if action == "discharge" and soc <= SOC_MIN:
            log.warning(
                f"Manual discharge requested but SoC={soc}% <= {SOC_MIN}% minimum. "
                "Switching to self_consumption."
            )
            action = "self_consumption"
        elif action == "charge" and soc >= SOC_MAX:
            log.warning(
                f"Manual charge requested but SoC={soc}% >= {SOC_MAX}% maximum. "
                "Switching to self_consumption."
            )
            action = "self_consumption"
        log.info(f"Manual mode: {action}")

    # Save state for HA sensors
    save_state(action, spot_price, soc)

    # Apply the mode
    success = set_work_mode(action, dry_run=args.dry_run)

    if success:
        log.info(f"✓ Battery mode: {action} | Spot: {spot_price:.2f}c/kWh | SoC: {soc}%")
        return 0
    else:
        log.error("✗ Failed to set battery mode")
        return 1


def main():
    parser = argparse.ArgumentParser(description="ha-smartshift battery controller")
    parser.add_argument(
        "--mode",
        choices=["auto", "charge", "discharge", "self_consumption"],
        default="auto",
        help="Control mode (default: auto)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done, don't actually change inverter",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print battery status and spot price only, don't change anything",
    )
    args = parser.parse_args()

    if args.status:
        battery = get_battery_state()
        try:
            spot = get_spot_price()
            action = decide_action(spot, battery["soc"])
            print(json.dumps({
                "soc": battery["soc"],
                "power_w": battery["power"],
                "spot_price_ckwh": round(spot, 3),
                "recommended_action": action,
            }, indent=2))
        except Exception as e:
            print(json.dumps({"error": str(e), "soc": battery["soc"]}))
        return

    sys.exit(run(args))


if __name__ == "__main__":
    main()
