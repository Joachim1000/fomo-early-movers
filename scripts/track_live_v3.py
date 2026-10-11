"""LIVE V3 research dataset and shadow ranking. No untrained AI probabilities."""
import json, math
from datetime import datetime, timezone
from pathlib import Path

DATA=Path("data")
latest=json.loads((DATA/"latest.json").read_text())
history=json.loads((DATA/"history.json").read_text())
path=DATA/"live_v3.json"
saved=json.loads(path.read_text()) if path.exists() else {"schema_version":1,"observations":[]}
rows=saved.setdefault("observations",[])
now=datetime.fromisoformat(latest["generated_at_utc"].replace("Z","+00:00"))
if now.tzinfo is None: raise ValueError("Timestamp must be timezone-aware")
def num(v):
    try:
        x=float(v)
        return x if math.isfinite(x) else None
    except (ValueError,TypeError): return None
def when(v):
    try: return datetime.fromisoformat(v.replace("Z","+00:00"))
    except (AttributeError,ValueError): return None
def clamp(x,a,b): return max(a,min(b,x))
seen={(r["mint"],r["pair_address"],r["observed_at_utc"]) for r in rows}
current=[]
for x in latest.get("eligible_4h_candidates",[]):
    mint,pair=x.get("mint"),x.get("pair_address")
    price=num(x.get("price_usd"))
    if not mint or not pair or not price or price<=0: continue
    series=sorted(((when(p.get("timestamp")),num(p.get("price_usd"))) for p in history.get(mint,[]) if p.get("pair_address")==pair),key=lambda z:z[0] or now)
    series=[(t,p) for t,p in series if t and p and p>0 and t<=now]
    def lag(minutes):
        eligible=[(t,p) for t,p in series if minutes-3 <= (now-t).total_seconds()/60 <= minutes+3]
        return min(eligible,key=lambda z:abs((now-z[0]).total_seconds()/60-minutes))[1] if eligible else None
    p5,p10,p15=lag(5),lag(10),lag(15)
    r5=100*(price/p5-1) if p5 else None
    r10=100*(price/p10-1) if p10 else None
    r15=100*(price/p15-1) if p15 else None
    acceleration=r5-(100*(p5/p10-1)) if p5 and p10 else None
    buys=int(x.get("buy_transactions_5m") or 0)
    sells=int(x.get("sell_transactions_5m") or 0)
    ratio=buys/max(1,sells)
    liquidity=num(x.get("liquidity_usd")) or 0
    volume=num(x.get("volume_5m_usd")) or 0
    # Shadow heuristic only: not ML, no estimated win probabilities.
    score=(clamp((acceleration or 0)*3,-20,20)
           +clamp((r5 or 0)*2,-20,20)
           +clamp((ratio-1)*10,-15,20)
           +clamp(math.log1p(volume)/math.log(10)-2,0,10)
           +clamp(math.log1p(liquidity)/math.log(10)-3,0,10)
           -clamp(max(0,(r15 or 0)-20),0,20)
           -clamp(max(0,-(r15 or 0))*2,0,30))
    item={"mint":mint,"pair_address":pair,"symbol":x.get("symbol"),
          "observed_at_utc":now.isoformat(),"entry_price_usd":price,
          "momentum_heuristic":num(x.get("momentum_heuristic")),
          "change_5m_pct":round(r5,3) if r5 is not None else None,
          "change_10m_pct":round(r10,3) if r10 is not None else None,
          "change_15m_pct":round(r15,3) if r15 is not None else None,
          "acceleration_pct_points":round(acceleration,3) if acceleration is not None else None,
          "buys_5m":buys,"sells_5m":sells,"buy_sell_ratio":round(ratio,3),
          "liquidity_usd":liquidity,"volume_5m_usd":volume,
          "v3_shadow_score":round(score,3),
          "return_15m_pct":None,"return_30m_pct":None,"return_60m_pct":None}
    current.append(item)
    key=(mint,pair,now.isoformat())
    if key not in seen:
        rows.append(item.copy());seen.add(key)
current.sort(key=lambda r:r["v3_shadow_score"],reverse=True)
# Negative 5m momentum cannot enter the top three while >=3 non-negative
# 5m candidates are available. Keep declining tokens visible further down.
positive=[r for r in current if r["change_5m_pct"] is not None and r["change_5m_pct"]>=0]
if len(positive)>=3:
    top=positive[:3]
    top_keys={(r["mint"],r["pair_address"]) for r in top}
    current=top+[r for r in current if (r["mint"],r["pair_address"]) not in top_keys]
for rank,x in enumerate(current,1): x["v3_shadow_rank"]=rank
# Only fill outcome when a genuine later same-pair observation falls within a
# narrow target window. Never use future data in features.
for row in rows:
    start=when(row["observed_at_utc"])
    if not start: continue
    for minutes in (15,30,60):
        field=f"return_{minutes}m_pct"
        if row.get(field) is not None: continue
        later=[]
        for p in history.get(row["mint"],[]):
            if p.get("pair_address")!=row["pair_address"]:continue
            t,price=when(p.get("timestamp")),num(p.get("price_usd"))
            if t and price and price>0 and minutes*60 <= (t-start).total_seconds() <= (minutes+5)*60:
                later.append((t,price))
        if later:
            t,price=min(later)
            row[field]=round(100*(price/row["entry_price_usd"]-1),4)
            row[f"observed_{minutes}m_at_utc"]=t.isoformat()
# Bounded dataset to keep market-data manageable; preserve last 14 days.
cutoff=now.timestamp()-14*86400
saved["observations"]=[r for r in rows if when(r["observed_at_utc"]) and when(r["observed_at_utc"]).timestamp()>=cutoff]
saved["generated_at_utc"]=now.isoformat()
saved["status"]="SHADOW_HEURISTIC_NOT_TRAINED_AI"
saved["model_trained"]=False
saved["ranking"]=current[:10]
saved["verified_30m_outcomes"]=sum(r.get("return_30m_pct") is not None for r in saved["observations"])
path.write_text(json.dumps(saved,ensure_ascii=False,indent=2)+"\n")
print("LIVE V3 shadow candidates:",len(current),"30m outcomes:",saved["verified_30m_outcomes"])
