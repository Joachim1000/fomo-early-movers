"""Independent LIVE V3.1 model sourced directly from latest market candidates and same-pair history.
Chronological logistic regression, with explicit heuristic fallback until enough verified labels exist.
"""
import json, math
from pathlib import Path
from datetime import datetime, timezone, timedelta, time
from zoneinfo import ZoneInfo
from collections import defaultdict

DATA=Path("data")
latest=json.loads((DATA/"latest.json").read_text())
path=DATA/"live_v31.json"
try:
    prior=json.loads(path.read_text()) if path.exists() and path.stat().st_size else {"schema_version":1,"observations":[]}
except (ValueError, OSError):
    prior={"schema_version":1,"observations":[]}
rows=prior.get("observations",[]) if isinstance(prior.get("observations"),list) else []
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
    # Beta risk gate: keep all observations for training but only rank qualified entries.
    # Rank viable candidates even when one historical price point is missing.
    # Missing features remain None in observations and never enter ML training.
    eligible_for_ranking = (
        (r5 is None or -5 <= r5 <= 15)
        and (r15 is None or -8 <= r15 <= 25)
        and (acceleration is None or acceleration >= -2)
        and buys >= 8 and ratio >= 1.3
        and liquidity >= 10000 and volume >= 1000
    )
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
          "v31_heuristic_score":round(score,3),
          "eligible_for_ranking":eligible_for_ranking,
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
    if not x.get('eligible_for_ranking'):continue
    f=features(x)
    if trained and not f:continue
    score=None
    if trained:
        standardized=[max(-5,min(5,(f[j]-means[j])/scales[j])) for j in range(len(FEATURES))]
        logit=max(-30,min(30,weights[0]+sum(a*b for a,b in zip(weights[1:],standardized))))
        score=round(100/(1+math.exp(-logit)),2)
    ranking.append({"mint":x["mint"],"pair_address":x["pair_address"],"symbol":x.get("symbol"),
                    "observed_at_utc":stamp,"entry_price_usd":x.get("entry_price_usd"),
                    "predicted_10m_gain_probability_pct":score,"candidate_source":"DIRECT_MARKET_SNAPSHOT","v31_heuristic_score":x.get("v31_heuristic_score"),"change_15m_pct":x.get("change_15m_pct"),"buy_sell_ratio":x.get("buy_sell_ratio"),"buys_5m":x.get("buys_5m"),"sells_5m":x.get("sells_5m")})
# Fill remaining display slots with independently scored, lower-confidence candidates.
# Keep the strict qualification flag and mark these explicitly as WATCHLIST,
# never as model-qualified signals or verified predictions.
if len(ranking)<10:
    already={(x["mint"],x["pair_address"]) for x in ranking}
    extras=sorted((x for x in current if (x["mint"],x["pair_address"]) not in already
                   and x["liquidity_usd"]>=5000 and x["volume_5m_usd"]>=500
                   and x["buys_5m"]>=3 and x["buy_sell_ratio"]>=1),
                  key=lambda x:x["v31_heuristic_score"],reverse=True)
    for x in extras[:10-len(ranking)]:
        ranking.append({"mint":x["mint"],"pair_address":x["pair_address"],"symbol":x.get("symbol"),
                        "observed_at_utc":stamp,"entry_price_usd":x.get("entry_price_usd"),
                        "predicted_10m_gain_probability_pct":None,
                        "candidate_source":"V31_PROVISIONAL_WATCHLIST",
                        "v31_heuristic_score":x.get("v31_heuristic_score"),
                        "change_15m_pct":x.get("change_15m_pct"),
                        "buy_sell_ratio":x.get("buy_sell_ratio"),
                        "buys_5m":x.get("buys_5m"),"sells_5m":x.get("sells_5m")})
ranking.sort(key=lambda x:(x["candidate_source"]=="V31_PROVISIONAL_WATCHLIST", -(x["predicted_10m_gain_probability_pct"] if trained and x["predicted_10m_gain_probability_pct"] is not None else x["v31_heuristic_score"])))
# Freeze the risk at the actual daily selection observation; do not recalculate it from rolling history.
def first_call_risk(obs):
    change=num(obs.get("change_15m_pct"))
    ratio=num(obs.get("buy_sell_ratio"))
    if change is None and ratio is None:return "UNKNOWN"
    if (change is not None and change<=-8) or (ratio is not None and ratio<0.9):return "HIGH"
    if (change is not None and change<0) or (ratio is not None and ratio<1.25):return "ELEVATED"
    return "LOW RELATIVE"

