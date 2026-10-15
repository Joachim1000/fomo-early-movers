import json, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, time, timedelta
from zoneinfo import ZoneInfo
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
FOMO_MAX_PAGES = 1

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

# Conserve paid FOMO calls: refresh leaderboard boards at most every 15 minutes.
# Collection is separately gated to 07:00–11:00 and 14:00–16:00 Warsaw time.
# DEX market prices can still refresh on each active collection run.
FOMO_CACHE_PATH = Path("data/fomo_cache.json")
fomo_now = datetime.now(timezone.utc)
try:
    fomo_cache = json.loads(FOMO_CACHE_PATH.read_text(encoding="utf-8"))
except (OSError, ValueError):
    fomo_cache = {}
cached_boards = fomo_cache.get("boards") or []
cached_at = fomo_cache.get("checked_at_utc")
try:
    cache_age = (fomo_now - datetime.fromisoformat(cached_at.replace("Z", "+00:00"))).total_seconds()
except (AttributeError, ValueError, TypeError):
    cache_age = float("inf")
refresh_fomo = cache_age >= 900 or len(cached_boards) != 3
if refresh_fomo:
    with ThreadPoolExecutor(max_workers=3) as pool:
        boards = list(pool.map(fetch_fomo_board, ("trending", "graduated", "most-held")))
    # Persist successful results; avoid replacing known boards with HTTP 402 errors.
    if all(board.get("status") == "ok" for board in boards):
        FOMO_CACHE_PATH.write_text(json.dumps({
            "checked_at_utc": fomo_now.isoformat(), "boards": boards
        }, ensure_ascii=False), encoding="utf-8")
    elif len(cached_boards) == 3:
        boards = cached_boards
else:
    boards = cached_boards if len(cached_boards) == 3 else [
        {"status": "cache_unavailable", "tokens": []} for _ in range(3)
    ]
fomo, graduated, most_held = boards
print("FOMO Trending tokens:", len(fomo["tokens"]))
fomo_boards = {"graduated": graduated, "most-held": most_held}

# Gather broad DEX discovery for price enrichment; not FOMO verification.

from collections import defaultdict

NOW = datetime.now(timezone.utc)
HISTORY_PATH = Path("data/history.json")
HISTORY_HOURS = 96
MIN_AGE_HOURS = 4
# Retain every observation during the trading day. At the 23:30 Warsaw
# rollover, keep the last four hours as a bridge for 4h-qualified signals.
# This avoids a midnight blind spot in LIVE/BETA and removes the 120/480 cap.
WARSAW = ZoneInfo("Europe/Warsaw")
local_now = NOW.astimezone(WARSAW)
last_rollover_date = local_now.date() if local_now.time() >= time(23, 30) else local_now.date() - timedelta(days=1)
last_rollover = datetime.combine(last_rollover_date, time(23, 30), tzinfo=WARSAW).astimezone(timezone.utc)
HISTORY_KEEP_FROM = last_rollover - timedelta(hours=MIN_AGE_HOURS)
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
def safe_fetch_discovery(endpoint):
    try:
        return endpoint, fetch(endpoint), None
    except Exception as exc:
        return endpoint, [], str(exc)

discovery_endpoints = ("/token-profiles/latest/v1", "/token-boosts/latest/v1", "/token-boosts/top/v1")
with ThreadPoolExecutor(max_workers=3) as pool:
    discovery_results = list(pool.map(lambda endpoint: safe_fetch_discovery(endpoint), discovery_endpoints))
for endpoint, items, error in discovery_results:
    if error:
        errors.append(f"{endpoint}: {error}")
    else:
        for item in items:
            if item.get("chainId") in ("solana", "ethereum", "base", "bsc", "arbitrum", "polygon", "optimism", "avalanche") and item.get("tokenAddress"):
                addresses.add(item["tokenAddress"])

# FOMO returns nested token.address and chain identifiers for Solana and EVM.
# Prior versions silently skipped all FOMO mints, leaving their history empty.
fomo_mints = set()
fomo_mint_keys = set()
fomo_network_by_mint = {}
FOMO_NETWORKS = {"solana":"solana","1399811149":"solana","ethereum":"ethereum","1":"ethereum","eip155:1":"ethereum","base":"base","8453":"base","eip155:8453":"base","bsc":"bsc","56":"bsc","arbitrum":"arbitrum","42161":"arbitrum","polygon":"polygon","137":"polygon","optimism":"optimism","10":"optimism","avalanche":"avalanche","43114":"avalanche"}
fomo_source_by_mint = {}
def fomo_key(mint, network):
    return (str(network or '').lower(), str(mint or '').lower() if str(network or '').lower() != 'solana' else str(mint or ''))

