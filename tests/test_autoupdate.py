"""Mise à jour automatique des images : filtres du playbook, lint de la clé
auto_update, passage lancé par le serveur."""

import asyncio
import importlib.util
import json
import shutil

import pytest
import yaml
from fastmcp.exceptions import ToolError

from conftest import ANSIBLE_DIR
from infra_mcp import autoupdate, config, lint, maintenance
from infra_mcp.lint import COLLECTION_DIR

_spec = importlib.util.spec_from_file_location("autoupdate_filter", COLLECTION_DIR / "plugins" / "filter" / "autoupdate.py")
f = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(f)

LABEL = "infra.autoupdate.image-id"
NEW = "sha256:" + "b" * 64


def spec(image, deployed=None):
    return json.dumps({"TaskTemplate": {"ContainerSpec": {"Image": image}},
                       "Labels": {LABEL: deployed} if deployed else {}})


def test_services_suivis():
    stacks = [{"name": "app", "auto_update": ["php", "worker"]}, {"name": "traefik"}, {"name": "x", "auto_update": []}]
    assert f.autoupdate_services(stacks) == ["app_php", "app_worker"]
    assert f.autoupdate_services(None) == []


def test_cibles():
    inspect = [{"item": "app_php", "rc": 0, "stdout": spec("ghcr.io/o/app:main@sha256:aaa", "sha256:old")},
               {"item": "app_worker", "rc": 0, "stdout": spec("ghcr.io/o/app:main")},
               {"item": "app_gone", "rc": 1, "stdout": ""}]
    assert f.autoupdate_targets(inspect) == [
        {"service": "app_php", "image": "ghcr.io/o/app:main", "deployed": "sha256:old"},
        {"service": "app_worker", "image": "ghcr.io/o/app:main", "deployed": None},
    ]


def test_rapport():
    inspect = [{"item": "app_php", "rc": 0}, {"item": "app_worker", "rc": 0}, {"item": "app_cron", "rc": 0},
               {"item": "app_gone", "rc": 1}]
    targets = [{"service": "app_php", "image": "o/app:main", "deployed": "sha256:old"},
               {"service": "app_worker", "image": "o/app:main", "deployed": NEW},
               {"service": "app_cron", "image": "o/cron:main", "deployed": None}]
    pulls = [{"item": "o/app:main", "rc": 0}, {"item": "o/cron:main", "rc": 1, "stderr": "manifest unknown"}]
    ids = [{"item": pulls[0], "rc": 0, "stdout": NEW}]
    update = [{"item": targets[0], "rc": 1, "stderr": "update rolled back"}, {"item": targets[1], "skipped": True},
              {"item": targets[2], "skipped": True}]
    report = f.autoupdate_report(inspect, targets, pulls, ids, update)
    assert report["updated"] == []
    assert report["failed"] == {"app_php": "update rolled back", "app_cron": "manifest unknown"}
    assert report["missing"] == ["app_gone"]
    assert report["images"]["app_worker"] == f"o/app:main ({NEW[:19]})"
    update[0] = {"item": targets[0], "rc": 0}
    assert f.autoupdate_report(inspect, targets, pulls, ids, update)["updated"] == ["app_php"]


def test_lint_de_la_cle_auto_update():
    services = {"php": {"image": "ghcr.io/o/app:main"}, "db": {"image": "pg@sha256:abc"}}
    assert lint._auto_update_violations(None, services) == []
    assert lint._auto_update_violations(["php"], services) == []
    assert lint._auto_update_violations("php", services) == ["liste de services attendue"]
    assert lint._auto_update_violations(["web"], services) == ["service 'web' absent du compose"]
    assert "figée par digest" in lint._auto_update_violations(["db"], services)[0]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "infra"
    shutil.copytree(ANSIBLE_DIR, root)
    path = root / "inventory" / "group_vars" / "swarm_managers" / "stacks.yml"
    data = yaml.safe_load(path.read_text())
    data["swarm_secrets"] = {"whoami_token": "x"}
    data["swarm_stacks"] = [{"name": "whoami", "secrets": ["whoami_token"]}]
    path.write_text(yaml.safe_dump(data))
    monkeypatch.setattr(config, "ANSIBLE_DIR", root)
    monkeypatch.setattr(config, "AUDIT_LOG", tmp_path / "audit.jsonl")
    return root


