"""infra-mcp sync : le repo de config n'avance que sur un commit qui passe le lint."""

import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import ANSIBLE_DIR
from infra_mcp.sync import SyncError, sync

PIEGE = "- hosts: all\n  tasks:\n    - name: piège\n      ansible.builtin.command: id\n      check_mode: false\n"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    """(dépôt de travail de l'auteur, clone du contrôleur), sur une branche main."""
    author = tmp_path / "author"
    shutil.copytree(ANSIBLE_DIR, author)
    git(author, "init", "-q", "-b", "main")
    git(author, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    git(author, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    controller = tmp_path / "controller"
    git(tmp_path, "clone", "-q", "--single-branch", str(author), str(controller))
    return author, controller


def commit(repo, message, files):
    for path, content in files.items():
        target = repo / path
        if content is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def test_a_jour(repos):
    _, controller = repos
    assert sync(controller).startswith("à jour")


def test_avance_sur_un_commit_sain(repos):
    author, controller = repos
    new = commit(author, "nouveau paquet", {"inventory/group_vars/all.yml": (author / "inventory/group_vars/all.yml").read_text() + "\n# rien\n"})
    assert "nouveau paquet" in sync(controller)
    assert git(controller, "rev-parse", "HEAD") == new


def test_refuse_un_commit_qui_ne_passe_pas_le_lint(repos):
    author, controller = repos
    before = git(controller, "rev-parse", "HEAD")
    commit(author, "piège", {"playbooks/site.yml": PIEGE})
    with pytest.raises(SyncError, match="ne passe pas infra-mcp lint") as exc:
        sync(controller)
    assert "check_mode" in str(exc.value)
    assert "requirements.txt change" not in str(exc.value)
    assert git(controller, "rev-parse", "HEAD") == before  # rien n'a bougé
    assert "check_mode" not in (controller / "playbooks" / "site.yml").read_text()
    # Le commit suivant, qui corrige, passe : le contrôleur reprend sa route.
    commit(author, "correction", {"playbooks/site.yml": (ANSIBLE_DIR / "playbooks" / "site.yml").read_text()})
    assert "correction" in sync(controller)


def test_indique_update_si_la_version_de_l_outil_change(repos):
    author, controller = repos
    commit(author, "outil et piège", {"playbooks/site.yml": PIEGE, "requirements.txt": "infra-mcp==9.9\n"})
    with pytest.raises(SyncError, match="infra-mcp-admin update"):
        sync(controller)


def test_refuse_un_historique_reecrit(repos):
    author, controller = repos
    commit(author, "a", {"README.md": "a\n"})
    sync(controller)
    git(author, "reset", "-q", "--hard", "HEAD~1")
    commit(author, "b", {"README.md": "b\n"})
    with pytest.raises(SyncError, match="historique réécrit"):
        sync(controller)


def test_refuse_un_lien_vers_l_exterieur(repos):
    author, controller = repos
    (author / "inventory" / "host_vars").mkdir(parents=True, exist_ok=True)
    (author / "inventory" / "host_vars" / "piege.yml").symlink_to("/etc/shadow")
    git(author, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    git(author, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "lien")
    before = git(controller, "rev-parse", "HEAD")
    with pytest.raises(SyncError):
        sync(controller)
    assert git(controller, "rev-parse", "HEAD") == before


def test_commande(repos):
    author, controller = repos
    commit(author, "piège", {"playbooks/site.yml": PIEGE})
    import sys
    res = subprocess.run([sys.executable, "-m", "infra_mcp.cli", "sync", str(controller)], capture_output=True, text=True)
    assert res.returncode == 1 and "ne passe pas infra-mcp lint" in res.stderr
