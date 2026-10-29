"""Opérations de maintenance : validation des paramètres, masquage, lecture de la
sortie JSON d'Ansible et mise en forme des rapports (docs/maintenance.md)."""

import json

import pytest
from fastmcp.exceptions import ToolError

from infra_mcp import lint, maintenance as m
from infra_mcp.lint import COLLECTION_DIR


@pytest.mark.parametrize("unit", ["ssh", "sshd.service", "ssh.socket"])
def test_ssh_jamais_redemarre(unit):
    with pytest.raises(ToolError, match="ssh"):
        m.check_unit(unit)


@pytest.mark.parametrize("bad", ["", "a b", "x;rm -rf /", "$(id)", "--help\n"])
def test_unite_invalide(bad):
    with pytest.raises(ToolError):
        m.check_unit(bad)


def test_unites_acceptees():
    for unit in ("cron", "docker.service", "getty@tty1.service", "infra-mcp.service"):
        assert m.check_unit(unit) == unit


@pytest.mark.parametrize("source, expected", [
    ("unit:docker.service", ("unit", "docker.service")),
    ("docker:traefik_traefik", ("docker", "traefik_traefik")),
])
def test_source_valide(source, expected):
    assert m.parse_source(source) == expected


@pytest.mark.parametrize("source", ["docker.service", "unit:", "docker:Traefik", "shell:ls", "docker:-x"])
def test_source_invalide(source):
    with pytest.raises(ToolError, match="source invalide"):
        m.parse_source(source)


def test_bornes():
    assert m.check_since("30m") == "30m" and m.check_lines(500) == 500
    for since in ("1 hour", "-1h", "1w", ""):
        with pytest.raises(ToolError):
            m.check_since(since)
    for lines in (0, 501):
        with pytest.raises(ToolError):
            m.check_lines(lines)
    with pytest.raises(ToolError, match="un seul hôte"):
        m.check_host("a,b")
    with pytest.raises(ToolError):
        m.check_stack("Ma-Stack")


def test_masquage_des_logs():
    text = "\n".join([
        "login token=abc123 ok",
        "Authorization: Bearer eyJhbGciOi",
        "jeton imcp_0123abcd_SECRET",
        "docker swarm join --token SWMTKN-1-3e1dw-8lts7",
        "PASSWORD: hunter2",
        "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----",
    ])
    out = m.redact_log(text)
    for secret in ("abc123", "eyJhbGciOi", "SECRET", "3e1dw", "hunter2", "AAAA"):
        assert secret not in out, out
    assert "Authorization: Bearer ***" in out


def test_filtre_des_logs():
    lines = ["2026-10-19T06:00:02Z b | erreur token=x", "2026-10-19T06:00:01Z a | ok"]
    assert m.filter_log_lines(lines, None) == ["2026-10-19T06:00:01Z a | ok", "2026-10-19T06:00:02Z b | erreur token=***"]
    assert m.filter_log_lines(lines, "ERREUR") == ["2026-10-19T06:00:02Z b | erreur token=***"]


def _json_output(tasks, stats):
    return "[WARNING]: bruit avant le JSON\n" + json.dumps({"plays": [{"tasks": tasks}], "stats": stats})


def test_lecture_de_la_sortie_json():
    out = _json_output(
        [
            {"task": {"name": "Paramètres valides"}, "hosts": {"a": {"changed": False}, "b": {"failed": True, "msg": "refusé"}}},
            {"task": {"name": "Ignorée"}, "hosts": {"a": {"failed": True, "ignore_errors": True, "msg": "x"}}},
            {"task": {"name": "Rapport"}, "hosts": {"a": {"maintenance_report": {"ok": 1}}}},
            {"task": {"name": "Gathering Facts"}, "hosts": {"c": {"unreachable": True, "msg": "timeout"}}},
            {"task": {"name": "Logs"}, "hosts": {"d": {"failed": True, "rc": 1, "stderr": "invalid duration",
                                                       "msg": "non-zero return code"}}},
        ],
        {"a": {"ok": 2}, "b": {"failures": 1}, "c": {"unreachable": 1}},
    )
    hosts = m.parse_json_output(out)
    assert hosts["a"] == {"report": {"ok": 1}, "stats": {"ok": 2}}
    assert hosts["b"]["error"] == "« Paramètres valides » : refusé"
    assert hosts["c"]["error"].startswith("injoignable")
    assert hosts["d"]["error"] == "« Logs » : non-zero return code\ninvalid duration"
    assert m.parse_json_output("pas de JSON") == {}


def test_rapport_status():
    report = {
        "uptime_seconds": 7200, "loadavg": "0.10 0.20 0.30 1/200 999",
        "memory_mb": {"real": {"total": 4000}, "nocache": {"free": 3000}},
        "mounts": [
            {"mount": "/", "fstype": "ext4", "size_total": 100e9, "size_available": 10e9},
            {"mount": "/run", "fstype": "tmpfs", "size_total": 1e9, "size_available": 1e9},
        ],
        "failed_units": ["foo.service loaded failed failed Foo"],
        "upgradable": ["Listing...", "curl/noble-security 8.5 amd64 [upgradable from: 8.4]",
                       "vim/noble-updates 9.1 amd64 [upgradable from: 9.0]"],
        "reboot_required": True, "reboot_pkgs": "linux-image-6.8\n",
        "docker": {"ContainersRunning": 3, "Swarm": {"LocalNodeState": "active", "ControlAvailable": True, "Nodes": 1}},
    }
    s = m.summarize_status(report)
    assert s["uptime_hours"] == 2.0 and s["load"] == ["0.10", "0.20", "0.30"]
    assert s["disks"] == [{"mount": "/", "size_gb": 100.0, "used_pct": 90, "alert": True}]
    assert s["failed_units"] == ["foo.service"]
    assert s["updates"] == {"pending": 2, "security": 1, "packages": ["curl", "vim"]}
    assert s["reboot_required"] and s["reboot_packages"] == ["linux-image-6.8"]
    assert s["docker"] == {"containers_running": 3, "swarm": "active", "manager": True, "nodes": 1}
    assert m.summarize_status({"docker": None})["docker"] is None
    assert s["crowdsec"] is None  # pas installé


