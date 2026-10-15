"""Script infra-mcp-admin, unités systemd livrées avec le paquet, user-data cloud-init."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from conftest import ANSIBLE_DIR
from infra_mcp import cloudinit, lint

DEPLOY = Path(cloudinit.__file__).parent / "deploy"
SCRIPT = DEPLOY / "infra-mcp-admin"


def test_fichiers_livres_avec_le_paquet():
    for name in ("infra-mcp-admin", "infra-mcp.service", "infra-mcp-sync.service", "infra-mcp-sync.timer"):
        assert (DEPLOY / name).is_file(), name
    assert SCRIPT.stat().st_mode & 0o111, "infra-mcp-admin doit être exécutable"


def test_script_syntaxe():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck absent (pip install shellcheck-py)")
def test_script_shellcheck():
    res = subprocess.run(["shellcheck", str(SCRIPT)], capture_output=True, text=True)
    assert res.returncode == 0, res.stdout


def test_unite_lit_le_fichier_local():
    unit = (DEPLOY / "infra-mcp.service").read_text()
    assert "EnvironmentFile=-/etc/infra-mcp/infra-mcp.env" in unit
    assert "Environment=INFRA_MCP_HOST=127.0.0.1" in unit  # écoute locale par défaut


HOSTVARS = {
    "ansible_user": "admin",
    "admin_users_global": [{"name": "admin", "sudo": True, "ssh_keys": ["ssh-ed25519 AAAA admin@poste"]}],
    "admin_users_host": [
        {"name": "lecteur", "ssh_keys": ["ssh-ed25519 BBBB lecteur@poste"]},
        {"name": "sans-cle", "sudo": True},
    ],
}


def test_user_data():
    text = cloudinit.user_data("srv-04", HOSTVARS)
    assert text.startswith("#cloud-config\n")
    data = yaml.safe_load(text)
    assert data["disable_root"] is True and data["ssh_pwauth"] is False
    # Toutes les couches, mais seulement les comptes qui ont une clé
    assert [u["name"] for u in data["users"]] == ["admin", "lecteur"]
    assert all(u["lock_passwd"] for u in data["users"])
    # sudo seulement pour les admins qui l'ont
    assert [f["path"] for f in data["write_files"]] == ["/etc/sudoers.d/90-admin"]
    assert data["write_files"][0]["permissions"] == "0440"


def test_sudo_au_format_du_role_base():
    # Le rôle base doit reconnaître le fichier comme le sien (marqueur, contenu
    # identique) : sinon il ne le gérerait pas en exclusif, ou le réécrirait.
    users_yml = (lint.COLLECTION_DIR / "roles" / "base" / "tasks" / "users.yml").read_text()
    task = next(t for t in yaml.safe_load(users_yml) if t.get("name") == "Sudo pour les admins concernés")
    expected = task["ansible.builtin.copy"]["content"].replace("{{ item.name }}", "admin")
    written = yaml.safe_load(cloudinit.user_data("srv-04", HOSTVARS))["write_files"][0]["content"]
    assert written == expected


@pytest.mark.parametrize(
    "hostvars, message",
    [
        ({"admin_users_global": [{"name": "a", "ssh_keys": ["k"]}]}, "aucun admin avec sudo"),
        ({"admin_users_global": [{"name": "a", "sudo": True}]}, "aucun admin avec sudo"),
        ({**HOSTVARS, "ansible_user": "deploy"}, "ansible_user (deploy)"),
    ],
)
def test_user_data_refuse(hostvars, message):
    with pytest.raises(ValueError, match=message.replace("(", r"\(").replace(")", r"\)")):
        cloudinit.user_data("srv-04", hostvars)


def test_commande_cloud_init():
    env = {"INFRA_MCP_ANSIBLE_DIR": str(ANSIBLE_DIR), "PATH": str(Path(sys.executable).parent)}
    ok = subprocess.run([sys.executable, "-m", "infra_mcp.cli", "cloud-init", "srv-03"],
                        capture_output=True, text=True, env={**__import__("os").environ, **env})
    assert ok.returncode == 0, ok.stderr
    assert yaml.safe_load(ok.stdout)["users"][0]["name"] == "admin"
    ko = subprocess.run([sys.executable, "-m", "infra_mcp.cli", "cloud-init", "inconnu"],
                        capture_output=True, text=True, env={**__import__("os").environ, **env})
    assert ko.returncode == 1 and "absent de l'inventaire" in ko.stderr
