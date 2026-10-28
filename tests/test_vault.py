"""infra-mcp secret set : le secret arrive chiffré dans vault.yml et déclaré dans swarm_secrets."""

import os
import shutil
import subprocess
import sys

import pytest
import yaml

from conftest import ANSIBLE_DIR
from infra_mcp import lint
from infra_mcp.vault import VARS_DIR, VaultError, read_vault, set_secret

PASSWORD = "test-password"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "infra"
    shutil.copytree(ANSIBLE_DIR, root)
    return root


def stacks(repo):
    return yaml.safe_load((repo / VARS_DIR / "stacks.yml").read_text())


def test_premier_secret(repo):
    out = set_secret(repo, "whoami_token", "s3cr3t\nsur deux lignes", PASSWORD)
    assert "vault_whoami_token ajoutée" in out and "ajouté à swarm_secrets" in out
    vault_file = repo / VARS_DIR / "vault.yml"
    assert vault_file.read_bytes().startswith(b"$ANSIBLE_VAULT")
    assert b"s3cr3t" not in vault_file.read_bytes()
    assert read_vault(vault_file, PASSWORD) == {"vault_whoami_token": "s3cr3t\nsur deux lignes"}
    assert stacks(repo)["swarm_secrets"] == {"whoami_token": "{{ vault_whoami_token }}"}
    # Le reste de stacks.yml est intact, commentaires compris.
    text = (repo / VARS_DIR / "stacks.yml").read_text()
    assert "#  whoami_token: \"{{ vault_whoami_token }}\"" in text
    assert "swarm_secrets_generated: []" in text
    assert lint.run(repo) == []


def test_second_secret_et_remplacement(repo):
    set_secret(repo, "a_token", "un", PASSWORD)
    set_secret(repo, "b_token", "deux", PASSWORD)
    out = set_secret(repo, "a_token", "trois", PASSWORD)
    assert "remplacée" in out and "ajouté à swarm_secrets" not in out
    assert read_vault(repo / VARS_DIR / "vault.yml", PASSWORD) == {"vault_a_token": "trois", "vault_b_token": "deux"}
    assert stacks(repo)["swarm_secrets"] == {"b_token": "{{ vault_b_token }}", "a_token": "{{ vault_a_token }}"}
    assert lint.run(repo) == []


def test_lisible_par_ansible_vault(repo, tmp_path):
    set_secret(repo, "whoami_token", "s3cr3t", PASSWORD)
    (tmp_path / "pass").write_text(PASSWORD + "\n")
    out = subprocess.run(
        [os.path.join(os.path.dirname(sys.executable), "ansible-vault"), "view", str(repo / VARS_DIR / "vault.yml")],
        env={**os.environ, "ANSIBLE_VAULT_PASSWORD_FILE": str(tmp_path / "pass")},
        capture_output=True, text=True, check=True,
    ).stdout
    assert yaml.safe_load(out) == {"vault_whoami_token": "s3cr3t"}


def test_mauvais_mot_de_passe(repo):
    set_secret(repo, "a_token", "un", PASSWORD)
    with pytest.raises(VaultError, match="mot de passe vault incorrect"):
        set_secret(repo, "b_token", "deux", "autre")
    assert "b_token" not in (stacks(repo)["swarm_secrets"] or {})


@pytest.mark.parametrize("name", ["Majuscule", "avec-tiret", "a.b", "1abc", ""])
def test_nom_invalide(repo, name):
    with pytest.raises(VaultError, match="nom invalide"):
        set_secret(repo, name, "x", PASSWORD)


def test_refus(repo, tmp_path):
    path = repo / VARS_DIR / "stacks.yml"
    path.write_text(path.read_text().replace("swarm_secrets_generated: []", "swarm_secrets_generated: [app_key]"))
    with pytest.raises(VaultError, match="swarm_secrets_generated"):
        set_secret(repo, "app_key", "x", PASSWORD)
    with pytest.raises(VaultError, match="valeur vide"):
        set_secret(repo, "a_token", "", PASSWORD)
    with pytest.raises(VaultError, match="racine du repo"):
        set_secret(tmp_path, "a_token", "x", PASSWORD)
    assert not (repo / VARS_DIR / "vault.yml").exists()


def test_commande(repo, tmp_path):
    (tmp_path / "pass").write_text(PASSWORD + "\n")
    res = subprocess.run(
        [os.path.join(os.path.dirname(sys.executable), "infra-mcp"), "secret", "set", "whoami_token", "--repo", str(repo)],
        input="s3cr3t\n", env={**os.environ, "ANSIBLE_VAULT_PASSWORD_FILE": str(tmp_path / "pass")},
        capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    assert "s3cr3t" not in res.stdout + res.stderr
    assert read_vault(repo / VARS_DIR / "vault.yml", PASSWORD) == {"vault_whoami_token": "s3cr3t"}
