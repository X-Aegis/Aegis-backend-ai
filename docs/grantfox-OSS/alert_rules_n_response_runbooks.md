# BK-15b — Alert rules and response runbooks

GrantFox Third Campaign write-up for [issue #37](https://github.com/X-Aegis/Aegis-backend-ai/issues/37).

This change adds the on-call path for the five BK-15b triggers: Prometheus rules, Alertmanager routing to Slack and PagerDuty, and one runbook per alert.

## What shipped

| Acceptance criterion | Where it lives |
| --- | --- |
| Prometheus / Alertmanager YAML for the five alert types | [`alerting/prometheus/rules/aegis-alerts.yml`](../../alerting/prometheus/rules/aegis-alerts.yml) |
| `runbooks/` with one Markdown file per alert | [`runbooks/`](../../runbooks/) |
| Each runbook: incident, investigation, escalation, remediation | same files, matching section headings |
| Slack / PagerDuty webhooks for critical alerts | [`alerting/alertmanager/alertmanager.yml`](../../alerting/alertmanager/alertmanager.yml) |
| Tests for rule syntax and runbook coverage | [`tests/test_alerting.py`](../../tests/test_alerting.py) |

Operator notes and a local Prometheus + Alertmanager compose file are in [`alerting/README.md`](../../alerting/README.md). Webhook secrets are mounted via `api_url_file` and `routing_key_file`; they are not committed.

## Alert catalog

| Alert | Severity | Fires when |
| --- | --- | --- |
| `ModelDriftDetected` | warning (Slack) | `aegis_model_drift_detected == 1` for 5m |
| `KeeperCircuitBreakerActivated` | critical (Slack + PagerDuty) | keeper breaker is open |
| `DataSourceDown` | critical | `aegis_datasource_up == 0` for 5m |
| `RebalanceFailed` | critical | `aegis_rebalance_failures_total` increased in 15m |
| `VaultTVLSuddenDrop` | critical | TVL now / TVL `offset 1h` is below `0.9` |

The gauges and counters are the metric contract for the BK-15a exporter (issue #36). Rules load without that exporter; they fire once the series exist.

## How to verify

```bash
pip install -r requirements.txt
python -m pytest tests/test_alerting.py
ruff check tests/test_alerting.py
```
