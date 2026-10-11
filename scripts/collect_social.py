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
handle="joachimtjoernelund"
state["followed_handle"]=handle
followed=set()
state["followed_status"]="unavailable"
if key:
    try:
        req=urllib.request.Request("https://api.fomoapi.io/v2/users/"+handle+"/following",headers={"Authorization":"Bearer "+key,"Accept":"application/json"})
        with urllib.request.urlopen(req,timeout=15) as res:
            payload=json.load(res)
        users=payload if isinstance(payload,list) else next((payload[k] for k in ("following","users","items","data") if isinstance(payload.get(k),list)),[])
        for user in users:
            if isinstance(user,dict):
                for profile in (user,user.get("user"),user.get("profile")):
                    if isinstance(profile,dict):
                        for field in ("id","userId","handle","username"):
                            if profile.get(field) is not None:
                                followed.add(str(profile[field]).lower().lstrip("@"))
        state["followed_status"]="ok" if users else "empty"
    except Exception as e:
        state["followed_status"]="api_error"
        state["followed_error"]=str(e)[:160]
state["followed_count"]=len(followed)
state["friends_feed_verified"]=False
state["friends_feed_status"]="not_verified_personal_feed"
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
        if isinstance(payload,dict) and isinstance(payload.get("data"),dict):
            rows=next((payload["data"][k] for k in ("alerts","items","results") if isinstance(payload["data"].get(k),list)),rows)
        for a in rows:
            if not isinstance(a,dict):continue
            t=dt(a.get("ts") or a.get("timestamp") or a.get("createdAt"))
            mint=a.get("tokenAddress") or a.get("token_address") or (a.get("token") or {}).get("address") if isinstance(a.get("token"),dict) else a.get("tokenAddress") or a.get("token_address")
            side=str(a.get("alertType") or a.get("type") or "").lower()
            side={"swap_buy":"buy","swap_sell":"sell","large_buy":"buy","large_sell":"sell","single_user_sell":"sell","multi_user_buy":"buy","multi_user_sell":"sell","thesis_created":"thesis"}.get(side,side)
            if side=="trade":side=str(a.get("side") or "").lower()
            if not (t and cutoff<=t<=now and mint and side in ("buy","sell","thesis")):continue
            eid=str(a.get("eventId") or a.get("id") or (str(mint)+str(t)+side+str(a.get("userId"))))
            token=a.get("token")
            symbol=token if isinstance(token,str) else (token or {}).get("symbol","")
            events[eid]={"id":eid,"ts":t.isoformat(),"mint":str(mint),"chain":str(a.get("chain") or ""),"symbol":str(symbol or "")[:30],"side":side,"trader":str(a.get("userId") or a.get("trader") or ""),"usd":float(a.get("usdValue") or 0)}
        state["last_fetch_at_utc"]=now.isoformat()
        state["status"]="ok" if rows else "empty_response"
    except Exception as e:
        state["status"]="api_error"
        state["error"]=str(e)[:180]
else:
    state["last_fetch_at_utc"]=old.get("last_fetch_at_utc")
    state["status"]=old.get("status","no_data")
groups={}
for e in events.values():
    is_followed=str(e.get("trader","")).lower().lstrip("@") in followed if followed else False
    k=(e["chain"],e["mint"])
    g=groups.setdefault(k,{"chain":e["chain"],"mint":e["mint"],"symbol":e["symbol"],"mentions":0,"buys":0,"sells":0,"buy_usd":0,"sell_usd":0,"traders":set(),"followed_buys":0,"followed_sells":0,"followed_mentions":0,"friends_unique_traders":set()})
    if e["symbol"]:g["symbol"]=e["symbol"]
    if e["side"]=="thesis":g["mentions"]+=1
    if e["side"]=="buy":g["buys"]+=1;g["buy_usd"]+=e["usd"]
    if e["side"]=="sell":g["sells"]+=1;g["sell_usd"]+=e["usd"]
    if is_followed:
        g["followed_"+{"buy":"buys","sell":"sells","thesis":"mentions"}[e["side"]]]+=1
        if e["trader"]:g["friends_unique_traders"].add(e["trader"])
    if e["trader"]:g["traders"].add(e["trader"])
top=[]
for g in groups.values():
    g["unique_traders"]=len(g.pop("traders"))
    g["friends_unique_traders"]=len(g.pop("friends_unique_traders"))
    g["net_usd"]=round(g["buy_usd"]-g["sell_usd"],2)
    g["buy_usd"]=round(g["buy_usd"],2)
    g["sell_usd"]=round(g["sell_usd"],2)
    top.append(g)
top.sort(key=lambda x:(-x["mentions"],-(x["buys"]+x["sells"]),-x["unique_traders"]))
state["events"]=sorted(events.values(),key=lambda x:x["ts"])[-2500:]
state["top10"]=sorted(top,key=lambda g:(-(g["mentions"]+2*g["followed_mentions"]),-(g["buys"]+g["sells"]+g["followed_buys"]+g["followed_sells"])))[:10]
state["events_count"]=len(events)
state["note"]="Global alerts are not a verified copy of the personal Friends feed. Followed counts only reflect matched trader IDs in retrieved alerts; $1K filter is not guaranteed. Repeated trades count separately; unique traders count once."
PATH.write_text(json.dumps(state,ensure_ascii=False,indent=2)+"\n")
print("Social feed:",state["status"],"events",len(events),"tokens",len(top))
