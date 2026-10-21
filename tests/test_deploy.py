"""Script infra-mcp-admin, unités systemd livrées avec le paquet, user-data cloud-init."""

import os
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
INSTALL = Path(__file__).parents[1] / "install.sh"


def test_fichiers_livres_avec_le_paquet():
    for name in ("infra-mcp-admin", "infra-mcp.service", "infra-mcp-sync.service", "infra-mcp-sync.timer",
                 "infra-mcp-update.service", "infra-mcp-update.path"):
        assert (DEPLOY / name).is_file(), name
    assert SCRIPT.stat().st_mode & 0o111, "infra-mcp-admin doit être exécutable"


def test_script_syntaxe():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck absent (pip install shellcheck-py)")
def test_script_shellcheck():
    res = subprocess.run(["shellcheck", str(SCRIPT), str(INSTALL)], capture_output=True, text=True)
    assert res.returncode == 0, res.stdout


def test_install_sh():
    subprocess.run(["bash", "-n", str(INSTALL)], check=True)
    text = INSTALL.read_text()
    # Récupère le script à son emplacement dans le repo, puis lui passe la main
    assert "/src/infra_mcp/deploy/infra-mcp-admin" in text
    assert SCRIPT.relative_to(INSTALL.parent).as_posix() == "src/infra_mcp/deploy/infra-mcp-admin"
    # Sûr à travers un tube : rien ne s'exécute avant la dernière ligne
    assert text.rstrip().splitlines()[-1] == 'main "$@"'
    # Refuse de tourner hors du premier plan du terminal (curl | sudo bash)
    assert 'pgid != "$tpgid"' in text


@pytest.mark.skipif(not shutil.which("script") or os.geteuid() != 0, reason="script(1) et root requis")
@pytest.mark.parametrize("command, ok", [("cat {copy} | bash -s -- --repo x/y", True),
                                         ("bash -c \"$(cat {copy})\" -- --repo x/y", True),
                                         ("cat {copy} | setsid -w bash -s -- --repo x/y", False)])
def test_install_sh_terminal(tmp_path, command, ok):
    # infra-mcp-admin remplacé par un script qui touche au terminal comme apt et
    # la saisie du mot de passe vault (tcsetattr, lecture).
    stub = tmp_path / "stub"
    stub.write_text('#!/bin/bash\nstty -echo; stty echo\nread -r -p "? " a\necho "OK [$a] [$*]"\n')
    copy = tmp_path / "install.sh"
    copy.write_text(INSTALL.read_text().replace(
        "https://raw.githubusercontent.com/quiche-lorraine/mcp-admin/$ref/src/infra_mcp/deploy/infra-mcp-admin",
        f"file://{stub}"))
    res = subprocess.run(["script", "-qec", command.format(copy=copy), "/dev/null"],
                         input="rep\n", capture_output=True, text=True, timeout=30)
    if ok:
        assert "OK [rep] [install --repo x/y]" in res.stdout, res.stdout
    else:
        assert "OK" not in res.stdout and res.returncode != 0


