#!/usr/bin/env python3
"""Keep only the most recent 7 x 24 hours of dashboard history in data/*.json.

Runs after collectors and before their commit, only inside enabled collection hours.
Does not rewrite Git commit history or delete source code/configuration.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path("data")
CUTOFF = datetime.now(timezone.utc) - timedelta(days=7)
TIMESTAMP_FIELDS = (
    "first_alert_at_utc", "first_qualified_at_utc", "observed_at_utc",
    "timestamp", "observed_at", "generated_at_utc", "capturedAt",
    "captured_at_utc", "created_at_utc", "time", "date",
)
DATE_KEY_FIELDS = ("days",)
FILES = (
    "history.json", "recommendations.json", "beta_spike_history.json",
    "beta_spike_v2_history.json", "early_spike_history.json",
    "beta_live_daily.json", "live_v31.json", "daily_snapshots.json",
    "fomo_cache.json",
)

def parse_date(value):
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None

def observation_date(obj):
    if not isinstance(obj, dict):
        return None
    for field in TIMESTAMP_FIELDS:
        if field in obj:
            dt = parse_date(obj[field])
            if dt is not None:
                return dt
    return None

def trim(node, parent_key=""):
    if isinstance(node, list):
        result = []
        for item in node:
            date = observation_date(item)
            if date is not None and date < CUTOFF:
                continue
            result.append(trim(item, parent_key))
        return result
    if isinstance(node, dict):
        result = {}
        for key, value in node.items():
            # YYYY-MM-DD keyed daily snapshots and timestamp-keyed maps.
            date = parse_date(key) if isinstance(key, str) and len(key) >= 10 and key[:4].isdigit() and key[4:5] == "-" else None
            if date is not None and date < CUTOFF:
                continue
            value = trim(value, key)
            # Price-history dictionary: remove empty mint arrays.
            if parent_key == "__history_root__" and isinstance(value, list) and not value:
                continue
            result[key] = value
        return result
    return node

def main():
    for name in FILES:
        path = ROOT / name
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if name == "history.json" and isinstance(data, dict):
            # Mint -> array of timestamped price observations.
            cleaned = {mint: kept for mint, rows in data.items()
                       if (kept := trim(rows)) or not isinstance(rows, list)}
        else:
            cleaned = trim(data)
        if cleaned != data:
            # Atomic replace so partial files cannot be committed.
            temp = path.with_suffix(".pruning.tmp")
            with temp.open("w", encoding="utf-8") as handle:
                json.dump(cleaned, handle, ensure_ascii=False, separators=(",", ":"))
            temp.replace(path)
            print(f"Pruned {name} (cutoff {CUTOFF.isoformat()})")
        else:
            print(f"No expired entries: {name}")

if __name__ == "__main__":
    main()
