"""Independently track experimental beta early spikes and verified 5m outcomes."""
import json
from datetime import datetime, timezone
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
path = Path("data/beta_spike_history.json")
saved = json.loads(path.read_text()) if path.exists() else {"schema_version": 1, "records": []}
rows = saved.setdefault("records", [])
now = parse(latest.get("generated_at_utc"))
if now is None:
    raise ValueError("Missing latest snapshot timestamp")
zone = ZoneInfo("Europe/Warsaw")
day = now.astimezone(zone).date().isoformat()
existing = {(x["day"], x["mint"], x["pair_address"]) for x in rows}
for item in latest.get("beta_alert_candidates", []):
    mint, pair = item.get("mint"), item.get("pair_address")
    price = number(item.get("price_usd"))
    if not mint or not pair or not price or (day, mint, pair) in existing:
        continue
    rows.append({
        "day": day, "mint": mint, "pair_address": pair,
        "symbol": item.get("symbol") or mint[:8],
        "first_alert_at_utc": now.isoformat(), "alert_price_usd": price,
        "initial_15m_change_pct": item.get("change_15m_pct"),
        "buys_5m": item.get("buy_transactions_5m"),
        "sells_5m": item.get("sell_transactions_5m"),
        "measurement_minutes": 5, "status": "PENDING", "price_5m_usd": None,
        "observed_5m_at_utc": None, "return_5m_pct": None
    })
    existing.add((day, mint, pair))
for row in rows:
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
saved["updated_at_utc"] = now.isoformat()
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(saved, ensure_ascii=False, indent=2) + "\n")
print("Beta spike records:", len(rows), "today:", sum(x["day"] == day for x in rows))
