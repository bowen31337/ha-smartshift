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
from pathlib import Path

# Load .env file if present (host dev environment — Docker uses run_smartshift.sh env vars)
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

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
AMBER_SITE_ID = os.environ.get("AMBER_SITE_ID", "")

# Decision thresholds (cents/kWh)
CHARGE_THRESHOLD = float(os.environ.get("CHARGE_THRESHOLD", "12"))   # Force-charge from grid if below this
PEAK_THRESHOLD = float(os.environ.get("PEAK_THRESHOLD", "28"))       # Peak pricing — discharge hard
SOLAR_PRESERVE_LOW = float(os.environ.get("SOLAR_PRESERVE_LOW", "15"))  # Solar window start — preserve battery
SOLAR_PRESERVE_HIGH = float(os.environ.get("SOLAR_PRESERVE_HIGH", "20")) # Solar window end — start releasing

# Safety limits
SOC_MIN = float(os.environ.get("SOC_MIN", "5"))    # Never discharge below (nearly empty)
SOC_MAX = float(os.environ.get("SOC_MAX", "95"))   # Never charge above
SOC_PEAK_RESERVE = float(os.environ.get("SOC_PEAK_RESERVE", "80"))  # Target SoC to have at peak start

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

def get_amber_prices() -> tuple[float, float] | None:
    """
    Fetch current buy + feed-in prices from Amber Electric API.
    Returns (buy_price, feedin_price) in c/kWh, or None on failure.

    buy_price:    general channel perKwh — what you pay to import (includes all fees)
    feedin_price: feedIn channel perKwh — Amber convention: NEGATIVE = Amber pays you
                  e.g. -14.0 means you earn 14c/kWh when exporting

    Falls back to site discovery if AMBER_SITE_ID not set.
    """
    if not AMBER_API_KEY:
        return None

    site_id = AMBER_SITE_ID
    if not site_id:
        try:
            req = urllib.request.Request(
                "https://api.amber.com.au/v1/sites",
                headers={"Authorization": f"Bearer {AMBER_API_KEY}", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                sites = json.loads(resp.read())
                if sites:
                    site_id = sites[0]["id"]
                    log.info(f"Amber: auto-discovered site {site_id}")
        except Exception as e:
            log.warning(f"Amber site discovery failed: {e}")
            return None

    url = f"https://api.amber.com.au/v1/sites/{site_id}/prices/current?next=0&previous=0"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {AMBER_API_KEY}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            buy = None
            feedin = None
            for item in data:
                if item.get("type") == "CurrentInterval":
                    if item.get("channelType") == "general":
                        buy = float(item["perKwh"])
                        earn = -buy  # just for display
                        log.info(
                            f"Amber buy: {buy:.2f} c/kWh "
                            f"(spot: {item.get('spotPerKwh','?')} c/kWh, {item.get('descriptor','?')})"
                        )
                    elif item.get("channelType") == "feedIn":
                        feedin = float(item["perKwh"])
                        earn = -feedin
                        log.info(f"Amber feed-in: {feedin:.2f} c/kWh (you earn {earn:.2f}c/kWh on export)")
            if buy is not None and feedin is not None:
                return buy, feedin
            if buy is not None:
                return buy, 0.0  # no feed-in channel
    except Exception as e:
        log.warning(f"Amber API error: {e}")
    return None


def get_spot_price_amber() -> float | None:
    """Compatibility wrapper — returns buy price only."""
    result = get_amber_prices()
    return result[0] if result else None


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
            # Add ~18c network/fee adder to approximate real Amber price
            AEMO_NETWORK_ADDER_CKWH = float(os.environ.get("AEMO_NETWORK_ADDER", "18.0"))
            price_ckwh = (price_mwh / 10.0) + AEMO_NETWORK_ADDER_CKWH
            log.info(f"AEMO NSW1 spot: {price_mwh:.2f} $/MWh + {AEMO_NETWORK_ADDER_CKWH:.1f}c network = {price_ckwh:.2f} c/kWh")
            return price_ckwh

        log.warning("AEMO: Could not parse NSW1 price from CSV")
        return None

    except Exception as e:
        log.warning(f"AEMO nemweb error: {e}")
        return None


def get_prices() -> tuple[float, float]:
    """
    Get current (buy_price, feed_in_price) in c/kWh.
    Priority: Amber API → AEMO nemweb fallback (feed_in=0 when using AEMO).
    Raises RuntimeError if all sources fail.
    """
    # 1. Try Amber (returns both buy + feed-in)
    result = get_amber_prices()
    if result is not None:
        return result

    # 2. Try AEMO nemweb (buy price only, no feed-in data)
    price = get_spot_price_aemo_nemweb()
    if price is not None:
        return price, 0.0

    raise RuntimeError(
        "Could not fetch spot price from any source. "
        "Set AMBER_API_KEY env var for Amber Electric API access."
    )


def get_spot_price() -> float:
    """Compatibility wrapper — returns buy price only."""
    return get_prices()[0]


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
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=5) as resp:
            data = json.loads(resp.read())
            soc = int(data.get("soc", 0))
            pb = int(data.get("pb", 0))  # Battery power in W (negative=charging, positive=discharging)
            log.info(f"Battery: SoC={soc}%, Power={pb}W")
            return {"soc": soc, "power": pb, "raw": data}
    except Exception as e:
        log.warning(f"Failed to get battery state: {e} — using last known SoC")
        # Fall back to last known SoC from state file
        state_file = os.environ.get("STATE_FILE", "/home/bowen/ha-smartshift/.current_state.json")
        try:
            with open(state_file) as f:
                last = json.load(f)
                soc = last.get("soc_pct", 50)
                log.info(f"Using last known SoC={soc}% from state file")
                return {"soc": soc, "power": 0, "raw": {}, "error": str(e)}
        except Exception:
            log.error("No fallback SoC available — using 50%")
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
    "self_consumption": 2,  # PV → home → battery → grid (inverter default)
    "discharge": 5,         # TOU mode — force battery export to grid at peak price
    "charge": 5,            # TOU mode — force battery charge (from PV/grid)
}


