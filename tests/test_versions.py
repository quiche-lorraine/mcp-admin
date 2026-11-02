"""Ce qui tourne dans les stacks : filtre du playbook, lint de la clé versions,
services décrits et appel du serveur."""

import asyncio
import base64
import importlib.util
import json
import shutil

import pytest
import yaml
from fastmcp.exceptions import ToolError

from conftest import ANSIBLE_DIR
from infra_mcp import config, lint, maintenance, runner, versions
from infra_mcp.lint import COLLECTION_DIR

_spec = importlib.util.spec_from_file_location("versions_filter", COLLECTION_DIR / "plugins" / "filter" / "versions.py")
f = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(f)

IMAGE_ID = "sha256:" + "a" * 64
DIGEST = "sha256:" + "d" * 64
SERVICES = [{"service": "app_php", "argv": ["php", "-v"]}, {"service": "app_db", "argv": None},
            {"service": "app_gone", "argv": ["x"]}]


def inspect(cid):
    return {"item": cid, "rc": 0, "stdout": json.dumps([{
        "Image": IMAGE_ID, "Config": {"Image": f"ghcr.io/o/app:main@{DIGEST}"},
        "State": {"StartedAt": "2026-11-02T08:00:00Z"}}])}


IMAGE = {"item": IMAGE_ID, "rc": 0, "stdout": json.dumps([{
    "RepoDigests": [f"other/app@sha256:{'0' * 64}", f"ghcr.io/o/app@{DIGEST}"],
    "Config": {"Labels": {"org.opencontainers.image.revision": "abc123",
                          "org.opencontainers.image.source": "https://github.com/o/app",
                          "org.opencontainers.image.version": ""}}}])}


def test_rapport():
    ps = [{"item": SERVICES[0], "stdout_lines": ["c1"]}, {"item": SERVICES[1], "stdout_lines": ["c2"]},
          {"item": SERVICES[2], "stdout_lines": []}]
    execs = [{"item": SERVICES[0], "rc": 0, "stdout_lines": ["PHP 8.4.27 (cli)"], "stderr_lines": []}]
    report = f.versions_report(SERVICES, ps, [inspect("c1"), inspect("c2")], [IMAGE], execs)
    assert report["app_php"] == {
        "image": "ghcr.io/o/app:main", "image_id": IMAGE_ID[:19], "digest": DIGEST,
        "started": "2026-11-02T08:00:00Z", "revision": "abc123", "source": "https://github.com/o/app",
        "version": ["PHP 8.4.27 (cli)"]}
    assert report["app_db"]["version"] is None and report["app_db"]["revision"] == "abc123"
    assert report["app_gone"] == {"error": "aucun conteneur en cours sur ce nœud"}
    execs[0].update(rc=124)
    assert f.versions_report(SERVICES, ps, [inspect("c1")], [IMAGE], execs)["app_php"]["version_error"] == "délai dépassé"


def test_depot_de_l_image():
    assert f._repository("ghcr.io/o/app:main@sha256:x") == "ghcr.io/o/app"
    assert f._repository("localhost:5000/app:1") == "localhost:5000/app"
    assert f._repository("traefik:v3.7") == "traefik"


def test_lint():
    services = {"php": {}, "db": {}}
    assert lint._versions_violations({"php": ["php", "-v"], "db": False}, services) == []
    assert "obligatoire" in lint._versions_violations(None, services)[0]
    assert lint._versions_violations({"php": ["php", "-v"]}, services) == ["service(s) sans commande : db (une commande, ou false)"]
    assert "absent du compose" in lint._versions_violations({"php": False, "db": False, "x": False}, services)[0]
    assert lint._versions_violations({"php": True, "db": []}, services) == [
        "php : liste non vide de textes, ou false", "db : liste non vide de textes, ou false"]
    assert "shell" in lint._versions_violations({"php": ["/bin/sh", "-c", "php -v"], "db": False}, services)[0]
    assert "paramètre" in lint._versions_violations({"php": ["php", "{x}"], "db": ["{{ x }}"]}, services)[0]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "infra"
    shutil.copytree(ANSIBLE_DIR, root)
    path = root / "inventory" / "group_vars" / "swarm_managers" / "stacks.yml"
    data = yaml.safe_load(path.read_text())
    data["swarm_secrets"] = {"whoami_token": "x"}
    data["swarm_stacks"] = [{"name": "whoami", "secrets": ["whoami_token"], "versions": {"web": ["/whoami", "-h"]}}]
    path.write_text(yaml.safe_dump(data))
    monkeypatch.setattr(config, "ANSIBLE_DIR", root)

    async def no_check():
        return None
    monkeypatch.setattr(runner, "check_repo", no_check)
    return path


def test_lint_du_repo(repo):
    assert [p for p in lint.stacks_inventory_violations(repo.parents[3]) if "versions" in p] == []
    data = yaml.safe_load(repo.read_text())
    del data["swarm_stacks"][0]["versions"]
    repo.write_text(yaml.safe_dump(data))
    assert any("versions : obligatoire" in p for p in lint.stacks_inventory_violations(repo.parents[3]))


def test_services_decrits(repo):
    assert versions.targets(None) == [{"service": "whoami_web", "argv": ["/whoami", "-h"]}]
    assert versions.targets("whoami") == versions.targets(None)
    with pytest.raises(ToolError, match="stack inconnue"):
        versions.targets("absente")


def test_appel(repo, monkeypatch):
    calls = []

    async def fake_run(name, extra, **kw):
        calls.append((name, json.loads(base64.b64decode(extra["maintenance_services_b64"])), kw))
        return {"mgr": {"report": {"whoami_web": {"image": "traefik/whoami:v1", "version": ["Usage", "token=abc"]}}}}
    monkeypatch.setattr(maintenance, "run", fake_run)
    report = asyncio.run(versions.run("whoami"))
    assert calls == [("stack_versions", [{"service": "whoami_web", "argv": ["/whoami", "-h"]}], {})]
    assert report["whoami_web"]["version"].startswith("Usage\n") and "abc" not in report["whoami_web"]["version"]


def test_echec_d_ansible(repo, monkeypatch):
    async def broken(*a, **k):
        return {"mgr": {"error": "UNREACHABLE"}}
    monkeypatch.setattr(maintenance, "run", broken)
    with pytest.raises(ToolError, match="UNREACHABLE"):
        asyncio.run(versions.run(None))