for board_name, entries in [("trending", fomo.get("tokens", []))] + [
    (name, board.get("tokens", [])) for name, board in fomo_boards.items()
]:
    for item in entries:
        token = item.get("token") or {}
        chain = str(item.get("network") or item.get("chainId") or token.get("network") or "").lower()
        network = FOMO_NETWORKS.get(chain)
        if not network:
            continue
        mint = token.get("address") or item.get("mint") or item.get("tokenAddress") or item.get("address")
        if isinstance(mint, str) and mint:
            fomo_mints.add(mint)
            fomo_mint_keys.add(fomo_key(mint, network))
            fomo_network_by_mint[mint.lower()] = network
            addresses.add(mint)
            fomo_source_by_mint.setdefault(fomo_key(mint, network), []).append(board_name)

# Always query FOMO addresses first; a capped alphabetical discovery list
# previously crowded out the tokens the user actually sees in the FOMO app.
ordered_addresses = sorted(fomo_mints) + sorted(addresses - fomo_mints)
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
    if pair.get("chainId") not in ("solana", "ethereum", "base", "bsc", "arbitrum", "polygon", "optimism", "avalanche"):
        continue
    expected = fomo_network_by_mint.get(str((pair.get("baseToken") or {}).get("address", "")).lower())
    if expected and pair.get("chainId") != expected:
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
# Keep V3.1 exact mint+pair prices refreshed for performance verification.
try:
    v31_records = json.loads(Path("data/live_v31.json").read_text(encoding="utf-8")).get("observations", [])
except (OSError, ValueError, AttributeError):
    v31_records = []
for record in v31_records:
    observed = parse_time(record.get("observed_at_utc")) if isinstance(record, dict) else None
    if observed and 0 <= (NOW - observed).total_seconds() <= HISTORY_HOURS * 3600:
        mint, pair_address = record.get("mint"), record.get("pair_address")
        if mint and pair_address:
            recent_recommendations.setdefault(mint, set()).add(pair_address)
for record in recommendation_records:
    observed = parse_time(record.get("observed_at_utc")) if isinstance(record, dict) else None
    if observed and 0 <= (NOW - observed).total_seconds() <= HISTORY_HOURS * 3600:
        mint, pair_address = record.get("mint"), record.get("pair_address")
        if mint and pair_address:
            recent_recommendations.setdefault(mint, set()).add(pair_address)

# Refresh previously tracked tokens independently of the discovery cap.
tracked_mints = sorted(set(history) | set(recent_recommendations))
tracked_pairs = {}
for i in range(0, len(tracked_mints), 20):
    try:
        response = fetch("/latest/dex/tokens/" + ",".join(tracked_mints[i:i + 20]))
        for pair in response.get("pairs") or []:
            if pair.get("chainId") not in ("solana", "ethereum", "base", "bsc", "arbitrum", "polygon", "optimism", "avalanche"):
                continue
            mint = (pair.get("baseToken") or {}).get("address")
            if mint not in history and mint not in recent_recommendations:
                continue
            prior_pairs = {p.get("pair_address") for p in history.get(mint, []) if isinstance(p, dict)} | recent_recommendations.get(mint, set())
            if pair.get("pairAddress") not in prior_pairs or numeric(pair.get("priceUsd")) <= 0:
                continue
            # Preserve every exact tracked pair, not just the highest-liquidity pair per mint.
            tracked_pairs[(mint, pair.get("pairAddress"))] = pair
    except Exception as exc:
        errors.append(f"Tracked token batch {i}: {exc}")

