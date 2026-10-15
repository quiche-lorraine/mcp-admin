"""Garde-fous « plan reste une simulation » et disposition des variables
(infra_mcp.lint), sur le repo et sur des cas construits."""

from pathlib import Path

import pytest
import yaml

from conftest import ANSIBLE_DIR
from infra_mcp import lint

YAML_FILES = sorted(
    p for root in (ANSIBLE_DIR, lint.COLLECTION_DIR) for p in root.rglob("*.yml") if p.is_file()
)


def test_fichiers_trouves():
    assert any(p.parent.name == "playbooks" for p in YAML_FILES)
    assert any("roles" in p.parts for p in YAML_FILES)


@pytest.mark.parametrize("path", YAML_FILES, ids=lambda p: str(p.relative_to(p.parents[3])))
def test_pas_d_execution_reelle_en_check_mode(path):
    violations = lint.real_execution_violations(path)
    assert not violations, "interdit, plan doit rester une simulation :\n" + "\n".join(violations)


def test_le_garde_fou_detecte_les_violations(tmp_path):
    path = tmp_path / "play.yml"
    path.write_text(yaml.safe_dump([{"hosts": "all", "tasks": [{"block": [
        {"name": "lecture", "ansible.builtin.command": "cat /etc/hostname", "check_mode": False},
    ]}]}]) + "# {{ lookup('pipe', 'id') }}\n# {{ query(\"ansible.builtin.pipe\", \"id\") }}\n")
    violations = lint.real_execution_violations(path)
    assert any("« lecture » : check_mode: False" in v for v in violations)
    assert sum("lookup pipe" in v for v in violations) == 2
    assert not lint._PIPE_RE.search("{{ lookup('ansible.builtin.vars', 'pipeline') }}")


def test_disposition_des_variables(tmp_path):
    base = tmp_path / "inventory" / "group_vars"
    (base / "swarm_managers").mkdir(parents=True)
    assert lint.vars_layout_violations(tmp_path) == []
    (base / "swarm_managers.yml").write_text("a: 1\n")
    assert lint.vars_layout_violations(tmp_path) == [
        "group_vars/swarm_managers.yml et group_vars/swarm_managers/ ensemble : Ansible ignore le fichier"
    ]


def test_repo_sans_probleme():
    assert lint.run(ANSIBLE_DIR) == []


def test_commande_lint(tmp_path):
    import subprocess, sys
    ok = subprocess.run([sys.executable, "-m", "infra_mcp.cli", "lint", str(ANSIBLE_DIR)], capture_output=True, text=True)
    assert ok.returncode == 0 and ok.stdout.strip() == "ok", ok.stdout + ok.stderr
    (tmp_path / "inventory" / "group_vars" / "x").mkdir(parents=True)
    (tmp_path / "inventory" / "group_vars" / "x.yml").write_text("a: 1\n")
    ko = subprocess.run([sys.executable, "-m", "infra_mcp.cli", "lint", str(tmp_path)], capture_output=True, text=True)
    assert ko.returncode == 1 and "1 problème(s)" in ko.stderr


# --- Le repo de config n'apporte que des données ---------------------------------
# Chaque cas reproduit une attaque vérifiée pendant la revue de sécurité : avec un
# simple token read, plan exécutait du code sur le contrôleur ou affichait un secret.