def sourced(code: str) -> str:
    """Exécute du bash avec les fonctions du script (sans lancer main)."""
    res = subprocess.run(["bash", "-c", f'source "{SCRIPT}"; {code}'], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return res.stdout


@pytest.mark.parametrize(
    "given, expected",
    [
        ("quiche-lorraine/infra", "git@github.com:quiche-lorraine/infra.git"),
        ("https://github.com/quiche-lorraine/infra", "git@github.com:quiche-lorraine/infra.git"),
        ("https://github.com/quiche-lorraine/infra.git/", "git@github.com:quiche-lorraine/infra.git"),
        ("git@github.com:quiche-lorraine/infra.git", "git@github.com:quiche-lorraine/infra.git"),
        ("file:///srv/infra.git", "file:///srv/infra.git"),
    ],
)
def test_normalize_repo(given, expected):
    assert sourced(f'normalize_repo "{given}"') == expected


def test_handover(tmp_path):
    stub = tmp_path / "infra-mcp-admin"
    stub.write_text('#!/bin/bash\necho "relais [$*] $INFRA_MCP_ADMIN_PINNED"\n')
    stub.chmod(0o755)
    setup = f'SELF="{stub}" DOMAIN=mcp.exemple.fr TOOL=""; '
    same = setup + f'RUNNING_SUM=$(sha256sum "{stub}" | cut -d" " -f1); handover install; echo suite'
    assert sourced(same) == "suite\n"  # c'est déjà le script installé
    assert sourced(setup + "RUNNING_SUM=x INFRA_MCP_ADMIN_PINNED=1 handover install; echo suite") == "suite\n"
    out = sourced(setup + "RUNNING_SUM=x; handover install; echo suite")
    assert out.endswith("relais [install --domain mcp.exemple.fr] 1\n")  # exec : pas de « suite »


def test_repo_suit_sa_seule_branche(tmp_path):
    """update ne récupère que la branche suivie, sans avancer le repo (step_sync le
    fera après le lint), et s'arrête si la récupération échoue."""
    origin, repo = tmp_path / "origin.git", tmp_path / "repo"
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main"]
    subprocess.run([*git, "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run([*git, "clone", "-q", str(origin), str(repo)], check=True, capture_output=True)
    for branch in ("main", "pr"):
        subprocess.run([*git, "-C", str(repo), "checkout", "-q", "-B", branch], check=True)
        subprocess.run([*git, "-C", str(repo), "commit", "-q", "--allow-empty", "-m", branch], check=True)
        subprocess.run([*git, "-C", str(repo), "push", "-q", "origin", branch], check=True, capture_output=True)
    subprocess.run([*git, "-C", str(repo), "checkout", "-q", "main"], check=True)
    subprocess.run([*git, "-C", str(repo), "branch", "-q", "-u", "origin/main"], check=True)
    setup = f'REPO_DIR="{repo}"; as_service() {{ (cd "$REPO_DIR" && "$@"); }}; '
    assert "à venir" not in sourced(setup + "step_repo")
    fetch = subprocess.run(["git", "-C", str(repo), "config", "--get-all", "remote.origin.fetch"],
                           capture_output=True, text=True, check=True).stdout
    assert fetch == "+refs/heads/main:refs/remotes/origin/main\n"
    # Un nouveau commit sur main : récupéré et annoncé, mais le repo ne bouge pas.
    other = tmp_path / "other"
    subprocess.run([*git, "clone", "-q", "-b", "main", str(origin), str(other)], check=True, capture_output=True)
    subprocess.run([*git, "-C", str(other), "commit", "-q", "--allow-empty", "-m", "nouveau"], check=True)
    subprocess.run([*git, "-C", str(other), "push", "-q", "origin", "main"], check=True, capture_output=True)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
    out = sourced(setup + 'step_repo; echo "cible=$SYNC_TARGET"')
    assert "à venir" in out and "nouveau" in out and "cible=origin/main" in out
    assert subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout == head
    shutil.rmtree(origin)
    res = subprocess.run(["bash", "-c", f'source "{SCRIPT}"; {setup}step_repo; echo suite'],
                         capture_output=True, text=True)
    assert res.returncode != 0 and "suite" not in res.stdout and "git fetch a échoué" in res.stderr


def test_inventory_user(tmp_path):
    inv = tmp_path / "hosts.yml"
    inv.write_text(yaml.safe_dump({"all": {"vars": {"ansible_user": "renaud"},
                                           "hosts": {"srv": {}, "autre": {"ansible_user": "admin"}}}}))
    bin_dir = Path(shutil.which("ansible-inventory")).parent
    setup = (f'VENV="{Path(sys.executable).parent.parent}"; ansible_bin() {{ echo "{bin_dir}/$1"; }}; '
             f'as_service() {{ "$@" -i "{inv}"; }}; ')
    assert sourced(setup + "inventory_user srv") == "renaud\n"
    assert sourced(setup + "inventory_user autre") == "admin\n"
    inv.write_text(yaml.safe_dump({"all": {"hosts": {"srv": {}}}}))
    assert sourced(setup + "inventory_user srv") == "root\n"


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


def test_outil_reinstalle_a_chaque_commit():
    # Même numéro de version d'un commit à l'autre : sans réinstallation forcée,
    # pip garderait l'ancien et update ne mettrait rien à jour.
    step = SCRIPT.read_text().split("step_tool() {", 1)[1].split("\n}\n", 1)[0]
    assert '--force-reinstall --no-deps "${spec[@]}"' in step


@pytest.mark.parametrize(
    "content, ok",
    [
        ("# outil\ninfra-mcp @ git+https://github.com/quiche-lorraine/mcp-admin@" + "a" * 40 + "\n", True),
        ("infra-mcp==0.2.0\n", True),
        ("infra-mcp @ git+https://github.com/quiche-lorraine/mcp-admin@main\n", False),  # pas figé
        ("infra-mcp @ git+https://evil.example/mcp-admin@" + "a" * 40 + "\n", False),
        ("infra-mcp==0.2.0\nrequests\n", False),  # paquet tiers
        ("--index-url https://evil.example/simple\ninfra-mcp==0.2.0\n", False),
        ("-e git+https://github.com/x/y@" + "a" * 40 + "#egg=infra-mcp\n", False),
        ("", False),
    ],
)
def test_requirements_verifie_avant_pip(tmp_path, content, ok):
    req = tmp_path / "requirements.txt"
    req.write_text(content)
    res = subprocess.run(["bash", "-c", f'source "{SCRIPT}"; check_requirements "{req}"'], capture_output=True, text=True)
    assert (res.returncode == 0) == ok, res.stderr


def test_synchro_conditionnee_par_le_lint():
    unit = (DEPLOY / "infra-mcp-sync.service").read_text()
    assert "ExecStart=/opt/infra-mcp/venv/bin/infra-mcp sync /opt/infra" in unit
    assert "git pull" not in unit
    # update et install passent aussi par le lint avant d'avancer le repo
    script = SCRIPT.read_text()
    assert "git pull" not in script.split("step_repo() {", 1)[1].split("\n}\n", 1)[0]
    assert script.count("    step_sync\n") == 2


def test_mise_a_jour_automatique():
    path = (DEPLOY / "infra-mcp-update.path").read_text()
    assert "PathChanged=/opt/infra/requirements.txt" in path and "Unit=infra-mcp-update.service" in path
    unit = (DEPLOY / "infra-mcp-update.service").read_text()
    assert "ExecStart=/usr/local/sbin/infra-mcp-admin update --auto" in unit
    script = SCRIPT.read_text()
    assert 'SELF=/usr/local/sbin/infra-mcp-admin' in script and 'REPO_DIR=/opt/infra' in script
    # Installées et activées par install et update
    assert '"$src"/infra-mcp-update.service "$src"/infra-mcp-update.path' in script
    assert "systemctl enable --quiet --now infra-mcp-update.path" in script
    # Le script relancé par handover reste en mode automatique
    assert "args+=(--auto)" in script.split("handover() {", 1)[1].split("\n}\n", 1)[0]


def test_mise_a_jour_automatique_sans_effet_si_a_jour(tmp_path):
    # Le venv de l'outil remplacé par un python qui dit « déjà à jour », puis « à mettre à jour ».
    for code, expected in ((0, "rien à faire"), (1, "ATTENTE")):
        venv = tmp_path / f"venv{code}"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text(f"#!/bin/sh\nexit {code}\n")
        (venv / "bin" / "python").chmod(0o755)
        out = sourced(f"""VENV={venv}; AUTO=1; SYNC_TARGET=HEAD
            as_service() {{ echo 'infra-mcp==1.0'; }}; wait_idle() {{ echo ATTENTE; }}
            auto_gate && echo SUITE || echo FIN""")
        assert expected in out and ("FIN" if code == 0 else "SUITE") in out, out
    # Sans --auto, ou dans le script relancé par handover : toujours la suite
    assert "SUITE" in sourced("AUTO=; auto_gate && echo SUITE")
    assert "SUITE" in sourced("AUTO=1; INFRA_MCP_ADMIN_PINNED=1; auto_gate && echo SUITE")


def test_rotation_du_journal_d_audit():
    conf = (DEPLOY / "infra-mcp.logrotate").read_text()
    assert "/var/log/infra-mcp/*.jsonl" in conf and "su infra-mcp infra-mcp" in conf
    assert "infra-mcp.logrotate" in SCRIPT.read_text()
