"""Append-only, simulated Spike Radar trades. No real orders or wallet access."""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

STAKE=25.0
STOP_PCT=-5.0
EXIT_MINUTES=10
path=Path("data/paper_trades.json")
ledger=json.loads(path.read_text()) if path.exists() else {"schema_version":1,"strategy":{"source":"SPIKE_RADAR","stake_usd":STAKE,"stop_loss_pct":STOP_PCT,"time_exit_minutes":EXIT_MINUTES},"trades":[]}
trades=ledger["trades"]
radar=json.loads(Path("data/spike_radar_history.json").read_text())
history=json.loads(Path("data/history.json").read_text())
latest=json.loads(Path("data/latest.json").read_text())
def dt(s):
    try: return datetime.fromisoformat(str(s).replace("Z","+00:00")).astimezone(timezone.utc)
    except (ValueError,TypeError): return None
def price(v):
    try:
        f=float(v)
        return f if 0<f<float("inf") else None
    except (ValueError,TypeError): return None
now=dt(latest.get("generated_at_utc"))
if not now: raise ValueError("No valid latest timestamp")
if abs((datetime.now(timezone.utc)-now).total_seconds())>7200: raise ValueError("Stale market snapshot; refusing paper trade updates")
# Initialize only from fresh signals; never backfill historical winners.
existing={(x["mint"],x["pair_address"],x["signal_at_utc"]) for x in trades}
for r in sorted(radar.get("records",[]),key=lambda x:x.get("first_alert_at_utc") or ""):
    start=dt(r.get("first_alert_at_utc")); entry=price(r.get("alert_price_usd"))
    mint,pair=r.get("mint"),r.get("pair_address")
    if not(start and mint and pair and entry) or not timedelta(0)<=now-start<=timedelta(minutes=3): continue
    key=(mint,pair,r["first_alert_at_utc"])
    if key in existing: continue
    trades.append({"id":mint+"|"+pair+"|"+r["first_alert_at_utc"],"mint":mint,"pair_address":pair,"symbol":r.get("symbol") or mint[:8],"signal_at_utc":r["first_alert_at_utc"],"entry_at_utc":r["first_alert_at_utc"],"entry_price_usd":entry,"stake_usd":STAKE,"status":"OPEN","exit_minutes":EXIT_MINUTES,"stop_loss_pct":STOP_PCT,"source":"SPIKE_RADAR"})
    existing.add(key)
for trade in trades:
    if trade.get("status")!="OPEN": continue
    start=dt(trade["entry_at_utc"]); entry=price(trade["entry_price_usd"])
    if not start or not entry: continue
    observations=sorted(((dt(p.get("timestamp")),price(p.get("price_usd"))) for p in history.get(trade["mint"],[]) if p.get("pair_address")==trade["pair_address"]),key=lambda z:z[0] or datetime.min.replace(tzinfo=timezone.utc))
    deadline=start+timedelta(minutes=int(trade.get("exit_minutes",EXIT_MINUTES)))
    # First observed price after signal is the first possible execution; no imaginary intrabar fills.
    for at,p in observations:
        if not at or not p or at<start or at>now: continue
        reason="STOP_LOSS" if p<=entry*(1+float(trade.get("stop_loss_pct",STOP_PCT))/100) else "TIME_EXIT" if at>=deadline else None
        if reason:
            trade.update(status="CLOSED",exit_reason=reason,exit_at_utc=at.isoformat(),exit_price_usd=p,return_pct=round((p/entry-1)*100,4),pnl_usd=round(STAKE*(p/entry-1),4),exit_delay_minutes=round((at-start).total_seconds()/60,2))
            break
    if trade["status"]=="OPEN" and now>=deadline:
        trade["status"]="AWAITING_PRICE"
# Awaiting positions can close when later exact-pair observations arrive.
for trade in trades:
    if trade.get("status")!="AWAITING_PRICE":continue
    start=dt(trade["entry_at_utc"]);entry=price(trade["entry_price_usd"])
    points=sorted(((dt(p.get("timestamp")),price(p.get("price_usd"))) for p in history.get(trade["mint"],[]) if p.get("pair_address")==trade["pair_address"]),key=lambda z:z[0] or datetime.min.replace(tzinfo=timezone.utc))
    deadline=start+timedelta(minutes=int(trade.get("exit_minutes",EXIT_MINUTES)))
    for at,p in points:
        if at and p and deadline<=at<=now:
            trade.update(status="CLOSED",exit_reason="TIME_EXIT_DELAYED",exit_at_utc=at.isoformat(),exit_price_usd=p,return_pct=round((p/entry-1)*100,4),pnl_usd=round(STAKE*(p/entry-1),4),exit_delay_minutes=round((at-start).total_seconds()/60,2))
            break
ledger["updated_at_utc"]=now.isoformat()
path.write_text(json.dumps(ledger,ensure_ascii=False,indent=2)+"\n")
print("Paper trades:",len(trades),"closed:",sum(x["status"]=="CLOSED" for x in trades))
