"""Track production Spike Radar alerts and verified 5m outcomes."""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

def parse(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None

def number(value):
    try:
        v = float(value)
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None

def estimate_buy_impact_pct(liquidity_usd, trade_usd=100):
    """Approximate $100 buy impact for a balanced constant-product AMM pool.

    Liquidity is assumed to be total pool TVL, half in quote assets.
    Excludes DEX fees, routing, concentrated liquidity and MEV.
    """
    liquidity = number(liquidity_usd)
    if liquidity is None:
        return None
    quote_reserve = liquidity / 2
    return round(100 * trade_usd / (quote_reserve + trade_usd), 2)

latest = json.loads(Path("data/latest.json").read_text())
history = json.loads(Path("data/history.json").read_text())
path = Path("data/spike_radar_history.json")
saved = json.loads(path.read_text()) if path.exists() else {"schema_version": 1, "records": []}
if not isinstance(saved, dict):
    saved = {"schema_version": 1, "records": []}
if not isinstance(saved.get("records"), list):
    saved["records"] = []
rows = saved["records"]
# Restore immutable closed-day values from the previous committed snapshot.
# The tracker must not recalculate old signals from refreshed market history.
# Existing closed-day rows are never mutated in this run.

# Enforce the midday signal block on historical data as well as new alerts.
# This also cleans stale records restored by a concurrent workflow checkout.
def radar_blocked(timestamp):
    at = parse(timestamp)
    return at is not None and at.astimezone(ZoneInfo("Europe/Warsaw")).hour not in (7, 8, 9, 10, 14, 15)

# Historical records are immutable once their trading day has closed.
# Midday restrictions apply to NEW signals only, not previously frozen history.
now = parse(latest.get("generated_at_utc"))
if now is None:
    raise ValueError("Missing latest snapshot timestamp")
zone = ZoneInfo("Europe/Warsaw")
day = now.astimezone(zone).date().isoformat()
# Freeze completed trading days at 23:30 Warsaw. Never revise prior-day
# signals, statuses, prices, or returns during subsequent collections.
local_now = now.astimezone(zone)
day_closed = (local_now.hour, local_now.minute) >= (23, 30)
def is_open_day_record(row):
    signal_time = parse(row.get("first_alert_at_utc"))
    return (not day_closed and signal_time is not None and
            signal_time.astimezone(zone).date().isoformat() == day)

# Every displayed Spike Radar snapshot is an independent observation.
# Deduplicate only the exact token/pair/snapshot timestamp, never by a time window.
# A token may reappear after 5 minutes or on successive collection cycles.
seen={(r.get("mint"),r.get("pair_address"),r.get("first_alert_at_utc")) for r in rows}
added=0
for item in ([] if day_closed or radar_blocked(now.isoformat()) else latest.get("early_alert_candidates", [])):
    mint, pair = item.get("mint"), item.get("pair_address")
    price = number(item.get("price_usd"))
    if not mint or not pair or not price:
        continue
    key=(mint,pair,now.isoformat())
    if key in seen:
        continue
    rows.append({
        "day": day, "mint": mint, "pair_address": pair,
        "symbol": item.get("symbol") or mint[:8],
        "first_alert_at_utc": now.isoformat(), "alert_price_usd": price,
        "initial_15m_change_pct": item.get("change_15m_pct"),
        "buys_5m": item.get("buy_transactions_5m"),
        "sells_5m": item.get("sell_transactions_5m"),
        "volume_5m_usd": item.get("volume_5m_usd"),
        "liquidity_usd": item.get("liquidity_usd"),
        "estimated_100usd_buy_impact_pct": estimate_buy_impact_pct(item.get("liquidity_usd")),
        "buy_acceleration_pct": item.get("buy_acceleration_pct"),
        "volume_acceleration_pct": item.get("volume_acceleration_pct"),
        "measurement_minutes": 5, "status": "PENDING", "price_5m_usd": None,
        "observed_5m_at_utc": None, "return_5m_pct": None
    })
    seen.add(key)
    added+=1
print(f"Spike Radar displayed observations added: {added}")
# Retry both exits independently on every collection, including records whose
# earlier attempt was NOT_VERIFIABLE. Never replace a measured price with a guess.
for row in rows:
    if not is_open_day_record(row):
        continue
    start = parse(row.get("first_alert_at_utc"))
    entry = number(row.get("alert_price_usd"))
    if not start or not entry:
        continue
    age = (now - start).total_seconds()
    if age < 300:
        continue
    points = []
    for point in history.get(row["mint"], []):
        if point.get("pair_address") != row.get("pair_address"):
            continue
        at, price = parse(point.get("timestamp")), number(point.get("price_usd"))
        if at and price and at >= start:
            points.append((at, price))
    points.sort(key=lambda p: p[0])
    for minutes in (5, 10):
        price_key = f"price_{minutes}m_usd"
        return_key = f"return_{minutes}m_pct"
        observed_key = f"observed_{minutes}m_at_utc"
        delay_key = "measurement_delay_minutes" if minutes == 5 else "measurement_10m_delay_minutes"
        status_key = "status" if minutes == 5 else "status_10m"
        if row.get(price_key) is not None and row.get(return_key) is not None:
            continue
        target = minutes * 60
        eligible = [(at, price) for at, price in points if (at-start).total_seconds() >= target]
        if eligible:
            at, price = eligible[0]
            delay = round((at-start).total_seconds()/60, 2)
            row.update({
                price_key: price,
                observed_key: at.isoformat(),
                delay_key: delay,
                return_key: round((price/entry-1)*100, 2),
                status_key: "MEASURED" if delay <= minutes+5 else "DELAYED"
            })
        elif age >= (minutes+5)*60:
            row[status_key] = "NOT_VERIFIABLE"
        else:
            row[status_key] = "PENDING"
# Performance retention: rolling seven days, independent of the 23:30 daily reset.
cutoff = now - timedelta(days=7)
saved["records"] = [row for row in rows if (parse(row.get("first_alert_at_utc")) or now) >= cutoff]
rows = saved["records"]
# Persist an explicit close snapshot once the Warsaw trading day has ended.
# This is a historical audit marker; subsequent runs never recalculate closed days.
closed_days = saved.get("closed_days")
if not isinstance(closed_days, dict):
    closed_days = {}
    saved["closed_days"] = closed_days
for historical_day in sorted({r.get("day") for r in rows if r.get("day")}):
    if historical_day < day or (historical_day == day and day_closed):
        closed_days.setdefault(historical_day, {
            "locked": True,
            "records": sum(r.get("day") == historical_day for r in rows)
        })
saved["updated_at_utc"] = now.isoformat()
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(saved, ensure_ascii=False, indent=2) + "\n")
print("Spike Radar records:", len(rows), "today:", sum(x["day"] == day for x in rows))
