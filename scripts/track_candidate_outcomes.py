#!/usr/bin/env python3
"""Compact 90-day first-discovery and 5/10/15-minute outcome dataset.

Capture every eligible market candidate (not just a ranked TOP 10). Store
signal-time features immutably, and only use later same-pair prices as labels.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA = Path("data")
latest = json.loads((DATA / "latest.json").read_text(encoding="utf-8"))
history = json.loads((DATA / "history.json").read_text(encoding="utf-8"))
path = DATA / "candidate_outcomes.json"
saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"schema_version": 1, "records": []}
records = saved.setdefault("records", [])
now = datetime.fromisoformat(latest["generated_at_utc"].replace("Z", "+00:00"))
cutoff = now - timedelta(days=90)
def parse(v):
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
records[:] = [r for r in records if (parse(r.get("first_seen_at_utc")) or now) >= cutoff]
by_pair = {(r.get("mint"), r.get("pair_address")): r for r in records}
new = 0
for x in latest.get("candidates", []):
    mint, pair = x.get("mint"), x.get("pair_address")
    try: price = float(x.get("price_usd") or 0)
    except (TypeError, ValueError): continue
    if not mint or not pair or price <= 0 or mint == "So11111111111111111111111111111111111111112":
        continue
    key = (mint, pair)
    if key in by_pair:
        continue
    row = {
        "mint": mint, "pair_address": pair, "symbol": x.get("symbol"),
        "first_seen_at_utc": now.isoformat(), "entry_price_usd": price,
        "pair_age_hours": x.get("pair_age_hours"),
        "buys_5m": x.get("buy_transactions_5m"),
        "sells_5m": x.get("sell_transactions_5m"),
        "volume_5m_usd": x.get("volume_5m_usd"),
        "liquidity_usd": x.get("liquidity_usd"),
        "momentum_heuristic": x.get("momentum_heuristic"),
        "trend_2h_pct": x.get("trend_2h_pct"),
        "outcomes": {}
    }
    records.append(row)
    by_pair[key] = row
    new += 1
for r in records:
    start = parse(r.get("first_seen_at_utc"))
    if not start:
        continue
    observations = []
    for z in history.get(r["mint"], []):
        if z.get("pair_address") != r["pair_address"]:
            continue
        t = parse(z.get("timestamp"))
        try: price = float(z.get("price_usd") or 0)
        except (TypeError, ValueError): continue
        if t and start < t <= start + timedelta(minutes=18) and price > 0:
            observations.append((t, price))
    for minutes in (5, 10, 15):
        label = str(minutes)
        if label in r["outcomes"] and r["outcomes"][label].get("status") != "NOT_VERIFIABLE":
            continue
        target = start + timedelta(minutes=minutes)
        # Only forward observations within three minutes: never use pre-signal prices.
        eligible = [(t, p) for t, p in observations if target <= t <= target + timedelta(minutes=3)]
        if eligible:
            t, price = min(eligible)
            r["outcomes"][label] = {
                "observed_at_utc": t.isoformat(),
                "price_usd": price,
                "return_pct": round(100 * (price / float(r["entry_price_usd"]) - 1), 4),
                "delay_seconds": round((t-target).total_seconds())
            }
        elif now > target + timedelta(minutes=3):
            r["outcomes"][label] = {"status": "NOT_VERIFIABLE"}
saved["updated_at_utc"] = now.isoformat()
saved["retention_days"] = 90
saved["records"] = records
path.write_text(json.dumps(saved, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
print(f"Candidate outcomes: {new} new; {len(records)} retained")
