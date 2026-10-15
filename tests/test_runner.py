import asyncio
import os

import pytest
from fastmcp.exceptions import ToolError

from infra_mcp import config, runner


@pytest.mark.parametrize("limit", ["all", "srv-01", "prod", "srv-01,srv-02", "swarm_workers", "a.b.c"])
def test_limit_valide(limit):
    assert runner.check_limit(limit) == limit


@pytest.mark.parametrize(
    "limit",
    [
        "",
        "@hosts.txt",  # fichier de limite
        "srv-01 srv-02",
        "srv-01,",
        ",srv-01",
        "!prod",  # exclusion
        "prod:&swarm",  # intersection
        "~srv.*",  # regex
        "srv-*",  # joker
        "--list",  # serait lu comme une option
        "-x",
        "srv-01,-x",
        "srv-01;reboot",
        "srv-01\n",
    ],
)
def test_limit_refusee(limit):
    with pytest.raises(ToolError):
        runner.check_limit(limit)


def test_playbooks_du_repo():
    assert runner.list_playbooks() == ["site", "stacks", "swarm-join"]
    assert runner.playbook_path("site") == str(config.ANSIBLE_DIR / "playbooks" / "site.yml")


@pytest.mark.parametrize(
    "name", ["../site", "site.yml", "Site", "/etc/passwd", "-site", "", "inconnu", "../policy/locked"]
)
def test_playbook_refuse(name):
    with pytest.raises(ToolError):
        runner.playbook_path(name)


def test_playbook_lien_vers_l_exterieur_refuse(tmp_path, monkeypatch):
    (tmp_path / "playbooks").mkdir()
    dehors = tmp_path / "dehors.yml"
    dehors.write_text("- hosts: all\n")
    (tmp_path / "playbooks" / "piege.yml").symlink_to(dehors)
    monkeypatch.setattr(config, "ANSIBLE_DIR", tmp_path)
    with pytest.raises(ToolError):
        runner.playbook_path("piege")


def test_redact():
    data = {
        "ansible_host": "203.0.113.10",
        "db_password": "x",
        "API_TOKEN": "x",
        "vault_db": "x",
        "ansible_ssh_private_key_file": "/k",
        "users": [{"name": "renaud", "secret": {"a": 1}}],
        "nested": {"ok": [1, 2], "client_secret": "x"},
    }
    assert runner.redact(data) == {
        "ansible_host": "203.0.113.10",
        "db_password": "***",
        "API_TOKEN": "***",
        "vault_db": "***",
        "ansible_ssh_private_key_file": "***",
        "users": [{"name": "renaud", "secret": "***"}],
        "nested": {"ok": [1, 2], "client_secret": "***"},
    }


def test_recap():
    output = """
TASK [base : Paquets présents] *****
changed: [srv-02]

PLAY RECAP *********************************************************************
srv-01                     : ok=12   changed=0    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
srv-02                     : ok=10   changed=2    unreachable=0    failed=1    skipped=0    rescued=0    ignored=0
"""
    recap = runner.recap(output)
    assert recap["srv-01"]["changed"] == 0
    assert recap["srv-02"] == {
        "ok": 10, "changed": 2, "unreachable": 0, "failed": 1, "skipped": 0, "rescued": 0, "ignored": 0,
    }


def test_truncate_garde_la_fin(monkeypatch):
    monkeypatch.setattr(config, "MAX_OUTPUT", 10)
    assert runner._truncate("court") == "court"
    out = runner._truncate("a" * 15 + "FIN_RECAP!")
    assert out.endswith("FIN_RECAP!")
    assert "15 caractères tronqués" in out


async def test_run_timeout_tue_tout_le_groupe(tmp_path):
    # Comme ansible-playbook, le processus lance un enfant (worker, ssh…) qui
    # doit mourir avec lui.
    pidfile = tmp_path / "enfant.pid"
    with pytest.raises(ToolError, match="délai dépassé"):
        await runner.run("sh", "-c", f"sleep 30 & echo $! > {pidfile}; wait", timeout=0.5)
    child = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("le processus enfant a survécu au délai dépassé")


async def test_un_seul_apply_a_la_fois(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    async def fake_run(*args, timeout=None):
        started.set()
        await release.wait()
        return {"returncode": 0, "output": ""}

    monkeypatch.setattr(runner, "run", fake_run)
    first = asyncio.create_task(runner.playbook("site", "all", check=False))
    await started.wait()
    with pytest.raises(ToolError, match="déjà en cours"):
        await runner.playbook("site", "all", check=False)
    # plan n'est pas bloqué par un apply en cours
    release.set()
    assert (await runner.playbook("site", "all", check=True))["returncode"] == 0
    assert (await first)["returncode"] == 0


def test_executables_du_venv(monkeypatch, tmp_path):
    # Sous systemd le PATH ne contient pas le venv : on prend ansible-* à côté de
    # l'interpréteur, là où vivent la collection infra_mcp.core et ses dépendances.
    (tmp_path / "ansible-playbook").touch()
    monkeypatch.setattr(runner.sys, "executable", str(tmp_path / "python"))
    assert runner._bin("ansible-playbook") == str(tmp_path / "ansible-playbook")
    assert runner._bin("absent") == "absent"
