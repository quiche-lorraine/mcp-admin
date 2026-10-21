"""Bout en bout du rôle crowdsec sur la machine elle-même : installation depuis le
dépôt de l'éditeur, plan propre au second passage, bannissement appliqué par
nftables, remonté par l'outil status. Installe des paquets et touche au pare-feu :
lancé seulement par le job « crowdsec » de la CI (INFRA_MCP_E2E_CROWDSEC=1)."""

import asyncio
import json
import os
import subprocess
import time

import pytest
import yaml

from infra_mcp import config, runner, server

pytestmark = pytest.mark.skipif(os.environ.get("INFRA_MCP_E2E_CROWDSEC") != "1",
                                reason="modifie la machine : INFRA_MCP_E2E_CROWDSEC=1 (job crowdsec de la CI)")

BANNED = "203.0.113.7"


def sudo(*args) -> str:
    return subprocess.run(["sudo", *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture(scope="module")
def infra(tmp_path_factory):
    root = tmp_path_factory.mktemp("infra")
    (root / "ansible.cfg").write_text("[defaults]\ninventory = inventory/hosts.yml\n")
    (root / "inventory").mkdir()
    # Python système : les modules apt et deb822_repository utilisent python3-apt.
    host = {"ansible_connection": "local", "ansible_python_interpreter": "/usr/bin/python3",
            "crowdsec_whitelist_host": ["198.51.100.0/24"]}
    (root / "inventory" / "hosts.yml").write_text(yaml.safe_dump({"all": {"hosts": {"cs": host}}}))
    (root / "crowdsec.yml").write_text(yaml.safe_dump([{
        "name": "CrowdSec", "hosts": "all", "become": True, "roles": ["infra_mcp.core.crowdsec"]}]))
    return root


def journals() -> str:
    """Journaux des services, pour comprendre un échec en CI."""
    out = subprocess.run(["sudo", "journalctl", "--no-pager", "-n", "60", "-u", "crowdsec",
                          "-u", "crowdsec-firewall-bouncer"], capture_output=True, text=True).stdout
    logs = subprocess.run(["sudo", "tail", "-n", "40", "/var/log/crowdsec-firewall-bouncer.log"],
                          capture_output=True, text=True).stdout
    return "\n--- journalctl ---\n" + out + "\n--- bouncer.log ---\n" + logs


def playbook(infra, *args):
    res = subprocess.run([runner._bin("ansible-playbook"), "crowdsec.yml", *args], cwd=infra,
                         capture_output=True, text=True, env={**os.environ, "ANSIBLE_NOCOLOR": "1"})
    assert res.returncode == 0, res.stdout[-4000:] + res.stderr[-2000:] + journals()
    return runner.recap(res.stdout)["cs"]


def test_plan_sur_machine_neuve(infra):
    # Rien d'installé : le plan passe sans échouer sur ce qui n'existe pas encore.
    assert playbook(infra, "--check", "--diff")["failed"] == 0


def test_installation_puis_plan_propre(infra):
    assert playbook(infra)["failed"] == 0
    assert playbook(infra, "--check", "--diff")["changed"] == 0
    for unit in ("crowdsec", "crowdsec-firewall-bouncer"):
        assert subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0, unit
    installed = {c["name"] for c in json.loads(sudo("cscli", "collections", "list", "-o", "json"))["collections"]}
    assert {"crowdsecurity/linux", "crowdsecurity/sshd", "crowdsecurity/traefik"} <= installed
    whitelist = sudo("cat", "/etc/crowdsec/parsers/s02-enrich/infra-mcp-whitelist.yaml")
    assert "198.51.100.0/24" in whitelist


def test_bannissement_applique_par_nftables(infra):
    sudo("cscli", "decisions", "add", "--ip", BANNED, "--duration", "10m", "--reason", "e2e")
    ruleset = ""
    for _ in range(30):  # le bouncer interroge la Local API toutes les 10 s
        ruleset = sudo("nft", "list", "ruleset")
        if BANNED in ruleset:
            break
        time.sleep(2)
    assert BANNED in ruleset
    # Les ports publiés par Docker passent par forward, pas par input
    assert "hook forward" in ruleset and "hook input" in ruleset

    previous, config.ANSIBLE_DIR = config.ANSIBLE_DIR, infra
    check_repo, runner.check_repo = runner.check_repo, _no_check
    try:
        fn = getattr(server.status, "fn", server.status)
        s = asyncio.run(fn(limit="cs"))["cs"]
    finally:
        config.ANSIBLE_DIR, runner.check_repo = previous, check_repo
    assert s["crowdsec"]["bans"] >= 1 and any(b["ip"] == BANNED for b in s["crowdsec"]["latest"]), s


async def _no_check():
    # Inventaire en connexion locale, refusé à juste titre par le lint d'un vrai repo.
    return None
