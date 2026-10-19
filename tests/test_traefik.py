"""Repérage d'un Traefik existant et route vers infra-mcp (infra_mcp.traefik)."""

import json

import pytest
import yaml

from infra_mcp import traefik

DEPLOY = traefik.Path(traefik.__file__).parent / "deploy"

# Comme le Traefik de Coolify : tout en options, routes dans un dossier monté.
COOLIFY_ARGS = [
    "--ping=true",
    "--entrypoints.http.address=:80",
    "--entrypoints.https.address=:443",
    "--entrypoints.https.http3",
    "--providers.file.directory=/traefik/dynamic/",
    "--providers.file.watch=true",
    "--certificatesresolvers.letsencrypt.acme.httpchallenge=true",
    "--certificatesresolvers.letsencrypt.acme.storage=/traefik/acme.json",
    "--providers.docker=true",
]


def inspect(args, mounts=(), networks=None, network_mode="coolify", env=()):
    return {
        "Path": "/entrypoint.sh",
        "Args": ["traefik", *args],
        "Config": {"Env": list(env)},
        "HostConfig": {"NetworkMode": network_mode},
        "Mounts": [{"Type": "bind", "Source": s, "Destination": d} for d, s in mounts],
        "NetworkSettings": {
            "Networks": networks
            if networks is not None
            else {"coolify": {"Gateway": "10.0.1.1", "IPAddress": "10.0.1.5", "IPPrefixLen": 24}}
        },
    }


def test_conteneur_en_options(tmp_path):
    dynamic = tmp_path / "dynamic"
    dynamic.mkdir()
    facts = traefik.docker_facts(inspect(COOLIFY_ARGS, mounts=[("/traefik", str(tmp_path))]))
    assert facts["source"] == "options de la ligne de commande"
    assert facts["file_dir"] == str(dynamic)  # chemin sur la machine, pas dans le conteneur
    assert facts["entrypoint"] == "https"
    assert facts["resolver"] == "letsencrypt"
    assert facts["backend_host"] == "10.0.1.1"
    assert facts["client_subnet"] == "10.0.1.0/24"


def test_conteneur_reseau_de_la_machine():
    facts = traefik.docker_facts(inspect(COOLIFY_ARGS, network_mode="host", networks={}))
    assert facts["backend_host"] == "127.0.0.1"
    assert facts["client_subnet"] == ""


def test_conteneur_sans_passerelle():
    # Réseau overlay : pas de passerelle exploitable, réglage à la main.
    nets = {"proxy": {"Gateway": "", "IPAddress": "10.0.9.4", "IPPrefixLen": 24}}
    assert traefik.docker_facts(inspect(COOLIFY_ARGS, networks=nets))["backend_host"] == ""


def test_conteneur_fichier_de_config_monte(tmp_path):
    (tmp_path / "traefik.yml").write_text(
        yaml.safe_dump(
            {
                "entryPoints": {"web": {"address": ":80"}, "WebSecure": {"address": "0.0.0.0:443"}},
                "certificatesResolvers": {"le": {"acme": {"tlsChallenge": {}}}},
                "providers": {"file": {"directory": "/etc/traefik/conf.d"}},
            }
        )
    )
    mounts = [("/etc/traefik", str(tmp_path)), ("/etc/traefik/conf.d", "/srv/routes")]
    # Le fichier l'emporte : Traefik ignore alors les options.
    facts = traefik.docker_facts(inspect(["--entrypoints.autre.address=:443"], mounts=mounts))
    assert facts["source"] == str(tmp_path / "traefik.yml")
    assert facts["entrypoint"] == "WebSecure"  # casse conservée
    assert facts["resolver"] == "le"
    assert facts["file_dir"] == "/srv/routes"  # montage le plus précis


def test_conteneur_variables_d_environnement():
    env = ["TRAEFIK_PROVIDERS_FILE_DIRECTORY=/routes", "TRAEFIK_ENTRYPOINTS_SECURE_ADDRESS=:443", "PATH=/bin"]
    facts = traefik.docker_facts(inspect([], mounts=[("/routes", "/srv/r")], env=env))
    assert facts["source"] == "variables d'environnement"
    assert facts["file_dir"] == "/srv/r"
    assert facts["entrypoint"] == "secure"


def test_resolver_de_l_entrypoint_prefere():
    flat = traefik.parse_args(
        [
            "--entrypoints.websecure.address=:443",
            "--entrypoints.websecure.http.tls.certresolver=zz",
            "--certificatesresolvers.aa.acme.tlschallenge=true",
            "--certificatesresolvers.zz.acme.tlschallenge=true",
        ]
    )
    assert traefik.static_facts(flat)["resolver"] == "zz"


def test_options_avec_valeur_separee():
    assert traefik.parse_args(["--configFile", "/x.toml", "--log.level=DEBUG", "--api"]) == {
        "configFile": "/x.toml",
        "log.level": "DEBUG",
        "api": "true",
    }


