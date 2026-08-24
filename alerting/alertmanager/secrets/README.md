# Alertmanager webhook secrets

Do not commit real Slack or PagerDuty credentials.

Create the two files locally (do not commit them). Each file is a single line, no quotes:

```bash
# slack_webhook.url — Slack incoming webhook for #aegis-alerts / #aegis-critical
# pagerduty.routing_key — PagerDuty Events API v2 routing key
cp pagerduty.routing_key.example pagerduty.routing_key
printf '%s' "$SLACK_WEBHOOK_URL" > slack_webhook.url
```

| File | Source |
| --- | --- |
| `slack_webhook.url` | Slack incoming webhook for `#aegis-alerts` / `#aegis-critical` |
| `pagerduty.routing_key` | PagerDuty Events API v2 integration / routing key |

Alertmanager reads these via `api_url_file` and `routing_key_file` in `alertmanager.yml`.
