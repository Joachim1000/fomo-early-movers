import json, urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE="https://api.dexscreener.com"
def fetch(path):
    req=urllib.request.Request(BASE+path,headers={"User-Agent":"FomoEarlyMovers/1.0"})
    with urllib.request.urlopen(req,timeout=25) as r: return json.load(r)

import os

fomo = {"status": "not_checked", "tokens": []}
api_key = os.environ.get("FOMO_API_KEY")

if api_key:
    try:
        request = urllib.request.Request(
            "https://api.fomoapi.io/v2/leaderboard/tokens/trending?limit=15",
            headers={
                "Authorization": "Bearer " + api_key,
                "Accept": "application/json"
            }
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)

        if not isinstance(result, dict) or not isinstance(result.get("tokens"), list):
            raise ValueError("Unexpected FOMO API response")

        fomo = {
            "status": "ok",
            "board": result.get("board"),
            "source": result.get("source"),
            "captured_at": result.get("capturedAt"),
            "stale": result.get("stale"),
            "age_hours": result.get("ageHours"),
            "count": result.get("count"),
            "tokens": result["tokens"][:15]
        }
        print("FOMO Trending tokens:", len(fomo["tokens"]))
    except Exception as exc:
        fomo = {"status": "error", "tokens": []}
        print("FOMO API error:", type(exc).__name__, str(exc))
else:
    fomo = {"status": "missing_key", "tokens": []}

# Gather broad discovery candidates; only independently observed 12h history qualifies.
from datetime import timedelta
from collections import defaultdict

NOW = datetime.now(timezone.utc)
HISTORY_PATH = Path("data/history.json")
HISTORY_HOURS = 96
MIN_AGE_HOURS = 12
MAX_DISCOVERY_ADDRESSES = 200
MAX_CANDIDATES = 200

def numeric(value):
    try:
        return float(value or 0)
    except (ValueError, TypeError):
        return 0.0

def parse_time(value):
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, tz=timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None

def load_history():
    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}

addresses = set()
errors = []
for endpoint in ("/token-profiles/latest/v1", "/token-boosts/latest/v1", "/token-boosts/top/v1"):
    try:
        for item in fetch(endpoint):
            if item.get("chainId") == "solana" and item.get("tokenAddress"):
                addresses.add(item["tokenAddress"])
    except Exception as exc:
        errors.append(f"{endpoint}: {exc}")

# Include the official FOMO board's Solana mint addresses when the API supplies them.
for token in fomo.get("tokens", []):
    if str(token.get("network", "")).lower() != "solana":
        continue
    for key in ("mint", "tokenAddress", "address", "contractAddress"):
        mint = token.get(key)
        if isinstance(mint, str) and mint:
            addresses.add(mint)
            break

pairs = []
for i in range(0, min(len(addresses), MAX_DISCOVERY_ADDRESSES), 20):
    try:
        batch = sorted(addresses)[:MAX_DISCOVERY_ADDRESSES][i:i + 20]
        pairs.extend(fetch("/latest/dex/tokens/" + ",".join(batch)).get("pairs") or [])
    except Exception as exc:
        errors.append(f"DEX batch {i}: {exc}")

best = {}
for pair in pairs:
    if pair.get("chainId") != "solana":
        continue
    mint = (pair.get("baseToken") or {}).get("address")
    if not mint:
        continue
    liq = numeric((pair.get("liquidity") or {}).get("usd"))
    if mint not in best or liq > numeric((best[mint].get("liquidity") or {}).get("usd")):
        best[mint] = pair

