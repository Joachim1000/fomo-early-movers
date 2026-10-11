# FOMO Early Movers

Solana market-data dashboard using DEX Screener and FOMO API when available.

## Collection schedule

- Target: **every 5 minutes, 24/7, including weekends** (288 runs per day).
- `scripts/collect.py` refreshes FOMO boards when the cache is at least 300 seconds old or incomplete; DEX market data is fetched on every collector run.
- `.github/workflows/collect.yml` uses **workflow_dispatch only**. An external scheduler (such as cron-job.org) must trigger this workflow every five minutes. Changing this repository alone does **not** change the external scheduler.
- Snapshots are written to the `market-data` branch. Dashboard freshness depends on successful workflow runs and publication.
- FOMO API HTTP 402 or other errors can prevent new FOMO data even when scheduling is correct. The collector retains prior valid cache when available; stale data should not be presented as fresh.

## Manual test

1. Open **Actions → Fomo Early Movers Data → Run workflow**.
2. Check workflow logs and the `market-data` branch's `data/latest.json` and `data/fomo_cache.json` timestamps.
3. Confirm the external scheduler calls workflow_dispatch every five minutes, all days.

If commits fail, review **Settings → Actions → General → Workflow permissions → Read and write permissions**.

The token screening is observational, not a buy recommendation. DEX transactions are not unique wallets; DEX discovery does not by itself verify FOMO Trending membership.