# One fixed daily selection at/after 08:00 Warsaw, using the prior day's
# independently observed candidate history. Never rerank today's portfolio.
zone=ZoneInfo("Europe/Warsaw")
local=now.astimezone(zone)
day=local.date().isoformat()
# Persistent first-seen registry survives observation retention and daily portfolio rotation.
first_calls=prior.setdefault("first_calls",{})
if not isinstance(first_calls,dict):
    first_calls={}
    prior["first_calls"]=first_calls
for obs in rows+current:
    mint,pair=obs.get("mint"),obs.get("pair_address")
    t=dt(obs.get("observed_at_utc"))
    if not mint or not pair or not t:continue
    key=mint+"|"+pair
    if key not in first_calls:
        first_calls[key]={"first_seen_at_utc":t.isoformat(),"first_call_risk":first_call_risk(obs)}
# Do not replace previously saved first-seen records on subsequent runs.
daily=prior.setdefault("daily_portfolios",{})
if not isinstance(daily,dict):
    daily={}
    prior["daily_portfolios"]=daily
if day not in daily and local.hour>=8:
    start=now-timedelta(hours=24)
    candidates={}
    for obs in rows+current:
        t=dt(obs.get("observed_at_utc"))
        mint,pair=obs.get("mint"),obs.get("pair_address")
        if not t or not mint or not pair or not start<=t<=now:continue
        if not (num(obs.get("entry_price_usd")) or 0)>0:continue
        if (num(obs.get("liquidity_usd")) or 0)<5000 or (num(obs.get("volume_5m_usd")) or 0)<500:continue
        if (num(obs.get("buys_5m")) or 0)<3 or (num(obs.get("buy_sell_ratio")) or 0)<1:continue
        key=(mint,pair)
        score=num(obs.get("v31_heuristic_score")) or 0
        # Prefer strong observations; break ties by most recent verified observation.
        if key not in candidates or (score,t)>(candidates[key][0],candidates[key][1]):
            candidates[key]=(score,t,obs)
    picks=sorted(candidates.values(),key=lambda z:(z[0],z[1]),reverse=True)[:10]
    def original_call(x):
        key=x["mint"]+"|"+x["pair_address"]
        return first_calls.get(key,{"first_seen_at_utc":x["observed_at_utc"],"first_call_risk":first_call_risk(x)})

    daily[day]={"selected_at_utc":now.isoformat(),"selection_target_local":"08:00",
                "status":"ACTIVE","selection_basis":"previous_24h_observations",
                "candidates":[{"mint":x["mint"],"pair_address":x["pair_address"],
                  "symbol":x.get("symbol"),"entry_price_usd":x["entry_price_usd"],
                  "selected_observation_at_utc":t.isoformat(),
                  "first_seen_at_utc":original_call(x)["first_seen_at_utc"],
                  "v31_heuristic_score":score,"v31_rank":i,
                  "buy_sell_ratio":x.get("buy_sell_ratio"),"buys_5m":x.get("buys_5m"),
                  "sells_5m":x.get("sells_5m"),"change_15m_pct":x.get("change_15m_pct"),
                  "candidate_source":"DAILY_FIXED_SELECTION","first_call_risk":original_call(x)["first_call_risk"]} for i,(score,t,x) in enumerate(picks,1)]}
# Backfill older daily picks once using their fixed selection snapshot fields.
# Existing saved values are immutable and never overwritten.
for portfolio in daily.values():
    for item in portfolio.get("candidates",[]):
        key=item.get("mint","")+"|"+item.get("pair_address","")
        record=first_calls.get(key)
        if "first_call_risk" not in item:
            item["first_call_risk"]=record["first_call_risk"] if record else "UNKNOWN"
        if "first_seen_at_utc" not in item and record:
            item["first_seen_at_utc"]=record["first_seen_at_utc"]

