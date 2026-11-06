"""infra-mcp vault : session interactive, écriture d'un coup, lint, commit sur une branche, push."""

import shutil
import subprocess

import pytest
import yaml

from conftest import ANSIBLE_DIR
from infra_mcp import vault_cli
from infra_mcp.vault import VARS_DIR, Change, VaultError, VaultRepo, read_vault, set_registry, set_secret

PASSWORD = "test-password"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """Clone du repo de config, sur une autre branche que main, avec un origin local."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--quiet", "--bare", "-b", "main", str(origin))
    root = tmp_path / "infra"
    shutil.copytree(ANSIBLE_DIR, root)
    git(root, "init", "--quiet", "-b", "main")
    git(root, "config", "user.email", "test@example.fr")
    git(root, "config", "user.name", "Test")
    git(root, "remote", "add", "origin", str(origin))
    git(root, "add", "-A")
    git(root, "commit", "--quiet", "-m", "départ")
    git(root, "push", "--quiet", "-u", "origin", "main")
    git(root, "switch", "--quiet", "-c", "autre")
    return root


class Script:
    """Terminal piloté par une liste de réponses ; garde ce qui a été affiché et copié."""

    def __init__(self, answers, clipboard=True):
        self.answers = list(answers)
        self.out, self.copied, self.clipboard = [], [], clipboard

    def _next(self, prompt):
        if not self.answers:
            raise AssertionError(f"question inattendue : {prompt}\n" + "\n".join(self.out[-15:]))
        answer = self.answers.pop(0)
        if answer is KeyboardInterrupt:
            raise KeyboardInterrupt
        self.out.append(f"? {prompt} → {answer if not prompt.startswith('!') else '***'}")
        return answer

    def say(self, text=""):
        self.out.append(text)

    def ask(self, prompt, default=""):
        return self._next(prompt) or default

    def hidden(self, prompt):
        return self._next("!" + prompt)

    def can_copy(self):
        return self.clipboard

    def copy(self, value):
        if self.clipboard:
            self.copied.append(value)
        return self.clipboard

    def clear(self):
        self.copied.append("")

    @property
    def text(self):
        return "\n".join(self.out)


def on_main(root):
    """Pour préparer un état commité que la session retrouvera sur main."""
    git(root, "switch", "--quiet", "main")


def vault(root):
    return read_vault(root / VARS_DIR / "vault.yml", PASSWORD)


def stacks(root):
    return yaml.safe_load((root / VARS_DIR / "stacks.yml").read_text())


def test_session_complete(repo):
    term = Script([
        PASSWORD, PASSWORD,                     # nouveau vault
        "1", "app_mailer_dsn", "s", "smtp://x", "smtp://x",
        "2", "service_password", "g", "",       # générée, longueur par défaut
        "",                                      # Entrée après l'avoir rangée
        "3", "app_key",
        "4", "ghcr.io", "quiche-lorraine", "s", "ghp_tok", "ghp_tok",
        "5", "s3:https://s3.gra.io.cloud.ovh.net/sauvegardes",
        "", "", "",                             # mot de passe généré (défaut g), longueur 32, Entrée
        "s", "AKIA_acces", "AKIA_acces", "s", "SK_secret", "SK_secret",
        "7",
        "",                                      # terminer
        "",                                      # écrire : oui
        "",                                      # pousser : oui
    ])
    assert vault_cli.run(repo, term) == 0, term.text
    # Branche poussée, commit au-dessus de main, sans aucune valeur.
    branch = git(repo, "symbolic-ref", "--short", "HEAD")
    assert branch.startswith("vault/")
    assert git(repo, "rev-parse", f"origin/{branch}") == git(repo, "rev-parse", "HEAD")
    assert git(repo, "rev-parse", "HEAD~1") == git(repo, "rev-parse", "main")
    message = git(repo, "log", "-1", "--format=%B")
    patch = git(repo, "show", "--format=", "HEAD")
    for value in ("smtp://x", "ghp_tok", "AKIA_acces", "SK_secret", *[v for v in term.copied if v]):
        assert value not in message + patch + term.text
    assert git(repo, "status", "--porcelain") == ""

    data = vault(repo)
    service, backup = data["vault_service_password"], data["vault_backup_password"]
    assert len(service) == 32 and service.isalnum() and len(backup) == 32
    assert term.copied == [service, "", backup, ""]  # copiée, puis presse-papier vidé
    assert data == {
        "vault_app_mailer_dsn": "smtp://x", "vault_service_password": service, "vault_registry_ghcr_io": "ghp_tok",
        "vault_backup_password": backup, "vault_backup_s3_access_key": "AKIA_acces", "vault_backup_s3_secret_key": "SK_secret",
    }
    config = stacks(repo)
    assert config["swarm_secrets"] == {"app_mailer_dsn": "{{ vault_app_mailer_dsn }}"}
    assert config["swarm_secrets_generated"] == ["app_key"]
    assert config["swarm_registries"]["ghcr.io"]["username"] == "quiche-lorraine"
    backup_cfg = yaml.safe_load((repo / VARS_DIR / "backup.yml").read_text())
    assert backup_cfg == {
        "backup_repository": "s3:https://s3.gra.io.cloud.ovh.net/sauvegardes",
        "backup_credentials": {"password": "{{ vault_backup_password }}",
                               "s3_access_key": "{{ vault_backup_s3_access_key }}",
                               "s3_secret_key": "{{ vault_backup_s3_secret_key }}"},
    }
    assert "autre → main" in term.text


def test_suppressions(repo):
    on_main(repo)
    set_secret(repo, "a_token", "un", PASSWORD)
    set_secret(repo, "b_token", "deux", PASSWORD)
    set_registry(repo, "ghcr.io", "bot", "x", PASSWORD)
    path = repo / VARS_DIR / "stacks.yml"
    path.write_text(path.read_text().replace("swarm_secrets_generated: []", "swarm_secrets_generated: [app_key, autre_key]"))
    git(repo, "commit", "--quiet", "-am", "secrets")
    term = Script([PASSWORD, "6", "a_token", "o", "6", "ghcr.io", "o", "6", "app_key", "o", "", "", "n"])
    assert vault_cli.run(repo, term) == 0, term.text
    assert vault(repo) == {"vault_b_token": "deux"}
    config = stacks(repo)
    assert config["swarm_secrets"] == {"b_token": "{{ vault_b_token }}"}
    assert config["swarm_registries"] == {}
    assert config["swarm_secrets_generated"] == ["autre_key"]
    assert "docker secret rm" in term.text and "git push -u origin vault/" in term.text
    # Le reste de stacks.yml est intact, commentaires compris.
    assert "#  whoami_token: \"{{ vault_whoami_token }}\"" in path.read_text()

    # Jusqu'au dernier : les blocs reprennent la forme vide du modèle.
    git(repo, "switch", "--quiet", "main")
    git(repo, "merge", "--quiet", "--ff-only", "-")
    term = Script([PASSWORD, "6", "b_token", "o", "6", "autre_key", "o", "", "", "n"])
    assert vault_cli.run(repo, term) == 0, term.text
    text = path.read_text()
    assert "swarm_secrets: {}" in text and "swarm_secrets_generated: []" in text


def test_suppression_refusee_si_utilise(repo):
    on_main(repo)
    set_secret(repo, "whoami_token", "x", PASSWORD)
    path = repo / VARS_DIR / "stacks.yml"
    path.write_text(path.read_text().replace(
        "swarm_stacks: []", "swarm_stacks:\n  - name: whoami\n    secrets: [whoami_token]\n    versions: {web: false}\n    backup: {}"))
    git(repo, "commit", "--quiet", "-am", "stack")
    with pytest.raises(VaultError, match="stack whoami"):
        VaultRepo(repo, PASSWORD).deletion("whoami_token")
    term = Script([PASSWORD, "6", "whoami_token", "6", "inconnu", ""])
    assert vault_cli.run(repo, term) == 0
    assert "refusé : whoami_token sert encore" in term.text and "rien à écrire" in term.text


@pytest.mark.parametrize("change, message", [
    (Change("secret", "Majuscule", "x"), "nom invalide"),
    (Change("secret", "a_token", ""), "valeur vide"),
    (Change("generated", "a_token"), "déjà dans le vault"),
    (Change("generated", "deja_genere"), "déjà dans swarm_secrets_generated"),
    (Change("variable", "a_token", "x"), "secret Docker"),
    (Change("registry", "https://ghcr.io", "x", username="bot"), "nom d'hôte"),
    (Change("backup_config", "s3:https://x/{{ lookup('pipe', 'id') }}"), "dépôt restic invalide"),
])
def test_refus(repo, change, message):
    set_secret(repo, "a_token", "un", PASSWORD)
    path = repo / VARS_DIR / "stacks.yml"
    path.write_text(path.read_text().replace("swarm_secrets_generated: []", "swarm_secrets_generated: [deja_genere]"))
    with pytest.raises(VaultError, match=message):
        VaultRepo(repo, PASSWORD).check(change)


def test_rien_n_est_ecrit(repo):
    # Ctrl-C au milieu, refus à la fin, ou rien de prévu : ni fichier, ni branche, ni commit.
    head = git(repo, "rev-parse", "main")
    for answers in (
        [PASSWORD, PASSWORD, "1", "a_token", KeyboardInterrupt],
        [PASSWORD, PASSWORD, "1", "a_token", "s", "x", "x", "", "n"],
        [PASSWORD, PASSWORD, ""],
    ):
        term = Script(answers)
        vault_cli.run(repo, term)
        assert not (repo / VARS_DIR / "vault.yml").exists()
        assert git(repo, "status", "--porcelain") == ""
        assert git(repo, "branch", "--list", "vault/*") == ""
        assert git(repo, "rev-parse", "HEAD") == head
    assert "rien n'a été écrit" in term.text or "rien à écrire" in term.text


def test_mot_de_passe_redemande(repo):
    on_main(repo)
    set_secret(repo, "a_token", "un", PASSWORD)
    git(repo, "commit", "--quiet", "-am", "vault")
    term = Script(["faux", PASSWORD, ""])
    assert vault_cli.run(repo, term) == 0
    assert "mot de passe vault incorrect" in term.text and "a_token" in term.text


def test_modifications_non_commitees(repo):
    (repo / "inventory" / "hosts.yml").write_text("all: {}\n")
    term = Script([])
    assert vault_cli.run(repo, term) == 1
    assert "non commitées" in term.text
    assert git(repo, "symbolic-ref", "--short", "HEAD") == "autre"


def test_lint_refuse(repo, monkeypatch):
    monkeypatch.setattr(vault_cli.lint, "run", lambda root: (
        [] if not (root / VARS_DIR / "vault.yml").exists() else ["nouveau problème"]))
    term = Script([PASSWORD, PASSWORD, "1", "a_token", "s", "x", "x", "", ""])
    assert vault_cli.run(repo, term) == 1
    assert "nouveau problème" in term.text
    assert not (repo / VARS_DIR / "vault.yml").exists()
    assert git(repo, "status", "--porcelain") == ""


def test_sans_presse_papier(repo):
    term = Script([PASSWORD, PASSWORD, "2", "x_password", "g", ""], clipboard=False)
    assert vault_cli.run(repo, term) == 0
    assert "aucun presse-papier" in term.text and "rien à écrire" in term.text


def test_sauvegardes_deja_configurees(repo):
    on_main(repo)
    (repo / VARS_DIR / "backup.yml").write_text(
        'backup_repository: s3:https://x/y\nbackup_credentials:\n  password: "{{ vault_backup_password }}"\n')
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "sauvegardes")
    term = Script([PASSWORD, PASSWORD, "5", "s", "pw", "pw", "s", "AK", "AK", "s", "SK", "SK", "", "", "n"])
    assert vault_cli.run(repo, term) == 0, term.text
    assert "déjà configuré" in term.text and "ne lit pas" in term.text
    assert set(vault(repo)) == {"vault_backup_password", "vault_backup_s3_access_key", "vault_backup_s3_secret_key"}


def test_adresse_de_la_pr(tmp_path):
    for url in ("git@github.com:quiche-lorraine/infra.git", "https://github.com/quiche-lorraine/infra"):
        git(tmp_path, "init", "--quiet", str(tmp_path / "r"))
        subprocess.run(["git", "remote", "remove", "origin"], cwd=tmp_path / "r", capture_output=True)
        git(tmp_path / "r", "remote", "add", "origin", url)
        assert vault_cli._pr_url(tmp_path / "r", "vault/1") == \
            "https://github.com/quiche-lorraine/infra/compare/main...vault/1?expand=1"


def test_stacks_yml_inattendu_laisse_intact(repo):
    # Indentation à 4 espaces : la retouche automatique échoue, le fichier n'est pas touché.
    on_main(repo)
    path = repo / VARS_DIR / "stacks.yml"
    path.write_text(path.read_text().replace(
        "swarm_registries: {}", 'swarm_registries:\n    ghcr.io:\n        username: bot\n        password: "{{ vault_registry_ghcr_io }}"'))
    git(repo, "commit", "--quiet", "-am", "4 espaces")
    before = path.read_text()
    term = Script([PASSWORD, PASSWORD, "6", "ghcr.io", "o", "", ""])
    assert vault_cli.run(repo, term) == 1
    assert "à la main" in term.text
    assert path.read_text() == before and not (repo / VARS_DIR / "vault.yml").exists()
    assert git(repo, "status", "--porcelain") == ""


def test_nom_de_branche_deja_pris(repo, monkeypatch):
    on_main(repo)
    monkeypatch.setattr(vault_cli, "datetime", type("D", (), {"now": staticmethod(lambda: __import__("datetime").datetime(2026, 1, 2, 3, 4, 5))}))
    git(repo, "branch", "vault/20260102-030405")
    term = Script([PASSWORD, PASSWORD, "1", "a_token", "s", "x", "x", "", "", "n"])
    assert vault_cli.run(repo, term) == 0, term.text
    assert git(repo, "symbolic-ref", "--short", "HEAD") == "vault/20260102-030405-2"