def test_processus_local_toml(tmp_path):
    conf = tmp_path / "traefik.toml"
    conf.write_text(
        '[entryPoints.websecure]\naddress = ":443"\n'
        '[certificatesResolvers.myresolver.acme]\nstorage = "acme.json"\n'
        f'[providers.file]\ndirectory = "{tmp_path}/dyn"\n'
    )
    proc = tmp_path / "proc" / "42"
    proc.mkdir(parents=True)
    (proc / "cmdline").write_bytes(b"/usr/local/bin/traefik\0--configFile=" + str(conf).encode() + b"\0")
    (proc / "environ").write_bytes(b"HOME=/\0")
    facts = traefik.process_facts(42, proc=tmp_path / "proc")
    assert facts["source"] == str(conf)
    assert (facts["entrypoint"], facts["resolver"], facts["backend_host"]) == ("websecure", "myresolver", "127.0.0.1")
    assert facts["file_dir"] == f"{tmp_path}/dyn"


def test_route():
    text = traefik.route("mcp.exemple.fr", "http://10.0.1.1:8765", "https", "letsencrypt")
    data = yaml.safe_load(text)
    router = data["http"]["routers"]["infra-mcp"]
    assert router == {
        "rule": "Host(`mcp.exemple.fr`)",
        "service": "infra-mcp",
        "tls": {"certResolver": "letsencrypt"},
        "entryPoints": ["https"],
    }
    assert data["http"]["services"]["infra-mcp"]["loadBalancer"]["servers"] == [{"url": "http://10.0.1.1:8765"}]


def test_route_sans_resolver_ni_entrypoint():
    router = yaml.safe_load(traefik.route("mcp.exemple.fr", "http://127.0.0.1:8765"))["http"]["routers"]["infra-mcp"]
    assert router["tls"] == {} and "entryPoints" not in router


@pytest.mark.parametrize(
    "domain, backend, entrypoint",
    [
        ("mcp.exemple.fr`) || Host(`autre.fr", "http://127.0.0.1:8765", ""),  # injection dans la règle
        ("localhost", "http://127.0.0.1:8765", ""),
        ("MCP.exemple.fr", "http://127.0.0.1:8765", ""),  # le script met en minuscules
        ("mcp.exemple.fr", "http://evil.example:80", ""),
        ("mcp.exemple.fr", "http://127.0.0.1:8765/x", ""),
        ("mcp.exemple.fr", "http://127.0.0.1:8765", "a b"),
    ],
)
def test_route_refuse(domain, backend, entrypoint):
    with pytest.raises(ValueError):
        traefik.route(domain, backend, entrypoint)


def test_ligne_de_commande_docker(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps([inspect(COOLIFY_ARGS)])))
    assert traefik.main(["docker"]) == 0
    lines = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert lines["entrypoint"] == "https" and lines["backend_host"] == "10.0.1.1"


def test_ligne_de_commande_check_domain():
    assert traefik.main(["check-domain", "mcp.exemple.fr"]) == 0
    assert traefik.main(["check-domain", "mcp.exemple.fr;rm"]) == 1


def test_config_statique_livree():
    static = yaml.safe_load((DEPLOY / "traefik.yml").read_text())
    facts = traefik.static_facts(traefik._flatten(static))
    # Ce que le script suppose en écrivant la route de son propre Traefik
    assert (facts["entrypoint"], facts["resolver"], facts["file_dir"]) == ("websecure", "letsencrypt", "/etc/traefik/dynamic")
    assert "api" not in static  # ni API ni tableau de bord
    assert list(static["entryPoints"]) == ["websecure"]  # rien sur le port 80
    # Marqueur qui distingue le Traefik du script d'un Traefik existant
    marker = (DEPLOY / "infra-mcp-admin").read_text().split('TRAEFIK_MARKER="', 1)[1].split('"', 1)[0]
    assert (DEPLOY / "traefik.yml").read_text().startswith(marker)


def test_unite_traefik():
    unit = (DEPLOY / "traefik.service").read_text()
    assert "DynamicUser=true" in unit
    assert "CapabilityBoundingSet=CAP_NET_BIND_SERVICE" in unit
    assert "--configFile=/etc/traefik/traefik.yml" in unit


def test_processus_reel(tmp_path):
    import subprocess
    import sys
    import time
    from pathlib import Path

    conf = tmp_path / "traefik.yml"
    conf.write_text("providers:\n  file:\n    directory: /srv/routes\n")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", f"--configFile={conf}"])
    try:
        # Juste après le lancement, /proc/<pid>/cmdline peut être encore vide (exec en
        # cours) : on attend qu'il soit visible, comme pour un Traefik déjà démarré.
        cmdline = Path(f"/proc/{child.pid}/cmdline")
        for _ in range(100):
            if cmdline.read_bytes():
                break
            time.sleep(0.05)
        facts = traefik.process_facts(child.pid)
    finally:
        child.kill()
    assert facts["file_dir"] == "/srv/routes"
