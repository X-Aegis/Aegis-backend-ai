# Runbook: Data source down

Alert: `DataSourceDown`  
Severity: critical (Slack `#aegis-critical` + PagerDuty)  
Component: ingestion and runtime deps (`services/forex_ingester.py`, database, Stellar RPC)

## Incident description

`aegis_datasource_up{source=...}` has been 0 for five minutes. The `source` label identifies which dependency failed:

| `source` | What it is |
| --- | --- |
| `fixer` | Fixer.io FX feed (`FIXER_API_KEY`) |
| `openexchangerates` | Open Exchange Rates feed (`OPEN_EXCHANGE_RATES_APP_ID`) |
| `exchangerate_api` | Keyless open endpoint used as the last-resort official fallback (`EXCHANGERATE_API_URL`) |
| `parallel_market` | Parallel-market ("street rate") feed (`PARALLEL_MARKET_API_URL`) |
| `stellar_rpc` | Soroban RPC used by the keeper |
| `database` | PostgreSQL / TimescaleDB for `fx_rates`, predictions, snapshots |

A single FX vendor down is recoverable: the ingester fails over down the official chain (`fixer` → `openexchangerates` → `exchangerate_api`) and keeps writing `fx_rates`. Every official vendor down, or database down, starves `GET /risk/current` and can drive bad keeper decisions.

The parallel-market feed is a separate leg, not a fallback for the official chain. Losing it does **not** stop ingestion, but `GET /risk/current` then scores the official series instead of the street rate users are actually exposed to — treat a parallel-feed outage as degraded, not down.

Fastest triage: `curl "$API/fx/sources"` shows every feed's last quote, its age and whether it is stale — it separates "one feed stalled" from "ingestion is dead".

## Investigation steps

1. Read the `source` label on the firing alert.
2. **fixer / openexchangerates**
   - Hit the vendor `/latest` endpoint with the configured key (from a secrets host, not chat).
   - Confirm HTTP status and `success` / `rates` payload for NGN, KES, GHS, ZAR.
   - Check quota / billing on the vendor dashboard.
3. **database**
   - Connect with the app DSN. `SELECT max(timestamp) FROM fx_rates;` and `SELECT 1;`.
   - Look for lock, failover, or exhausted connection-pool errors in the API process.
4. **stellar_rpc**
   - Query the configured `SOROBAN_RPC_URL` health / getLatestLedger.
   - Distinguish testnet (`https://soroban-testnet.stellar.org`) vs a private RPC.
5. Check whether the other FX sources are still up (`GET /fx/sources`). If yes, ingestion should fail over; if `fx_rates` stopped entirely, every path failed or `save_fx_rates` is erroring.
   - A feed that answers HTTP 200 but keeps returning the *same* timestamp is treated as down by the stale-rate guard: the ingester drops those quotes and fails over. Look for `Stale-rate guard: dropping ...` in the ingester logs.
6. Correlate with `ModelDriftDetected` — stale prints often show up as error-stream drift after the outage.

## Escalation path

| Time / condition | Who |
| --- | --- |
| T+0 | On-call backend — PagerDuty ack |
| Vendor outage confirmed | Post status in `#aegis-critical`; watch-only if failover is healthy |
| Both FX vendors **or** database down > 10m | `@bbkenny` and whoever owns hosting (Render / DB) |
| Stellar RPC down while breaker is closed | Follow keeper runbooks in parallel |

## Remediation actions

1. If one FX vendor is down and the other is healthy: keep ingesting; silence only that `source` label after noting the vendor incident.
2. If both FX vendors are down: pause the keeper (or rely on the circuit breaker) so allocations do not move on stale volatility.
3. Rotate or restore `FIXER_API_KEY` / `OPEN_EXCHANGE_RATES_APP_ID` if the vendor returns 401/403.
4. Restore database connectivity or fail over. Do not backfill invented rates.
5. For RPC: switch `SOROBAN_RPC_URL` to a healthy endpoint if the primary is down; restart the keeper after the URL change.
6. When `aegis_datasource_up` returns to 1, confirm a fresh `fx_rates` row (`GET /fx/sources` shows `is_stale: false`) and that the `/risk/current` timestamp is recent.
7. Resolve PagerDuty only after the labeled source is up for a full 5-minute `for` window.
