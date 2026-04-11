#!/bin/bash
# Poll GPU stats from 10.0.0.30 and write JSON to shared location
# Called by systemd timer or cron every 60s

set -e
OUT="/home/bowen/ha-smartshift/.gpu_stats.json"
TMP="${OUT}.tmp"

# Run nvidia-smi over SSH, parse CSV, emit JSON
ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no -i /home/bowen/.ssh/id_ed25519_alexchen peter@10.0.0.30 \
  'nvidia-smi --query-gpu=index,name,temperature.gpu,power.draw,utilization.gpu,memory.used,memory.total,fan.speed --format=csv,noheader,nounits; echo "MINER:$(systemctl is-active miner_scheduler 2>&1)"' \
  2>/dev/null | python3 -c "
import sys, json, datetime
gpus = []
miner_status = 'unknown'
total_power = 0
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    if line.startswith('MINER:'):
        miner_status = line.split(':',1)[1]
        continue
    parts = [p.strip() for p in line.split(',')]
    if len(parts) < 8: continue
    gpu = {
        'index': int(parts[0]),
        'name': parts[1].replace('NVIDIA GeForce ', ''),
        'temp': int(float(parts[2])) if parts[2] != '[N/A]' else 0,
        'power': round(float(parts[3]), 1) if parts[3] != '[N/A]' else 0,
        'util': int(float(parts[4])) if parts[4] != '[N/A]' else 0,
        'mem_used': int(parts[5]) if parts[5] != '[N/A]' else 0,
        'mem_total': int(parts[6]) if parts[6] != '[N/A]' else 0,
        'fan': int(parts[7]) if parts[7] != '[N/A]' else 0,
    }
    total_power += gpu['power']
    gpus.append(gpu)

out = {
    'timestamp': datetime.datetime.now().isoformat(),
    'gpus': gpus,
    'total_power_w': round(total_power, 1),
    'gpu_count': len(gpus),
    'miner_status': miner_status,
    'any_mining': any(g['util'] > 20 for g in gpus),
}
print(json.dumps(out, indent=2))
" > "$TMP" && mv "$TMP" "$OUT"
