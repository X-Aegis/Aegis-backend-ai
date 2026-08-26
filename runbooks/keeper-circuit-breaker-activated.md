# Runbook: Keeper circuit breaker activated

Alert: `KeeperCircuitBreakerActivated`  
Severity: critical (Slack `#aegis-critical` + PagerDuty)  
Component: keeper bot (`services/keeper_bot.py`)

## Incident description

The off-chain keeper opened its circuit breaker after repeated failed `rebalance` submissions. While the breaker is open, automatic allocation changes are refused even if `GET /risk/current` crosses `HIGH_VOLATILITY_CUTOFF` (default 80) or `REBALANCE_THRESHOLD` (default 5%).

The vault can sit in the wrong mix (fully FX vs fully stable) until the breaker is closed.

Typical causes:

- Burst of Soroban simulation / submit errors (see RebalanceFailed)
- Bad signing backend (`ADMIN_SECRET_KEY`, AWS KMS, Vault transit)
- Unreachable Soroban RPC (`SOROBAN_RPC_URL`)
- Missing `SOROBAN_CONTRACT_ID` after a config deploy
- Breaker left latched after a resolved RPC incident

## Investigation steps

1. Page-ack in PagerDuty. Confirm `aegis_keeper_circuit_breaker_open == 1` in Prometheus.
2. Read keeper logs for the last poll cycle (`POLL_INTERVAL_SECONDS`, default 3600s):
   - `Failed to fetch volatility score`
   - `Soroban transaction failed`
   - `SOROBAN_CONTRACT_ID or ADMIN_SECRET_KEY is not configured`
3. Check whether `RebalanceFailed` is also firing. If it is inhibited, the breaker is the parent incident — still read the rebalance runbook.
4. Probe dependencies:
   - Model API: `GET {MODEL_API_BASE_URL}/risk/current?horizon={RISK_HORIZON}`
   - Stellar RPC: health of `SOROBAN_RPC_URL` (default testnet)
   - Signing: `SIGNING_BACKEND` in `{env_key, aws_kms, vault}`
5. Confirm the last successful allocation (`KeeperBot._last_allocation`) vs the allocation implied by the current volatility score (`compute_target_allocation`).
6. Check vault TVL (`vault_snapshots` / `aegis_vault_tvl`). If TVL also dropped, treat as a combined vault incident.

## Escalation path

| Time / condition | Who |
| --- | --- |
| T+0 | On-call DevOps — PagerDuty ack, Slack `#aegis-critical` |
| T+15m still open, or vault TVL dropping | `@bbkenny` (project lead) and protocol / contract owner |
| Signing or KMS / Vault outage | Platform / secrets owner; do not paste keys into Slack |
| Suspected contract revert on `rebalance` | Soroban contract maintainer before a manual invoke |

## Remediation actions

1. Stop retry storms. Leave the breaker open until the underlying submit path is healthy.
2. Restore the failing dependency (RPC, keys, contract id, model API) using `runbooks/rebalance-failed.md` and `runbooks/data-source-down.md` as needed.
3. Dry-run: simulate a `rebalance` invoke against the configured `SOROBAN_CONTRACT_ID` without submitting if the SDK path allows it.
4. When simulation succeeds, reset the breaker (process restart or explicit reset metric) and run **one** keeper cycle (`run_once`).
5. Confirm `aegis_keeper_circuit_breaker_open == 0` and that `aegis_rebalance_failures_total` stops increasing.
6. If the vault is still off-target after the successful tx, schedule a supervised rebalance rather than widening `REBALANCE_THRESHOLD`.
7. Post a short timeline in the PagerDuty incident before resolve.
