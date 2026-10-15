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
