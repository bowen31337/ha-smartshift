# ha-smartshift

DIY battery automation for Home Assistant using live wholesale electricity spot prices — a free alternative to Amber Electric's SmartShift subscription.

Automatically charges your battery when electricity is cheap and exports to the grid when prices peak.

## How It Works

```
AEMO / Amber Spot Price (5-min intervals)
              ↓
        Decision Engine
              ↓
  ┌───────────┴───────────┐
  │ price < 5c/kWh        │→ Force CHARGE (if SoC < 95%)
  │ price 5–25c/kWh       │→ Self-consumption (default)
  │ price > 25c/kWh       │→ Force DISCHARGE to grid (if SoC > 20%)
  └───────────────────────┘
              ↓
    POST /setting.cgi → AISWEI inverter
```

## Hardware

- **Inverter:** AISWEI ASW12kH-T3 12kW hybrid (local API at `https://192.168.x.x`)
- **Battery:** 46kWh total capacity
- **Home Assistant:** running locally on same network

## Setup

### 1. Clone and configure

```bash
git clone https://github.com/bowen31337/ha-smartshift
cd ha-smartshift
cp .env.example .env
# Edit .env — set INVERTER_URL and optionally AMBER_API_KEY
```

### 2. Test manually

```bash
# Check current status (no changes)
uv run python scripts/inverter_control.py --status

# Dry run auto mode
uv run python scripts/inverter_control.py --dry-run
```

### 3. Add to Home Assistant

Copy/merge these files into your HA config:

```bash
# Add REST sensor + shell commands to configuration.yaml
cat ha/configuration_addon.yaml >> /path/to/ha/config/configuration.yaml

# Add automations
cat ha/automations_addon.yaml >> /path/to/ha/config/automations.yaml

# Restart HA
```

### 4. Add dashboard card

In HA → Overview → Edit → Add Card → Manual → paste contents of `dashboard/smartshift_card.yaml`

## Price Sources

1. **Amber Electric API** (optional, set `AMBER_API_KEY`) — 5-min intervals, most accurate
2. **AEMO NemWeb** (free, no key needed) — 30-min trading intervals, public data

## Expected Earnings (46kWh battery, NSW)

| Scenario | Daily | Annual |
|---|---|---|
| Conservative (2 peak events/day) | $4–8 | $1,500–3,000 |
| Active (daily peak export) | $8–14 | $3,000–5,000 |

Calculation: charge at ~3c/kWh (solar surplus), discharge at 25–40c/kWh peak = ~22–37c/kWh spread × ~40kWh usable.

## Safety

- Never discharges below 20% SoC (configurable via `SOC_MIN`)
- Never charges above 95% SoC (configurable via `SOC_MAX`)
- Manual overrides via HA dashboard buttons

## Environment Variables

See `.env.example` for full list. Key settings:

| Variable | Default | Description |
|---|---|---|
| `INVERTER_URL` | `https://10.0.0.6` | Inverter local IP |
| `INVERTER_SN` | — | Inverter serial number |
| `CHARGE_THRESHOLD` | `5` | Charge when spot < this (c/kWh) |
| `DISCHARGE_THRESHOLD` | `25` | Discharge when spot > this (c/kWh) |
| `SOC_MIN` | `20` | Never discharge below this % |
| `SOC_MAX` | `95` | Never charge above this % |
| `AMBER_API_KEY` | — | Optional Amber API key |
