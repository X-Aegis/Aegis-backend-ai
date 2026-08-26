# Runbook: Vault TVL sudden drop (>10% in 1 hour)

Alert: `VaultTVLSuddenDrop`  
Severity: critical (Slack `#aegis-critical` + PagerDuty)  
Component: vault accounting (`vault_snapshots.tvl`, metric `aegis_vault_tvl`)

## Incident description

Total Value Locked is now below 90% of the reading from one hour ago (`aegis_vault_tvl / aegis_vault_tvl offset 1h < 0.9`). A move that large in 60 minutes is not normal yield noise — treat it as capital leaving the vault, a pricing/oracle fault, or a failed defensive rebalance.

The schema stores snapshots as `(timestamp, tvl, share_price)`. A TVL crash with a stable `share_price` looks like withdrawals. A TVL crash with a crashing `share_price` looks like mark-to-market or oracle damage.

## Investigation steps

1. Ack PagerDuty immediately. This is a user-fund incident until proven otherwise.
2. Plot `aegis_vault_tvl` and `share_price` (or `vault_snapshots`) over the last 6 hours. Note the exact drop start.
3. Check sibling alerts: `RebalanceFailed`, `KeeperCircuitBreakerActivated`, `DataSourceDown`, `ModelDriftDetected`.
4. Compare current target allocation (`GET /risk/current` → `compute_target_allocation`) with the last keeper submit. A vault left 100% in FX during a spike can lose TVL without a withdraw bug.
5. Confirm the exporter is reading the same unit/scale as `vault_snapshots.tvl` (no 1e7 / stroop mismatch).
6. On-chain: vault contract balance vs the off-chain snapshot. If chain TVL is flat and the metric dumped, it is an exporter / DB bug — still keep the page open until confirmed.
7. Look for large withdraw events or share burns in the same window.

## Escalation path

| Time / condition | Who |
| --- | --- |
| T+0 | On-call DevOps **and** protocol lead — PagerDuty + Slack `#aegis-critical` |
| On-chain TVL also down >10% | `@bbkenny` immediately; consider pausing deposits/withdraws at the product layer |
| Metric-only drop (chain healthy) | Backend owner; keep the page until the gauge is fixed so we do not hide a real crash |
| Suspected exploit / unauthorized `rebalance` | Freeze keeper keys, notify maintainers, do not reset the breaker to “retry faster” |

## Remediation actions

1. Pause the keeper if it is still submitting (`circuit breaker` open or process stop). Do not auto-rebalance blindly into a crash.
2. If the drop is a bad price print: halt ingestion of the bad source (`data-source-down.md`) and do not persist bogus `vault_snapshots`.
3. If the drop is real withdrawals: that may be expected user behavior — document volume, confirm share accounting, and only then resolve.
4. If the drop is a failed hedge (volatility high, vault still in FX): once RPC and signing are healthy, perform a **supervised** rebalance to the stable mix (`HIGH_VOLATILITY_CUTOFF`).
5. Recalibrate the snapshot job so `aegis_vault_tvl` matches on-chain TVL after the incident.
6. Resolve PagerDuty when TVL is explained, the gauge is trustworthy, and (if required) a corrective rebalance has landed.
7. Write the hour-over-hour percentages and the root cause in the incident ticket before close.