ATTACKS = [
    ("playbooks/filter_plugins/x.py", "import os\n", False, "fichier Python interdit"),
    ("playbooks/library/mod.txt", "", False, "dossier de plugins ou de modules interdit"),
    ("inventory/dyn.sh", "#!/bin/sh\necho {}\n", True, "fichier exécutable interdit"),
    ("inventory/host_vars/srv-02.yml", 'ansible_ssh_common_args: "-o ProxyCommand=id"\n', False,
     "ansible_ssh_common_args : interdit"),
    ("inventory/host_vars/srv-02.yml", 'ansible_python_interpreter: "/bin/sh -c id #"\n', False,
     "ansible_python_interpreter : interdit"),
    ("inventory/host_vars/srv-02.yml", "ansible_connection: local\n", False, "ansible_connection : interdit"),
    ("playbooks/x.yml", "- hosts: all\n  tasks:\n    - ansible.builtin.debug: {msg: \"{{ lookup('file', '/etc/passwd') }}\"}\n",
     False, "lookup file interdit"),
    ("playbooks/x.yml", "- hosts: all\n  tasks:\n    - ansible.builtin.debug: {var: item}\n      with_lines: cat /etc/passwd\n",
     False, "with_lines interdit"),
    ("playbooks/x.yml", "- hosts: all\n  tasks:\n    - ansible.builtin.slurp: {src: a}\n      delegate_to: localhost\n",
     False, "delegate_to : interdit"),
    ("playbooks/x.yml", "- hosts: localhost\n  tasks: []\n", False, "hosts: localhost : interdit"),
    ("playbooks/x.yml", "- hosts: all\n  connection: local\n  tasks: []\n", False, "connection : interdit"),
    ("playbooks/x.yml", "- hosts: all\n  tasks:\n    - ansible.builtin.copy: {src: /etc/infra-mcp/vault-pass, dest: /tmp/x}\n",
     False, "src: /etc/infra-mcp/vault-pass : chemin hors du repo interdit"),
    ("playbooks/x.yml", "- hosts: all\n  vars_files: [../../secret.yml]\n  tasks: []\n", False, "chemin hors du repo interdit"),
    ("inventory/group_vars/prod/secrets.yml", "$ANSIBLE_VAULT;1.1;AES256\n6162\n", False,
     "fichier chiffré à nommer vault.yml"),
    ("inventory/group_vars/prod/inline.yml", "api_key: !vault |\n  $ANSIBLE_VAULT;1.1;AES256\n  6162\n", False,
     "valeur !vault en ligne interdite"),
    ("playbooks/x.yml", "- hosts: all\n  tasks:\n    - ansible.builtin.copy: {content: \"{{ vault_db }}\", dest: /tmp/x}\n",
     False, "vault_* n'est utilisable que dans swarm_secrets"),
    ("ansible.cfg", "[defaults]\ninventory = inventory/hosts.yml\nfilter_plugins = ./x\n", False,
     "[defaults] filter_plugins non autorisé"),
    ("ansible.cfg", "[ssh_connection]\nssh_args = -o ProxyCommand=id\n", False, "[ssh_connection] ssh_args non autorisé"),
]


def _config_repo(tmp_path):
    import shutil

    repo = tmp_path / "infra"
    shutil.copytree(ANSIBLE_DIR, repo)
    return repo


@pytest.mark.parametrize("path, content, executable, expected", ATTACKS, ids=[a[3] for a in ATTACKS])
def test_attaque_refusee(tmp_path, path, content, executable, expected):
    repo = _config_repo(tmp_path)
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    if executable:
        target.chmod(0o755)
    problems = lint.data_only_violations(repo)
    assert any(expected in p for p in problems), problems


@pytest.mark.parametrize(
    "path, content",
    [
        ("inventory/group_vars/swarm_managers/vault.yml", "$ANSIBLE_VAULT;1.1;AES256\n6162\n"),
        ("inventory/group_vars/swarm_managers/stacks.yml",
         "swarm_stacks: []\nswarm_stacks_absent: []\nswarm_secrets:\n  db: \"{{ vault_db }}\"\n"),
        ("inventory/host_vars/srv-02.yml", "ansible_python_interpreter: /usr/bin/python3\nansible_host: 192.0.2.1\n"),
        ("playbooks/x.yml", "- hosts: all\n  tasks:\n    - ansible.builtin.debug: {msg: \"{{ query('ansible.builtin.varnames', '^a') }}\"}\n"),
        ("inventory/group_vars/all.yml", "firewall_allow_global:\n  - {port: \"80\", proto: tcp, src: 10.0.0.0/8}\n"),
    ],
)
def test_donnees_legitimes_acceptees(tmp_path, path, content):
    repo = _config_repo(tmp_path)
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(content)
    assert lint.data_only_violations(repo) == []