def test_rapport_crowdsec():
    alerts = [
        {"decisions": [{"type": "ban", "value": "203.0.113.7", "scenario": "crowdsecurity/ssh-bf", "duration": "3h59m"}]},
        {"decisions": [{"type": "captcha", "value": "198.51.100.1"}, {"type": "ban", "value": "198.51.100.2"}]},
        {"decisions": None},
    ]
    s = m.summarize_status({"crowdsec": alerts})["crowdsec"]
    assert s["bans"] == 2
    assert s["latest"][0] == {"ip": "203.0.113.7", "scenario": "crowdsecurity/ssh-bf", "duration": "3h59m"}
    assert m.summarize_status({"crowdsec": []})["crowdsec"] == {"bans": 0, "latest": []}


def test_rapport_stack():
    tasks = [
        {"Name": "web_app.1", "DesiredState": "Running", "CurrentState": "Running 2 hours ago"},
        {"Name": "web_app.1", "DesiredState": "Shutdown", "CurrentState": "Failed 3 hours ago", "Error": "exit 1", "Node": "n1"},
        {"Name": "web_app.2", "DesiredState": "Running", "CurrentState": "Preparing 1 second ago"},
        {"Name": "web_agent.x1y2", "DesiredState": "Running", "CurrentState": "Running 5 min ago"},
    ]
    s = m.summarize_stack(tasks)
    assert s["web_app"]["desired"] == 2 and s["web_app"]["running"] == 1
    assert s["web_app"]["errors"] == [{"state": "Failed 3 hours ago", "node": "n1", "error": "exit 1"}]
    assert s["web_agent"] == {"desired": 1, "running": 1, "errors": []}


def test_rapport_updates():
    simulated = {"mode": "upgrade", "simulation": True, "reboot_required": False,
                 "output": ["Reading package lists...", "Inst curl [8.4] (8.5 Ubuntu:24.04/noble-security [amd64])",
                            "Conf curl (8.5 Ubuntu:24.04/noble-security [amd64])"]}
    applied = {"mode": "security", "simulation": False, "reboot_required": True,
               "output": ["Unpacking vim (2:9.1) over (2:9.0) ...", "Setting up vim (2:9.1) ...", "Setting up curl (8.5) ..."]}
    assert m.summarize_updates(simulated)["packages"] == ["curl"]
    security = {"output": ["Allowed origins are: o=Ubuntu,a=noble-security",
                           "Packages that will be upgraded: libssl3 openssl"]}
    assert m.summarize_updates(security)["packages"] == ["libssl3", "openssl"]
    assert m.summarize_updates(applied) == {"mode": "security", "simulation": False,
                                            "packages": ["curl", "vim"], "count": 2, "reboot_required": True}


def test_playbooks_du_socle():
    """Chaque outil a son playbook, qui finit par la tâche « Rapport » lue par le serveur,
    et le socle reste conforme aux règles de l'outil (pas de check_mode: false)."""
    names = ["status", "journal", "service_logs", "stack_status", "health", "updates", "reboot",
             "restart_service", "stack_restart", "docker_prune"]
    for name in names:
        path = COLLECTION_DIR / "playbooks" / f"maintenance_{name}.yml"
        assert path.is_file(), path
        assert "name: Rapport" in path.read_text(), path
        assert lint.real_execution_violations(path) == [], path


@pytest.mark.parametrize("unit, controller, deferred", [
    ("infra-mcp.service", True, True),
    # infra-mcp-update attend qu'aucun ansible-playbook ne tourne : relancé par
    # ce playbook sans délai, il l'attendrait pendant que celui-ci l'attend.
    ("infra-mcp-update.service", True, True),
    ("docker.service", True, False),
    ("infra-mcp.service", False, False),
])
def test_redemarrages_differes(unit, controller, deferred):
    import jinja2
    import yaml

    play = yaml.safe_load((COLLECTION_DIR / "playbooks" / "maintenance_restart_service.yml").read_text())[0]
    rendered = jinja2.Environment().from_string(play["vars"]["_deferred"]).render(
        _controller=controller, _unit=unit)
    assert rendered == str(deferred)


async def test_repo_hors_lint_rien_n_est_execute(monkeypatch):
    """Comme plan et apply : un repo déployé qui ne passe pas le lint bloque tout."""
    from infra_mcp import runner

    called = []
    monkeypatch.setattr(lint, "run", lambda path: ["playbooks/x.yml : check_mode: False"])
    monkeypatch.setattr(runner, "run", lambda *a, **k: called.append(a))
    with pytest.raises(ToolError, match="ne passe pas infra-mcp lint"):
        await m.run("status", {}, limit="all")
    assert called == []
