"""Exécute pour de vrai les garde-fous du rôle base (roles/base/tasks/checks.yml)
sur localhost, avec des couches de variables construites pour chaque cas."""

import json
import re
import os
import subprocess

import pytest
import yaml

from conftest import ANSIBLE_DIR

CHECKS = ANSIBLE_DIR / "roles" / "base" / "tasks" / "checks.yml"
LOCKED = ANSIBLE_DIR / "policy" / "locked.yml"

BASE = {
    "ansible_user": "renaud",
    "firewall_default_incoming": "deny",
    "admin_users_global": [{"name": "renaud", "sudo": True, "ssh_keys": ["ssh-ed25519 AAAA"]}],
    "firewall_allow_global": [
        {"port": "22", "proto": "tcp"},
        {"port": "443", "proto": "tcp"},
    ],
}


def run_checks(tmp_path, **layers) -> dict:
    """Renvoie les variables fusionnées, ou lève AssertionError avec le message d'échec."""
    playbook = [
        {
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "vars_files": [str(LOCKED)],
            "vars": {**BASE, **layers},
            "tasks": [
                {"ansible.builtin.import_tasks": str(CHECKS)},
                {
                    "ansible.builtin.copy": {
                        "dest": str(tmp_path / "out.json"),
                        "content": "{{ {'remove': base_firewall_remove, 'absent': base_users_absent} | to_json }}",
                    }
                },
            ],
        }
    ]
    path = tmp_path / "play.yml"
    path.write_text(yaml.safe_dump(playbook, allow_unicode=True))
    res = subprocess.run(
        ["ansible-playbook", "-i", "localhost,", str(path)],
        cwd=tmp_path,  # hors du repo : son ansible.cfg ne s'applique pas
        env={**os.environ, "ANSIBLE_NOCOLOR": "1"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    if res.returncode != 0:
        raise AssertionError(res.stdout + res.stderr)
    return json.loads((tmp_path / "out.json").read_text())


def test_les_couches_s_additionnent(tmp_path):
    out = run_checks(
        tmp_path,
        users_absent_global=["ancien"],
        users_absent_host=["stagiaire", "ancien"],
        firewall_remove_global=[{"port": "8080", "proto": "tcp"}],
        firewall_remove_host=[{"port": "8765", "proto": "tcp"}],
    )
    assert sorted(out["absent"]) == ["ancien", "stagiaire"]
    assert {r["port"] for r in out["remove"]} == {"8080", "8765"}


def test_sans_retrait(tmp_path):
    assert run_checks(tmp_path) == {"remove": [], "absent": []}


def test_restreindre_une_source(tmp_path):
    # Cas réel : un port ouvert à tous, qu'on ne veut plus ouvrir qu'à un sous-réseau.
    out = run_checks(
        tmp_path,
        firewall_allow_host=[{"port": "8765", "proto": "tcp", "src": "10.0.1.0/24"}],
        firewall_remove_host=[{"port": "8765", "proto": "tcp"}],
    )
    assert out["remove"] == [{"port": "8765", "proto": "tcp"}]


@pytest.mark.parametrize(
    "layers, message",
    [
        ({"users_absent_host": ["renaud"]}, "admin_users_* et users_absent_*"),
        ({"users_absent_host": ["root"]}, "ne peut pas contenir root"),
        (
            {"ansible_user": "deploy", "admin_users_global": [], "users_absent_host": ["deploy"]},
            "ne peut pas contenir deploy",
        ),
        ({"ansible_user": "root", "admin_users_global": [], "users_absent_host": ["root"]}, "ne peut pas contenir root"),
        ({"firewall_remove_host": [{"port": "22", "proto": "tcp", "src": "0.0.0.0/0"}]}, "port SSH"),
        ({"firewall_remove_host": [{"port": 22, "proto": "tcp"}]}, "port SSH"),
        ({"ansible_port": 2222, "firewall_remove_host": [{"port": "2222", "proto": "tcp"}]}, "port SSH"),
        ({"firewall_remove_host": [{"port": "443", "proto": "tcp"}]}, "443/tcp est dans firewall_allow_*"),
        ({"firewall_remove_host": [{"port": 443, "proto": "tcp"}]}, "443/tcp est dans firewall_allow_*"),
        ({"firewall_default_incoming": "allow"}, "verrouillée"),
        # Port 22 protégé même quand ansible_port dit autre chose
        ({"ansible_port": 2222, "firewall_remove_host": [{"port": "22", "proto": "tcp"}]}, "port SSH"),
        # Entrées mal formées : elles ne doivent pas passer en silence
        ({"users_absent_host": [{"name": "ancien"}]}, "attend des noms de compte"),
        ({"users_absent_host": ["../../etc/passwd"]}, "attend des noms de compte"),
        ({"firewall_remove_host": ["8080/tcp"]}, "règle firewall invalide"),
        ({"firewall_remove_host": [{"port": "8080"}]}, "règle firewall invalide"),
        ({"firewall_allow_host": [{"proto": "tcp"}]}, "règle firewall invalide"),
        # Le compte d'Ansible ne perd pas son sudo
        (
            {"admin_users_global": [{"name": "renaud", "sudo": False, "ssh_keys": ["k"]}]},
            "le compte qu'utilise Ansible",
        ),
        ({"admin_users_global": []}, "le compte qu'utilise Ansible"),
    ],
)
def test_configurations_refusees(tmp_path, layers, message):
    with pytest.raises(AssertionError, match=re.escape(message)):
        run_checks(tmp_path, **layers)
