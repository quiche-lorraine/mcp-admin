"""Bout en bout de la mise à jour automatique sur un vrai swarm à un nœud (job
« stacks » de la CI ; sauté sans swarm local) : une image poussée à nouveau sous
le même tag est déployée, une image inchangée ne relance rien."""

import asyncio
import subprocess
import sys
import time

import pytest
import yaml

from infra_mcp import autoupdate, config, runner
from test_stacks_e2e import _swarm_active, docker

pytestmark = pytest.mark.skipif(not _swarm_active(), reason="pas de swarm local (docker swarm init)")

STACK = "e2eauto"
# Registre local sans authentification. 127.0.0.1 : Docker y accepte le HTTP.
REGISTRY = "127.0.0.1:5056"
TAG = f"{REGISTRY}/e2e/whoami:main"


async def _no_check():
    return None


def push(source: str) -> None:
    """Pousse source sous TAG."""
    if subprocess.run(["docker", "image", "inspect", source], capture_output=True).returncode:
        docker("pull", source)
    docker("tag", source, TAG)
    docker("push", TAG)


def image() -> str:
    return docker("service", "inspect", "--format", "{{.Spec.TaskTemplate.ContainerSpec.Image}}", f"{STACK}_web")


def tasks() -> set[str]:
    return set(docker("service", "ps", "-q", "--filter", "desired-state=running", f"{STACK}_web").split())


@pytest.fixture
def infra(tmp_path, monkeypatch):
    docker("run", "-d", "--name", "e2eauto-registry", "-p", f"{REGISTRY}:5000", "registry:2.8.3")
    root = tmp_path / "infra"
    (root / "inventory" / "group_vars" / "swarm_managers").mkdir(parents=True)
    (root / "ansible.cfg").write_text("[defaults]\ninventory = inventory/hosts.yml\n")
    (root / "inventory" / "hosts.yml").write_text(yaml.safe_dump({"all": {"children": {"swarm_managers": {"hosts": {
        "mgr": {"ansible_connection": "local", "ansible_python_interpreter": sys.executable}}}}}}))
    (root / "inventory" / "group_vars" / "swarm_managers" / "stacks.yml").write_text(yaml.safe_dump({
        "swarm_stacks": [{"name": STACK, "auto_update": ["web", "absent"]}]}))
    compose = root / "compose.yml"
    compose.write_text(yaml.safe_dump({"services": {"web": {"image": TAG, "deploy": {"replicas": 1}}}}))
    monkeypatch.setattr(config, "ANSIBLE_DIR", root)
    monkeypatch.setattr(runner, "check_repo", _no_check)
    try:
        yield compose
    finally:
        subprocess.run(["docker", "stack", "rm", STACK], capture_output=True)
        subprocess.run(["docker", "rm", "-f", "e2eauto-registry"], capture_output=True)
        subprocess.run(["docker", "image", "rm", TAG], capture_output=True)


def label() -> str:
    return docker("service", "inspect", "--format", '{{index .Spec.Labels "infra.autoupdate.image-id"}}', f"{STACK}_web")


def running_image() -> str:
    """Identifiant de l'image du conteneur en cours (swarm à un nœud)."""
    for _ in range(60):
        ids = docker("ps", "-q", "--filter", f"label=com.docker.swarm.service.name={STACK}_web").split()
        if len(ids) == 1:
            return docker("inspect", "--format", "{{.Image}}", ids[0])
        time.sleep(1)
    pytest.fail(docker("service", "ps", "--no-trunc", f"{STACK}_web"))


def test_mise_a_jour_automatique(infra):
    push("traefik/whoami:v1.10.3")
    # Déployé comme le fait apply : le tag seul.
    docker("stack", "deploy", "--detach=false", "--resolve-image", "never", "-c", str(infra), STACK)
    first = running_image()

    # 1. premier passage : l'image n'est pas notée sur le service, il est relancé une fois
    report = asyncio.run(autoupdate.run_once())
    assert report["updated"] == [f"{STACK}_web"] and report["failed"] == {}, report
    assert report["missing"] == [f"{STACK}_absent"], report
    assert label() == first == running_image()
    assert image() == TAG

    # 2. rien de nouveau : rien n'est relancé
    before = tasks()
    report = asyncio.run(autoupdate.run_once())
    assert report["updated"] == [] and report["failed"] == {}, report
    assert tasks() == before

    # 3. nouvelle image sous le même tag : déployée
    push("traefik/whoami:v1.10.1")
    report = asyncio.run(autoupdate.run_once())
    assert report["updated"] == [f"{STACK}_web"], report
    second = running_image()
    assert second != first and label() == second
    assert tasks() != before