history = load_history()
candidates = []
cutoff = NOW - timedelta(hours=HISTORY_HOURS)
for mint, pair in best.items():
    tx = (pair.get("txns") or {}).get("m5") or {}
    buys = int(tx.get("buys") or 0)
    sells = int(tx.get("sells") or 0)
    liquidity = numeric((pair.get("liquidity") or {}).get("usd"))
    volume = numeric((pair.get("volume") or {}).get("m5"))
    score = round(min(volume / max(liquidity, 1), 3) * 10 + min(buys, 100) * 0.3, 2)
    pair_created = parse_time(pair.get("pairCreatedAt"))
    pair_age_hours = round((NOW - pair_created).total_seconds() / 3600, 2) if pair_created else None

    records = history.get(mint, [])
    records = [r for r in records if isinstance(r, dict) and
               (parse_time(r.get("timestamp")) is not None) and
               cutoff <= parse_time(r.get("timestamp")) <= NOW]
    records.append({
        "timestamp": NOW.isoformat(),
        "pair_address": pair.get("pairAddress"),
        "price_usd": numeric(pair.get("priceUsd")),
        "liquidity_usd": liquidity,
        "volume_5m_usd": volume,
        "buys_5m": buys,
        "sells_5m": sells,
    })
    records.sort(key=lambda r: r["timestamp"])
    # History must belong to the same exact pair, not a different pair for the same mint.
    same_pair = [r for r in records if r.get("pair_address") == pair.get("pairAddress")]
    history[mint] = records[-120:]
    span = ((parse_time(same_pair[-1]["timestamp"]) - parse_time(same_pair[0]["timestamp"])).total_seconds() / 3600) if len(same_pair) > 1 else 0
    historical_prices = [r for r in same_pair if numeric(r.get("price_usd")) > 0 and numeric(r.get("liquidity_usd")) > 0]
    eligible = (pair_age_hours is not None and pair_age_hours >= MIN_AGE_HOURS and
                span >= MIN_AGE_HOURS and len(historical_prices) >= 3 and
                numeric(historical_prices[0].get("price_usd")) > 0 and
                numeric(historical_prices[-1].get("price_usd")) > 0)
    label = ("UNDGÅ" if liquidity < 5000 else
             "TIDLIG KANDIDAT" if eligible and buys >= 5 and buys > sells else "AFVENT")
    candidates.append({
        "mint": mint, "network": "solana",
        "pair_address": pair.get("pairAddress"),
        "symbol": (pair.get("baseToken") or {}).get("symbol"),
        "pair_url": pair.get("url"), "price_usd": pair.get("priceUsd"),
        "market_cap_usd": pair.get("marketCap"), "liquidity_usd": liquidity,
        "volume_5m_usd": volume, "buy_transactions_5m": buys,
        "sell_transactions_5m": sells, "momentum_heuristic": score,
        "pair_created_at_utc": pair_created.isoformat() if pair_created else None,
        "pair_age_hours": pair_age_hours,
        "token_age_hours_estimate": pair_age_hours,
        "token_age_source": "DEX Screener pairCreatedAt (pair age, not proven token mint age)",
        "token_age_verified": False,
        "observed_history_hours": round(span, 2),
        "observed_history_start_utc": same_pair[0]["timestamp"] if same_pair else None,
        "observed_history_end_utc": same_pair[-1]["timestamp"] if same_pair else None,
        "historical_observations": len(historical_prices),
        "history_12h_verified": eligible,
        "minimum_history_hours": MIN_AGE_HOURS,
        "history_note": "12h+ observed same-pair snapshots and pair age" if eligible else "12h trading history not independently established",
        "label": label, "confidence": "LOW",
        "verified_unique_buyer_wallets": None,
        "verified_unique_seller_wallets": None,
        "contract_safety_verified": False
    })

# Retain prior observations for mints temporarily absent from discovery.
for mint in list(history):
    kept = [r for r in history[mint] if isinstance(r, dict) and
            (parse_time(r.get("timestamp")) is not None) and
            cutoff <= parse_time(r.get("timestamp")) <= NOW]
    if kept:
        history[mint] = kept[-120:]
    else:
        del history[mint]

candidates.sort(key=lambda x: x["momentum_heuristic"], reverse=True)
eligible_candidates = [x for x in candidates if x["history_12h_verified"]]
payload = {
    "generated_at_utc": NOW.isoformat(),
    "source": "DEX Screener public API and FOMO Trending when available",
    "fomo_trending": fomo,
    "social_media_used": False,
    "errors": errors,
    "discovered_candidate_count": len(candidates),
    "eligible_12h_count": len(eligible_candidates),
    "top_5": eligible_candidates[:5],
    "candidates": candidates[:MAX_CANDIDATES],
    "eligible_12h_candidates": eligible_candidates[:MAX_CANDIDATES],
    "warning": "12h verification requires same-pair snapshots spanning >=12h AND pair age >=12h; pair creation alone is insufficient. Transaction counts are not unique wallets. No contract safety or KØB verification."
}
Path("data").mkdir(parents=True, exist_ok=True)
HISTORY_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
Path("data/latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"Discovered {len(candidates)} candidates; 12h verified: {len(eligible_candidates)}; errors={len(errors)}")
if not addresses:
    raise SystemExit("No addresses retrieved")
