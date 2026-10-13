"""Independent BETA SPIKE V2 experiment with immediate qualifying alerts."""
import json
from datetime import datetime, timezone
from pathlib import Path

def dt(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None

def positive(value):
    try:
        n = float(value)
        return n if n > 0 else None
    except (ValueError, TypeError):
        return None

latest = json.loads(Path("data/latest.json").read_text())
history = json.loads(Path("data/history.json").read_text())
path = Path("data/beta_spike_v2_history.json")
saved = json.loads(path.read_text()) if path.exists() else {}
rows = saved.setdefault("records", [])
pending = saved.setdefault("pending_confirmation", {})  # Legacy state, no longer required
active = saved.setdefault("active_alerts", {})
now = dt(latest.get("generated_at_utc"))
if now is None:
    raise ValueError("Missing snapshot timestamp")

# V2: higher liquidity, stronger buy pressure, cap already-extended moves.
qualifying = {}
for x in latest.get("candidates", []):
    mint, pair = x.get("mint"), x.get("pair_address")
    price = positive(x.get("price_usd"))
    buys = int(x.get("buy_transactions_5m") or 0)
    sells = int(x.get("sell_transactions_5m") or 0)
    liquidity = float(x.get("liquidity_usd") or 0)
    change = x.get("change_15m_pct")
    age = x.get("pair_age_hours")
    if not (mint and pair and price and x.get("early_alert")
            and buys >= 25 and buys >= 2 * max(sells, 1)
            and liquidity >= 20000 and change is not None
            and 5 <= float(change) <= 20
            and age is not None and float(age) > 2):
        continue
    qualifying[mint + "|" + pair] = x

# Alert immediately on the first qualifying snapshot; no 4–12 minute wait.
# Existing active episode state prevents duplicate alerts until qualification disappears.
pending.clear()
for key in list(active):
    if key not in qualifying:
        active.pop(key, None)
for key, x in qualifying.items():
    if key in active:
        continue
    rows.append({
        "mint": x["mint"], "pair_address": x["pair_address"],
        "symbol": x.get("symbol") or x["mint"][:8],
        "first_qualified_at_utc": now.isoformat(),
        "first_alert_at_utc": now.isoformat(),
        "alert_price_usd": float(x["price_usd"]),
        "change_15m_pct": x.get("change_15m_pct"),
        "buys_5m": x.get("buy_transactions_5m"),
        "sells_5m": x.get("sell_transactions_5m"),
        "liquidity_usd": x.get("liquidity_usd"),
        "status": "PENDING", "return_5m_pct": None,
        "price_5m_usd": None, "observed_5m_at_utc": None
    })
    active[key] = True

for row in rows:
    if row.get("status") == "MEASURED":
        continue
    start = dt(row.get("first_alert_at_utc"))
    if start is None or (now-start).total_seconds() < 300:
        continue
    samples = []
    for point in history.get(row["mint"], []):
        if point.get("pair_address") != row["pair_address"]:
            continue
        at, p = dt(point.get("timestamp")), positive(point.get("price_usd"))
        if at and p and 300 <= (at-start).total_seconds() <= 600:
            samples.append((at, p))
    if samples:
        at, p = min(samples, key=lambda v: v[0])
        row.update(status="MEASURED", price_5m_usd=p,
                   observed_5m_at_utc=at.isoformat(),
                   measurement_delay_minutes=round((at-start).total_seconds()/60, 2),
                   return_5m_pct=round(100*(p/float(row["alert_price_usd"])-1), 2))
    elif (now-start).total_seconds() > 600:
        row["status"] = "NOT_VERIFIABLE"

saved["schema_version"] = 1
saved["updated_at_utc"] = now.isoformat()
saved["strategy"] = "BETA V2: 25 buys, buy/sell >=2, liquidity >=$20k, 15m gain 5–20%, immediate first qualifying snapshot"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(saved, ensure_ascii=False, indent=2)+"\n")
print("BETA V2 observations:", len(rows), "awaiting confirmation:", len(pending))
