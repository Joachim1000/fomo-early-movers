"""Isolated simulated trading. Reads Spike Radar market-data, writes only data/paper_trades.json on main."""
import json, urllib.request
from pathlib import Path
from datetime import datetime, timezone, timedelta

BASE="https://raw.githubusercontent.com/Joachim1000/fomo-early-movers/market-data/data/"
LEDGER=Path("data/paper_trades.json")
STAKE=25
def get(name):
    with urllib.request.urlopen(urllib.request.Request(BASE+name,headers={"User-Agent":"momentum-x-paper-simulator"}),timeout=25) as response:
        return json.load(response)
def when(s):
    try:return datetime.fromisoformat(str(s).replace("Z","+00:00")).astimezone(timezone.utc)
    except (ValueError,TypeError):return None
def positive(v):
    try:
        n=float(v)
        return n if 0<n<float("inf") else None
    except (TypeError,ValueError):return None
now=datetime.now(timezone.utc)
radar=get("spike_radar_history.json")
history=get("history.json")
latest=get("latest.json")
snapshot=when(latest.get("generated_at_utc"))
if not snapshot or abs((now-snapshot).total_seconds())>900:
    raise RuntimeError("Market snapshot is stale; no trades opened or closed")
ledger=json.loads(LEDGER.read_text()) if LEDGER.exists() else {"schema_version":1,"strategy":{"source":"SPIKE RADAR","stake_usd":STAKE,"stop_loss_pct":5,"exit_minutes":10},"trades":[],"initialized_at_utc":now.isoformat()}
trades=ledger["trades"]
seen={x["id"] for x in trades}
# Do not backfill historical signals on first deployment.
initialized=when(ledger["initialized_at_utc"])
for r in sorted(radar.get("records",[]),key=lambda x:x.get("first_alert_at_utc") or ""):
    mint,pair=r.get("mint"),r.get("pair_address")
    start=when(r.get("first_alert_at_utc"));entry=positive(r.get("alert_price_usd"))
    if not(mint and pair and start and entry) or start<initialized or start>now:continue
    uid=mint+"|"+pair+"|"+start.isoformat()
    if uid in seen:continue
    trades.append({"id":uid,"mint":mint,"pair_address":pair,"symbol":r.get("symbol") or mint[:8],"entry_at_utc":start.isoformat(),"entry_price_usd":entry,"stake_usd":STAKE,"status":"OPEN","stop_loss_pct":5,"exit_minutes":10,"source":"SPIKE RADAR"})
    seen.add(uid)
for t in trades:
    if t["status"]=="CLOSED":continue
    start=when(t["entry_at_utc"]);deadline=start+timedelta(minutes=10);entry=t["entry_price_usd"]
    points=sorted(((when(p.get("timestamp")),positive(p.get("price_usd"))) for p in history.get(t["mint"],[]) if p.get("pair_address")==t["pair_address"]),key=lambda x:x[0] or datetime.min.replace(tzinfo=timezone.utc))
    for at,price in points:
        if not(at and price and start<=at<=now):continue
        reason="STOP_LOSS" if price<=entry*0.95 else ("TIME_EXIT" if at>=deadline else None)
        if reason:
            t.update(status="CLOSED",exit_reason=reason,exit_at_utc=at.isoformat(),exit_price_usd=price,return_pct=round(100*(price/entry-1),4),pnl_usd=round(STAKE*(price/entry-1),4))
            break
    if t["status"]!="CLOSED" and now>=deadline:t["status"]="AWAITING_PRICE"
ledger["updated_at_utc"]=now.isoformat()
LEDGER.parent.mkdir(parents=True,exist_ok=True)
LEDGER.write_text(json.dumps(ledger,ensure_ascii=False,indent=2)+"\n")
print("Paper trades:",len(trades),"closed:",sum(t["status"]=="CLOSED" for t in trades))
