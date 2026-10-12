"""Immutable 23:30 Europe/Warsaw DAILY V3 TOP 10 snapshot."""
import json
from datetime import datetime, timezone, time
from pathlib import Path
from zoneinfo import ZoneInfo

DATA = Path("data")
ZONE = ZoneInfo("Europe/Warsaw")
now = datetime.now(timezone.utc)
local = now.astimezone(ZONE)
if (local.hour, local.minute) < (23, 30):
    print("V3 daily lock not due yet")
    raise SystemExit(0)

date = local.date().isoformat()
cutoff = datetime.combine(local.date(), time(23, 30), tzinfo=ZONE).astimezone(timezone.utc)
out = DATA / "v3_daily_snapshots.json"
saved = json.loads(out.read_text()) if out.exists() and out.stat().st_size else {"schema_version": 1, "days": {}}
days = saved.setdefault("days", {})
if date in days:
    print("V3 daily already locked:", date)
    raise SystemExit(0)

def dt(value):
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return t.astimezone(timezone.utc) if t.tzinfo else None
    except (ValueError, TypeError):
        return None

def positive(value):
    try:
        v = float(value)
        return v if 0 < v < float("inf") else None
    except (ValueError, TypeError):
        return None

v3 = json.loads((DATA / "live_v3.json").read_text())
history = json.loads((DATA / "history.json").read_text())
first = {}
for row in v3.get("observations", []):
    observed = dt(row.get("observed_at_utc"))
    mint, pair = row.get("mint"), row.get("pair_address")
    rank = row.get("v3_top10_rank")
    try:
        rank = int(rank)
    except (ValueError, TypeError):
        continue
    price = positive(row.get("entry_price_usd"))
    if not observed or observed > cutoff or observed.astimezone(ZONE).date().isoformat() != date:
        continue
    if not mint or not pair or not price or not (1 <= rank <= 10):
        continue
    key = (mint, pair)
    if key not in first or observed < dt(first[key]["observed_at_utc"]):
        first[key] = row

results = []
for (mint, pair), row in first.items():
    entry_at = dt(row["observed_at_utc"])
    later = []
    for p in history.get(mint, []):
        observed = dt(p.get("timestamp"))
        price = positive(p.get("price_usd"))
        if p.get("pair_address") == pair and observed and entry_at < observed <= cutoff and price:
            later.append((observed, price))
    latest = max(later, default=None)
    entry_price = positive(row["entry_price_usd"])
    ret = round((latest[1] / entry_price - 1) * 100, 4) if latest else None
    results.append({
        "mint": mint, "pair_address": pair, "symbol": row.get("symbol"),
        "first_observed_at_utc": row["observed_at_utc"],
        "initial_rank": int(row["v3_top10_rank"]),
        "entry_price_usd": entry_price,
        "exit_price_usd": latest[1] if latest else None,
        "exit_observed_at_utc": latest[0].isoformat() if latest else None,
        "hold_return_pct": ret,
        "status": "VERIFIED" if latest else "PENDING"
    })
results.sort(key=lambda x: (x["hold_return_pct"] is None, -(x["hold_return_pct"] or 0)))
days[date] = {
    "locked_at_utc": now.isoformat(), "cutoff_at_utc": cutoff.isoformat(),
    "timezone": "Europe/Warsaw", "model": "LIVE V3 BETA AI",
    "eligible_entries": len(results),
    "verified_entries": sum(x["status"] == "VERIFIED" for x in results),
    "top10": results[:10],
    "all_entries": results
}
out.write_text(json.dumps(saved, indent=2, ensure_ascii=False) + "\n")
print("Locked DAILY V3", date, "entries:", len(results))
