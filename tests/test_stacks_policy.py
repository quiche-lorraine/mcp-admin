"""Politique de sécurité des stacks (stacks/*/compose.yml d’un repo de config).

Merger une stack revient à lancer des conteneurs sur les nœuds du swarm : un
conteneur privilégié ou qui monte / ou le socket Docker vaut root sur l'hôte.
Ces options sont refusées, sauf exception déclarée sur le service avec sa raison :

    x-allow:
      docker-socket: "Portainer doit piloter Docker"
"""

import pytest
import yaml

from conftest import ANSIBLE_DIR
from infra_mcp import lint
from infra_mcp.lint import compose_violations as violations

STACKS_DIR = ANSIBLE_DIR / "stacks"


COMPOSE_FILES = sorted(STACKS_DIR.glob("*/compose.yml"))


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.parent.name)
def test_stacks_du_repo(path):
    problems = violations(yaml.safe_load(path.read_text()))
    assert not problems, "\n".join(problems)


def test_inventaire_coherent_avec_les_stacks():
    assert lint.stacks_inventory_violations(ANSIBLE_DIR) == []


def test_incoherences_d_inventaire_detectees(tmp_path):
    vars_dir = tmp_path / "inventory" / "group_vars" / "swarm_managers"
    vars_dir.mkdir(parents=True)
    (tmp_path / "stacks" / "app").mkdir(parents=True)
    (tmp_path / "stacks" / "app" / "compose.yml").write_text(yaml.safe_dump(
        {"services": {"web": {"image": "nginx:1.27.2"}}, "secrets": {"db": {"external": True}}}))
    (vars_dir / "stacks.yml").write_text(yaml.safe_dump({
        "swarm_stacks": [{"name": "app", "secrets": ["api"]}, {"name": "absente"}],
        "swarm_stacks_absent": ["app"],
        "swarm_secrets": {},
    }))
    problems = "\n".join(lint.stacks_inventory_violations(tmp_path))
    assert "app : dans swarm_stacks et swarm_stacks_absent" in problems
    assert "absente" in problems and "absent" in problems
    assert "secrets du compose absents de swarm_stacks : ['db']" in problems
    assert "secrets absents de swarm_secrets : ['api']" in problems


def _svc(**kwargs):
    return {"services": {"app": {"image": "nginx:1.27.2", **kwargs}}}


@pytest.mark.parametrize(
    "compose, expected",
    [
        (_svc(image="nginx"), "image sans tag"),
        (_svc(image="nginx:latest"), "latest"),
        (_svc(image="registry:5000/nginx"), "image sans tag"),
        (_svc(privileged=True), "privileged"),
        (_svc(network_mode="host"), "réseau de l'hôte"),
        (_svc(networks=["host"]), "réseau de l'hôte"),
        (_svc(pid="host"), "pid de l'hôte"),
        (_svc(cap_add=["NET_ADMIN"]), "cap_add"),
        (_svc(devices=["/dev/fuse"]), "devices"),
        (_svc(security_opt=["apparmor:unconfined"]), "unconfined"),
        (_svc(volumes=["/var/run/docker.sock:/var/run/docker.sock"]), "socket Docker"),
        (_svc(volumes=[{"type": "bind", "source": "/", "target": "/host"}]), "chemin sensible"),
        (_svc(volumes=["/etc:/host-etc:ro"]), "chemin sensible"),
        (_svc(volumes=["/etc/localtime:/etc/localtime"]), "chemin sensible"),  # en écriture
        (_svc(volumes=["./data:/data"]), "montage relatif"),
        (_svc(deploy={"labels": ["a=b"]}), "deploy.labels doit être un mapping"),
        (_svc(**{"x-allow": {"privileged": ""}}), "chaque exception à sa raison"),
        (_svc(**{"x-allow": {"root": "parce que"}}), "exception inconnue"),
        ({**_svc(), "secrets": {"s": {"file": "./s.txt"}}}, "doit être external"),
        ({**_svc(), "configs": {"c": {"file": "./nginx.conf"}}}, "pas encore pris en charge"),
    ],
)
def test_violations_detectees(compose, expected):
    assert any(expected in v for v in violations(compose)), violations(compose)


@pytest.mark.parametrize(
    "compose",
    [
        _svc(),
        _svc(image="nginx@sha256:" + "a" * 64),
        _svc(image="registry.example.fr:5000/team/app:2.1"),
        _svc(volumes=["data:/data", "/etc/localtime:/etc/localtime:ro", "/srv/app:/app"]),
        _svc(privileged=True, **{"x-allow": {"privileged": "agent de monitoring noyau"}}),
        _svc(volumes=["/var/run/docker.sock:/var/run/docker.sock:ro"], **{"x-allow": {"docker-socket": "portainer"}}),
        _svc(deploy={"labels": {"a": "b"}}),
        {**_svc(), "secrets": {"s": {"external": True}}},
    ],
)
def test_configurations_acceptees(compose):
    assert violations(compose) == []
