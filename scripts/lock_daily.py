"""Freeze each Poland-local day's positive TOP 10 calls once after 23:30."""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Europe/Warsaw")
now = datetime.now(timezone.utc)
local = now.astimezone(ZONE)
if (local.hour, local.minute) < (23, 30):
    print("Daily snapshot not due yet")
    raise SystemExit(0)

path = Path("data/daily_snapshots.json")
saved = json.loads(path.read_text()) if path.exists() else {"schema_version": 1, "days": {}}
days = saved.setdefault("days", {})
date = local.date().isoformat()
if date in days:
    print("Daily snapshot already locked:", date)
    raise SystemExit(0)

recommendations = json.loads(Path("data/recommendations.json").read_text()).get("records", [])
history = json.loads(Path("data/history.json").read_text())
first = {}
first_seen = {}
for record in recommendations:
    try:
        observed = datetime.fromisoformat(record["observed_at_utc"].replace("Z", "+00:00"))
        price = float(record["observation_price_usd"])
        mint, pair = record["mint"], record["pair_address"]
    except (KeyError, ValueError, TypeError):
        continue
    if observed.astimezone(ZONE).date().isoformat() != date or observed > now:
        continue
    if price <= 0:
        continue
    key = (mint, pair)
    if key not in first_seen or observed < first_seen[key]:
        first_seen[key] = observed
    if record.get("displayed_status") != "POSITIVE":
        continue
    if key not in first or observed < first[key][0]:
        first[key] = (observed, record)

results = []
missing = 0
for (mint, pair), (start, record) in first.items():
    valid = []
    for observation in history.get(mint, []):
        if observation.get("pair_address") != pair:
            continue
        try:
            t = datetime.fromisoformat(observation["timestamp"].replace("Z", "+00:00"))
            p = float(observation["price_usd"])
        except (KeyError, ValueError, TypeError):
            continue
        if start < t <= now and p > 0:
            valid.append((t, p))
    if not valid:
        missing += 1
        continue
    t, p = max(valid)
    entry = float(record["observation_price_usd"])
    results.append({
        "mint": mint, "pair_address": pair, "symbol": record.get("symbol"),
        "first_seen_at_utc": first_seen[(mint, pair)].isoformat(),
        "first_positive_at_utc": start.isoformat(),
        "entry_price_usd": entry, "last_price_at_utc": t.isoformat(),
        "last_price_usd": p, "hold_return_pct": round(100 * (p / entry - 1), 4),
        "price_age_minutes_at_lock": round((now - t).total_seconds() / 60, 1),
    })
results.sort(key=lambda x: x["hold_return_pct"], reverse=True)
days[date] = {
    "evaluated_day": date, "locked_at_utc": now.isoformat(),
    "positive_calls": len(first), "verified_holds": len(results),
    "missing_later_prices": missing,
    "up": sum(x["hold_return_pct"] > 0 for x in results),
    "average_hold_return_pct": round(sum(x["hold_return_pct"] for x in results) / len(results), 4) if results else None,
    "top_10": results[:10],
}
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(saved, ensure_ascii=False, indent=2) + "\n")
print("Locked", date, "positive calls", len(first), "verified", len(results))
