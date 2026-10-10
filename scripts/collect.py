import json, urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE="https://api.dexscreener.com"
def fetch(path):
    req=urllib.request.Request(BASE+path,headers={"User-Agent":"FomoEarlyMovers/1.0"})
    with urllib.request.urlopen(req,timeout=25) as r: return json.load(r)

import os

api_key = os.environ.get("FOMO_API_KEY")

# Retrieve the maximum distinct tokens the FOMO API exposes. Stop if the API
# ignores offset, returns an empty page, or repeats a page; never invent entries.
FOMO_PAGE_LIMIT = 100
FOMO_MAX_PAGES = 20

def fetch_fomo_board(board):
    if not api_key:
        return {"status": "missing_key", "tokens": []}
    tokens, seen = [], set()
    metadata = {}
    for page in range(FOMO_MAX_PAGES):
        offset = page * FOMO_PAGE_LIMIT
        url = ("https://api.fomoapi.io/v2/leaderboard/tokens/" + board +
               "?limit=" + str(FOMO_PAGE_LIMIT) + "&offset=" + str(offset))
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + api_key,
            "Accept": "application/json"
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.load(response)
        except Exception as exc:
            if not tokens:
                return {"status": "error", "tokens": [], "error": str(exc)[:180]}
            metadata["pagination_warning"] = str(exc)[:180]
            break
        if not isinstance(result, dict) or not isinstance(result.get("tokens"), list):
            if not tokens:
                return {"status": "error", "tokens": [], "error": "Invalid API payload"}
            metadata["pagination_warning"] = "Invalid subsequent page"
            break
        if page == 0:
            metadata = {k: result.get(k) for k in ("source", "capturedAt", "stale", "ageHours")}
        batch = result["tokens"]
        new = 0
        for item in batch:
            token = item.get("token") or {}
            mint = token.get("address") or item.get("mint") or item.get("tokenAddress") or item.get("address")
            network = str(item.get("network", ""))
            key = (network, mint) if mint else (network, json.dumps(item, sort_keys=True))
            if key in seen:
                continue
            seen.add(key)
            tokens.append(item)
            new += 1
        if not batch or new == 0 or len(batch) < FOMO_PAGE_LIMIT:
            break
    return {
        "status": "ok", "board": board, "tokens": tokens,
        "count": len(tokens), "pages_checked": page + 1,
        **metadata
    }

fomo = fetch_fomo_board("trending")
print("FOMO Trending tokens:", len(fomo["tokens"]))
fomo_boards = {board: fetch_fomo_board(board) for board in ("graduated", "most-held")}

# Gather broad DEX discovery for price enrichment; not FOMO verification.

from datetime import timedelta
from collections import defaultdict

NOW = datetime.now(timezone.utc)
HISTORY_PATH = Path("data/history.json")
HISTORY_HOURS = 96
MIN_AGE_HOURS = 4
MAX_DISCOVERY_ADDRESSES = 3000
MAX_CANDIDATES = 3000

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

# The FOMO API returns nested token.address and numeric Solana network 1399811149.
# Prior versions silently skipped all FOMO mints, leaving their history empty.
fomo_sol_mints = set()
fomo_source_by_mint = {}
for board_name, entries in [("trending", fomo.get("tokens", []))] + [
    (name, board.get("tokens", [])) for name, board in fomo_boards.items()
]:
    for item in entries:
        token = item.get("token") or {}
        chain = str(item.get("network", "")).lower()
        if chain not in ("solana", "1399811149"):
            continue
        mint = token.get("address") or item.get("mint") or item.get("tokenAddress") or item.get("address")
        if isinstance(mint, str) and mint:
            fomo_sol_mints.add(mint)
            addresses.add(mint)
            fomo_source_by_mint.setdefault(mint, []).append(board_name)

# Always query FOMO addresses first; a capped alphabetical discovery list
# previously crowded out the tokens the user actually sees in the FOMO app.
ordered_addresses = sorted(fomo_sol_mints) + sorted(addresses - fomo_sol_mints)
ordered_addresses = ordered_addresses[:MAX_DISCOVERY_ADDRESSES]
pairs = []
for i in range(0, len(ordered_addresses), 20):
    try:
        batch = ordered_addresses[i:i + 20]
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

# Recommendation mints are tracked even when absent from both discovery and retained history.
try:
    recommendation_records = json.loads(Path("data/recommendations.json").read_text(encoding="utf-8")).get("records", [])
except (OSError, ValueError, AttributeError):
    recommendation_records = []
recent_recommendations = {}
for record in recommendation_records:
    observed = parse_time(record.get("observed_at_utc")) if isinstance(record, dict) else None
    if observed and 0 <= (NOW - observed).total_seconds() <= HISTORY_HOURS * 3600:
        mint, pair_address = record.get("mint"), record.get("pair_address")
        if mint and pair_address:
            recent_recommendations.setdefault(mint, set()).add(pair_address)

# Refresh previously tracked tokens independently of the discovery cap.
tracked_mints = sorted((set(history) | set(recent_recommendations)) - set(best))
tracked_pairs = {}
for i in range(0, len(tracked_mints), 20):
    try:
        response = fetch("/latest/dex/tokens/" + ",".join(tracked_mints[i:i + 20]))
        for pair in response.get("pairs") or []:
            if pair.get("chainId") != "solana":
                continue
            mint = (pair.get("baseToken") or {}).get("address")
            if mint not in history and mint not in recent_recommendations or mint in best:
                continue
            prior_pairs = {p.get("pair_address") for p in history.get(mint, []) if isinstance(p, dict)} | recent_recommendations.get(mint, set())
            if pair.get("pairAddress") not in prior_pairs or numeric(pair.get("priceUsd")) <= 0:
                continue
            previous = tracked_pairs.get(mint)
            if previous is None or numeric((pair.get("liquidity") or {}).get("usd")) > numeric((previous.get("liquidity") or {}).get("usd")):
                tracked_pairs[mint] = pair
    except Exception as exc:
        errors.append(f"Tracked token batch {i}: {exc}")

