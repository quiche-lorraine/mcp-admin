"""Bout en bout des opérations de maintenance sur un vrai swarm à un nœud (job
« stacks » de la CI ; sauté sans swarm local). Les outils sont appelés comme le
ferait le serveur MCP, sur un inventaire où le manager est joint en local."""

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from infra_mcp import config, runner, server
from test_stacks_e2e import _swarm_active, docker

pytestmark = pytest.mark.skipif(not _swarm_active(), reason="pas de swarm local (docker swarm init)")

STACK = "e2emaint"


def _systemd() -> bool:
    return Path("/run/systemd/system").is_dir()


def _apt_python() -> str | None:
    """Python système avec python3-apt (requis par le module apt en check mode)."""
    for candidate in ("/usr/bin/python3", *sorted(map(str, Path("/usr/bin").glob("python3.[0-9]*")))):
        if subprocess.run([candidate, "-c", "import apt_pkg"], capture_output=True).returncode == 0:
            return candidate
    return None


async def _no_check():
    return None


def call(tool, **kwargs):
    fn = getattr(server, tool)
    return asyncio.run(getattr(fn, "fn", fn)(**kwargs))


@pytest.fixture(scope="module")
def infra(tmp_path_factory):
    root = tmp_path_factory.mktemp("infra")
    (root / "ansible.cfg").write_text("[defaults]\ninventory = inventory/hosts.yml\n")
    local = {"ansible_connection": "local", "ansible_python_interpreter": sys.executable}
    hosts = {
        "mgr": local,
        # Le contrôleur est reconnu à son adresse locale.
        "ctl": {**local, "ansible_host": "127.0.0.1"},
    }
    if python := _apt_python():
        hosts["sys"] = {"ansible_connection": "local", "ansible_python_interpreter": python}
    (root / "inventory").mkdir()
    (root / "inventory" / "hosts.yml").write_text(yaml.safe_dump({"all": {
        "hosts": {k: v for k, v in hosts.items() if k != "mgr"},
        "children": {"swarm": {"children": {"swarm_managers": {"hosts": {"mgr": hosts["mgr"]}}}}},
    }}))
    compose = root / "compose.yml"
    compose.write_text(yaml.safe_dump({"services": {"web": {
        "image": "traefik/whoami:v1.10.3", "deploy": {"replicas": 2}}}}))
    docker("stack", "deploy", "--detach=false", "-c", str(compose), STACK)
    previous, config.ANSIBLE_DIR = config.ANSIBLE_DIR, root
    # L'inventaire de test joint le manager en local (ansible_connection), ce que le
    # lint refuse à juste titre dans un vrai repo de config : le contrôle est testé
    # à part (test_maintenance.py), il est court-circuité ici.
    check_repo, runner.check_repo = runner.check_repo, _no_check
    yield root
    config.ANSIBLE_DIR, runner.check_repo = previous, check_repo
    subprocess.run(["docker", "stack", "rm", STACK], capture_output=True)


def test_status(infra):
    s = call("status", limit="mgr")["mgr"]
    assert "error" not in s, s
    assert s["docker"]["swarm"] == "active" and s["docker"]["manager"] is True
    assert s["disks"] and s["load"]


def test_stack_status_et_logs(infra):
    s = call("stack_status", stack=STACK)["mgr"]
    assert s[f"{STACK}_web"]["desired"] == 2 and s[f"{STACK}_web"]["running"] == 2, s
    lines = call("logs", source=f"docker:{STACK}_web", lines=20, since="1h")["mgr"]["lines"]
    assert any("Starting up on port 80" in line for line in lines), lines
    assert call("logs", source=f"docker:{STACK}_web", grep="introuvable")["mgr"]["lines"] == []


def test_stack_restart(infra):
    def task_ids():
        return set(docker("service", "ps", "-q", "--filter", "desired-state=running", f"{STACK}_web").split())

    before = task_ids()
    dry = call("stack_restart", stack=STACK, dry_run=True)["mgr"]
    assert dry == {"services": [f"{STACK}_web"], "simulation": True}
    assert task_ids() == before
    assert call("stack_restart", stack=STACK, service="web")["mgr"]["simulation"] is False
    assert not task_ids() & before  # nouvelles tâches
    assert "introuvable" in call("stack_restart", stack=STACK, service="absent")["mgr"]["error"]


def test_docker_prune(infra):
    dry = call("docker_prune", limit="mgr", dry_run=True)["mgr"]
    assert dry["simulation"] is True and dry["reclaimed_bytes"] == 0
    applied = call("docker_prune", limit="mgr")["mgr"]
    assert applied["simulation"] is False and applied["reclaimed_bytes"] >= 0
    # L'image d'une stack qui tourne n'est jamais supprimée
    assert call("stack_status", stack=STACK)["mgr"][f"{STACK}_web"]["running"] == 2


def test_reboot_du_controleur_differe(infra):
    r = call("reboot", limit="ctl", only_if_required=False, dry_run=True)["ctl"]
    assert r == {"action": "redémarrage programmé dans une minute", "simulation": True}
    assert call("reboot", limit="ctl", dry_run=True)["ctl"]["action"] in ("aucune", "redémarrage programmé dans une minute")


@pytest.mark.skipif(not _apt_python(), reason="pas de python3-apt")
def test_updates_simulation(infra):
    for mode in ("upgrade", "security", "dist-upgrade"):
        u = call("updates", limit="sys", mode=mode, dry_run=True)["sys"]
        assert "error" not in u, u
        assert u["mode"] == mode and u["simulation"] is True and u["count"] == len(u["packages"])


@pytest.mark.skipif(not (_systemd() and shutil.which("journalctl")), reason="pas de systemd")
def test_service_systemd(infra):
    r = call("restart_service", host="ctl", service="docker", dry_run=True)["ctl"]
    assert r["unit"] == "docker.service" and r["simulation"] is True and r["active_state"] == "active", r
    assert "inconnue" in call("restart_service", host="ctl", service="absent-e2e", dry_run=True)["ctl"]["error"]
    lines = call("logs", source="unit:docker.service", host="ctl", lines=5, since="1d")["ctl"]["lines"]
    assert lines, lines
