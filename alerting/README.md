# Alerting — rules, routing, and runbooks

Prometheus rules and Alertmanager receivers for the five BK-15b incident types. Response procedures live in [`runbooks/`](../runbooks/).

## Alert types

| Alert | Severity | Metric / expression | Runbook |
| --- | --- | --- | --- |
| `ModelDriftDetected` | warning | `aegis_model_drift_detected == 1` for 5m | [model-drift-detected](../runbooks/model-drift-detected.md) |
| `KeeperCircuitBreakerActivated` | critical | `aegis_keeper_circuit_breaker_open == 1` | [keeper-circuit-breaker-activated](../runbooks/keeper-circuit-breaker-activated.md) |
| `DataSourceDown` | critical | `aegis_datasource_up == 0` for 5m | [data-source-down](../runbooks/data-source-down.md) |
| `RebalanceFailed` | critical | `increase(aegis_rebalance_failures_total[15m]) > 0` | [rebalance-failed](../runbooks/rebalance-failed.md) |
| `VaultTVLSuddenDrop` | critical | TVL now / TVL 1h ago `< 0.9` | [vault-tvl-sudden-drop](../runbooks/vault-tvl-sudden-drop.md) |

Warning alerts go to Slack. Critical alerts go to Slack **and** PagerDuty.

## Files

```text
alerting/
├── prometheus/prometheus.yml          # scrape + Alertmanager target + rule_files
├── prometheus/rules/aegis-alerts.yml  # the five alert rules
├── alertmanager/alertmanager.yml      # Slack + PagerDuty routing
├── alertmanager/secrets/              # webhook files (not committed)
└── docker-compose.yml                 # local Prometheus + Alertmanager
```

## Local stack

```bash
# write slack_webhook.url and pagerduty.routing_key as described in
# alerting/alertmanager/secrets/README.md — never commit those two files
docker compose -f alerting/docker-compose.yml up
```

Prometheus UI: http://localhost:9090 — Alertmanager UI: http://localhost:9093

The `/metrics` scrape targets (`api:8000`, `keeper:8001`) are the contract for the BK-15a exporter. Until that exporter is live, rules still load and evaluate; they fire once the gauges/counters exist.
