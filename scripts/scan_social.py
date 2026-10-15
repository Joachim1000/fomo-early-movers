"""Best-effort, read-only public social scan; exact mint matches only."""
import json, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
data=json.loads(Path("data/live_v31.json").read_text())
now=datetime.now(timezone.utc)
def scan(x):
 mint=x.get("mint",""); out={"mint":mint,"symbol":x.get("symbol"),"status":"NOT_VERIFIED","mentions":[],"unavailable_sources":[]}
 if not mint:return out
 urls=[("bluesky","https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts?"+urllib.parse.urlencode({"q":mint,"limit":20,"sort":"latest"})),("reddit","https://www.reddit.com/search.json?"+urllib.parse.urlencode({"q":mint,"sort":"new","limit":20,"t":"day"}))]
 for provider,url in urls:
  try:
   req=urllib.request.Request(url,headers={"User-Agent":"FomoEarlyMovers/1.0 public research"})
   with urllib.request.urlopen(req,timeout=8) as r: payload=json.load(r)
   if provider=="bluesky":
    posts=[(p.get("record",{}).get("text",""),p.get("record",{}).get("createdAt"),"https://bsky.app/profile/"+p.get("author",{}).get("handle","")+"/post/"+p.get("uri","").split("/")[-1]) for p in payload.get("posts",[])]
   else:
    posts=[(p.get("data",{}).get("title","")+" "+p.get("data",{}).get("selftext",""),datetime.fromtimestamp(p.get("data",{}).get("created_utc",0),timezone.utc).isoformat(),"https://www.reddit.com"+p.get("data",{}).get("permalink","")) for p in payload.get("data",{}).get("children",[])]
   for body,ts,link in posts:
    try: at=datetime.fromisoformat(str(ts).replace("Z","+00:00"))
    except (ValueError,TypeError):continue
    if at.tzinfo and now-timedelta(hours=4)<=at<=now+timedelta(minutes=5) and mint.lower() in body.lower():
     out["mentions"].append({"source":provider,"timestamp":at.isoformat(),"url":link,"excerpt":body[:180]})
  except Exception:out["unavailable_sources"].append(provider)
 out["mentions"].sort(key=lambda p:p["timestamp"],reverse=True)
 out["mentions"]=out["mentions"][:8]
 if out["mentions"]:out["status"]="PUBLIC_MENTIONS_FOUND"
 return out
with ThreadPoolExecutor(max_workers=4) as pool: tokens=list(pool.map(scan,(data.get("ranking") or [])[:10]))
Path("data/social_v31.json").write_text(json.dumps({"updated_at_utc":now.isoformat(),"scope":"Exact contract/mint matches on public Bluesky and Reddit, last 4h; not verified endorsements","tokens":tokens},indent=2)+"\n")
print("Social scan:",sum(len(x["mentions"]) for x in tokens),"recent mentions")