# These are price-only observations, not new FOMO discovery candidates.
for mint, pair in tracked_pairs.items():
    tx = (pair.get("txns") or {}).get("m5") or {}
    history.setdefault(mint, []).append({
        "timestamp": NOW.isoformat(),
        "pair_address": pair.get("pairAddress"),
        "price_usd": numeric(pair.get("priceUsd")),
        "liquidity_usd": numeric((pair.get("liquidity") or {}).get("usd")),
        "volume_5m_usd": numeric((pair.get("volume") or {}).get("m5")),
        "buys_5m": int(tx.get("buys") or 0),
        "sells_5m": int(tx.get("sells") or 0),
    })

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
    # Rank using both current 5m activity and the full observed 2h same-pair price trend.
    trend_window = [p for p in historical_prices if
                    (NOW - parse_time(p["timestamp"])).total_seconds() <= 7200]
    trend_pct = None
    trend_r2 = None
    trend_bonus = 0.0
    if len(trend_window) >= 2:
        import math
        t0 = parse_time(trend_window[0]["timestamp"])
        xs = [(parse_time(p["timestamp"]) - t0).total_seconds() / 3600 for p in trend_window]
        ys = [math.log(numeric(p["price_usd"])) for p in trend_window]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        denominator = sum((x - mx) ** 2 for x in xs)
        if denominator > 0:
            slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denominator
            fit = [my + slope * (x - mx) for x in xs]
            total = sum((y - my) ** 2 for y in ys)
            residual = sum((y - f) ** 2 for y, f in zip(ys, fit))
            trend_r2 = max(0.0, 1 - residual / total) if total > 1e-12 else 0.0
            trend_pct = 100 * math.expm1(slope * (xs[-1] - xs[0]))
            # Modest, bounded trend influence; weak trends receive reduced credit.
            trend_bonus = round(max(-10.0, min(10.0, trend_pct * trend_r2 * 0.4)), 2)
    score = round(score + trend_bonus, 2)
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
        "trend_2h_pct": round(trend_pct, 2) if trend_pct is not None else None,
        "trend_2h_r2": round(trend_r2, 3) if trend_r2 is not None else None,
        "trend_2h_score_contribution": trend_bonus,
        "pair_created_at_utc": pair_created.isoformat() if pair_created else None,
        "pair_age_hours": pair_age_hours,
        "token_age_hours_estimate": pair_age_hours,
        "token_age_source": "DEX Screener pairCreatedAt (pair age, not proven token mint age)",
        "token_age_verified": False,
        "observed_history_hours": round(span, 2),
        "observed_history_start_utc": same_pair[0]["timestamp"] if same_pair else None,
        "observed_history_end_utc": same_pair[-1]["timestamp"] if same_pair else None,
        "historical_observations": len(historical_prices),
        "history_12h_verified": False,
        "history_4h_verified": eligible,
        "fomo_trending_verified": "trending" in fomo_source_by_mint.get(mint, []),
        "fomo_board_verified": mint in fomo_sol_mints,
        "fomo_boards": fomo_source_by_mint.get(mint, []),
        "pre_trending_screening": mint in fomo_sol_mints and "trending" not in fomo_source_by_mint.get(mint, []),
        "minimum_history_hours": MIN_AGE_HOURS,
        "history_note": "4h+ observed same-pair snapshots and pair age" if eligible else "4h history not independently established",
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
eligible_candidates = [x for x in candidates if x["history_4h_verified"] and x["fomo_board_verified"]]
payload = {
    "generated_at_utc": NOW.isoformat(),
    "source": "DEX Screener public API and FOMO Trending when available",
    "fomo_trending": fomo,
    "fomo_other_boards": fomo_boards,
    "fomo_verified_mints": [{"mint": mint, "network": "solana", "boards": boards} for mint, boards in fomo_source_by_mint.items()],
    "social_media_used": False,
    "errors": errors,
    "discovered_candidate_count": len(candidates),
    "tracked_recommendation_mints": len(recent_recommendations),
    "refreshed_tracked_mints": len(tracked_pairs),
    "eligible_4h_count": len(eligible_candidates),
    "fomo_dex_matched_count": sum(1 for x in candidates if x["fomo_board_verified"]),
    "eligible_12h_count": 0,
    "top_5": eligible_candidates[:5],
    "candidates": candidates[:MAX_CANDIDATES],
    "eligible_4h_candidates": eligible_candidates[:MAX_CANDIDATES],
    "eligible_12h_candidates": [],
    "warning": "4h verification requires timestamped same-pair observations spanning >=4h AND pair age >=4h. FOMO candidates are matched by exact Solana mint. Transaction counts are not unique wallets. No contract safety or KØB verification."
}
Path("data").mkdir(parents=True, exist_ok=True)
HISTORY_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
Path("data/latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"Discovered {len(candidates)} candidates; FOMO-matched 4h verified: {len(eligible_candidates)}; errors={len(errors)}")
if not addresses:
    raise SystemExit("No addresses retrieved")
