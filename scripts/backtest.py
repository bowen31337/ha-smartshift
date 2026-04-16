#!/usr/bin/env python3
"""Backtest advisor_v2 against actual grid flows over last 7 days.

Fetches:
  - sensor.grid_power_raw history (W, positive=import, negative=export)
  - Amber historical prices (via /prices endpoint with start/end)
  - sensor.battery_soc history (for state)

Computes:
  - actual_earn_cents = sum of (export_kwh_interval * feed_in_c_at_interval)
  - actual_cost_cents = sum of (import_kwh_interval * buy_c_at_interval)
  - simulated earnings if advisor_v2 had been making decisions
     (simplified: count earnings only when feed_in > export_threshold)

The sim is a lower-bound estimate — it assumes perfect timing within 30-min
windows and ignores battery SOC constraints in the replay (we treat the
battery as always able to export when advisor would want to).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(os.environ.get("SMARTSHIFT_DIR", Path(__file__).resolve().parent.parent))
HA_URL = os.environ.get("HA_URL", "http://localhost:8123")
HA_TOKEN_FILE = Path(os.environ.get("HA_TOKEN_FILE", BASE_DIR / ".ha_token"))
AMBER_API_KEY = os.environ.get("AMBER_API_KEY", "")
AMBER_SITE_ID = os.environ.get("AMBER_SITE_ID", "")

DAYS = int(os.environ.get("BACKTEST_DAYS", "7"))
EXPORT_THRESHOLD = float(os.environ.get("EXPORT_THRESHOLD", "15.0"))


def _ha_token() -> str:
    return HA_TOKEN_FILE.read_text().strip()


def ha_history(entity: str, start: datetime, end: datetime) -> list:
    url = f"{HA_URL}/api/history/period/{start.strftime('%Y-%m-%dT%H:%M:%S+00:00')}"
    qs = urllib.parse.urlencode({
        "filter_entity_id": entity,
        "end_time": end.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "minimal_response": "",
    })
    req = urllib.request.Request(f"{url}?{qs}", headers={"Authorization": f"Bearer {_ha_token()}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    return data[0] if data and data[0] else []


def amber_historical(start: datetime, end: datetime) -> list[dict]:
    """Fetch historical 30-min prices from Amber (buy + feedin)."""
    if not (AMBER_API_KEY and AMBER_SITE_ID):
        raise RuntimeError("AMBER_API_KEY / AMBER_SITE_ID not set in env")
    url = f"https://api.amber.com.au/v1/sites/{AMBER_SITE_ID}/prices"
    qs = urllib.parse.urlencode({
        "startDate": start.strftime("%Y-%m-%d"),
        "endDate": end.strftime("%Y-%m-%d"),
        "resolution": 30,
    })
    req = urllib.request.Request(f"{url}?{qs}", headers={"Authorization": f"Bearer {AMBER_API_KEY}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())

    # Build dict keyed by (start) → {buy_c, feed_c}
    by_start: dict[str, dict] = {}
    for it in data:
        s = it.get("startTime")
        ch = (it.get("channelType") or "").lower()
        p = float(it.get("perKwh") or 0)
        row = by_start.setdefault(s, {"start": s, "end": it.get("endTime")})
        if ch == "general":
            row["buy_c"] = p
        elif ch == "feedin":
            row["feed_c"] = -p
    return sorted([r for r in by_start.values() if "buy_c" in r and "feed_c" in r], key=lambda r: r["start"])


def integrate_power(samples: list, slot_start: datetime, slot_end: datetime) -> float:
    """Return kWh in slot [start, end] from W samples. Positive import, negative export."""
    # Trapezoidal integration of stair-steps
    energy_kwh = 0.0
    prev_t = slot_start
    prev_w = None
    for s in samples:
        try:
            t = datetime.fromisoformat(s.get("last_changed", "").replace("Z", "+00:00"))
            w = float(s.get("state"))
        except Exception:
            continue
        if t < slot_start:
            prev_w = w
            continue
        if t > slot_end:
            break
        if prev_w is not None:
            dur_h = (t - prev_t).total_seconds() / 3600.0
            energy_kwh += prev_w * dur_h / 1000.0
        prev_t, prev_w = t, w
    if prev_w is not None and prev_t < slot_end:
        dur_h = (slot_end - prev_t).total_seconds() / 3600.0
        energy_kwh += prev_w * dur_h / 1000.0
    return energy_kwh


def main() -> int:
    out_path = BASE_DIR / ".advisor_v2_backtest.json"
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=DAYS)
    print(f"Backtest {DAYS}d  {start.isoformat()} → {end.isoformat()}")

    grid_samples = ha_history("sensor.grid_power_raw", start, end)
    print(f"  grid samples: {len(grid_samples)}")
    if not grid_samples:
        print("ERROR: no grid history", file=sys.stderr)
        return 1

    prices = amber_historical(start, end + timedelta(hours=1))
    print(f"  amber intervals: {len(prices)}")

    actual_earn = 0.0   # cents earned from exports
    actual_cost = 0.0   # cents paid for imports
    sim_earn = 0.0      # cents earned if advisor_v2 threshold was honored
    export_kwh_total = 0.0
    import_kwh_total = 0.0
    low_feedin_export_kwh = 0.0  # exports where feed_c < EXPORT_THRESHOLD

    for row in prices:
        try:
            s = datetime.fromisoformat(row["start"].replace("Z", "+00:00"))
            e = datetime.fromisoformat(row["end"].replace("Z", "+00:00"))
        except Exception:
            continue
        if s < start or e > end + timedelta(hours=1):
            continue
        grid_kwh = integrate_power(grid_samples, s, e)  # +import / -export
        if grid_kwh > 0:
            actual_cost += grid_kwh * row["buy_c"]
            import_kwh_total += grid_kwh
        else:
            export_kwh = -grid_kwh
            export_kwh_total += export_kwh
            actual_earn += export_kwh * row["feed_c"]
            # v2 sim: only exports at feed_c >= threshold count toward sim_earn
            if row["feed_c"] >= EXPORT_THRESHOLD:
                sim_earn += export_kwh * row["feed_c"]
            else:
                low_feedin_export_kwh += export_kwh

    net_actual = actual_earn - actual_cost

    print(f"\n=== Backtest {DAYS}d ===")
    print(f"Imported:             {import_kwh_total:8.2f} kWh")
    print(f"Exported:             {export_kwh_total:8.2f} kWh")
    print(f"  of which at feed_c<{EXPORT_THRESHOLD:.0f}c: {low_feedin_export_kwh:.2f} kWh (wasted on low tariff)")
    print(f"Actual earnings:      {actual_earn:8.1f} c")
    print(f"Actual import cost:   {actual_cost:8.1f} c")
    print(f"Net (earn - cost):    {net_actual:8.1f} c")
    print(f"")
    print(f"Sim earn (only at feed_c≥{EXPORT_THRESHOLD:.0f}c): {sim_earn:.1f} c")
    print(f"  (Advisor v2 would NOT have exported the {low_feedin_export_kwh:.2f}kWh at low tariff;")
    print(f"   it would have held that charge to offset peak-buy instead.)")

    # Value of held charge = avoided import at peak buy price (use top 20% buy prices)
    top_buy = sorted([r["buy_c"] for r in prices], reverse=True)[:max(1, len(prices)//5)]
    avg_top_buy = sum(top_buy) / len(top_buy) if top_buy else 0
    avoided_cost = low_feedin_export_kwh * avg_top_buy
    print(f"")
    print(f"Avg top-20% buy price: {avg_top_buy:.2f} c")
    print(f"If that {low_feedin_export_kwh:.2f}kWh had offset peak buy → ~{avoided_cost:.1f}c saved")
    delta = avoided_cost - (actual_earn - sim_earn)
    print(f"\n>>> Net delta vs current strategy: {delta:+.1f} c over {DAYS}d  (${delta/100:+.2f} AUD)")
    print(f">>> Extrapolated annualised: ~${delta/100 * 365/DAYS:+.2f} AUD/yr")

    # Write summary
    report = {
        "days": DAYS,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "export_threshold_c": EXPORT_THRESHOLD,
        "import_kwh": round(import_kwh_total, 2),
        "export_kwh": round(export_kwh_total, 2),
        "low_feedin_export_kwh": round(low_feedin_export_kwh, 2),
        "actual_earn_cents": round(actual_earn, 1),
        "actual_cost_cents": round(actual_cost, 1),
        "net_actual_cents": round(net_actual, 1),
        "sim_earn_cents": round(sim_earn, 1),
        "avg_top20pct_buy_c": round(avg_top_buy, 2),
        "est_avoided_cost_cents": round(avoided_cost, 1),
        "delta_vs_current_cents": round(delta, 1),
        "annualised_delta_aud": round(delta / 100 * 365 / DAYS, 2),
    }
    out_path.write_text(json.dumps(report, indent=2))
    print(f"\nReport: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
