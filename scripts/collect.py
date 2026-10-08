import json, urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE="https://api.dexscreener.com"
def fetch(path):
    req=urllib.request.Request(BASE+path,headers={"User-Agent":"FomoEarlyMovers/1.0"})
    with urllib.request.urlopen(req,timeout=25) as r: return json.load(r)

addresses=set()
errors=[]
for endpoint in ("/token-profiles/latest/v1","/token-boosts/latest/v1","/token-boosts/top/v1"):
    try:
        for item in fetch(endpoint):
            if item.get("chainId")=="solana" and item.get("tokenAddress"):
                addresses.add(item["tokenAddress"])
    except Exception as e: errors.append(f"{endpoint}: {e}")

pairs=[]
for i in range(0,min(len(addresses),100),20):
    try:
        batch=list(sorted(addresses))[i:i+20]
        pairs+=fetch("/latest/dex/tokens/"+",".join(batch)).get("pairs",[])
    except Exception as e: errors.append(f"batch {i}: {e}")

best={}
for pair in pairs:
    if pair.get("chainId")!="solana": continue
    mint=(pair.get("baseToken") or {}).get("address")
    if not mint: continue
    liq=float((pair.get("liquidity") or {}).get("usd") or 0)
    if mint not in best or liq>float((best[mint].get("liquidity") or {}).get("usd") or 0):
        best[mint]=pair

candidates=[]
for mint,pair in best.items():
    tx=(pair.get("txns") or {}).get("m5") or {}
    buys=int(tx.get("buys") or 0); sells=int(tx.get("sells") or 0)
    liquidity=float((pair.get("liquidity") or {}).get("usd") or 0)
    volume=float((pair.get("volume") or {}).get("m5") or 0)
    score=round(min(volume/max(liquidity,1),3)*10+min(buys,100)*0.3,2)
    candidates.append({
        "mint":mint,"symbol":(pair.get("baseToken") or {}).get("symbol"),
        "pair_url":pair.get("url"),"price_usd":pair.get("priceUsd"),
        "market_cap_usd":pair.get("marketCap"),"liquidity_usd":liquidity,
        "volume_5m_usd":volume,"buy_transactions_5m":buys,
        "sell_transactions_5m":sells,"momentum_heuristic":score,
        "label":"UNDGÅ" if liquidity<5000 else ("TIDLIG KANDIDAT" if buys>=5 and buys>sells else "AFVENT"),
        "confidence":"LOW","verified_unique_buyer_wallets":None,
        "verified_unique_seller_wallets":None,"contract_safety_verified":False
    })
candidates.sort(key=lambda x:x["momentum_heuristic"],reverse=True)
payload={"generated_at_utc":datetime.now(timezone.utc).isoformat(),
 "source":"DEX Screener public API; NOT Fomo official Trending",
 "social_media_used":False,"errors":errors,"top_5":candidates[:5],
 "candidates":candidates[:50],
 "warning":"Transactions are NOT unique wallets. No KØB signals without independent safety verification."}
Path("data").mkdir(parents=True, exist_ok=True)
Path("data/latest.json").write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\\n")
print(f"Saved {len(candidates)} candidates; errors={len(errors)}")
if not addresses: raise SystemExit("No addresses retrieved")