# These are price-only observations, not new FOMO discovery candidates.
for (mint, pair_address), pair in tracked_pairs.items():
    # The discovery pass already records its chosen pair below; avoid duplicate samples.
    if mint in best and best[mint].get('pairAddress') == pair_address:
        continue
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
cutoff = HISTORY_KEEP_FROM
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
    history[mint] = records
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
    # Fast-moving watchlist: separate from the conservative 4h confirmation.
    # Use only same-pair observations; never infer an unobserved intraperiod peak.
    recent_15m = [p for p in historical_prices if
                  0 <= (NOW - parse_time(p["timestamp"])).total_seconds() <= 900]
    baseline = next((p for p in recent_15m
                     if (NOW - parse_time(p["timestamp"])).total_seconds() >= 240), None)
    change_15m = (round(100 * (numeric(pair.get("priceUsd")) /
                    numeric(baseline["price_usd"]) - 1), 2)
                  if baseline and numeric(baseline["price_usd"]) > 0 else None)
    # Early momentum: measured against the closest observed same-pair price
    # between 2 and 5 minutes old, never an inferred candle high.
    short_baselines = [p for p in recent_15m if
                       120 <= (NOW - parse_time(p["timestamp"])).total_seconds() <= 300]
    short_baseline = min(short_baselines, key=lambda p:
                         abs((NOW - parse_time(p["timestamp"])).total_seconds() - 180)) if short_baselines else None
    change_2_5m = (round(100 * (numeric(pair.get("priceUsd")) /
                   numeric(short_baseline["price_usd"]) - 1), 2)
                   if short_baseline and numeric(short_baseline["price_usd"]) > 0 else None)
    prior_activity = [p for p in recent_15m if
                      300 <= (NOW - parse_time(p["timestamp"])).total_seconds() <= 600]
    prior_buys = max((int(p.get("buys_5m") or 0) for p in prior_activity), default=None)
    prior_volume = max((numeric(p.get("volume_5m_usd")) for p in prior_activity), default=None)
    early_momentum = (pair_age_hours is not None and pair_age_hours > 2 and
                      fomo_key(mint, pair.get('chainId')) in fomo_mint_keys and
                      liquidity >= 5000 and volume >= 1000 and
                      buys >= 8 and buys >= 1.5 * max(sells, 1) and
                      change_2_5m is not None and 2 <= change_2_5m <= 25 and
                      prior_buys is not None and buys >= prior_buys * 1.2)
    radar_time_allowed = not (12 <= NOW.astimezone(WARSAW).hour < 15)
    fast_alert = (radar_time_allowed and
                  pair_age_hours is not None and pair_age_hours > 2 and
                   fomo_key(mint, pair.get('chainId')) in fomo_mint_keys and liquidity >= 5000 and
                  buys >= 8 and buys >= 1.5 * max(sells, 1) and
                  volume >= 1000 and change_15m is not None and
                  change_15m >= 3)
    # Spike Radar V2 shadow model: momentum, pressure, acceleration and exhaustion.
    pressure = buys / max(sells, 1)
    accelerating = prior_buys is not None and buys >= prior_buys * 1.2
    exhaustion = change_15m is not None and change_15m > 25 or (
        prior_buys is not None and buys < prior_buys * 0.8)
    v2_score = min(100, round(
        25 * min(pressure / 3, 1) +
        25 * min(buys / 35, 1) +
        20 * min(volume / 5000, 1) +
        (20 if accelerating else 0) +
        (10 if change_15m is not None and 3 <= change_15m <= 15 else 0)
        - (30 if exhaustion else 0)))
    # The same midday signal pause applies to both Spike Radar models.
    v2_alert = (radar_time_allowed and
                pair_age_hours is not None and pair_age_hours > 2 and
                fomo_key(mint, pair.get('chainId')) in fomo_mint_keys and
                liquidity >= 5000 and volume >= 1500 and
                change_15m is not None and 3 <= change_15m <= 25 and
                buys >= 15 and pressure >= 2 and accelerating and
                not exhaustion and v2_score >= 75)
    label = ("UNDGÅ" if liquidity < 5000 else
             "TIDLIG KANDIDAT" if eligible and buys >= 5 and buys > sells else "AFVENT")
    candidates.append({
        "mint": mint, "network": pair.get("chainId"),
        "early_alert": fast_alert,
        "early_momentum": early_momentum,
        "change_2_5m_pct": change_2_5m,
        "buy_acceleration_pct": round(100 * (buys / prior_buys - 1), 2) if prior_buys is not None and prior_buys > 0 else None,
        "volume_acceleration_pct": round(100 * (volume / prior_volume - 1), 2) if prior_volume is not None and prior_volume > 0 else None,
        "spike_v2_alert": v2_alert, "spike_v2_score": v2_score,
        "change_15m_pct": change_15m,
        "early_alert_observation_count": len(recent_15m),
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
        "fomo_trending_verified": "trending" in fomo_source_by_mint.get(fomo_key(mint, pair.get('chainId')), []),
        "fomo_board_verified": fomo_key(mint, pair.get('chainId')) in fomo_mint_keys,
        "fomo_boards": fomo_source_by_mint.get(fomo_key(mint, pair.get('chainId')), []),
        "pre_trending_screening": fomo_key(mint, pair.get('chainId')) in fomo_mint_keys and "trending" not in fomo_source_by_mint.get(fomo_key(mint, pair.get('chainId')), []),
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
        history[mint] = kept
    else:
        del history[mint]

candidates.sort(key=lambda x: x["momentum_heuristic"], reverse=True)
early_alert_candidates = [x for x in candidates if x["early_alert"]]
early_momentum_candidates = [x for x in candidates if x["early_momentum"]]
early_momentum_candidates.sort(key=lambda x: (x["change_2_5m_pct"], x["momentum_heuristic"]), reverse=True)
spike_v2_candidates = [x for x in candidates if x["spike_v2_alert"]]
spike_v2_candidates.sort(key=lambda x: (x["spike_v2_score"], x["momentum_heuristic"]), reverse=True)
early_alert_candidates.sort(key=lambda x: (x["change_15m_pct"], x["momentum_heuristic"]), reverse=True)
eligible_candidates = [x for x in candidates if x["history_4h_verified"] and x["fomo_board_verified"]]
payload = {
    "generated_at_utc": NOW.isoformat(),
    "source": "DEX Screener public API and FOMO Trending when available",
    "fomo_trending": fomo,
    "fomo_other_boards": fomo_boards,
    "fomo_verified_mints": [{"mint": mint, "network": network, "boards": boards} for (network, mint), boards in fomo_source_by_mint.items()],
    "social_media_used": False,
    "errors": errors,
    "discovered_candidate_count": len(candidates),
    "tracked_recommendation_mints": len(recent_recommendations),
    "refreshed_tracked_mints": len(tracked_pairs),
    "eligible_4h_count": len(eligible_candidates),
    "spike_v2_count": len(spike_v2_candidates),
    "spike_v2_candidates": spike_v2_candidates[:20],
    "early_momentum_count": len(early_momentum_candidates),
    "early_momentum_candidates": early_momentum_candidates[:20],
    "early_alert_count": len(early_alert_candidates),
    "early_alert_candidates": early_alert_candidates[:20],
    "fomo_dex_matched_count": sum(1 for x in candidates if x["fomo_board_verified"]),
    "eligible_12h_count": 0,
    "top_5": eligible_candidates[:5],
    "candidates": candidates[:MAX_CANDIDATES],
    "eligible_4h_candidates": eligible_candidates[:MAX_CANDIDATES],
    "eligible_12h_candidates": [],
    "warning": "4h verification requires timestamped same-pair observations spanning >=4h AND pair age >=4h. FOMO candidates are matched by exact address and network when available. Transaction counts are not unique wallets. No contract safety or KØB verification."
}
Path("data").mkdir(parents=True, exist_ok=True)
# GitHub rejects any individual file above 100 MiB. Use compact JSON and
# prune oldest observations only when needed, preserving newest price history.
# Leave substantial headroom so the next scheduled collector can still publish.
MAX_HISTORY_BYTES = 75 * 1024 * 1024
history_json = json.dumps(history, ensure_ascii=False, separators=(",", ":"))
if len(history_json.encode("utf-8")) > MAX_HISTORY_BYTES:
    print("History exceeds 75 MiB; trimming oldest observations in 1h increments")
    trim_from = HISTORY_KEEP_FROM
    for _ in range(36):
        trim_from += timedelta(hours=1)
        for mint in list(history):
            kept = [row for row in history[mint] if (parse_time(row.get("timestamp")) or NOW) >= trim_from]
            if kept:
                history[mint] = kept
            else:
                del history[mint]
        history_json = json.dumps(history, ensure_ascii=False, separators=(",", ":"))
        if len(history_json.encode("utf-8")) <= MAX_HISTORY_BYTES:
            break
    if len(history_json.encode("utf-8")) > MAX_HISTORY_BYTES:
        raise RuntimeError("History remains above GitHub safe size after time pruning; refusing oversized push")
HISTORY_PATH.write_text(history_json, encoding="utf-8")
Path("data/latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"Discovered {len(candidates)} candidates; FOMO-matched 4h verified: {len(eligible_candidates)}; errors={len(errors)}")
if not addresses:
    raise SystemExit("No addresses retrieved")