def _new_ssl_ctx():
    """Create a fresh SSL context (avoids ESP32 connection reuse issues)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _post_inverter(payload: dict) -> dict:
    """POST JSON payload to /setting.cgi. Returns response dict."""
    url = f"{INVERTER_URL}/setting.cgi"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Connection": "close",   # ESP32 has very limited concurrent connections
        },
        method="POST",
    )
    # Use a fresh SSL context per write — ESP32 TLS sessions are single-use
    with urllib.request.urlopen(req, context=_new_ssl_ctx(), timeout=15) as resp:
        return json.loads(resp.read())


def _setbattery(mod_r: int) -> dict:
    """Send a single setbattery command with the given mod_r. Returns response."""
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
    return _post_inverter(payload)


def get_current_mod_r() -> int:
    """Read the current mod_r from the inverter. Returns -1 on error."""
    url = f"{INVERTER_URL}/getdev.cgi?device=4"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=10) as resp:
            data = json.loads(resp.read())
            return int(data.get("mod_r", -1))
    except Exception as e:
        log.warning(f"Could not read current mod_r: {e}")
        return -1


def set_work_mode(mode: str, dry_run: bool = False) -> bool:
    """
    Set inverter battery work mode.
    mode: 'self_consumption' | 'charge' | 'discharge'

    Key behaviour: the AISWEI inverter requires a mode CYCLE to re-activate TOU.
    Simply writing mod_r=5 saves the setting but the TOU engine only re-triggers
    when it sees a transition from a different mode. So we always cycle:
      - For TOU (discharge/charge): write mod_r=2, wait, write mod_r=5
      - For self_consumption: write mod_r=2 directly

    Returns True on success.
    """
    if mode not in WORK_MODE_MAP:
        raise ValueError(f"Unknown mode: {mode}. Valid: {list(WORK_MODE_MAP)}")

    mod_r = WORK_MODE_MAP[mode]

    if dry_run:
        log.info(f"[DRY RUN] Would set mode={mode} (mod_r={mod_r}, with cycle if TOU)")
        return True

    log.info(f"Setting battery mode: {mode} (mod_r={mod_r})")
    try:
        if mod_r == 5:
            # TOU requires mode cycle: set self_consumption first, then TOU
            # This re-triggers the TOU engine on the inverter
            current = get_current_mod_r()
            if current == 5:
                log.info("Already in TOU mode — cycling via self_consumption first")
                r = _setbattery(2)
                if r.get("dat") != "ok":
                    log.error(f"Cycle to self_consumption failed: {r}")
                    return False
                import time as _t; _t.sleep(2)  # ESP32 needs settling time
            r = _setbattery(5)
        else:
            r = _setbattery(mod_r)

        if r.get("dat") != "ok":
            log.error(f"setbattery failed: {r}")
            return False
        log.info(f"Battery mode set to {mode} (mod_r={mod_r}) ✓")
        return True
    except Exception as e:
        log.error(f"Failed to set work mode: {e}")
        return False


# ─── Decision logic ───────────────────────────────────────────────────────────

# ---------------------------------------------------------------------------
# Solar / Weather Forecast
# ---------------------------------------------------------------------------

SOLAR_CAPACITY_KW = float(os.environ.get("SOLAR_CAPACITY_KW", "20.0"))  # panel max output
BATTERY_CAPACITY_KWH = float(os.environ.get("BATTERY_CAPACITY_KWH", "46.0"))
# Kellyville NSW default coords
LATITUDE = float(os.environ.get("LATITUDE", "-33.7114"))
LONGITUDE = float(os.environ.get("LONGITUDE", "150.9457"))


def get_solar_forecast() -> dict:
    """
    Fetch tomorrow's solar production estimate from Open-Meteo.
    Returns dict with:
      - tomorrow_kwh: estimated kWh production tomorrow (simple model)
      - tomorrow_sunny: True if mostly clear (avg cloud < 40%)
      - tomorrow_peak_hours: number of hours with >400 W/m² radiation
      - confidence: 'sunny' | 'partly_cloudy' | 'cloudy'
    Returns empty dict on failure.
    """
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={LATITUDE}&longitude={LONGITUDE}"
            f"&hourly=shortwave_radiation,cloudcover"
            f"&timezone=Australia/Sydney&forecast_days=2"
        )
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        hourly = data["hourly"]
        times = hourly["time"]
        radiation = hourly["shortwave_radiation"]
        cloud = hourly["cloudcover"]

        # Find tomorrow's daylight hours (index 24-47 = tomorrow 00:00-23:00)
        tomorrow_rad = []
        tomorrow_cloud = []
        for i in range(24, min(48, len(times))):
            hour = int(times[i][11:13])
            if 6 <= hour <= 18:  # daylight
                tomorrow_rad.append(radiation[i] or 0)
                tomorrow_cloud.append(cloud[i] or 0)

        if not tomorrow_rad:
            return {}

        # Simple solar estimate: sum(radiation) * panel_efficiency * area_factor
        # radiation is W/m² per hour, for a 20kW system we scale proportionally
        # Peak radiation ~1000 W/m² = 100% of rated capacity
        est_kwh = sum(r / 1000.0 * SOLAR_CAPACITY_KW for r in tomorrow_rad)
        avg_cloud = sum(tomorrow_cloud) / len(tomorrow_cloud)
        peak_hours = sum(1 for r in tomorrow_rad if r > 400)

        if avg_cloud < 30:
            confidence = "sunny"
        elif avg_cloud < 60:
            confidence = "partly_cloudy"
        else:
            confidence = "cloudy"

        result = {
            "tomorrow_kwh": round(est_kwh, 1),
            "tomorrow_sunny": avg_cloud < 40,
            "tomorrow_peak_hours": peak_hours,
            "avg_cloud": round(avg_cloud, 1),
            "confidence": confidence,
        }
        log.info(
            f"Solar forecast: {result['tomorrow_kwh']}kWh tomorrow, "
            f"{result['confidence']} (cloud={result['avg_cloud']}%, "
            f"peak_hours={result['tomorrow_peak_hours']})"
        )
        return result

    except Exception as e:
        log.warning(f"Solar forecast failed: {e}")
        return {}


def get_forecast_earn(lookahead_intervals: int = 6) -> list[float]:
    """
    Fetch next N x 5-min feed-in earn prices from Amber forecast.
    Returns list of earn values (positive = we earn per kWh exported).
    Returns [] on failure.
    """
    if not AMBER_API_KEY:
        return []
    site_id = AMBER_SITE_ID
    if not site_id:
        return []
    try:
        url = (
            f"https://api.amber.com.au/v1/sites/{site_id}/prices/current"
            f"?next={lookahead_intervals}&previous=0"
        )
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {AMBER_API_KEY}", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        earns = []
        for item in data:
            if item.get("channelType") == "feedIn" and item.get("type") == "ForecastInterval":
                earns.append(-float(item["perKwh"]))
        return earns
    except Exception as e:
        log.warning(f"Forecast fetch failed: {e}")
        return []


def decide_action(spot_price: float, soc: int, feed_in_price: float = 0.0) -> str:
    """
    Decide battery action to maximise profit on 46kWh battery.

    Key facts:
      - Never buy from grid (sell price always > 4c — no arbitrage value)
      - feed_in_price: Amber feed-in perKwh (NEGATIVE = we earn that amount on export)
      - 46kWh >> home evening load → must export to grid to monetise full capacity
      - 20kW solar can refill battery in ~2.5h on a sunny day

    Strategy layers:
      1. Safety floor: SoC ≤ SOC_MIN → self_consumption
      2. Weather-adjusted discharge floor:
         - Sunny tomorrow → can drain to SOC_MIN (solar refills fast)
         - Cloudy tomorrow → keep reserve (SOC_FLOOR_CLOUDY, default 30%)
         - Partly cloudy → keep modest reserve (SOC_FLOOR_PARTLY, default 20%)
      3. Price lookahead: hold if 30% better price coming in 30 min
      4. Export threshold: only discharge when earn ≥ threshold

    Safety:
    - NEVER discharge below SOC_MIN (5%)
    """
    export_threshold = float(os.environ.get("EXPORT_THRESHOLD", "10.0"))
    hold_ratio = float(os.environ.get("HOLD_RATIO", "1.3"))
    soc_floor_cloudy = float(os.environ.get("SOC_FLOOR_CLOUDY", "30"))
    soc_floor_partly = float(os.environ.get("SOC_FLOOR_PARTLY", "20"))

    # Safety floor — absolute minimum
    if soc <= SOC_MIN:
        return "self_consumption"

    earn_now = -feed_in_price

    if earn_now < export_threshold:
        return "self_consumption"

    # --- Weather-adjusted discharge floor ---
    solar = get_solar_forecast()
    discharge_floor = SOC_MIN  # default: drain to minimum (sunny assumption)

    if solar:
        confidence = solar.get("confidence", "sunny")
        if confidence == "cloudy":
            discharge_floor = soc_floor_cloudy
            log.info(
                f"Weather: cloudy tomorrow ({solar.get('tomorrow_kwh', '?')}kWh est) "
                f"→ keeping {discharge_floor}% reserve"
            )
        elif confidence == "partly_cloudy":
            discharge_floor = soc_floor_partly
            log.info(
                f"Weather: partly cloudy tomorrow ({solar.get('tomorrow_kwh', '?')}kWh est) "
                f"→ keeping {discharge_floor}% reserve"
            )
        else:
            log.info(
                f"Weather: sunny tomorrow ({solar.get('tomorrow_kwh', '?')}kWh est) "
                f"→ aggressive discharge OK (floor={discharge_floor}%)"
            )

    if soc <= discharge_floor:
        log.info(
            f"SoC={soc}% ≤ weather floor {discharge_floor}% → hold for tomorrow's load"
        )
        return "self_consumption"

    # --- Price lookahead ---
    forecast = get_forecast_earn(lookahead_intervals=6)
    if forecast:
        best_forecast = max(forecast)
        if best_forecast > earn_now * hold_ratio:
            log.info(
                f"Lookahead: earn_now={earn_now:.2f}c, "
                f"best_forecast={best_forecast:.2f}c in 30min → HOLD for better price"
            )
            return "self_consumption"

    log.info(f"Export decision: earn={earn_now:.2f}c/kWh ≥ {export_threshold}c threshold → discharge")
    return "discharge"


def save_state(action: str, spot_price: float, soc: int, feed_in_price: float = 0.0) -> None:
    """Save current state to a JSON file for HA sensor pickup."""
    state_file = os.environ.get(
        "STATE_FILE",
        "/home/bowen/ha-smartshift/.current_state.json"
    )
    state = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "spot_price_ckwh": round(spot_price, 3),
        "feed_in_ckwh": round(feed_in_price, 3),
        "export_earn_ckwh": round(-feed_in_price, 3),
        "soc_pct": soc,
        "thresholds": {
            "charge_below": CHARGE_THRESHOLD,
            "discharge_above": PEAK_THRESHOLD,
            "soc_min": SOC_MIN,
            "soc_max": SOC_MAX,
        },
        "solar_forecast": get_solar_forecast(),
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

    # ESP32 has limited concurrent connections — give it a moment after reads
    import time as _t; _t.sleep(1)

    # Get spot + feed-in prices
    try:
        spot_price, feed_in_price = get_prices()
        earn = -feed_in_price
        log.info(f"Prices: buy={spot_price:.2f}c  export_earn={earn:.2f}c/kWh")
    except RuntimeError as e:
        log.error(str(e))
        return 1

    # Decide action
    if args.mode == "auto":
        action = decide_action(spot_price, soc, feed_in_price)
        export_earn = -feed_in_price
        log.info(
            f"Decision: buy={spot_price:.2f}c  earn={export_earn:.2f}c/kWh  SoC={soc}% → {action}"
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
    save_state(action, spot_price, soc, feed_in_price)

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