def _track(repo, services):
    path = repo / "inventory" / "group_vars" / "swarm_managers" / "stacks.yml"
    data = yaml.safe_load(path.read_text())
    data["swarm_stacks"][0]["auto_update"] = services
    path.write_text(yaml.safe_dump(data))


def test_lint_du_repo(repo):
    _track(repo, ["web"])
    assert [p for p in lint.stacks_inventory_violations(repo) if "auto_update" in p] == []
    _track(repo, ["absent"])
    assert "stack whoami : auto_update : service 'absent' absent du compose" in lint.stacks_inventory_violations(repo)


def test_rien_a_suivre_pas_d_ansible(repo, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("Ansible lancé sans stack suivie")
    monkeypatch.setattr(maintenance, "run", boom)
    assert asyncio.run(autoupdate.run_once()) is None


def test_passage(repo, monkeypatch, capsys):
    _track(repo, ["web"])
    calls = []

    async def fake_run(name, extra, **kw):
        calls.append((name, kw))
        return {"mgr": {"report": {"updated": ["whoami_web"], "failed": {"whoami_x": "boom"},
                                   "missing": [], "images": {"whoami_web": NEW}}}}
    monkeypatch.setattr(maintenance, "run", fake_run)
    asyncio.run(autoupdate._pass())
    assert calls == [("stack_autoupdate", {"write": True})]
    err = capsys.readouterr().err
    assert f"whoami_web mis à jour : {NEW}" in err
    assert "ERROR whoami_x : mise à jour en échec : boom" in err
    entry = json.loads(config.AUDIT_LOG.read_text().splitlines()[-1])
    assert entry["client"] == "auto_update" and entry["status"] == "error"
    assert entry["result"]["updated"] == ["whoami_web"]


def test_passage_sans_changement_silencieux(repo, monkeypatch, capsys):
    _track(repo, ["web"])

    async def fake_run(name, extra, **kw):
        return {"mgr": {"report": {"updated": [], "failed": {}, "missing": [], "images": {"whoami_web": NEW}}}}
    monkeypatch.setattr(maintenance, "run", fake_run)
    asyncio.run(autoupdate._pass())
    assert capsys.readouterr().err == ""
    assert not config.AUDIT_LOG.exists()


def test_passage_reporte_pendant_un_apply(repo, monkeypatch, capsys):
    _track(repo, ["web"])

    async def busy(*a, **k):
        raise ToolError("une action est déjà en cours, réessaie plus tard")
    monkeypatch.setattr(maintenance, "run", busy)
    asyncio.run(autoupdate._pass())
    assert capsys.readouterr().err == ""


def test_echec_d_ansible_journalise(repo, monkeypatch, capsys):
    _track(repo, ["web"])

    async def broken(*a, **k):
        return {"mgr": {"error": "UNREACHABLE"}}
    monkeypatch.setattr(maintenance, "run", broken)
    asyncio.run(autoupdate._pass())
    assert "ERROR UNREACHABLE" in capsys.readouterr().err


def test_boucle_coupee_a_zero(monkeypatch):
    monkeypatch.setattr(config, "AUTOUPDATE_INTERVAL", 0)

    async def go():
        return autoupdate.start()
    assert asyncio.run(go()) is None


def test_boucle_demarree_une_fois(monkeypatch):
    monkeypatch.setattr(config, "AUTOUPDATE_INTERVAL", 3600)
    monkeypatch.setattr(autoupdate, "_task", None)

    async def go():
        first, second = autoupdate.start(), autoupdate.start()
        assert first is second and not first.done()
        first.cancel()
    asyncio.run(go())
