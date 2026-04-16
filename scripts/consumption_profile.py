#!/usr/bin/env python3
"""Build hourly home-consumption profile from HA history.

Queries sensor.home_load_power over the last 14 days and produces an hourly
average consumption profile split by weekday vs weekend. The profile is used
by advisor_v2.py to predict future load and reserve battery for peak-buy
hours instead of blindly exporting during low feed-in windows.

Output: $SMARTSHIFT_DIR/.consumption_profile.json
Run weekly (or lazy-recompute when stale > 7 days).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from pathlib import Path

BASE_DIR = Path(os.environ.get("SMARTSHIFT_DIR", Path(__file__).resolve().parent.parent))
HA_URL = os.environ.get("HA_URL", "http://localhost:8123")
HA_TOKEN_FILE = Path(os.environ.get("HA_TOKEN_FILE", BASE_DIR / ".ha_token"))
PROFILE_PATH = BASE_DIR / ".consumption_profile.json"
HISTORY_DAYS = int(os.environ.get("CONSUMPTION_DAYS", "14"))
ENTITY_ID = os.environ.get("LOAD_ENTITY", "sensor.home_load_power")


def _token() -> str:
    return HA_TOKEN_FILE.read_text().strip()


def _get(url: str) -> list:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_token()}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def fetch_history(days: int = 14) -> list[dict]:
    """Fetch raw state history for home_load_power."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    # HA expects ISO8601 with offset; use +00:00 format
    start_s = start.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    end_s = end.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    qs = urllib.parse.urlencode({
        "filter_entity_id": ENTITY_ID,
        "end_time": end_s,
        "minimal_response": "",  # presence is what matters
    })
    url = f"{HA_URL}/api/history/period/{start_s}?{qs}"
    data = _get(url)
    # HA returns [[{state, last_changed}, ...]] — one list per entity
    if not data or not isinstance(data, list) or not data[0]:
        return []
    return data[0]


def build_profile(samples: list[dict]) -> dict:
    """Aggregate samples into hourly buckets split by weekday/weekend.

    Each sample: {state: "number", last_changed: ISO8601, ...}
    Uses local Sydney time for hour-of-day bucketing.
    """
    # Sydney offset: AEDT=+11, AEST=+10. Approximate as +10h for stability.
    # (A ~1h edge drift is negligible over 14d averages.)
    SYDNEY_OFFSET = timedelta(hours=10)

    buckets: dict[tuple[int, bool], list[float]] = defaultdict(list)  # (hour, is_weekend) -> [W]
    prev_ts = None
    prev_val = None
    for s in samples:
        try:
            w = float(s.get("state"))
        except (TypeError, ValueError):
            continue
        if w < 0 or w > 50_000:
            # unrealistic, skip
            continue
        ts_raw = s.get("last_changed") or s.get("last_updated")
        if not ts_raw:
            continue
        # parse ISO
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        local = ts + SYDNEY_OFFSET
        hour = local.hour
        is_weekend = local.weekday() >= 5
        buckets[(hour, is_weekend)].append(w)
        prev_ts, prev_val = ts, w

    profile = {"weekday": {}, "weekend": {}}
    for (hour, is_weekend), vals in buckets.items():
        if not vals:
            continue
        key = "weekend" if is_weekend else "weekday"
        vals_sorted = sorted(vals)
        # Trimmed mean (drop top+bottom 10%) for robustness
        n = len(vals_sorted)
        trim = max(1, n // 10)
        trimmed = vals_sorted[trim:n - trim] if n > 2 * trim else vals_sorted
        avg_w = sum(trimmed) / len(trimmed) if trimmed else 0.0
        profile[key][str(hour)] = {
            "avg_w": round(avg_w, 1),
            "samples": n,
            "p50": round(vals_sorted[n // 2], 1),
            "p90": round(vals_sorted[int(n * 0.9)], 1),
        }

    # Fill missing hours with overall average (safety)
    for key in ("weekday", "weekend"):
        all_vals = [p["avg_w"] for p in profile[key].values()]
        fallback = sum(all_vals) / len(all_vals) if all_vals else 500.0
        for h in range(24):
            profile[key].setdefault(str(h), {"avg_w": round(fallback, 1), "samples": 0, "p50": fallback, "p90": fallback})

    return profile


def main() -> int:
    print(f"Fetching {HISTORY_DAYS} days of {ENTITY_ID} from {HA_URL} ...")
    samples = fetch_history(HISTORY_DAYS)
    print(f"  {len(samples)} raw samples")
    if not samples:
        print("ERROR: no samples returned from HA")
        return 1

    profile = build_profile(samples)
    out = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "source_entity": ENTITY_ID,
        "days_window": HISTORY_DAYS,
        "sample_count": len(samples),
        "profile": profile,
        "units": "W (watts, hourly average)",
        "notes": "Hourly averages of instantaneous home_load_power, split weekday/weekend. Trimmed 10% each tail.",
    }
    PROFILE_PATH.write_text(json.dumps(out, indent=2))
    # Quick summary
    print(f"\nProfile written: {PROFILE_PATH}")
    print(f"Peak hours (weekday, top 5 avg_w):")
    peaks = sorted(profile["weekday"].items(), key=lambda kv: kv[1]["avg_w"], reverse=True)[:5]
    for h, p in peaks:
        print(f"  {h:>2}:00  {p['avg_w']:>7.1f} W  (p90={p['p90']:.0f}, n={p['samples']})")
    print(f"Trough hours (weekday, bottom 5 avg_w):")
    troughs = sorted(profile["weekday"].items(), key=lambda kv: kv[1]["avg_w"])[:5]
    for h, p in troughs:
        print(f"  {h:>2}:00  {p['avg_w']:>7.1f} W  (p90={p['p90']:.0f}, n={p['samples']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
