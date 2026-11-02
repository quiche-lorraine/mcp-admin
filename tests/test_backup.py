"""Sauvegarde des stacks : filtres des playbooks, lint de la clé backup, plan et
appels du serveur, planification."""

import asyncio
import base64
import importlib.util
import json
import shutil
from datetime import datetime

import pytest
import yaml
from fastmcp.exceptions import ToolError

from conftest import ANSIBLE_DIR
from infra_mcp import backup, config, lint, maintenance, runner
from infra_mcp.lint import COLLECTION_DIR

_spec = importlib.util.spec_from_file_location("backup_filter", COLLECTION_DIR / "plugins" / "filter" / "backup.py")
f = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(f)

PLAN = {"dumps": [{"stack": "app", "service": "db", "argv": ["pg_dump", "-Fc"]},
                  {"stack": "app", "service": "gone", "argv": ["x"]}],
        "volumes": [{"stack": "app", "volume": "app_files"}, {"stack": "app", "volume": "app_absent"}]}
SUMMARY = json.dumps({"message_type": "summary", "snapshot_id": "0123456789abcdef", "total_bytes_processed": 42,
                      "data_added": 7})


def items():
    ps = [{"item": PLAN["dumps"][0], "stdout_lines": ["c1"]}, {"item": PLAN["dumps"][1], "stdout_lines": []}]
    vols = [{"item": PLAN["volumes"][0], "rc": 0, "stdout": json.dumps([{"Mountpoint": "/var/lib/docker/volumes/app_files/_data"}])},
            {"item": PLAN["volumes"][1], "rc": 1, "stdout": ""}]
    return f.backup_items(PLAN, ps, vols)


def test_plan_du_playbook():
    assert items() == [
        {"stack": "app", "kind": "dump", "name": "db", "argv": ["pg_dump", "-Fc"], "filename": "app/db.dump", "container": "c1"},
        {"stack": "app", "kind": "dump", "name": "gone", "argv": ["x"], "filename": "app/gone.dump", "error": "aucun conteneur en cours"},
        {"stack": "app", "kind": "volume", "name": "app_files", "path": "/var/lib/docker/volumes/app_files/_data"},
        {"stack": "app", "kind": "volume", "name": "app_absent", "error": "volume introuvable"},
    ]


def test_resultats_et_etat():
    its = items()
    dumps = [{"item": its[0], "rc": 0, "stdout": '{"message_type":"status"}\n' + SUMMARY}]
    copies = [{"item": its[2], "rc": 1, "stderr": "Fatal: unable to open repository"}]
    report = f.backup_results(its, dumps, copies)["app"]
    assert report["ok"] is False
    assert report["items"]["dump:db"] == {"snapshot": "01234567", "bytes": 42, "added_bytes": 7}
    assert report["items"]["volume:app_files"] == {"error": "Fatal: unable to open repository"}
    assert report["items"]["dump:gone"] == {"error": "aucun conteneur en cours"}

    ok = {"app": {"ok": True, "items": {"dump:db": {"snapshot": "x"}}}}
    status = f.backup_status("", ok, "2026-11-02T03:00:00+00:00")
    assert status["stacks"]["app"] == {"last_run": "2026-11-02T03:00:00+00:00", "ok": True, "errors": {},
                                       "last_success": "2026-11-02T03:00:00+00:00"}
    # Un échec garde la dernière réussite ; le check est noté.
    status = f.backup_status(json.dumps(status), {"app": report}, "2026-11-03T03:00:00+00:00", {"rc": 1, "stderr": "bad pack"})
    assert status["stacks"]["app"]["last_success"] == "2026-11-02T03:00:00+00:00" and status["stacks"]["app"]["ok"] is False
    assert status["check"] == {"at": "2026-11-03T03:00:00+00:00", "ok": False, "error": "bad pack"}
    assert f.backup_status("pas du json", ok, "t")["stacks"]["app"]["ok"] is True


