# Runbook: Model drift detected

Alert: `ModelDriftDetected`  
Severity: warning (Slack `#aegis-alerts`)  
Component: FX volatility model (`services/drift_detection.py`, `api/monitoring.py`)

## Incident description

The live forecasting model is systematically worse than it was when the ADWIN window or Page-Hinkley statistic last reset. Detectors consume `|predicted − actual|` on the volatility score (0–100). A fire means concept drift on the error stream, not a single bad tick.

Typical causes:

- Regime change in NGN/KES/GHS/ZAR that the current window has not seen
- Stale or fallback FX rates from ingestion (see DataSourceDown)
- Outcomes recorded against the wrong `pair` / `horizon`
- Detector hyperparameters (`adwin_delta`, `ph_lambda`) too sensitive after a data gap

## Investigation steps

1. Confirm the alert labels (`pair`, `horizon`) in Prometheus / Alertmanager.
2. Pull recent detector state:
   - `GET /monitoring/drift?pair=<pair>&horizon=<horizon>&limit=50`
   - Inspect `adwin_drift_detected`, `ph_drift_detected`, `rolling_mae`, `rolling_rmse`, `ph_statistic`.
3. Compare `predicted` vs `actual` on the last 20 `drift_events` rows. A sudden MAE jump with healthy data sources points at the model; a jump that tracks a source outage points at ingestion.
4. Check `GET /risk/current?horizon=<horizon>` — if the live score is still being consumed by the keeper, treat drift as an allocation-quality incident, not just an ML ticket.
5. Review `fx_rates` freshness for that pair (Fixer.io / Open Exchange Rates). Stale prints will inflate absolute error.
6. If only Page-Hinkley fired and ADWIN did not, look for a slow climb in error (gradual drift). If only ADWIN fired, look for an abrupt level shift.

## Escalation path

| Time / condition | Who |
| --- | --- |
| Immediate | On-call backend / ML — acknowledge in Slack `#aegis-alerts` |
| Drift persists > 1h **or** rolling MAE doubles | Page the backend lead; comment on the incident thread |
| Drift coincides with keeper rebalances or TVL move | Escalate as critical to `@bbkenny` and follow the vault / keeper runbooks |
| Suspected bad production model artifact | Maintainer (`@bbkenny`) before any rollback or retraining merge |

## Remediation actions

1. **Do not** silently disable the alert. Silence in Alertmanager only after a documented cause.
2. If ingestion is stale, fix the source first (`runbooks/data-source-down.md`) and wait for new (prediction, actual) pairs before resetting detectors.
3. If the model is wrong for the current regime:
   - Stop the keeper from treating new scores as high-confidence (raise `REBALANCE_THRESHOLD` or pause the bot) until accuracy recovers.
   - Retrain / reload the volatility model on a window that includes the new regime.
4. After a confirmed concept change, reset ADWIN / Page-Hinkley state (new `DriftMonitor` or equivalent) so the statistic does not stay latched.
5. Record the incident: pair, horizon, MAE/RMSE before/after, and whether allocations were paused.
6. Resolve the alert only when `aegis_model_drift_detected` returns to 0 for a full evaluation interval.
