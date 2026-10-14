"""Close existing DAILY portfolios from collected exact-pair prices; no new market collection."""
import json
from datetime import datetime, time, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

zone=ZoneInfo("Europe/Warsaw")
local=datetime.now(zone)
if (local.hour,local.minute)<(23,30):
    raise SystemExit("Daily close not due")
day=local.date().isoformat()
cutoff=datetime.combine(local.date(),time(23,30),tzinfo=zone).astimezone(timezone.utc)
def parse(t):
    try:return datetime.fromisoformat(str(t).replace("Z","+00:00"))
    except (ValueError,TypeError):return None
def prices(history,mint,pair,start=None):
    out=[]
    for row in history.get(mint,[]):
        if row.get("pair_address")!=pair:continue
        t=parse(row.get("timestamp"))
        try:p=float(row.get("price_usd"))
        except (ValueError,TypeError):continue
        if t and p>0 and (start is None or t>start) and t<=cutoff:out.append((t,p))
    return sorted(out)
data=Path("data")
history=json.loads((data/"history.json").read_text())
vpath=data/"live_v31.json"
v=json.loads(vpath.read_text())
portfolio=v.get("daily_portfolios",{}).get(day)
if portfolio and portfolio.get("status")!="LOCKED":
    selected=parse(portfolio.get("selected_at_utc"))
    for item in portfolio.get("candidates",[]):
        observed=prices(history,item["mint"],item["pair_address"])
        try:entry=float(item["entry_price_usd"])
        except (ValueError,TypeError):entry=0
        item["observed_at_utc"]=cutoff.isoformat()
        if observed and entry>0:
            t,p=observed[-1]
            item.update(last_price_usd=p,last_price_at_utc=t.isoformat(),return_pct=round(100*(p/entry-1),3))
        else:
            for field in ("last_price_usd","last_price_at_utc","return_pct"):item.pop(field,None)
        for hours in (1,4,6):
            target=selected+timedelta(hours=hours) if selected else cutoff+timedelta(days=1)
            exits=[(t,p) for t,p in observed if target<=t<=min(cutoff,target+timedelta(minutes=15))]
            item[f"return_{hours}h_pct"]=round(100*(exits[0][1]/entry-1),3) if exits and entry>0 else None
    portfolio.update(status="LOCKED",locked_at_utc=datetime.now(timezone.utc).isoformat(),evaluation_cutoff_utc=cutoff.isoformat())
    vpath.write_text(json.dumps(v,indent=2,ensure_ascii=False)+"\n")
    print("Locked V3.1",day)
else:print("V3.1 already locked or no portfolio",day)
# Lock BETA LIVE from already-recorded candidates, with no additional observation.
bpath=data/"beta_live_daily.json"
b=json.loads(bpath.read_text())
days=b.setdefault("days",{})
if day not in days:
    first={}
    for row in b.get("records",[]):
        t=parse(row.get("observed_at_utc"))
        if not t or t.astimezone(zone).date().isoformat()!=day or t>cutoff:continue
        key=(row.get("mint"),row.get("pair_address"))
        if not all(key):continue
        if key not in first or t<first[key][0]:first[key]=(t,row)
    results=[];missing=0
    for (mint,pair),(start,row) in first.items():
        observed=prices(history,mint,pair,start)
        try:entry=float(row.get("observation_price_usd"))
        except (ValueError,TypeError):entry=0
        if not observed or entry<=0:missing+=1;continue
        t,p=observed[-1]
        results.append({**row,"last_price_at_utc":t.isoformat(),"last_price_usd":p,"hold_return_pct":round(100*(p/entry-1),4),"price_age_minutes_at_lock":round((cutoff-t).total_seconds()/60,1)})
    results.sort(key=lambda x:x["hold_return_pct"],reverse=True)
    days[day]={"evaluated_day":day,"locked_at_utc":datetime.now(timezone.utc).isoformat(),"evaluation_cutoff_utc":cutoff.isoformat(),"first_entries":len(first),"verified_holds":len(results),"missing_later_prices":missing,"up":sum(x["hold_return_pct"]>0 for x in results),"average_hold_return_pct":round(sum(x["hold_return_pct"] for x in results)/len(results),4) if results else None,"top_10":results[:10]}
    bpath.write_text(json.dumps(b,indent=2,ensure_ascii=False)+"\n")
    print("Locked BETA LIVE",day)
else:print("BETA LIVE already locked",day)
