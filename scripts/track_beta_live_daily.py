"""Track BETA LIVE TOP 10 independently and freeze daily hold results at 23:30 Warsaw."""
import json
from datetime import datetime, timezone, time
from pathlib import Path
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Europe/Warsaw")
latest = json.loads(Path("data/latest.json").read_text())
history = json.loads(Path("data/history.json").read_text())
stamp = latest["generated_at_utc"]
now = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
if now.tzinfo is None:
    raise ValueError("Snapshot must include timezone")
path = Path("data/beta_live_daily.json")
saved = json.loads(path.read_text()) if path.exists() else {"schema_version": 1, "records": [], "days": {}}
rows = saved.setdefault("records", [])
days = saved.setdefault("days", {})
seen = {(r["mint"], r["pair_address"], r["observed_at_utc"]) for r in rows}
candidates = []
for x in latest.get("eligible_4h_candidates", []):
    mint, pair = x.get("mint"), x.get("pair_address")
    if not mint or not pair or mint == "So11111111111111111111111111111111111111112":
        continue
    current = float(x.get("price_usd") or 0)
    if current <= 0:
        continue
    all_prices = sorted(
        [(datetime.fromisoformat(z["timestamp"].replace("Z", "+00:00")), float(z["price_usd"]))
         for z in history.get(mint, []) if z.get("pair_address") == pair and float(z.get("price_usd") or 0) > 0
         and datetime.fromisoformat(z["timestamp"].replace("Z", "+00:00")) <= now],
        key=lambda p: p[0])
    # LIVE BETA focuses on sustained 6h-7d growth, not 5-minute spikes.
    def window(hours):
        return [(t,p) for t,p in all_prices if (now-t).total_seconds() <= hours*3600]
    def trend(hours):
        eligible = [(t,p) for t,p in all_prices if hours*.8 <= (now-t).total_seconds()/3600 <= hours*1.2]
        if not eligible: return None
        t,p = min(eligible,key=lambda z:abs((now-z[0]).total_seconds()/3600-hours))
        return 100*(current/p-1)
    def clamp(v,lo,hi): return max(lo,min(hi,v))
    w24=window(24)
    w6=window(6)
    w168=window(168)
    p24=[p for _,p in w24]+[current]
    p168=[p for _,p in w168]+[current]
    peak24=max(p24)
    low24=min(p24)
    drawdown24=100*(current/peak24-1)
    range24=100*(peak24/low24-1) if low24 else 0
    peak7=max(p168)
    drawdown7=100*(current/peak7-1)
    t6,t24,t72,t168=(trend(h) for h in (6,24,72,168))
    buys=int(x.get("buy_transactions_5m") or 0)
    sells=int(x.get("sell_transactions_5m") or 0)
    ratio=buys/max(1,sells)
    liquidity=float(x.get("liquidity_usd") or 0)
    volume=float(x.get("volume_5m_usd") or 0)
    # Separate trend persistence from short-lived pumps.
    trend_score=sum(clamp(v,-30,35)*weight for v,weight in ((t6,.25),(t24,.4),(t72,.25),(t168,.1)) if v is not None)
    continuity=sum(1 for v in (t6,t24,t72,t168) if v is not None and v>0)
    liquidity_score=clamp((liquidity-10000)/5000,0,12)
    activity_score=clamp(volume/max(liquidity,1)*30,0,8)
    risk_penalty=clamp(-drawdown24*1.1,0,28)+clamp(max(0,range24-25)*.6,0,20)+clamp(-drawdown7*.25,0,15)
    flow_score=clamp((ratio-1)*7,-12,10)
    spike_option=clamp((t6 or 0)*.15,0,6) if t6 is not None and drawdown24>-15 else 0
    confidence=sum(v is not None for v in (t6,t24,t72,t168))
    age=float(x.get("pair_age_hours") or 0)
    missing_penalty=(4-confidence)*4
    score=(trend_score + continuity*3 + liquidity_score + activity_score + flow_score
           + spike_option - risk_penalty - missing_penalty)
    if age<4: continue
    if liquidity<10000: score-=20
    if ratio<.8: score-=12
    risk="HIGH" if drawdown24<=-20 or range24>=90 or liquidity<10000 else "ELEVATED" if drawdown24<=-10 or range24>=45 or confidence<2 else "LOW RELATIVE"
    candidates.append((score,x,risk,{"trend_6h_pct":t6,"trend_24h_pct":t24,"trend_3d_pct":t72,"trend_7d_pct":t168,"drawdown_24h_pct":round(drawdown24,3),"drawdown_7d_pct":round(drawdown7,3),"range_24h_pct":round(range24,3),"trend_windows_verified":confidence}))
candidates.sort(key=lambda row: row[0], reverse=True)
added = 0
for rank,(score,x,risk,metrics) in enumerate(candidates[:10],1):
    key = (x["mint"],x["pair_address"],stamp)
    if key in seen:
        continue
    rows.append({"mint":x["mint"],"pair_address":x["pair_address"],"symbol":x.get("symbol"),
                 "observed_at_utc":stamp,"observation_price_usd":float(x["price_usd"]),
                 "rank":rank,"beta_score":round(score,3),"risk":risk,**metrics})
    seen.add(key)
    added += 1
# A day is frozen only at/after the Poland-local 23:30 release.
local = now.astimezone(ZONE)
if (local.hour,local.minute) >= (23,30):
    day = local.date().isoformat()
    if day not in days:
        cutoff = datetime.combine(local.date(),time(23,30),tzinfo=ZONE).astimezone(timezone.utc)
        first = {}
        for row in rows:
            t = datetime.fromisoformat(row["observed_at_utc"].replace("Z","+00:00"))
            if t.astimezone(ZONE).date().isoformat()!=day or t>cutoff:
                continue
            key = (row["mint"],row["pair_address"])
            if key not in first or t < first[key][0]:
                first[key]=(t,row)
        results=[]
        missing=0
        for (mint,pair),(entry_time,entry) in first.items():
            later=[]
            for z in history.get(mint,[]):
                if z.get("pair_address")!=pair:
                    continue
                try:
                    t=datetime.fromisoformat(z["timestamp"].replace("Z","+00:00"))
                    p=float(z["price_usd"])
                except (KeyError,ValueError,TypeError):
                    continue
                if entry_time<t<=cutoff and p>0:
                    later.append((t,p))
            if not later:
                missing+=1
                continue
            t,p=max(later)
            results.append({**entry,"last_price_at_utc":t.isoformat(),"last_price_usd":p,
                            "hold_return_pct":round(100*(p/entry["observation_price_usd"]-1),4),
                            "price_age_minutes_at_lock":round((cutoff-t).total_seconds()/60,1)})
        results.sort(key=lambda x:x["hold_return_pct"],reverse=True)
        days[day]={"evaluated_day":day,"locked_at_utc":now.isoformat(),"first_entries":len(first),
                   "verified_holds":len(results),"missing_later_prices":missing,
                   "up":sum(x["hold_return_pct"]>0 for x in results),
                   "average_hold_return_pct":round(sum(x["hold_return_pct"] for x in results)/len(results),4) if results else None,
                   "top_10":results[:10]}
saved["updated_at_utc"]=stamp
path.write_text(json.dumps(saved,indent=2,ensure_ascii=False)+"\n")
print("Beta live observations added:",added,"daily locks:",len(days))
