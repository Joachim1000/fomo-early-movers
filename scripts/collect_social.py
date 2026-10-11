"""Aggregate FOMO feed mentions and buy/sell alerts in a rolling 120-minute window."""
import json, os, urllib.request, urllib.parse
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT=Path("data")
ROOT.mkdir(exist_ok=True)
PATH=ROOT/"social_top10.json"
now=datetime.now(timezone.utc)
cutoff=now-timedelta(minutes=120)
state={"updated_at_utc":now.isoformat(),"window_minutes":120,"source":"FOMO API /v2/alerts","status":"no_data","events":[],"top10":[]}
try:
    old=json.loads(PATH.read_text()) if PATH.exists() else {}
except (ValueError,OSError):
    old={}
events={}
def dt(v):
    try:
        if isinstance(v,(int,float)):
            return datetime.fromtimestamp(v/1000 if v>1e11 else v,timezone.utc)
        return datetime.fromisoformat(str(v).replace("Z","+00:00")).astimezone(timezone.utc)
    except (ValueError,TypeError,OverflowError):
        return None
for e in old.get("events",[]):
    t=dt(e.get("ts"))
    if t and cutoff<=t<=now:
        events[str(e.get("id"))]=e
key=os.environ.get("FOMO_API_KEY","")
last=dt(old.get("last_fetch_at_utc"))
due=not last or (now-last).total_seconds()>=300
if not key:
    state["status"]="missing_api_key"
elif due:
    try:
        params=urllib.parse.urlencode({"limit":200,"since":cutoff.isoformat()})
        req=urllib.request.Request("https://api.fomoapi.io/v2/alerts?"+params,headers={"Authorization":"Bearer "+key,"Accept":"application/json"})
        with urllib.request.urlopen(req,timeout=25) as res:
            payload=json.load(res)
        rows=payload if isinstance(payload,list) else next((payload[k] for k in ("alerts","items","data","results") if isinstance(payload.get(k),list)),[])
        for a in rows:
            if not isinstance(a,dict):continue
            t=dt(a.get("ts") or a.get("timestamp") or a.get("createdAt"))
            mint=a.get("tokenAddress") or a.get("token_address") or (a.get("token") or {}).get("address") if isinstance(a.get("token"),dict) else a.get("tokenAddress") or a.get("token_address")
            side=str(a.get("alertType") or a.get("type") or "").lower()
            if side=="trade":side=str(a.get("side") or "").lower()
            if not (t and cutoff<=t<=now and mint and side in ("buy","sell","thesis")):continue
            eid=str(a.get("eventId") or a.get("id") or (str(mint)+str(t)+side+str(a.get("userId"))))
            token=a.get("token")
            symbol=token if isinstance(token,str) else (token or {}).get("symbol","")
            events[eid]={"id":eid,"ts":t.isoformat(),"mint":str(mint),"chain":str(a.get("chain") or ""),"symbol":str(symbol or "")[:30],"side":side,"trader":str(a.get("userId") or a.get("trader") or ""),"usd":float(a.get("usdValue") or 0)}
        state["last_fetch_at_utc"]=now.isoformat()
        state["status"]="ok"
    except Exception as e:
        state["status"]="api_error"
        state["error"]=str(e)[:180]
else:
    state["last_fetch_at_utc"]=old.get("last_fetch_at_utc")
    state["status"]=old.get("status","no_data")
groups={}
for e in events.values():
    k=(e["chain"],e["mint"])
    g=groups.setdefault(k,{"chain":e["chain"],"mint":e["mint"],"symbol":e["symbol"],"mentions":0,"buys":0,"sells":0,"buy_usd":0,"sell_usd":0,"traders":set()})
    if e["symbol"]:g["symbol"]=e["symbol"]
    if e["side"]=="thesis":g["mentions"]+=1
    if e["side"]=="buy":g["buys"]+=1;g["buy_usd"]+=e["usd"]
    if e["side"]=="sell":g["sells"]+=1;g["sell_usd"]+=e["usd"]
    if e["trader"]:g["traders"].add(e["trader"])
top=[]
for g in groups.values():
    g["unique_traders"]=len(g.pop("traders"))
    g["net_usd"]=round(g["buy_usd"]-g["sell_usd"],2)
    g["buy_usd"]=round(g["buy_usd"],2)
    g["sell_usd"]=round(g["sell_usd"],2)
    top.append(g)
top.sort(key=lambda x:(-x["mentions"],-(x["buys"]+x["sells"]),-x["unique_traders"]))
state["events"]=sorted(events.values(),key=lambda x:x["ts"])[-2500:]
state["top10"]=top[:10]
state["events_count"]=len(events)
state["note"]="Mentions count thesis feed events, not full FOMO chat. Buy/sell counts are feed alerts, not all on-chain trades."
PATH.write_text(json.dumps(state,ensure_ascii=False,indent=2)+"\n")
print("Social feed:",state["status"],"events",len(events),"tokens",len(top))
