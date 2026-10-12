"""V3.1 shadow ML ranking: chronological out-of-sample logistic regression.
Never calls a heuristic score 'AI'. Requires verified 10m forward outcomes.
"""
import json, math
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

DATA=Path("data")
src=json.loads((DATA/"live_v3.json").read_text())
path=DATA/"live_v31.json"
prior=json.loads(path.read_text()) if path.exists() else {"schema_version":1,"observations":[]}
rows=src.get("observations",[])
now=datetime.fromisoformat(src["generated_at_utc"].replace("Z","+00:00"))
history=json.loads((DATA/"history.json").read_text())
def dt(v):
    try:return datetime.fromisoformat(str(v).replace("Z","+00:00"))
    except (ValueError,TypeError):return None
def num(v):
    try:
        n=float(v)
        return n if math.isfinite(n) else None
    except (ValueError,TypeError):return None
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
current=[]
stamp=src["generated_at_utc"]
for x in src.get("ranking",[]):
    f=features(x)
    if not f:continue
    score=None
    if trained:
        standardized=[max(-5,min(5,(f[j]-means[j])/scales[j])) for j in range(len(FEATURES))]
        logit=max(-30,min(30,weights[0]+sum(a*b for a,b in zip(weights[1:],standardized))))
        score=round(100/(1+math.exp(-logit)),2)
    current.append({"mint":x["mint"],"pair_address":x["pair_address"],"symbol":x.get("symbol"),
                    "observed_at_utc":stamp,"entry_price_usd":x.get("entry_price_usd"),
                    "predicted_10m_gain_probability_pct":score,"baseline_v3_rank":x.get("v3_shadow_rank")})
if trained:current.sort(key=lambda x:x["predicted_10m_gain_probability_pct"],reverse=True)
for rank,x in enumerate(current,1):x["v31_rank"]=rank
# No fabricated ML results: until sufficient labels, publish baseline-only candidates.
seen={(x["mint"],x["pair_address"],x["observed_at_utc"]) for x in prior["observations"]}
for x in current[:10]:
    key=x["mint"],x["pair_address"],x["observed_at_utc"]
    if key not in seen:prior["observations"].append(x)
prior["observations"]=prior["observations"][-10000:]
prior.update({"generated_at_utc":stamp,"model":"L2_LOGISTIC_REGRESSION","trained":trained,
              "status":"TRAINED_SHADOW" if trained else "WAITING_FOR_VERIFIED_10M_TRAINING_DATA",
              "training_examples":len(train),"positive_examples":pos,"negative_examples":neg,
              "target":"verified 10m return >= +3% (before trading costs)",
              "ranking":current[:10]})
path.write_text(json.dumps(prior,indent=2,ensure_ascii=False)+"\n")
print("V3.1",prior["status"],"training examples",len(train))
