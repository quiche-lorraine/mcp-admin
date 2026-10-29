"""Outil health : modèle de surveillance du socle et ses surcharges (filtres du
playbook maintenance_health), alertes, et validation de la clé monitoring des stacks."""

import importlib.util

from infra_mcp import lint, maintenance as m
from infra_mcp.lint import COLLECTION_DIR

_spec = importlib.util.spec_from_file_location("monitoring", COLLECTION_DIR / "plugins" / "filter" / "monitoring.py")
f = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(f)

PATTERN = r"\b(err|error|crit|critical|fatal|panic|exception|traceback)\b"


def _tasks(*names):
    return [{"Name": n, "DesiredState": "Running", "CurrentState": "Running 2 hours ago"} for n in names]


def test_lignes_d_erreur():
    lines = ["ok", "ERROR boom", "level=error x", "terrorisme", "WRN deprecated", "CRITICAL [console] db"]
    assert f.error_lines(lines, PATTERN) == {"count": 3, "last": ["ERROR boom", "level=error x", "CRITICAL [console] db"]}
    assert f.error_lines(lines, PATTERN, [r"\[console\]", "boom"])["count"] == 1
    assert len(f.error_lines(["error"] * 50, PATTERN)["last"]) == f.KEEP


def test_services_et_surcharges():
    stacks = [
        {"item": {"name": "app", "monitoring": {
            "ignore": ["database"],
            "services": {"worker": {"error_pattern": "boom", "ignore": ["retry"]}, "redis": {"logs": False}}}},
         "results": _tasks("app_php.1", "app_worker.1", "app_redis.1", "app_php.2")},
        {"item": {"name": "proxy", "monitoring": {"logs": False}}, "results": _tasks("proxy_traefik.abc")},
        {"item": {"name": "plain"}, "results": _tasks("plain_web.1")},
    ]
    targets = f.monitoring_services(stacks, PATTERN, ["global"])
    assert targets == [
        {"name": "app_php", "error_pattern": PATTERN, "ignore": ["global", "database"]},
        {"name": "app_worker", "error_pattern": "boom", "ignore": ["global", "database", "retry"]},
        {"name": "plain_web", "error_pattern": PATTERN, "ignore": ["global"]},
    ]


def test_stacks_surveillees():
    stacks = [{"name": "a"}, {"name": "b", "monitoring": {"enabled": False}}, {"name": "c", "monitoring": None},
              {"name": "d", "monitoring": {"logs": False}}]
    assert [s["name"] for s in f.monitoring_stacks(stacks)] == ["a", "c", "d"]
    assert f.monitoring_stacks(None) == []


def test_constats():
    results = [
        {"item": "infra-mcp.service", "stdout_lines": ["2026-10-29T08:00:00 error a"]},
        {"item": "absente.service", "stdout_lines": []},
        {"item": {"name": "app_php", "error_pattern": "boom", "ignore": []},
         "stdout_lines": ["2026-10-29T08:00:02Z boom 2"], "stderr_lines": ["2026-10-29T08:00:01Z boom 1"]},
    ]
    assert f.monitoring_findings(results, PATTERN, []) == {
        "infra-mcp.service": {"count": 1, "last": ["2026-10-29T08:00:00 error a"]},
        "app_php": {"count": 2, "last": ["2026-10-29T08:00:01Z boom 1", "2026-10-29T08:00:02Z boom 2"]},
    }


def _status(**over):
    return {"report": {"uptime_seconds": 3600, "loadavg": "0.1 0.2 0.3 1/2 3",
                       "memory_mb": {"real": {"total": 1911}, "nocache": {"free": 1000}},
                       "mounts": [{"mount": "/", "size_total": 100e9, "size_available": 20e9, "fstype": "ext4"}],
                       "failed_units": [], "upgradable": [], "reboot_required": False, "reboot_pkgs": "",
                       "docker": None, "crowdsec": None, **over}}