# Daily portfolios freeze after 23:30 Warsaw, with the latest exact-pair observed price.
for date,portfolio in daily.items():
    if portfolio.get("status")=="LOCKED":continue
    cutoff_local=datetime.combine(datetime.fromisoformat(date).date(),time(23,30),tzinfo=zone)
    cutoff_utc=cutoff_local.astimezone(timezone.utc)
    locking=now>=cutoff_utc
    if locking:
        portfolio["status"]="LOCKED"
        portfolio["locked_at_utc"]=now.isoformat()
        portfolio["evaluation_cutoff_utc"]=cutoff_utc.isoformat()
    for item in portfolio.get("candidates",[]):
        # Published DAILY profit is final only at the 23:30 Warsaw cutoff.
        # Previously LOCKED portfolios are skipped above and never recalculated.
        if not locking:
            item.pop("last_price_usd",None)
            item.pop("last_price_at_utc",None)
            item.pop("return_pct",None)
            continue
        item["observed_at_utc"]=cutoff_utc.isoformat()
        prices=[(dt(p.get("timestamp")),num(p.get("price_usd")))
                for p in history.get(item["mint"],[])
                if p.get("pair_address")==item["pair_address"]]
        prices=[(t,p) for t,p in prices if t and p and p>0 and t<=cutoff_utc]
        if prices:
            t,p=max(prices)
            item["last_price_usd"]=p
            item["last_price_at_utc"]=t.isoformat()
            entry=num(item.get("entry_price_usd"))
            item["return_pct"]=round((p/entry-1)*100,3) if entry else None
            # Fixed holding-period returns measured from the immutable daily entry.
            # Use the first verified same-pair observation at/after each target,
            # with a 15-minute tolerance. Never use data beyond 23:30.
            selected=dt(portfolio.get("selected_at_utc"))
            if selected and entry:
                for hours in (1,4,6):
                    target=selected+timedelta(hours=hours)
                    field=f"return_{hours}h_pct"
                    if target>cutoff_utc:
                        item[field]=None
                        continue
                    exits=[(t0,p0) for t0,p0 in prices if target<=t0<=min(target+timedelta(minutes=15),cutoff_utc)]
                    item[field]=round(100*(min(exits)[1]/entry-1),3) if exits else None
        else:
            item.pop("last_price_usd",None)
            item.pop("last_price_at_utc",None)
            item.pop("return_pct",None)
# Retain portfolios for seven days.
daily={k:v for k,v in daily.items() if k>=(local.date()-timedelta(days=7)).isoformat()}
prior["daily_portfolios"]=daily
# LIVE is the fixed 08:00 Warsaw selection for this calendar day.
# Daily reporting uses the most recent LOCKED portfolio, never today's active one.
ranking = [dict(x) for x in daily.get(day, {}).get("candidates", [])] if (8, 0) <= (local.hour, local.minute) < (23, 30) else []
for rank,x in enumerate(ranking,1):
    x["v31_rank"]=rank
# No fabricated ML results: until sufficient labels, publish baseline-only candidates.
# Store one observation per mint/pair/timestamp. Only TOP 10 entries carry
# v31_rank; all other candidates remain eligible for independent ML training.
existing={}
for x in rows:
    key=(x.get("mint"),x.get("pair_address"),x.get("observed_at_utc"))
    if all(key) and key not in existing:
        existing[key]=x
for x in current:
    key=(x["mint"],x["pair_address"],stamp)
    if key not in existing:
        existing[key]=x.copy()
for x in ranking[:10]:
    key=(x["mint"],x["pair_address"],stamp)
    row=existing.get(key)
    if row is not None:
        row["v31_rank"]=x["v31_rank"]
        row["predicted_10m_gain_probability_pct"]=x.get("predicted_10m_gain_probability_pct")
# Preserve the seven-day window, rather than an arbitrary 10,000-row cap.
# Only keep recent full feature observations; exact rank and entry remain stored.
cutoff=now-timedelta(days=7)
# Keep one representative observation per mint/pair/hour. This matches the
# independent training sample granularity and prevents multi-megabyte
# snapshots from overwhelming the browser. Preserve the earliest observation
# in each hour so historical first-call features remain stable.
sampled={}
for x in existing.values():
    t=dt(x.get("observed_at_utc"))
    if not t or t<cutoff:continue
    key=(x.get("mint"),x.get("pair_address"),int(t.timestamp()//3600))
    if not all(key[:2]):continue
    if key not in sampled or t<dt(sampled[key].get("observed_at_utc")):
        sampled[key]=x
prior["observations"]=sorted(sampled.values(),key=lambda x:x.get("observed_at_utc") or "")
prior.update({"generated_at_utc":stamp,"model":"L2_LOGISTIC_REGRESSION","trained":trained,
              "status":"TRAINED_INDEPENDENT" if trained else "INDEPENDENT_HEURISTIC_WAITING_FOR_VERIFIED_10M_TRAINING_DATA",
              "training_examples":len(train),"positive_examples":pos,"negative_examples":neg,
              "target":"verified 10m return >= +3% (before trading costs)",
              "ranking":ranking[:10]})
path.write_text(json.dumps(prior,indent=2,ensure_ascii=False)+"\n")
print("V3.1",prior["status"],"training examples",len(train))
