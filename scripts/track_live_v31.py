"""Independent LIVE V3.1 model sourced directly from latest market candidates and same-pair history.
Chronological logistic regression, with explicit heuristic fallback until enough verified labels exist.
"""
import json, math
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

DATA=Path("data")
latest=json.loads((DATA/"latest.json").read_text())
path=DATA/"live_v31.json"
prior=json.loads(path.read_text()) if path.exists() else {"schema_version":1,"observations":[]}
rows=prior.get("observations",[])
now=datetime.fromisoformat(latest["generated_at_utc"].replace("Z","+00:00"))
history=json.loads((DATA/"history.json").read_text())
def dt(v):
    try:return datetime.fromisoformat(str(v).replace("Z","+00:00"))
    except (ValueError,TypeError):return None
def num(v):
    try:
        n=float(v)
        return n if math.isfinite(n) else None
    except (ValueError,TypeError):return None
def when(v):return dt(v)
def clamp(x,a,b):return max(a,min(b,x))
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

FEATURES=["change_5m_pct","change_10m_pct","change_15m_pct","acceleration_pct_points","buy_sell_ratio","liquidity_usd","volume_5m_usd"]
def features(x):
    v=[num(x.get(k)) for k in FEATURES]
    if any(z is None for z in v):return None
    v[4]=min(v[4],20)
    v[5]=math.log1p(max(0,v[5]))
    v[6]=math.log1p(max(0,v[6]))
    return [max(-100,min(100,z)) for z in v]
# Use first observation per mint/pair per hour to limit highly correlated duplicates.
examples={}
for x in rows:
    t=dt(x.get("observed_at_utc"))
    f=features(x)
    if not t or f is None or not x.get("mint") or not x.get("pair_address"):continue
    key=(x["mint"],x["pair_address"],int(t.timestamp()//3600))
    if key in examples:continue
    entry=num(x.get("entry_price_usd"))
    if not entry or entry<=0:continue
    exits=[]
    for p in history.get(x["mint"],[]):
        pt=dt(p.get("timestamp"));price=num(p.get("price_usd"))
        if p.get("pair_address")==x["pair_address"] and pt and price and price>0 and 600<=(pt-t).total_seconds()<=900:
            exits.append((pt,price))
    if not exits:continue
    exit_price=min(exits)[1]
    examples[key]=(t,f,1 if exit_price/entry>=1.03 else 0)
ordered=sorted(examples.values(),key=lambda z:z[0])
# Train only on historical outcomes whose 10m exit precedes the live prediction.
train=[z for z in ordered if z[0].timestamp()+900<=now.timestamp()-3600]
pos=sum(z[2] for z in train)
neg=len(train)-pos
trained=len(train)>=100 and pos>=20 and neg>=20
weights=None
if trained:
    # Standardization fitted on training observations only; L2-regularized logistic regression.
    means=[sum(z[1][j] for z in train)/len(train) for j in range(len(FEATURES))]
    scales=[max(1e-6,(sum((z[1][j]-means[j])**2 for z in train)/len(train))**.5) for j in range(len(FEATURES))]
    samples=[([max(-5,min(5,(f[j]-means[j])/scales[j])) for j in range(len(FEATURES))],y) for _,f,y in train]
    weights=[0.0]*(len(FEATURES)+1)
    for epoch in range(180):
        grad=[0.0]*len(weights)
        for f,y in samples:
            score=max(-30,min(30,weights[0]+sum(a*b for a,b in zip(weights[1:],f))))
            prob=1/(1+math.exp(-score))
            err=prob-y
            grad[0]+=err
            for j,z in enumerate(f):grad[j+1]+=err*z
        rate=0.15
        for j in range(len(weights)):
            weights[j]-=rate*(grad[j]/len(samples)+(0.03*weights[j] if j else 0))
ranking=[]
stamp=latest["generated_at_utc"]
for x in current:
    f=features(x)
    if not f:continue
    score=None
    if trained:
        standardized=[max(-5,min(5,(f[j]-means[j])/scales[j])) for j in range(len(FEATURES))]
        logit=max(-30,min(30,weights[0]+sum(a*b for a,b in zip(weights[1:],standardized))))
        score=round(100/(1+math.exp(-logit)),2)
    ranking.append({"mint":x["mint"],"pair_address":x["pair_address"],"symbol":x.get("symbol"),
                    "observed_at_utc":stamp,"entry_price_usd":x.get("entry_price_usd"),
                    "predicted_10m_gain_probability_pct":score,"candidate_source":"DIRECT_MARKET_SNAPSHOT","v31_heuristic_score":x.get("v3_shadow_score"),"change_15m_pct":x.get("change_15m_pct"),"buy_sell_ratio":x.get("buy_sell_ratio"),"buys_5m":x.get("buys_5m"),"sells_5m":x.get("sells_5m")})
if trained:ranking.sort(key=lambda x:x["predicted_10m_gain_probability_pct"],reverse=True)
else:ranking.sort(key=lambda x:x["v31_heuristic_score"],reverse=True)
for rank,x in enumerate(ranking,1):
    x["v31_rank"]=rank
    x["v3_top10_rank"]=rank
# No fabricated ML results: until sufficient labels, publish baseline-only candidates.
seen={(x["mint"],x["pair_address"],x["observed_at_utc"]) for x in prior["observations"]}
# Persist full V3.1 feature observations for future training; not dependent on retired V3.
existing={(x.get("mint"),x.get("pair_address"),x.get("observed_at_utc")) for x in prior["observations"]}
for x in current:
    key=(x["mint"],x["pair_address"],stamp)
    if key not in existing:
        prior["observations"].append(x.copy());existing.add(key)
for x in ranking[:10]:
    key=x["mint"],x["pair_address"],x["observed_at_utc"]
    if key not in seen:prior["observations"].append(x)
prior["observations"]=prior["observations"][-10000:]
prior.update({"generated_at_utc":stamp,"model":"L2_LOGISTIC_REGRESSION","trained":trained,
              "status":"TRAINED_SHADOW" if trained else "INDEPENDENT_HEURISTIC_WAITING_FOR_VERIFIED_10M_TRAINING_DATA",
              "training_examples":len(train),"positive_examples":pos,"negative_examples":neg,
              "target":"verified 10m return >= +3% (before trading costs)",
              "ranking":ranking[:10]})
path.write_text(json.dumps(prior,indent=2,ensure_ascii=False)+"\n")
print("V3.1",prior["status"],"training examples",len(train))
