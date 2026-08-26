"""Syntax and coverage checks for Prometheus / Alertmanager rules and runbooks."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ALERT_RULES = ROOT / "alerting" / "prometheus" / "rules" / "aegis-alerts.yml"
PROMETHEUS_CFG = ROOT / "alerting" / "prometheus" / "prometheus.yml"
ALERTMANAGER_CFG = ROOT / "alerting" / "alertmanager" / "alertmanager.yml"
RUNBOOKS_DIR = ROOT / "runbooks"

REQUIRED_ALERTS = {
    "ModelDriftDetected": "model-drift-detected.md",
    "KeeperCircuitBreakerActivated": "keeper-circuit-breaker-activated.md",
    "DataSourceDown": "data-source-down.md",
    "RebalanceFailed": "rebalance-failed.md",
    "VaultTVLSuddenDrop": "vault-tvl-sudden-drop.md",
}

REQUIRED_RUNBOOK_HEADINGS = (
    "Incident description",
    "Investigation steps",
    "Escalation path",
    "Remediation actions",
)


def _load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    assert isinstance(data, dict), f"{path} must parse to a mapping"
    return data


def _alert_rules() -> list[dict]:
    payload = _load_yaml(ALERT_RULES)
    rules: list[dict] = []
    for group in payload["groups"]:
        for rule in group.get("rules", []):
            if "alert" in rule:
                rules.append(rule)
    return rules


def _rules_by_name() -> dict[str, dict]:
    return {rule["alert"]: rule for rule in _alert_rules()}


def test_alert_rule_file_is_valid_yaml():
    payload = _load_yaml(ALERT_RULES)
    assert payload["groups"], "alerting rules must define at least one group"
    for group in payload["groups"]:
        assert group.get("name"), "each rule group needs a name"
        assert group.get("rules"), f"group {group.get('name')} has no rules"


def test_prometheus_and_alertmanager_configs_are_valid_yaml():
    prom = _load_yaml(PROMETHEUS_CFG)
    am = _load_yaml(ALERTMANAGER_CFG)

    assert "aegis-alerts.yml" in "".join(prom["rule_files"])
    assert prom["alerting"]["alertmanagers"]
    assert am["route"]["receiver"]
    assert am["receivers"]


def test_every_required_alert_is_defined():
    names = set(_rules_by_name())
    missing = set(REQUIRED_ALERTS) - names
    extra = names - set(REQUIRED_ALERTS)
    assert not missing, f"missing alert rules: {sorted(missing)}"
    assert not extra, f"unexpected alert rules: {sorted(extra)}"


def test_alert_rule_fields_and_severity():
    for name, rule in _rules_by_name().items():
        assert rule.get("expr"), f"{name} is missing expr"
        assert isinstance(rule["expr"], str) and rule["expr"].strip()
        assert rule.get("for"), f"{name} is missing for"
        assert rule.get("labels", {}).get("severity") in {"warning", "critical"}
        annotations = rule.get("annotations") or {}
        for field in ("summary", "description", "runbook_url"):
            assert annotations.get(field), f"{name} is missing annotation {field}"


def test_each_alert_has_a_matching_runbook():
    for name, filename in REQUIRED_ALERTS.items():
        rule = _rules_by_name()[name]
        runbook = RUNBOOKS_DIR / filename
        assert runbook.is_file(), f"runbook missing for {name}: {runbook}"
        url = rule["annotations"]["runbook_url"]
        assert url.endswith(filename), (
            f"{name} runbook_url does not point at {filename}"
        )


def test_runbooks_contain_required_sections():
    for filename in REQUIRED_ALERTS.values():
        text = (RUNBOOKS_DIR / filename).read_text(encoding="utf-8")
        assert text.strip(), f"{filename} is empty"
        missing = [
            heading for heading in REQUIRED_RUNBOOK_HEADINGS if heading not in text
        ]
        assert not missing, f"{filename} missing sections: {missing}"


def test_runbooks_directory_has_no_orphan_alert_docs():
    markdown = {path.name for path in RUNBOOKS_DIR.glob("*.md")}
    expected = set(REQUIRED_ALERTS.values())
    assert markdown == expected, (
        f"runbooks/ should only cover the five alerts: {markdown}"
    )


def test_vault_tvl_rule_detects_ten_percent_drop_in_one_hour():
    expr = " ".join(_rules_by_name()["VaultTVLSuddenDrop"]["expr"].split())
    assert "offset 1h" in expr
    assert "0.9" in expr
    assert "aegis_vault_tvl" in expr


def test_critical_alerts_route_to_slack_and_pagerduty():
    am = _load_yaml(ALERTMANAGER_CFG)
    critical_names = {
        name
        for name, rule in _rules_by_name().items()
        if rule["labels"]["severity"] == "critical"
    }
    assert critical_names == {
        "KeeperCircuitBreakerActivated",
        "DataSourceDown",
        "RebalanceFailed",
        "VaultTVLSuddenDrop",
    }

    routes = am["route"].get("routes") or []
    assert any("critical" in str(route.get("matchers", [])) for route in routes)

    receivers = {receiver["name"]: receiver for receiver in am["receivers"]}
    critical = receivers[am["route"]["routes"][0]["receiver"]]
    assert critical.get("slack_configs"), "critical receiver must notify Slack"
    assert critical.get("pagerduty_configs"), "critical receiver must notify PagerDuty"

    slack = critical["slack_configs"][0]
    pagerduty = critical["pagerduty_configs"][0]
    assert slack.get("api_url_file"), "Slack webhook must come from a mounted file"
    assert pagerduty.get("routing_key_file"), (
        "PagerDuty key must come from a mounted file"
    )
    assert "hooks.slack.com" not in str(am)
    assert not pagerduty.get("routing_key")
    assert not slack.get("api_url")
