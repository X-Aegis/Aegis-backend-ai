# Runbook: Rebalance failed

Alert: `RebalanceFailed`  
Severity: critical (Slack `#aegis-critical` + PagerDuty)  
Component: keeper submit path (`execute_rebalance_transaction` in `services/keeper_bot.py`)

## Incident description

`aegis_rebalance_failures_total` increased in the last 15 minutes. A keeper cycle decided a rebalance was required (allocation delta ≥ `REBALANCE_THRESHOLD`, or first run) and the Soroban transaction failed during simulation, fee application, signing, or submit.

If failures continue, `KeeperCircuitBreakerActivated` should fire and this alert may be inhibited.

Failure points in the current bot:

1. Missing `ADMIN_SECRET_KEY` (`OSError`)
2. Invalid secret (Stellar keypair parse)
3. `server.load_account` / RPC errors
4. `simulate_transaction` returns `error`
5. `send_transaction` returns `errorResultXdr`
6. KMS / Vault signing selected but XDR injection still unimplemented (falls through to local wallet)

## Investigation steps

1. Ack PagerDuty. Graph `increase(aegis_rebalance_failures_total[15m])` and keeper logs around the scrape.
2. Reproduce the decision: fetch `GET /risk/current`, compute `compute_target_allocation(score)`, compare to the last submitted allocation.
3. Confirm config: `SOROBAN_CONTRACT_ID`, `SOROBAN_RPC_URL`, `SOROBAN_NETWORK_PASSPHRASE`, `SIGNING_BACKEND`.
4. Isolate the stage from logs:
   - `Simulation failed` → contract args, footprint, or auth
   - `Transaction submission failed` → on-chain revert / fee / sequence
   - `Failed to fetch volatility score` → this increment should **not** count as a rebalance failure; if it does, the exporter is mis-labeled
5. Check account sequence and native XLM balance on `SOROBAN_SOURCE_ACCOUNT` / the keypair public key.
6. If `SIGNING_BACKEND` is `aws_kms` or `vault`, verify the fallback warning and whether a local key was actually used.

## Escalation path

| Time / condition | Who |
| --- | --- |
| T+0 | On-call DevOps — PagerDuty ack |
| Simulation revert on `rebalance` | Contract maintainer + `@bbkenny` |
| Key / KMS / Vault failure | Secrets owner; never paste `ADMIN_SECRET_KEY` or tokens |
| More than two failed submits **or** breaker open | Follow `keeper-circuit-breaker-activated.md` and page the lead |

## Remediation actions

1. Do not keep hammering `run_once` while simulation is failing — you will burn sequence numbers and trip the breaker.
2. Fix the specific stage (RPC URL, fees, auth, contract id, account funding).
3. For a contract revert: inspect the invoke footprint and the `rebalance` auth requirement (`scval.to_address` of the signer).
4. After a successful **simulation**, submit one transaction. Confirm the hash on the network used by `SOROBAN_NETWORK_PASSPHRASE`.
5. Verify the vault’s on-chain mix matches `compute_target_allocation` for the current score.
6. If the breaker opened during the incident, reset it only after the successful submit (see breaker runbook).
7. Resolve when `increase(aegis_rebalance_failures_total[15m])` is 0 and no new error logs appear on the next poll.
