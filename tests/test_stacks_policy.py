"""Politique de sécurité des stacks (ansible/stacks/*/compose.yml).

Merger une stack revient à lancer des conteneurs sur les nœuds du swarm : un
conteneur privilégié ou qui monte / ou le socket Docker vaut root sur l'hôte.
Ces options sont refusées, sauf exception déclarée sur le service avec sa raison :

    x-allow:
      docker-socket: "Portainer doit piloter Docker"
"""

from pathlib import Path

import pytest
import yaml

from conftest import ANSIBLE_DIR

STACKS_DIR = ANSIBLE_DIR / "stacks"
STACKS_VARS = ANSIBLE_DIR / "inventory" / "group_vars" / "swarm_managers" / "stacks.yml"

EXCEPTIONS = {
    "privileged", "host-network", "host-pid", "host-ipc", "cap-add", "devices",
    "security-opt", "docker-socket", "host-path",
}
DOCKER_SOCKETS = {"/var/run/docker.sock", "/run/docker.sock"}
SENSITIVE = ("/etc", "/root", "/home", "/proc", "/sys", "/boot", "/dev", "/run", "/var/run", "/var/lib/docker")
HARMLESS = {"/etc/localtime", "/etc/timezone"}  # lecture seule, courants pour l'heure


def _bind_sources(service):
    for volume in service.get("volumes", []) or []:
        if isinstance(volume, str):
            parts = volume.split(":")
            if len(parts) >= 2 and parts[0].startswith(("/", ".", "~")):
                yield parts[0], "ro" in parts[2:3]
        elif isinstance(volume, dict) and volume.get("type") == "bind":
            yield volume.get("source", ""), bool(volume.get("read_only"))


def _image_problem(image):
    if not image:
        return "image manquante"
    if "@sha256:" in image:
        return None
    name = image.rsplit("/", 1)[-1]
    if ":" not in name:
        return f"image sans tag : {image} (tag explicite obligatoire)"
    if name.endswith(":latest"):
        return f"image en latest : {image} (tag explicite obligatoire)"
    return None


def violations(compose: dict) -> list[str]:
    found = []
    services = compose.get("services") or {}
    if not services:
        found.append("aucun service")
    for name, svc in services.items():
        allowed = svc.get("x-allow") or {}
        if not isinstance(allowed, dict) or not all(isinstance(r, str) and r.strip() for r in allowed.values()):
            found.append(f"{name} : x-allow doit associer chaque exception à sa raison")
            allowed = {}
        for rule in set(allowed) - EXCEPTIONS:
            found.append(f"{name} : exception inconnue {rule} (connues : {', '.join(sorted(EXCEPTIONS))})")

        def check(rule, condition, message):
            if condition and rule not in allowed:
                found.append(f"{name} : {message} (exception : x-allow: {{{rule}: \"raison\"}})")

        if problem := _image_problem(svc.get("image")):
            found.append(f"{name} : {problem}")
        networks = svc.get("networks") or []
        check("privileged", svc.get("privileged") is True, "privileged")
        check("host-network", svc.get("network_mode") == "host" or "host" in networks, "réseau de l'hôte")
        check("host-pid", svc.get("pid") == "host", "pid de l'hôte")
        check("host-ipc", svc.get("ipc") == "host", "ipc de l'hôte")
        check("cap-add", bool(svc.get("cap_add")), "cap_add")
        check("devices", bool(svc.get("devices")), "devices")
        check("security-opt", any("unconfined" in str(o) for o in svc.get("security_opt") or []), "security_opt unconfined")
        for source, read_only in _bind_sources(svc):
            path = source.rstrip("/") or "/"
            if not path.startswith("/"):
                found.append(f"{name} : montage relatif {source} (le compose est déployé hors du repo : volume nommé ou chemin absolu)")
            elif path in DOCKER_SOCKETS:
                check("docker-socket", True, f"montage du socket Docker {source}")
            elif path in HARMLESS and read_only:
                continue
            elif path == "/" or path.startswith(SENSITIVE):
                check("host-path", True, f"montage d'un chemin sensible de l'hôte {source}")
        labels = (svc.get("deploy") or {}).get("labels")
        if labels is not None and not isinstance(labels, dict):
            found.append(f"{name} : deploy.labels doit être un mapping (le playbook y ajoute infra.stack-hash)")
    for kind in ("secrets", "configs"):
        for key, definition in (compose.get(kind) or {}).items():
            if kind == "secrets" and not (definition or {}).get("external"):
                found.append(f"secret {key} : doit être external (créé par le playbook depuis le vault)")
            if kind == "configs" and "file" in (definition or {}):
                found.append(f"config {key} : file: n'est pas encore pris en charge (le compose est déployé hors du repo)")
    return found


COMPOSE_FILES = sorted(STACKS_DIR.glob("*/compose.yml"))


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.parent.name)
def test_stacks_du_repo(path):
    problems = violations(yaml.safe_load(path.read_text()))
    assert not problems, "\n".join(problems)


def test_inventaire_coherent_avec_les_stacks():
    config = yaml.safe_load(STACKS_VARS.read_text()) or {}
    declared = set((config.get("swarm_secrets") or {}).keys())
    for stack in config.get("swarm_stacks") or []:
        compose_path = STACKS_DIR / stack["name"] / "compose.yml"
        assert compose_path.is_file(), f"{compose_path} absent"
        compose = yaml.safe_load(compose_path.read_text())
        used = set((compose.get("secrets") or {}).keys())
        listed = set(stack.get("secrets") or [])
        assert used <= listed, f"{stack['name']} : secrets du compose absents de swarm_stacks : {used - listed}"
        assert listed <= declared, f"{stack['name']} : secrets absents de swarm_secrets : {listed - declared}"
    assert not set(s["name"] for s in config.get("swarm_stacks") or []) & set(config.get("swarm_stacks_absent") or [])


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
