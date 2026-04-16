#!/usr/bin/env python3
"""SmartShift Advisor v2 — profit-maximizing battery strategy.

Improvement over v1: uses learned home consumption profile + Amber forward
prices + solar forecast to decide WHEN to discharge (export) rather than
blindly holding while feed-in is ~0c.

Algorithm (greedy + reservation):
  1. Pull forward price curve (next ~24h, 30-min resolution) from Amber.
  2. Pull current SOC and battery specs from HA.
  3. Load learned consumption profile from .consumption_profile.json.
  4. For each 30-min forward interval, compute:
        expected_load_kwh   (from profile)
        expected_solar_kwh  (simple daylight heuristic + today's forecast)
        net_surplus_kwh     = solar - load  (positive = free export capacity)
  5. Rank intervals by feed_in_price descending.
  6. Reserve energy for load during peak-buy hours (buy_price > charge_threshold).
     Then allocate remaining battery capacity to export during top-feed-in windows
     where feed_in_price > export_threshold.
  7. Decide immediate action based on current interval:
        - If in a planned export window → "export_peak"
        - If buy price < charge_threshold AND SOC < soc_max → "charge"
        - If buy price > discharge_threshold AND SOC > discharge_floor → "discharge" (self-use)
        - Else "hold"
  8. Write advice JSON with plan window + reasoning.

Reads:
  - $SMARTSHIFT_DIR/.ha_token
  - $SMARTSHIFT_DIR/.consumption_profile.json
  - HA API (current SOC, solar forecast)
  - Amber API (forward prices)

Writes:
  - $SMARTSHIFT_DIR/.ai_advice.json
  - $SMARTSHIFT_DIR/.advisor_v2_plan.json  (debug/verbose plan)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- Config -----------------------------------------------------------------
BASE_DIR = Path(os.environ.get("SMARTSHIFT_DIR", Path(__file__).resolve().parent.parent))
HA_URL = os.environ.get("HA_URL", "http://localhost:8123")
HA_TOKEN_FILE = Path(os.environ.get("HA_TOKEN_FILE", BASE_DIR / ".ha_token"))
AMBER_API_KEY = os.environ.get("AMBER_API_KEY", "")
AMBER_SITE_ID = os.environ.get("AMBER_SITE_ID", "")

PROFILE_PATH = BASE_DIR / ".consumption_profile.json"
ADVICE_PATH = BASE_DIR / ".ai_advice.json"
PLAN_PATH = BASE_DIR / ".advisor_v2_plan.json"

# Defaults (overridable via HA input_number / env)
EXPORT_THRESHOLD_DEFAULT = float(os.environ.get("EXPORT_THRESHOLD", "15.0"))  # c/kWh
CHARGE_THRESHOLD = float(os.environ.get("CHARGE_THRESHOLD", "10.0"))          # c/kWh (buy)
DISCHARGE_THRESHOLD = float(os.environ.get("DISCHARGE_THRESHOLD", "28.0"))    # c/kWh (buy)
SOC_MIN = float(os.environ.get("SOC_MIN", "10.0"))
SOC_MAX = float(os.environ.get("SOC_MAX", "95.0"))
DISCHARGE_FLOOR = float(os.environ.get("DISCHARGE_FLOOR", "20.0"))
BATTERY_KWH = float(os.environ.get("BATTERY_KWH", "10.0"))
BATTERY_W_MAX = float(os.environ.get("BATTERY_W_MAX", "5000.0"))

SYDNEY_OFFSET = timedelta(hours=10)  # close enough; drift <1h

# --- HA helpers -------------------------------------------------------------
def _ha_token() -> str:
    return HA_TOKEN_FILE.read_text().strip()


def ha_get(path: str) -> dict | list:
    url = f"{HA_URL}{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_ha_token()}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def ha_state(entity: str, default=None):
    try:
        d = ha_get(f"/api/states/{entity}")
        return d.get("state", default)
    except Exception:
        return default


def fnum(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# --- Amber helpers ----------------------------------------------------------
def amber_forward_prices(next_intervals: int = 48) -> list[dict]:
    """Fetch forward price curve. Returns list of {start, end, buy_c, feed_c, channel}.
    Falls back to empty list on failure.
    """
    if not (AMBER_API_KEY and AMBER_SITE_ID):
        print("WARN: AMBER_API_KEY / AMBER_SITE_ID not set in env", file=sys.stderr)
        return []
    url = f"https://api.amber.com.au/v1/sites/{AMBER_SITE_ID}/prices/current"
    qs = urllib.parse.urlencode({"next": next_intervals, "resolution": 30})
    req = urllib.request.Request(
        f"{url}?{qs}",
        headers={"Authorization": f"Bearer {AMBER_API_KEY}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except Exception as e:
        print(f"WARN: Amber API unreachable: {e}", file=sys.stderr)
        return []

    # Normalize into per-interval pairs (general = buy, feedIn = feed)
    by_interval: dict[str, dict] = {}
    for item in data:
        start = item.get("startTime")
        end = item.get("endTime")
        ch = (item.get("channelType") or "").lower()  # "general" or "feedin"
        price = fnum(item.get("perKwh"), 0.0)
        key = f"{start}|{end}"
        row = by_interval.setdefault(key, {"start": start, "end": end})
        if ch == "general":
            row["buy_c"] = price
        elif ch == "feedin":
            row["feed_c"] = -price  # feedIn is reported as negative; flip to earn>0
    rows = sorted(by_interval.values(), key=lambda r: r["start"])
    # Drop rows missing either side (should be paired)
    return [r for r in rows if "buy_c" in r and "feed_c" in r]


# --- Consumption profile ----------------------------------------------------
def load_profile() -> dict:
    if not PROFILE_PATH.exists():
        print(f"WARN: {PROFILE_PATH} missing, using flat default", file=sys.stderr)
        return {"profile": {"weekday": {}, "weekend": {}}}
    return json.loads(PROFILE_PATH.read_text())


def expected_load_kwh(profile: dict, dt_local: datetime, minutes: int = 30) -> float:
    """Predict load for a 30-min interval starting at dt_local (local Sydney)."""
    is_weekend = dt_local.weekday() >= 5
    key = "weekend" if is_weekend else "weekday"
    hour_block = profile.get("profile", {}).get(key, {}).get(str(dt_local.hour), {})
    avg_w = hour_block.get("avg_w", 500.0)
    return (avg_w * minutes / 60.0) / 1000.0  # kWh


# --- Solar forecast ---------------------------------------------------------
def expected_solar_kwh_today_remaining() -> float:
    """Estimate remaining solar today from existing SmartShift state."""
    state_file = BASE_DIR / ".current_state.json"
    if state_file.exists():
        try:
            s = json.loads(state_file.read_text())
            fc = s.get("solar_forecast") or {}
            if isinstance(fc, dict):
                return fnum(fc.get("today_kwh"), 0.0)
        except Exception:
            pass
    return 0.0


def solar_share_at_hour(hour: int) -> float:
    """Rough daylight curve — returns fraction of daily solar in this hour.
    Sums to ~1.0 over 6-20. Conservative Sydney-autumn estimate.
    """
    # Peak ~12:00, near-zero before 6 or after 19.
    daylight_curve = {
        6: 0.02, 7: 0.04, 8: 0.06, 9: 0.08, 10: 0.10, 11: 0.12,
        12: 0.14, 13: 0.14, 14: 0.12, 15: 0.08, 16: 0.05, 17: 0.03, 18: 0.02,
    }
    return daylight_curve.get(hour, 0.0)


# --- Planner ----------------------------------------------------------------
def build_plan(
    forward: list[dict],
    profile: dict,
    soc_pct: float,
    export_threshold_c: float,
) -> dict:
    """Build a per-interval plan: charge/discharge/hold/export.

    Returns dict with:
      intervals: list of plan rows
      summary: expected_profit_cents, windows
    """
    available_kwh = max(0.0, (soc_pct - DISCHARGE_FLOOR) / 100.0 * BATTERY_KWH)
    headroom_kwh = max(0.0, (SOC_MAX - soc_pct) / 100.0 * BATTERY_KWH)
    max_kwh_per_interval = BATTERY_W_MAX * 0.5 / 1000.0  # 30 min
    today_solar_remaining = expected_solar_kwh_today_remaining()

    # Annotate each interval with expected load/solar
    intervals = []
    total_load_kwh = 0.0
    total_solar_kwh = 0.0
    for row in forward:
        try:
            start = datetime.fromisoformat(row["start"].replace("Z", "+00:00"))
        except Exception:
            continue
        local = start + SYDNEY_OFFSET
        load = expected_load_kwh(profile, local, minutes=30)
        solar_share = solar_share_at_hour(local.hour) * 0.5  # half hour slice
        solar = today_solar_remaining * solar_share if local.date() == (datetime.now(timezone.utc) + SYDNEY_OFFSET).date() else 0.0
        net_surplus = solar - load  # kWh
        intervals.append({
            "start": row["start"],
            "end": row["end"],
            "local_hour": local.hour,
            "buy_c": row["buy_c"],
            "feed_c": row["feed_c"],
            "load_kwh": round(load, 3),
            "solar_kwh": round(solar, 3),
            "net_surplus_kwh": round(net_surplus, 3),
            "action": "hold",
            "energy_kwh": 0.0,
        })
        total_load_kwh += load
        total_solar_kwh += solar

    if not intervals:
        return {"intervals": [], "summary": {}, "error": "no forward prices"}

    # --- Reservation pass: reserve energy for peak-buy hours where solar < load
    remaining_battery = available_kwh
    # Rank by buy_c desc — cover expensive shortage first
    buy_rank = sorted(range(len(intervals)), key=lambda i: intervals[i]["buy_c"], reverse=True)
    reserved_for_load = 0.0
    for i in buy_rank:
        iv = intervals[i]
        if iv["buy_c"] < DISCHARGE_THRESHOLD:
            break  # not expensive enough to reserve
        shortage = max(0.0, iv["load_kwh"] - max(0.0, iv["solar_kwh"]))
        take = min(shortage, remaining_battery, max_kwh_per_interval)
        if take > 0:
            iv["action"] = "discharge"  # to self
            iv["energy_kwh"] = round(take, 3)
            remaining_battery -= take
            reserved_for_load += take

    # --- Export pass: allocate remaining battery to top feed-in windows
    feed_rank = sorted(range(len(intervals)), key=lambda i: intervals[i]["feed_c"], reverse=True)
    export_windows = 0
    exported_kwh = 0.0
    expected_earn = 0.0  # cents
    for i in feed_rank:
        iv = intervals[i]
        if iv["action"] != "hold":
            continue  # already reserved
        if iv["feed_c"] < export_threshold_c:
            break  # below threshold, done
        cap = min(remaining_battery, max_kwh_per_interval)
        if cap <= 0:
            break
        iv["action"] = "export_peak"
        iv["energy_kwh"] = round(cap, 3)
        earn = cap * iv["feed_c"]  # feed_c already in c/kWh, cap in kWh → cents
        expected_earn += earn
        exported_kwh += cap
        remaining_battery -= cap
        export_windows += 1

    # --- Charge pass: if headroom available AND cheap buy AND no solar, charge
    headroom = headroom_kwh
    buy_rank_asc = sorted(range(len(intervals)), key=lambda i: intervals[i]["buy_c"])
    for i in buy_rank_asc:
        iv = intervals[i]
        if iv["action"] != "hold":
            continue
        if iv["buy_c"] > CHARGE_THRESHOLD:
            break
        if iv["solar_kwh"] > 0.05:
            continue  # solar will charge for free; don't grid-charge
        cap = min(headroom, max_kwh_per_interval)
        if cap <= 0:
            break
        iv["action"] = "charge"
        iv["energy_kwh"] = round(cap, 3)
        headroom -= cap

    summary = {
        "expected_earn_cents": round(expected_earn, 1),
        "export_windows": export_windows,
        "exported_kwh": round(exported_kwh, 3),
        "reserved_for_load_kwh": round(reserved_for_load, 3),
        "total_forecast_load_kwh": round(total_load_kwh, 3),
        "total_forecast_solar_kwh": round(total_solar_kwh, 3),
        "battery_available_kwh": round(available_kwh, 3),
        "battery_leftover_kwh": round(remaining_battery, 3),
    }
    return {"intervals": intervals, "summary": summary}


# --- Immediate action decision ---------------------------------------------
def decide_now(plan: dict, current_buy_c: float, current_feed_c: float, soc_pct: float) -> tuple[str, str, float]:
    intervals = plan.get("intervals", [])
    if not intervals:
        return "hold", "No forward price data — holding in self-consumption.", 0.4

    first = intervals[0]
    action = first["action"]

    # Next-window scan for reasoning
    next_export = next((iv for iv in intervals if iv["action"] == "export_peak"), None)
    next_reserve = next((iv for iv in intervals if iv["action"] == "discharge"), None)

    if action == "export_peak":
        reasoning = (
            f"Export window NOW: feed-in {first['feed_c']:.2f}c ≥ threshold "
            f"{EXPORT_THRESHOLD_DEFAULT:.1f}c. Discharging {first['energy_kwh']:.2f}kWh to grid."
        )
        return "export_peak", reasoning, 0.9
    if action == "discharge":
        reasoning = (
            f"Reserve for peak-buy ({first['buy_c']:.2f}c buy price). Discharging to cover home load."
        )
        return "discharge", reasoning, 0.85
    if action == "charge":
        reasoning = (
            f"Cheap grid ({first['buy_c']:.2f}c ≤ charge threshold {CHARGE_THRESHOLD:.1f}c) and no solar."
            f" Charging battery."
        )
        return "charge", reasoning, 0.85

    # HOLD — explain WHY (this is the key improvement vs v1)
    reason_parts = [
        f"Current feed-in {current_feed_c:.2f}c below export threshold {EXPORT_THRESHOLD_DEFAULT:.1f}c."
    ]
    if next_export:
        local = datetime.fromisoformat(next_export["start"].replace("Z", "+00:00")) + SYDNEY_OFFSET
        reason_parts.append(
            f"Better export window at {local.strftime('%H:%M')} "
            f"({next_export['feed_c']:.2f}c, ~{next_export['energy_kwh']:.2f}kWh planned)."
        )
    if next_reserve:
        local = datetime.fromisoformat(next_reserve["start"].replace("Z", "+00:00")) + SYDNEY_OFFSET
        reason_parts.append(
            f"Reserving battery for peak-buy at {local.strftime('%H:%M')} "
            f"({next_reserve['buy_c']:.2f}c)."
        )
    if not next_export and not next_reserve:
        reason_parts.append("No profitable windows in next 24h — full self-consumption.")
    return "hold", " ".join(reason_parts), 0.8


# --- Entry point ------------------------------------------------------------
def main() -> int:
    profile = load_profile()
    soc_pct = fnum(ha_state("sensor.battery_soc"), 50.0)

    # Respect the HA override for export threshold
    ha_override = fnum(ha_state("input_number.smartshift_export_price_override"), 0.0)
    export_threshold = ha_override if ha_override > 0 else EXPORT_THRESHOLD_DEFAULT

    forward = amber_forward_prices(next_intervals=48)
    print(f"Forward intervals: {len(forward)}")

    plan = build_plan(forward, profile, soc_pct, export_threshold)
    PLAN_PATH.write_text(json.dumps(plan, indent=2))

    current_buy = fnum(forward[0]["buy_c"], 0.0) if forward else fnum(ha_state("sensor.smartshift_spot_price"), 0.0)
    current_feed = fnum(forward[0]["feed_c"], 0.0) if forward else 0.0

    strategy, reasoning, confidence = decide_now(plan, current_buy, current_feed, soc_pct)
    summary = plan.get("summary", {})

    next_export = next((iv for iv in plan.get("intervals", []) if iv["action"] == "export_peak"), None)
    next_window = None
    if next_export:
        next_window = {
            "start_iso": next_export["start"],
            "end_iso": next_export["end"],
            "action": "export_peak",
            "expected_earn_cents": round(next_export["energy_kwh"] * next_export["feed_c"], 2),
            "feed_c": next_export["feed_c"],
            "energy_kwh": next_export["energy_kwh"],
        }

    alerts: list[str] = []
    if soc_pct > 95 and not next_export:
        alerts.append("Battery near-full but no export windows — feed-in below threshold.")
    if summary.get("battery_leftover_kwh", 0) > 1.0 and next_export:
        alerts.append(f"{summary['battery_leftover_kwh']:.1f}kWh battery unused — consider lowering export threshold.")

    advice = {
        "schema_version": "2.0",
        "strategy": strategy,
        "export_threshold": round(export_threshold, 2),
        "discharge_floor": DISCHARGE_FLOOR,
        "reasoning": reasoning,
        "confidence": confidence,
        "written_at": datetime.now(timezone.utc).isoformat(),
        "alerts": alerts,
        "expected_profit_today_cents": summary.get("expected_earn_cents", 0.0),
        "next_window": next_window,
        "plan_summary": summary,
        "current_prices": {"buy_c": current_buy, "feed_c": current_feed},
        "soc_pct": soc_pct,
        "source": "advisor_v2",
    }
    ADVICE_PATH.write_text(json.dumps(advice, indent=2))
    print(f"\nStrategy: {strategy}   Confidence: {confidence}")
    print(f"Reasoning: {reasoning}")
    if next_window:
        print(f"Next export window: {next_window}")
    print(f"Plan summary: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
