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
    recent = [(t,p) for t,p in all_prices if (now-t).total_seconds() <= 3600]
    short = [(t,p) for t,p in all_prices if (now-t).total_seconds() <= 900]
    baseline = next((p for t,p in short if (now-t).total_seconds() >= 600), None)
    change15 = 100*(current/baseline-1) if baseline else None
    peak = max([p for _,p in recent]+[current]) if recent else None
    low = min([p for _,p in recent]+[current]) if recent else None
    drawdown = 100*(current/peak-1) if peak else None
    volatility = 100*(peak/low-1) if peak and low else None
    buys = int(x.get("buy_transactions_5m") or 0)
    sells = int(x.get("sell_transactions_5m") or 0)
    ratio = buys/max(sells,1)
    enough = len(recent) >= 3 and baseline is not None
    hard = (drawdown is not None and drawdown <= -12) or (change15 is not None and change15 <= -8) or (volatility is not None and volatility >= 45) or (buys+sells>0 and ratio<0.9)
    soft = (drawdown is not None and drawdown <= -5) or (change15 is not None and change15 < 0) or (volatility is not None and volatility >= 25) or (buys+sells>0 and ratio<1.25)
    risk = "HIGH" if hard else "ELEVATED · LIMITED" if not enough else "ELEVATED" if soft else "LOW RELATIVE"
    penalty = (12 if not enough else 0) + min(25,max(0,-(drawdown or 0))*1.1) + min(20,max(0,-(change15 or 0))*1.5) + min(15,max(0,(volatility or 0)-20)*0.4) + (12 if ratio<1 else 5 if ratio<1.25 else 0) + (8 if float(x.get("liquidity_usd") or 0)<10000 else 0)
    score = float(x.get("momentum_heuristic") or 0)-penalty
    candidates.append((score,x,risk))
candidates.sort(key=lambda row: row[0], reverse=True)
added = 0
for rank,(score,x,risk) in enumerate(candidates[:10],1):
    key = (x["mint"],x["pair_address"],stamp)
    if key in seen:
        continue
    rows.append({"mint":x["mint"],"pair_address":x["pair_address"],"symbol":x.get("symbol"),
                 "observed_at_utc":stamp,"observation_price_usd":float(x["price_usd"]),
                 "rank":rank,"beta_score":round(score,3),"risk":risk})
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