def test_alertes():
    now = "2026-11-03T10:00:00+00:00"
    assert f.backup_alerts(False, [], "", 26, now) == []
    assert f.backup_alerts(False, ["app"], "", 26, now) == ["sauvegardes : backup_repository non défini, aucune stack n'est sauvegardée"]
    assert f.backup_alerts(True, ["app"], "", 26, now) == ["stack app : aucune sauvegarde réussie"]
    fresh = json.dumps({"stacks": {"app": {"ok": True, "last_success": "2026-11-03T03:00:00+00:00"}}, "check": {"ok": True}})
    assert f.backup_alerts(True, ["app"], fresh, 26, now) == []
    old = json.dumps({"stacks": {"app": {"ok": False, "errors": {"dump:db": "x"}, "last_success": "2026-11-01T03:00:00+00:00"}},
                      "check": {"ok": False, "error": "bad pack"}})
    assert f.backup_alerts(True, ["app"], old, 26, now) == [
        "stack app : dernière sauvegarde réussie il y a 55 h",
        "stack app : dernière sauvegarde en échec (dump:db)",
        "sauvegardes : restic check en échec : bad pack"]


def test_stacks_attendues_et_instantanes():
    stacks = [{"name": "a", "backup": {"dumps": {"db": ["x"]}}}, {"name": "b", "backup": {}}, {"name": "c"}]
    assert f.backup_expected(stacks) == ["a"]
    snaps = [{"time": "2026-11-01T03:00:00Z", "short_id": "aa", "tags": ["app", "dump:db"], "summary": {"total_bytes_processed": 1}},
             {"time": "2026-11-02T03:00:00Z", "short_id": "bb", "tags": ["app", "dump:db"], "summary": {"total_bytes_processed": 2}},
             {"time": "2026-11-02T03:00:00Z", "short_id": "cc", "tags": ["app", "volume:app_files"]},
             {"time": "2026-11-02T03:00:00Z", "short_id": "dd", "tags": ["autre"]}]
    assert f.backup_snapshots(json.dumps(snaps)) == {"app": {
        "dump:db": {"count": 2, "latest": "2026-11-02T03:00:00Z", "snapshot": "bb", "bytes": 2},
        "volume:app_files": {"count": 1, "latest": "2026-11-02T03:00:00Z", "snapshot": "cc", "bytes": None}}}
    assert f.backup_snapshots("pas du json") == {}


def test_lint():
    compose = {"services": {"db": {}, "web": {}}, "volumes": {"db_data": {}, "files": {}}}
    ok = {"dumps": {"db": ["pg_dump", "-Fc"]}, "volumes": ["files"], "exclude": {"db_data": "couvert par le dump"}}
    assert lint._backup_violations(ok, compose) == []
    assert lint._backup_violations({}, {"services": {"web": {}}}) == []
    assert "obligatoire" in lint._backup_violations(None, compose)[0]
    assert lint._backup_violations({"volumes": ["files"]}, compose) == ["volume(s) ni copiés ni écartés : db_data"]
    assert lint._backup_violations({"volumes": ["files", "x"], "exclude": {"db_data": "", "files": "y"}}, compose) == [
        "volumes : 'x' absent des volumes du compose",
        "exclude : db_data : raison attendue (texte)",
        "volume files : à la fois copié et écarté"]
    assert lint._backup_violations({"dumps": {"cache": ["sh", "-c", "x"]}, "exclude": {"db_data": "a", "files": "b"}}, compose) == [
        "dumps : service 'cache' absent du compose",
        "dumps : cache : 'sh' ne peut pas lancer la commande (shell)"]
    assert "clé inconnue" in lint._backup_violations({"volume": []}, compose)[0]


