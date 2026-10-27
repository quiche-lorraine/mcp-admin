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
    assert "secrets absents de swarm_secrets et swarm_secrets_generated : ['api']" in problems


def test_secret_genere_accepte(tmp_path):
    vars_dir = tmp_path / "inventory" / "group_vars" / "swarm_managers"
    vars_dir.mkdir(parents=True)
    (tmp_path / "stacks" / "app").mkdir(parents=True)
    (tmp_path / "stacks" / "app" / "compose.yml").write_text(yaml.safe_dump(
        {"services": {"web": {"image": "nginx:1.27.2"}}, "secrets": {"key": {"external": True}}}))
    (vars_dir / "stacks.yml").write_text(yaml.safe_dump({
        "swarm_stacks": [{"name": "app", "secrets": ["key"]}],
        "swarm_secrets": {},
        "swarm_secrets_generated": ["key"],
    }))
    assert lint.stacks_inventory_violations(tmp_path) == []


def test_reseaux_partages_mal_nommes(tmp_path):
    vars_dir = tmp_path / "inventory" / "group_vars" / "swarm_managers"
    vars_dir.mkdir(parents=True)
    (vars_dir / "stacks.yml").write_text(yaml.safe_dump({
        "swarm_stacks": [], "swarm_secrets": {}, "swarm_networks": ["proxy", "Proxy", "ingress", "a b", "proxy\n"],
    }))
    problems = lint.stacks_inventory_violations(tmp_path)
    assert problems == [f"swarm_networks : nom invalide ou réservé : {n!r}" for n in ("Proxy", "ingress", "a b", "proxy\n")]
    (vars_dir / "stacks.yml").write_text(yaml.safe_dump({"swarm_stacks": [], "swarm_networks": "proxy"}))
    assert lint.stacks_inventory_violations(tmp_path) == ["swarm_networks : liste de noms de réseaux attendue"]


def test_reseau_partage_accepte_par_lint(tmp_path):
    """run() passe swarm_networks à la politique des compose."""
    vars_dir = tmp_path / "inventory" / "group_vars" / "swarm_managers"
    vars_dir.mkdir(parents=True)
    (tmp_path / "stacks" / "app").mkdir(parents=True)
    compose = {**_svc(networks=["proxy"]), "networks": {"proxy": {"external": True}}}
    (tmp_path / "stacks" / "app" / "compose.yml").write_text(yaml.safe_dump(compose))
    config = {"swarm_stacks": [{"name": "app"}], "swarm_secrets": {}}
    (vars_dir / "stacks.yml").write_text(yaml.safe_dump(config))
    assert any("external interdit" in p for p in lint.run(tmp_path))
    (vars_dir / "stacks.yml").write_text(yaml.safe_dump({**config, "swarm_networks": ["proxy"]}))
    assert not any("réseau" in p for p in lint.run(tmp_path)), lint.run(tmp_path)


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
        # Contournements vérifiés sur un vrai swarm pendant la revue de sécurité
        (_svc(volumes=["${HOME}:/data"]), "interpolation interdite"),
        (_svc(environment={"X": "$HOME"}), "interpolation interdite"),
        ({**_svc(volumes=["etc:/host-etc"]),
          "volumes": {"etc": {"driver": "local", "driver_opts": {"type": "none", "o": "bind", "device": "/etc"}}}},
         "chemin sensible de l'hôte /etc"),
        ({**_svc(volumes=[{"type": "volume", "source": "s", "target": "/s"}]),
          "volumes": {"s": {"driver_opts": {"o": "bind", "device": "/var/run/docker.sock"}}}},
         "socket Docker"),
        # Seuls les réseaux de swarm_networks (ici proxy) peuvent être external
        ({**_svc(networks=["back"]), "networks": {"back": {"external": True}}}, "external interdit"),
        # Le réseau désigné doit être celui de swarm_networks, sous le nom de sa clé
        ({**_svc(networks=["proxy"]), "networks": {"proxy": {"external": True, "name": "autre"}}}, "sans name"),
        ({**_svc(networks=["proxy"]), "networks": {"proxy": {"external": {"name": "autre"}}}}, "sans name"),
    ],
)
def test_violations_detectees(compose, expected):
    found = violations(compose, shared_networks=["proxy"])
    assert any(expected in v for v in found), found


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
        _svc(command=["sh", "-c", "echo $$HOME"]),  # $$ : un $ littéral, pas d'interpolation
        {**_svc(volumes=["data:/data"]),  # volume NFS : driver_opts sans montage de l'hôte
         "volumes": {"data": {"driver_opts": {"type": "nfs", "o": "addr=10.0.0.5,rw", "device": ":/export"}}}},
        {**_svc(networks=["back"]), "networks": {"back": {"driver": "overlay"}}},
        {**_svc(networks=["back"]), "networks": {"back": {"external": False}}},
        {**_svc(networks=["proxy"]), "networks": {"proxy": {"external": True}}},  # réseau partagé
    ],
)
def test_configurations_acceptees(compose):
    assert violations(compose, shared_networks=["proxy"]) == []