def test_tout_va_bien():
    health = {"report": {"thresholds": {"disk_alert_pct": 85, "memory_alert_mb": 150}, "since": "24h",
                         "units": {"infra-mcp.service": {"count": 0, "last": []}},
                         "stacks": {"app": _tasks("app_php.1")}, "services": {"app_php": {"count": 0, "last": []}}}}
    r = m.summarize_health({"ctl": _status()}, {"ctl": health})
    assert r["ok"] is True and r["alerts"] == []
    assert r["stacks"]["app"]["app_php"]["logs"] == {"count": 0, "last": []}
    assert r["hosts"]["ctl"]["logs"] == {"infra-mcp.service": {"count": 0, "last": []}}


def test_alertes_et_seuils_surcharges():
    health = {"report": {"thresholds": {"disk_alert_pct": 70, "memory_alert_mb": 1500}, "since": "6h",
                         "units": {"infra-mcp.service": {"count": 2, "last": ["token=abc error"]}},
                         "stacks": {"app": [{"Name": "app_php.1", "DesiredState": "Running",
                                             "CurrentState": "Rejected 1 minute ago", "Error": "no such image"}],
                                    "vide": []},
                         "services": {"app_php": {"count": 1, "last": ["boom"]}}}}
    status = _status(failed_units=["foo.service loaded failed failed Foo"], reboot_required=True,
                     upgradable=["curl/noble-security 1 amd64 [upgradable from: 0]"])
    r = m.summarize_health({"ctl": status, "w1": {"error": "injoignable : timeout"}},
                           {"ctl": health, "w1": {"error": "injoignable : timeout"}})
    assert r["ok"] is False
    assert r["alerts"] == [
        "ctl : disque / plein à 80 %",
        "ctl : 1000 Mo de mémoire disponible",
        "ctl : unités en échec : foo.service",
        "ctl : redémarrage requis",
        "ctl : 1 mise(s) à jour en attente (dont 1 de sécurité)",
        "ctl : 2 ligne(s) d'erreur depuis 6h dans le journal de infra-mcp.service",
        "stack app : app_php à 0/1 répliques",
        "stack app : 1 ligne(s) d'erreur depuis 6h dans les logs de app_php",
        "stack vide : aucun service déployé",
        "w1 : injoignable : timeout",
    ]
    assert r["hosts"]["ctl"]["logs"]["infra-mcp.service"]["last"] == ["token=*** error"]
    assert r["hosts"]["w1"] == {"error": "injoignable : timeout"}


def test_rapports_de_plusieurs_plays():
    out = {"plays": [
        {"tasks": [{"task": {"name": "Rapport"}, "hosts": {"mgr": {"maintenance_report": {"units": {}}}}}]},
        {"tasks": [{"task": {"name": "Rapport"}, "hosts": {"mgr": {"maintenance_report": {"stacks": {}}}}}]},
    ]}
    import json
    assert m.parse_json_output(json.dumps(out))["mgr"]["report"] == {"units": {}, "stacks": {}}


def test_lint_de_la_cle_monitoring():
    assert lint._monitoring_violations(None) == []
    assert lint._monitoring_violations({"enabled": False, "logs": True, "error_pattern": "x", "ignore": ["a"],
                                        "services": {"worker": {"logs": False}, "web": None}}) == []
    problems = lint._monitoring_violations({"logz": True, "ignore": "a", "error_pattern": "(",
                                            "services": {"worker": {"enabled": False, "ignore": ["["]}}})
    assert any("clé inconnue 'logz'" in p for p in problems)
    assert any(p.startswith("ignore : list attendu") for p in problems)
    assert any("expression invalide '('" in p for p in problems)
    assert any(p.startswith("services.worker : clé inconnue 'enabled'") for p in problems)
    assert any(p.startswith("services.worker : expression invalide '['") for p in problems)
    assert lint._monitoring_violations(["x"]) == ["dictionnaire attendu"]