def test_vault_dans_backup_credentials(tmp_path):
    vars_dir = tmp_path / "inventory" / "group_vars" / "swarm_managers"
    vars_dir.mkdir(parents=True)
    (vars_dir / "backup.yml").write_text(yaml.safe_dump({
        "backup_repository": "s3:https://s3.sbg.io.cloud.ovh.net/b",
        "backup_credentials": {"password": "{{ vault_backup_password }}"},
        "ailleurs": "{{ vault_backup_password }}"}))
    problems = lint.data_only_violations(tmp_path)
    assert len(problems) == 1 and "'{{ vault_backup_password }}' : vault_* n'est utilisable" in problems[0], problems


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "infra"
    shutil.copytree(ANSIBLE_DIR, root)
    compose = root / "stacks" / "whoami" / "compose.yml"
    data = yaml.safe_load(compose.read_text())
    data["volumes"] = {"files": {}, "named": {"name": "shared-files"}, "cache": {}}
    compose.write_text(yaml.safe_dump(data))
    path = root / "inventory" / "group_vars" / "swarm_managers" / "stacks.yml"
    data = yaml.safe_load(path.read_text())
    data["swarm_secrets"] = {"whoami_token": "x"}
    data["swarm_stacks"] = [{"name": "whoami", "secrets": ["whoami_token"], "versions": {"web": False},
                             "backup": {"dumps": {"web": ["/whoami", "-h"]}, "volumes": ["files", "named"],
                                        "exclude": {"cache": "régénéré"}}}]
    path.write_text(yaml.safe_dump(data))
    monkeypatch.setattr(config, "ANSIBLE_DIR", root)
    monkeypatch.setattr(config, "AUDIT_LOG", tmp_path / "audit.jsonl")

    async def no_check():
        return None
    monkeypatch.setattr(runner, "check_repo", no_check)
    return root


def test_plan(repo):
    assert lint.stacks_inventory_violations(repo) == []
    assert backup.plan(None) == {
        "dumps": [{"stack": "whoami", "service": "web", "argv": ["/whoami", "-h"]}],
        "volumes": [{"stack": "whoami", "volume": "whoami_files"}, {"stack": "whoami", "volume": "shared-files"}]}
    with pytest.raises(ToolError, match="stack inconnue"):
        backup.plan("absente")


def test_appels(repo, monkeypatch):
    calls = []

    async def fake_run(name, extra, **kw):
        calls.append((name, kw))
        if name == "backup":
            assert json.loads(base64.b64decode(extra["maintenance_plan_b64"])) == backup.plan(None)
            assert extra["maintenance_check"] is True
            return {"mgr": {"report": {"stacks": {"whoami": {"ok": True, "items": {}}}, "forget": "ok", "check": "ok"}}}
        return {"mgr": {"report": {"configured": True, "error": "password=hunter2 refusé", "snapshots": {}}}}
    monkeypatch.setattr(maintenance, "run", fake_run)
    assert asyncio.run(backup.run("whoami", check=True))["stacks"]["whoami"]["ok"] is True
    assert "hunter2" not in asyncio.run(backup.snapshots())["error"]
    assert calls == [("backup", {"write": True}), ("backup_list", {})]


def test_passage_planifie(repo, monkeypatch, capsys):
    async def failed(name, extra, **kw):
        return {"mgr": {"report": {"stacks": {"whoami": {"ok": False, "items": {"dump:web": {"error": "boom"}}}},
                                   "check": "ok"}}}
    monkeypatch.setattr(maintenance, "run", failed)
    asyncio.run(backup._pass())
    assert "ERROR whoami : sauvegarde en échec : {'dump:web': 'boom'}" in capsys.readouterr().err
    assert json.loads(config.AUDIT_LOG.read_text().splitlines()[-1])["status"] == "error"

    async def unconfigured(name, extra, **kw):
        return {"mgr": {"error": "« Dépôt configuré » : sauvegardes non configurées : backup_repository…"}}
    monkeypatch.setattr(maintenance, "run", unconfigured)
    asyncio.run(backup._pass())
    assert capsys.readouterr().err == ""


def test_prochaine_heure():
    now = datetime(2026, 11, 2, 2, 30)
    assert backup.next_run(now, "03:00") == datetime(2026, 11, 2, 3, 0)
    assert backup.next_run(datetime(2026, 11, 2, 3, 0), "03:00") == datetime(2026, 11, 3, 3, 0)


def test_planification_coupee(monkeypatch):
    monkeypatch.setattr(config, "BACKUP_AT", "")

    async def go():
        return backup.start()
    assert asyncio.run(go()) is None
