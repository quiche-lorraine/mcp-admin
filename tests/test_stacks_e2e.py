"""Bout en bout sur un vrai swarm : plan, apply, modification, rotation de secret,
suppression. Lancé par le job « stacks » de la CI ; sauté s'il n'y a pas de swarm
local (docker swarm init)."""

import json
import os
import shutil
import subprocess
import sys
import time

import pytest
import yaml

from conftest import ANSIBLE_DIR
from infra_mcp.runner import recap


def _swarm_active() -> bool:
    try:
        out = subprocess.run(
            ["docker", "info", "--format", "{{.Swarm.LocalNodeState}} {{.Swarm.ControlAvailable}}"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return out.stdout.strip() == "active true"


pytestmark = pytest.mark.skipif(not _swarm_active(), reason="pas de swarm local (docker swarm init)")

STACK, SECRET, NETWORK = "e2etest", "e2etest_token", "e2etest_shared"


def docker(*args) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=True).stdout.strip()


class Env:
    def __init__(self, root):
        self.root = root
        self.inv = root / "inv"
        self.vars_dir = self.inv / "group_vars" / "swarm_managers"
        self.vars_dir.mkdir(parents=True)
        (self.inv / "hosts.yml").write_text(yaml.safe_dump({"all": {"children": {"swarm_managers": {"hosts": {
            "mgr": {"ansible_connection": "local", "ansible_python_interpreter": sys.executable},
        }}}}}))
        self.stacks = root / "stacks"
        shutil.copytree(ANSIBLE_DIR / "stacks" / "whoami", self.stacks / STACK)
        compose = self.compose_path.read_text().replace("whoami_token", SECRET)
        self.compose_path.write_text(compose)
        (root / "vault-pass").write_text("test-password\n")
        self.env = {**os.environ, "ANSIBLE_NOCOLOR": "1", "ANSIBLE_VAULT_PASSWORD_FILE": str(root / "vault-pass")}
        self.config(stacks=[{"name": STACK, "secrets": [SECRET]}])
        self.vault({"vault_token": "s3cr3t-v1"})
        self.log = ""

    @property
    def compose_path(self):
        return self.stacks / STACK / "compose.yml"

    def config(self, stacks, absent=(), networks=()):
        (self.vars_dir / "stacks.yml").write_text(yaml.safe_dump({
            "stacks_install_prereqs": False,
            "swarm_stacks": stacks,
            "swarm_stacks_absent": list(absent),
            "swarm_secrets": {SECRET: "{{ vault_token }}"},
            "swarm_networks": list(networks),
        }))

    def vault(self, data):
        path = self.vars_dir / "vault.yml"
        path.write_text(yaml.safe_dump(data))
        subprocess.run(["ansible-vault", "encrypt", str(path)], env=self.env, check=True, capture_output=True)

    def run(self, *extra, ok=True) -> dict:
        res = subprocess.run(
            ["ansible-playbook", "-i", str(self.inv / "hosts.yml"), str(ANSIBLE_DIR / "playbooks" / "stacks.yml"),
             "-e", f"stacks_dir={self.stacks}", "--diff", *extra],
            cwd=self.root, env=self.env, capture_output=True, text=True, timeout=600,
        )
        self.log += res.stdout + res.stderr
        assert (res.returncode == 0) == ok, res.stdout[-3000:] + res.stderr[-2000:]
        return recap(res.stdout).get("mgr", {})

    def service(self) -> dict:
        return json.loads(docker("service", "inspect", f"{STACK}_web"))[0]["Spec"]


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    subprocess.run(["docker", "stack", "rm", STACK], capture_output=True)
    # Le réseau partagé ne se supprime qu'une fois les tâches de la stack arrêtées.
    for _ in range(30):
        if subprocess.run(["docker", "network", "rm", NETWORK], capture_output=True).returncode == 0 \
                or NETWORK not in docker("network", "ls", "--format", "{{.Name}}").split():
            break
        time.sleep(1)
    for name in docker("secret", "ls", "--format", "{{.Name}}").split():
        if name.startswith(SECRET):
            subprocess.run(["docker", "secret", "rm", name], capture_output=True)
    assert "s3cr3t" not in e.log, "un secret est apparu dans la sortie d'Ansible"


def test_cycle_de_vie_d_une_stack(env):
    # 1. plan : annonce le secret et la stack, ne crée rien
    assert env.run("--check")["changed"] == 2
    assert STACK not in docker("stack", "ls", "--format", "{{.Name}}")
    assert "à créer" in env.log

    # 2. apply : stack déployée, empreinte posée, secret versionné
    assert env.run()["changed"] == 2
    spec = env.service()
    assert "infra.stack-hash" in spec["Labels"] and spec["Labels"]["role"] == "exemple"
    assert spec["TaskTemplate"]["ContainerSpec"]["Secrets"][0]["SecretName"] == f"{SECRET}_v1"
    options = json.loads(docker("network", "inspect", f"{STACK}_default"))[0]["Options"]
    assert options.get("encrypted") == "true", options  # trafic entre nœuds chiffré

    # 3. rien n'a changé : plan et apply ne font rien
    assert env.run("--check")["changed"] == 0
    assert env.run()["changed"] == 0

    # 4. compose modifié : plan annonce le redéploiement, apply l'effectue
    env.compose_path.write_text(env.compose_path.read_text().replace("replicas: 2", "replicas: 3"))
    assert env.run("--check")["changed"] == 1
    assert "à redéployer" in env.log
    assert env.run()["changed"] == 1
    assert env.service()["Mode"]["Replicated"]["Replicas"] == 3

    # 5. rotation du secret : nouvelle version, service mis à jour
    env.vault({"vault_token": "s3cr3t-v2"})
    assert env.run("--check")["changed"] == 1
    assert env.run()["changed"] == 2
    assert env.service()["TaskTemplate"]["ContainerSpec"]["Secrets"][0]["SecretName"] == f"{SECRET}_v2"

    # 6. suppression
    env.config(stacks=[], absent=[STACK])
    assert env.run("--check")["changed"] == 1
    assert STACK in docker("stack", "ls", "--format", "{{.Name}}")
    assert env.run()["changed"] == 1
    assert STACK not in docker("stack", "ls", "--format", "{{.Name}}")


def test_reseau_partage(env):
    compose = yaml.safe_load(env.compose_path.read_text())
    compose["services"]["web"]["networks"] = ["default", NETWORK]
    compose["networks"] = {NETWORK: {"external": True}}
    env.compose_path.write_text(yaml.safe_dump(compose))

    # 1. réseau external absent de swarm_networks : refusé
    env.run("--check", ok=False)
    assert f"réseaux external absents de swarm_networks : {NETWORK}" in env.log

    # 2. déclaré : plan annonce le réseau, apply le crée chiffré et y branche la stack
    env.config(stacks=[{"name": STACK, "secrets": [SECRET]}], networks=[NETWORK])
    assert env.run("--check")["changed"] == 3  # secret, réseau, stack
    assert NETWORK not in docker("network", "ls", "--format", "{{.Name}}").split()
    assert env.run()["changed"] == 3
    net = json.loads(docker("network", "inspect", NETWORK))[0]
    assert (net["Driver"], net["Scope"], net["Options"].get("encrypted")) == ("overlay", "swarm", "true"), net
    targets = [n["Target"] for n in env.service()["TaskTemplate"]["Networks"]]
    assert net["Id"] in targets, targets

    # 3. idempotent
    assert env.run("--check")["changed"] == 0
    assert env.run()["changed"] == 0


def test_variable_vault_mal_nommee_refusee(env):
    env.vault({"vault_token": "s3cr3t-v1", "db_password": "s3cr3t-v1"})
    env.run("--check", ok=False)
    assert "db_password doivent être renommées" in env.log


def test_reseaux_propres_a_la_stack(env):
    # Réseaux déclarés avec des options (internal) ou sans définition : créés par la
    # stack et chiffrés par le socle. Un réseau avec options sans clé external
    # faisait échouer la lecture des compose.
    compose = yaml.safe_load(env.compose_path.read_text())
    compose["services"]["web"]["networks"] = ["default", "interne", "simple"]
    compose["networks"] = {"interne": {"internal": True}, "simple": None}
    env.compose_path.write_text(yaml.safe_dump(compose))
    assert env.run("--check")["changed"] == 2  # secret, stack
    assert env.run()["changed"] == 2
    for name, internal in (("interne", True), ("simple", False)):
        net = json.loads(docker("network", "inspect", f"{STACK}_{name}"))[0]
        assert net["Options"].get("encrypted") == "true", net
        assert net["Internal"] is internal, net
    assert env.run("--check")["changed"] == 0
