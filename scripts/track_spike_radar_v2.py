"""Track Spike Radar V2 signals and verified 5m/10m outcomes."""
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

latest = json.loads(Path("data/latest.json").read_text())
history = json.loads(Path("data/history.json").read_text())
path = Path("data/spike_radar_v2_history.json")
saved = json.loads(path.read_text()) if path.exists() else {"schema_version": 1, "records": []}
if not isinstance(saved, dict):
    saved = {"schema_version": 1, "records": []}
if not isinstance(saved.get("records"), list):
    saved["records"] = []
rows = saved["records"]
# One-day cleanup only: remove V2 signals from 20:00–20:59 Warsaw time on 2026-10-16.
# Preserve every other date/hour, including older 20:00 signals.
def excluded_oct16_20h(row):
    at = parse(row.get("first_alert_at_utc"))
    if at is None:
        return False
    local = at.astimezone(ZoneInfo("Europe/Warsaw"))
    return local.date().isoformat() == "2026-10-16" and local.hour == 20

rows[:] = [row for row in rows if not excluded_oct16_20h(row)]

# Restore immutable closed-day values from the previous committed snapshot.
# The tracker must not recalculate old signals from refreshed market history.
# Existing closed-day rows are never mutated in this run.

# Enforce the midday signal block on historical data as well as new alerts.
# This also cleans stale records restored by a concurrent workflow checkout.
def radar_blocked(timestamp):
    at = parse(timestamp)
    return at is not None and at.astimezone(ZoneInfo("Europe/Warsaw")).hour not in (7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21)

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

# Record a new episode only after a previously active signal disappears.
state = saved.get("active_alerts")
if not isinstance(state, dict):
    state = {}
    saved["active_alerts"] = state
current = set()
for item in ([] if day_closed or radar_blocked(now.isoformat()) else latest.get("spike_v2_candidates", [])):
    mint, pair = item.get("mint"), item.get("pair_address")
    price = number(item.get("price_usd"))
    if not mint or not pair or not price or str(item.get("network", "")).lower() != "solana":
        continue
    # Beta entry guard: avoid already-extended 15m momentum (>15%).
    # Missing/non-numeric changes are rejected rather than silently bypassing the guard.
    try:
        change_15m = float(item.get("change_15m_pct"))
    except (TypeError, ValueError):
        continue
    if not (-float("inf") < change_15m < 15.0):
        continue
    key = mint + "|" + pair
    current.add(key)
    if state.get(key):
        continue
    rows.append({
        "day": day, "mint": mint, "pair_address": pair,
        "signal_tier": "STRONG SIGNAL", "network": item.get("network"),
        "symbol": item.get("symbol") or mint[:8],
        "first_alert_at_utc": now.isoformat(), "alert_price_usd": price,
        "initial_15m_change_pct": item.get("change_15m_pct"),
        "buys_5m": item.get("buy_transactions_5m"),
        "sells_5m": item.get("sell_transactions_5m"),
        "volume_5m_usd": item.get("volume_5m_usd"),
        "liquidity_usd": item.get("liquidity_usd"),
        "buy_acceleration_pct": item.get("buy_acceleration_pct"),
        "volume_acceleration_pct": item.get("volume_acceleration_pct"),
        "spike_v2_score": item.get("spike_v2_score"), "measurement_minutes": 5, "status": "PENDING", "price_5m_usd": None,
        "observed_5m_at_utc": None, "return_5m_pct": None
    })
    state[key] = True
for key in list(state):
    if key not in current:
        del state[key]
for row in rows:
    if not is_open_day_record(row):
        continue
    if "measurement_minutes" not in row:
        row["measurement_minutes"] = 15 if row.get("status") == "MEASURED" else 5
    if row.get("status") not in ("PENDING", "NOT_VERIFIABLE") and row.get("return_10m_pct") is not None:
        continue
    start = parse(row.get("first_alert_at_utc"))
    if not start or (now - start).total_seconds() < 300:
        continue
    later = []
    for point in history.get(row["mint"], []):
        if point.get("pair_address") != row["pair_address"]:
            continue
        at, price = parse(point.get("timestamp")), number(point.get("price_usd"))
        if at and price and 300 <= (at - start).total_seconds() <= 86400:
            later.append((at, price))
    ten = [(at, price) for at, price in later if (at - start).total_seconds() >= 600]
    if ten and row.get("return_10m_pct") is None:
        at10, price10 = min(ten, key=lambda p: p[0])
        row.update(price_10m_usd=price10, observed_10m_at_utc=at10.isoformat(),
                   measurement_10m_delay_minutes=round((at10-start).total_seconds()/60, 2),
                   return_10m_pct=round((price10 / row["alert_price_usd"] - 1)*100, 2),
                   status_10m="MEASURED" if (at10-start).total_seconds() <= 900 else "DELAYED")
    elif (now-start).total_seconds() > 900 and row.get("return_10m_pct") is None:
        row["status_10m"] = "NOT_VERIFIABLE"
    if row.get("status") not in ("PENDING", "NOT_VERIFIABLE"):
        continue
    if later:
        at, price = min(later, key=lambda p: p[0])
        delay = round((at - start).total_seconds() / 60, 2)
        row.update(status="MEASURED" if delay <= 10 else "DELAYED", measurement_delay_minutes=delay, price_5m_usd=price,
                   observed_5m_at_utc=at.isoformat(),
                   return_5m_pct=round((price / row["alert_price_usd"] - 1) * 100, 2))
    elif (now - start).total_seconds() > 600:
        row["status"] = "NOT_VERIFIABLE"  # Retry if a later verified same-pair observation becomes available
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
print("Beta spike records:", len(rows), "today:", sum(x["day"] == day for x in rows))
