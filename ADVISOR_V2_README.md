# SmartShift Advisor v2 — Profit-Maximising Battery Strategy

## Problem with v1

The v1 advisor wrote to `.ai_advice.json` with strategy strings like `"hold"` and
generic reasoning ("feed-in too low"). It did not quantify the opportunity cost
of exporting at near-zero feed-in while peak buy prices 5 hours later were 25-38c.

Observed failure mode (2026-04-16 midday):
- Battery at 100% SOC
- Exporting 11+ kW to grid at feed-in **0.3c/kWh** (essentially free)
- Peak buy prices **25-38c/kWh** between 5-9 PM
- Advisor said `"hold"` — correctly identifying no export opportunity, but
  never timing discharge to cover the peak-buy shortfall.

## v2 Algorithm

1. **Consumption profile** (`consumption_profile.py`) — pulls 14 days of
   `sensor.home_load_power`, builds an hourly weekday/weekend profile with
   trimmed-mean robustness. Written to `.consumption_profile.json`.
   Run weekly (or lazily when the file is >7 days old).

2. **Forward planner** (`advisor_v2.py`) — every hour:
   - Fetches Amber's 48-interval forward price curve (24h, 30-min resolution).
   - For each forward interval, estimates `expected_load_kwh` (from profile)
     and `expected_solar_kwh` (from today's forecast × daylight share curve).
   - **Reservation pass:** ranks intervals by buy price descending; for each
     interval above the discharge threshold (default 28c), reserves battery
     energy equal to `max(0, load - solar)`. This guarantees the battery
     covers expensive-grid shortfalls before anything else.
   - **Export pass:** ranks remaining intervals by feed-in price descending;
     allocates remaining battery to the highest-feed-in windows where
     feed-in ≥ export threshold (default 15c, overridable via HA slider).
   - **Charge pass:** if headroom exists and grid price is cheap (<10c) and
     no solar is expected in that interval, plans a grid-charge.

3. **Immediate decision** — reads the first (current) interval's action and
   emits one of: `charge`, `discharge`, `export_peak`, `hold`. The reasoning
   string cites the actual numbers (current prices, next export window time,
   next peak-buy time).

4. **Backtest** (`backtest.py`) — replays last 7 days of real grid flows
   against Amber historical prices and reports the delta between what v1
   did and what v2 *would have done*. Writes `.advisor_v2_backtest.json`.

## Results (2026-04-16 backtest, 7-day window)

- Exported 379 kWh — **all** at feed-in < 15c
- Actual earnings: $14.47
- Estimated opportunity cost (if shifted to peak-buy offset): **~$97/week**
- Annualised opportunity: **~$2,000-5,000/year** (depending on capture rate)

## HA Integration

- Reads `input_number.smartshift_export_price_override` as export threshold
  when set > 0 (HA dashboard slider).
- Reads `sensor.battery_soc` for current SOC.
- Writes `.ai_advice.json` — consumed by `inverter_control.py` unchanged.
- Writes `.advisor_v2_plan.json` (debug) with the full 48-interval plan.

The inverter control script is **unchanged**; it consumes `.ai_advice.json`
like before. v2 is a drop-in replacement for the advice producer.

## File Layout

```
ha-smartshift/
├── scripts/
│   ├── consumption_profile.py   # weekly — build load pattern
│   ├── advisor_v2.py            # hourly — planner + decision
│   ├── backtest.py              # on-demand — 7-day replay
│   └── inverter_control.py      # (unchanged) — acts on .ai_advice.json
├── .ha_token                    # HA long-lived token (gitignored)
├── .consumption_profile.json    # produced by consumption_profile.py
├── .ai_advice.json              # produced by advisor_v2.py
├── .advisor_v2_plan.json        # debug plan
├── .advisor_v2_backtest.json    # backtest report
└── ADVISOR_V2_README.md         # this file
```

## Required env vars

```bash
export AMBER_API_KEY="<your-amber-api-key>"   # from https://app.amber.com.au/developers/
export AMBER_SITE_ID="<your-amber-site-id>"
export HA_URL="http://localhost:8123"          # HA base URL (default shown)
export HA_TOKEN_FILE=".ha_token"               # Path to HA long-lived token file
```

## Cron Wiring

- **Hourly:** `advisor_v2.py` (replaces v1 advisor cron)
- **Weekly (Sun 4am):** `consumption_profile.py` (rebuild load pattern)

Cron payload update goes through `cron update` — the existing
"SmartShift AI Advisor (Hourly)" job is repointed to call `advisor_v2.py`.

## Tuning

Via env (or HA slider `input_number.smartshift_export_price_override`):

| Variable | Default | Effect |
|---|---|---|
| `EXPORT_THRESHOLD` | 15.0 | Feed-in c/kWh above which to actively export |
| `CHARGE_THRESHOLD` | 10.0 | Buy c/kWh below which to grid-charge |
| `DISCHARGE_THRESHOLD` | 28.0 | Buy c/kWh above which to reserve battery |
| `SOC_MIN` / `SOC_MAX` | 10/95 | Battery operating range |
| `DISCHARGE_FLOOR` | 20 | Never discharge below this SOC |
| `BATTERY_KWH` | 10 | Usable capacity estimate |

## Future Improvements

1. **Solar forecast integration** — currently uses today's cached Solcast
   forecast from `.current_state.json` plus a static daylight-share curve.
   Replace with proper half-hourly solar forecast per interval.
2. **MILP optimiser** — the greedy allocator is good enough for single-battery
   single-site; a proper MILP could capture multi-day dynamics and solar
   curtailment trade-offs.
3. **Feedback loop** — measure actual vs. predicted load after each cycle,
   auto-adjust the consumption profile weights.
