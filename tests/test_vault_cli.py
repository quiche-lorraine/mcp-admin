"""infra-mcp vault : préparation git, écriture d'un coup, lint, commit sur une branche, push,
et l'interface plein écran pilotée comme au clavier."""

import datetime as dt
import shutil
import subprocess

import pytest
import yaml
from textual.widgets import Button, Input, Select

from conftest import ANSIBLE_DIR
from infra_mcp import vault_cli, vault_tui
from infra_mcp.vault import VARS_DIR, Change, VaultError, VaultRepo, read_vault, set_registry, set_secret
from infra_mcp.vault_tui import AddScreen, BackupScreen, ConfirmScreen, ValueField, ValueScreen, VaultApp, table_rows

PASSWORD = "test-password"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """Clone du repo de config sur main, avec un origin local."""
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
    return root


def commit(root, message="état"):
    git(root, "add", "-A")
    git(root, "commit", "--quiet", "-m", message)


class Script:
    """Terminal piloté par une liste de réponses (avant et après l'interface plein écran)."""

    def __init__(self, answers=()):
        self.answers, self.out = list(answers), []

    def _next(self, prompt):
        if not self.answers:
            raise AssertionError(f"question inattendue : {prompt}\n" + "\n".join(self.out))
        answer = self.answers.pop(0)
        if answer is KeyboardInterrupt:
            raise KeyboardInterrupt
        self.out.append(f"? {prompt}")
        return answer

    def say(self, text=""):
        self.out.append(text)

    def ask(self, prompt, default=""):
        return self._next(prompt) or default

    def hidden(self, prompt):
        return self._next(prompt)

    @property
    def text(self):
        return "\n".join(self.out)


class Clipboard:
    def __init__(self, available=True):
        self.ok, self.history = available, []

    def available(self):
        return self.ok

    def copy(self, value):
        self.history.append(value)
        return self.ok

    def clear(self):
        self.history.append("")


def vault(root):
    return read_vault(root / VARS_DIR / "vault.yml", PASSWORD)


def stacks(root):
    return yaml.safe_load((root / VARS_DIR / "stacks.yml").read_text())


# --- Préparation, écriture, push ----------------------------------------------------


def test_prepare_passe_sur_main_a_jour(repo):
    git(repo, "switch", "--quiet", "-c", "autre")
    term = Script()
    assert vault_cli.prepare(repo / "inventory", term) == repo  # depuis un sous-dossier aussi
    assert git(repo, "symbolic-ref", "--short", "HEAD") == "main"
    assert "autre → main" in term.text


def test_prepare_refuse_les_modifications_non_commitees(repo):
    git(repo, "switch", "--quiet", "-c", "autre")
    (repo / "inventory" / "hosts.yml").write_text("all: {}\n")
    with pytest.raises(VaultError, match="non commitées"):
        vault_cli.prepare(repo, Script())
    assert git(repo, "symbolic-ref", "--short", "HEAD") == "autre"


def test_mot_de_passe_redemande(repo):
    set_secret(repo, "a_token", "un", PASSWORD)
    term = Script(["faux", PASSWORD])
    assert set(vault_cli.open_vault(repo, term).vault) == {"vault_a_token"}
    assert "mot de passe vault incorrect" in term.text
    with pytest.raises(VaultError, match="trois essais"):
        vault_cli.open_vault(repo, Script(["a", "b", "c"]))


def test_ecriture_commit_sans_valeur(repo):
    r = VaultRepo(repo, PASSWORD)
    result = vault_cli.write_and_commit(r, [
        Change("secret", "app_mailer_dsn", "smtp://s3cr3t"),
        Change("variable", "backup_password", "Gener3Pass", generated=True),
        Change("generated", "app_key"),
        Change("registry", "ghcr.io", "ghp_t0k3n", username="quiche-lorraine"),
        Change("variable", "backup_s3_access_key", "AKIA_acces"),
        Change("variable", "backup_s3_secret_key", "SK_s3cr3t"),
        Change("backup_config", "s3:https://s3.gra.io.cloud.ovh.net/sauvegardes"),
    ])
    assert result["branch"].startswith("vault/") and result["commit_error"] is None and result["backup_created"]
    assert git(repo, "symbolic-ref", "--short", "HEAD") == result["branch"]
    assert git(repo, "rev-parse", "HEAD~1") == git(repo, "rev-parse", "main")
    assert git(repo, "status", "--porcelain") == ""
    shown = git(repo, "log", "-1", "--format=%B") + git(repo, "show", "--format=", "HEAD") + "\n".join(result["done"])
    for value in ("smtp://s3cr3t", "Gener3Pass", "ghp_t0k3n", "AKIA_acces", "SK_s3cr3t"):
        assert value not in shown
    assert "- app_mailer_dsn : secret Docker ajouté" in git(repo, "log", "-1", "--format=%B")
    assert vault(repo)["vault_backup_password"] == "Gener3Pass"
    config = stacks(repo)
    assert config["swarm_secrets"] == {"app_mailer_dsn": "{{ vault_app_mailer_dsn }}"}
    assert config["swarm_secrets_generated"] == ["app_key"]
    assert config["swarm_registries"]["ghcr.io"]["username"] == "quiche-lorraine"
    assert yaml.safe_load((repo / VARS_DIR / "backup.yml").read_text())["backup_credentials"] == {
        "password": "{{ vault_backup_password }}",
        "s3_access_key": "{{ vault_backup_s3_access_key }}",
        "s3_secret_key": "{{ vault_backup_s3_secret_key }}",
    }


def test_suppressions(repo):
    set_secret(repo, "a_token", "un", PASSWORD)
    set_secret(repo, "b_token", "deux", PASSWORD)
    set_registry(repo, "ghcr.io", "bot", "x", PASSWORD)
    path = repo / VARS_DIR / "stacks.yml"
    path.write_text(path.read_text().replace("swarm_secrets_generated: []", "swarm_secrets_generated: [app_key, autre_key]"))
    commit(repo)
    r = VaultRepo(repo, PASSWORD)
    result = vault_cli.write_and_commit(r, [r.deletion("a_token"), r.deletion("ghcr.io"), r.deletion("app_key")])
    assert result["swarm_deleted"] == ["a_token", "app_key"]
    assert vault(repo) == {"vault_b_token": "deux"}
    config = stacks(repo)
    assert config["swarm_secrets"] == {"b_token": "{{ vault_b_token }}"}
    assert config["swarm_registries"] == {}
    assert config["swarm_secrets_generated"] == ["autre_key"]
    assert "#  whoami_token: \"{{ vault_whoami_token }}\"" in path.read_text()  # commentaires gardés
    # Jusqu'au dernier : les blocs reprennent la forme vide du modèle.
    r = VaultRepo(repo, PASSWORD)
    vault_cli.write_and_commit(r, [r.deletion("b_token"), r.deletion("autre_key")])
    assert "swarm_secrets: {}" in path.read_text() and "swarm_secrets_generated: []" in path.read_text()


def test_suppression_refusee_si_utilise(repo):
    set_secret(repo, "whoami_token", "x", PASSWORD)
    path = repo / VARS_DIR / "stacks.yml"
    path.write_text(path.read_text().replace(
        "swarm_stacks: []", "swarm_stacks:\n  - name: whoami\n    secrets: [whoami_token]\n    versions: {web: false}\n    backup: {}"))
    with pytest.raises(VaultError, match="stack whoami"):
        VaultRepo(repo, PASSWORD).deletion("whoami_token")
    with pytest.raises(VaultError, match="ni dans le vault"):
        VaultRepo(repo, PASSWORD).deletion("inconnu")


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


def test_lint_refuse_rien_n_est_ecrit(repo, monkeypatch):
    monkeypatch.setattr(vault_cli.lint, "run", lambda root: (
        [] if not (root / VARS_DIR / "vault.yml").exists() else ["nouveau problème"]))
    with pytest.raises(VaultError, match="nouveau problème"):
        vault_cli.write_and_commit(VaultRepo(repo, PASSWORD), [Change("secret", "a_token", "x")])
    assert not (repo / VARS_DIR / "vault.yml").exists()
    assert git(repo, "status", "--porcelain") == "" and git(repo, "branch", "--list", "vault/*") == ""


def test_stacks_yml_inattendu_laisse_intact(repo):
    # Indentation à 4 espaces : la retouche automatique échoue, le fichier n'est pas touché.
    path = repo / VARS_DIR / "stacks.yml"
    path.write_text(path.read_text().replace(
        "swarm_registries: {}", 'swarm_registries:\n    ghcr.io:\n        username: bot\n        password: "{{ vault_registry_ghcr_io }}"'))
    commit(repo)
    before = path.read_text()
    r = VaultRepo(repo, PASSWORD)
    with pytest.raises(VaultError, match="à la main"):
        vault_cli.write_and_commit(r, [r.deletion("ghcr.io")])
    assert path.read_text() == before and git(repo, "status", "--porcelain") == ""


def test_nom_de_branche_deja_pris(repo, monkeypatch):
    monkeypatch.setattr(vault_cli, "datetime", type("D", (), {"now": staticmethod(lambda: dt.datetime(2026, 1, 2, 3, 4, 5))}))
    git(repo, "branch", "vault/20260102-030405")
    result = vault_cli.write_and_commit(VaultRepo(repo, PASSWORD), [Change("secret", "a_token", "x")])
    assert result["branch"] == "vault/20260102-030405-2"


class FakeApp:
    """Remplace l'interface plein écran : prévoit des changements et appelle writer, comme « w »."""

    changes: list = []

    def __init__(self, repo, writer, clipboard):
        self.writer = writer

    def run(self):
        return self.writer(self.changes) if self.changes else None


def test_run_complet_jusqu_au_push(repo, monkeypatch):
    git(repo, "switch", "--quiet", "-c", "autre")
    monkeypatch.setattr(FakeApp, "changes", [Change("secret", "a_token", "x")])
    term = Script([PASSWORD, PASSWORD, ""])  # nouveau vault (deux fois), pousser : oui
    assert vault_cli.run(repo, term, app_class=FakeApp) == 0, term.text
    branch = git(repo, "symbolic-ref", "--short", "HEAD")
    assert git(repo, "rev-parse", f"origin/{branch}") == git(repo, "rev-parse", "HEAD")
    assert "a_token : secret Docker ajouté" in term.text


def test_run_sans_rien_ecrire(repo, monkeypatch):
    monkeypatch.setattr(FakeApp, "changes", [])
    term = Script([PASSWORD, PASSWORD])
    assert vault_cli.run(repo, term, app_class=FakeApp) == 0
    assert "rien n'a été écrit" in term.text and not (repo / VARS_DIR / "vault.yml").exists()
    term = Script([PASSWORD, KeyboardInterrupt])
    assert vault_cli.run(repo, term, app_class=FakeApp) == 130


def test_adresse_de_la_pr(tmp_path):
    for url in ("git@github.com:quiche-lorraine/infra.git", "https://github.com/quiche-lorraine/infra"):
        git(tmp_path, "init", "--quiet", str(tmp_path / "r"))
        subprocess.run(["git", "remote", "remove", "origin"], cwd=tmp_path / "r", capture_output=True)
        git(tmp_path / "r", "remote", "add", "origin", url)
        assert vault_cli._pr_url(tmp_path / "r", "vault/1") == \
            "https://github.com/quiche-lorraine/infra/compare/main...vault/1?expand=1"


# --- Interface plein écran ----------------------------------------------------------


def make_app(repo, clipboard=None):
    written = []

    def writer(changes):
        written.extend(changes)
        return {"branch": "vault/x"}

    app = VaultApp(VaultRepo(repo, PASSWORD), writer=writer, clipboard=clipboard or Clipboard())
    return app, written


def fill(screen, selector, value):
    screen.query_one(selector, Input).value = value


def fill_value(field: ValueField, value):
    field.query_one(".value", Input).value = value
    field.query_one(".confirm", Input).value = value


async def press_ok(pilot):
    pilot.app.screen.query_one("#ok", Button).press()
    await pilot.pause()


async def press_generate(pilot, field=None):
    (field or pilot.app.screen.query_one(ValueField)).query_one(".generate", Button).press()
    await pilot.pause()


async def test_tableau_de_l_existant(repo):
    set_secret(repo, "a_token", "un", PASSWORD)
    set_registry(repo, "ghcr.io", "bot", "x", PASSWORD)
    app, _ = make_app(repo)
    async with app.run_test(size=(120, 40)):
        assert [(name, kind, state) for _, name, kind, state in app.rows] == [
            ("a_token", "secret", "inchangé"), ("ghcr.io", "registry", "inchangé")]


async def test_ajout_saisi_puis_ecriture(repo):
    app, written = make_app(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("a")
        screen = app.screen
        assert isinstance(screen, AddScreen)
        fill(screen, "#name", "app_mailer_dsn")
        fill_value(screen.query_one(ValueField), "smtp://x")
        await press_ok(pilot)
        assert app.rows[-1][1:] == ("app_mailer_dsn", "secret", "+ ajouté")
        await pilot.press("w")
        assert isinstance(app.screen, ConfirmScreen)
        await press_ok(pilot)
    assert app.return_value == {"branch": "vault/x"}
    assert [(c.kind, c.name, c.value) for c in written] == [("secret", "app_mailer_dsn", "smtp://x")]


async def test_ajout_refuse_reste_ouvert(repo):
    app, _ = make_app(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("a")
        fill(app.screen, "#name", "Majuscule")
        fill_value(app.screen.query_one(ValueField), "x")
        await press_ok(pilot)
        assert isinstance(app.screen, AddScreen)
        assert "nom invalide" in str(app.screen.query_one("#error").render())
        # Saisies différentes
        fill(app.screen, "#name", "ok_name")
        app.screen.query_one(".confirm", Input).value = "y"
        await press_ok(pilot)
        assert "diffèrent" in str(app.screen.query_one("#error").render())
        await pilot.press("escape")
        assert not app.pending


async def test_valeur_generee_par_le_presse_papier(repo):
    clipboard = Clipboard()
    app, written = make_app(repo, clipboard)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("a")
        screen = app.screen
        screen.query_one("#kind", Select).value = "variable"
        fill(screen, "#name", "service_password")
        await press_generate(pilot)
        generated = clipboard.history[0]
        assert len(generated) == 32 and generated.isalnum()
        assert screen.query_one(".value", Input).disabled
        await press_ok(pilot)
        assert clipboard.history == [generated, ""]  # copiée, puis presse-papier vidé
        assert app.rows[-1][3] == "+ ajouté ● générée"
        await pilot.press("w")
        await press_ok(pilot)
    assert written[0].value == generated and written[0].generated


async def test_sans_presse_papier_pas_de_generation(repo):
    app, _ = make_app(repo, Clipboard(available=False))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("a")
        await press_generate(pilot)
        assert app.screen.query_one(ValueField).generated is None


async def test_secret_genere_sur_le_serveur_et_registre(repo):
    app, _ = make_app(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("a")
        app.screen.query_one("#kind", Select).value = "generated"
        await pilot.pause()
        assert not app.screen.query_one(ValueField).display
        fill(app.screen, "#name", "app_key")
        await press_ok(pilot)
        await pilot.press("a")
        app.screen.query_one("#kind", Select).value = "registry"
        await pilot.pause()
        fill(app.screen, "#name", "ghcr.io")
        fill(app.screen, "#username", "bot")
        fill_value(app.screen.query_one(ValueField), "ghp_x")
        await press_ok(pilot)
    assert [(c.kind, c.name, c.username) for c in app.pending.values()] == [
        ("generated", "app_key", ""), ("registry", "ghcr.io", "bot")]


async def test_remplacer_supprimer_annuler(repo):
    set_secret(repo, "a_token", "un", PASSWORD)
    set_secret(repo, "b_token", "deux", PASSWORD)
    app, _ = make_app(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        # a_token (ligne 0) : remplacé
        await pilot.press("r")
        assert isinstance(app.screen, ValueScreen)
        fill_value(app.screen.query_one(ValueField), "neuf")
        await press_ok(pilot)
        assert app.rows[0][3] == "~ remplacé"
        # b_token (ligne 1) : supprimé, après confirmation
        await pilot.press("down", "s")
        assert isinstance(app.screen, ConfirmScreen)
        await press_ok(pilot)
        assert app.rows[1][3] == "− supprimé"
        # annuler le remplacement de a_token
        await pilot.press("up", "u")
        assert app.rows[0][3] == "inchangé"
    assert [c.kind for c in app.pending.values()] == ["delete"]


async def test_suppression_refusee_dans_l_interface(repo):
    set_secret(repo, "whoami_token", "x", PASSWORD)
    path = repo / VARS_DIR / "stacks.yml"
    path.write_text(path.read_text().replace(
        "swarm_stacks: []", "swarm_stacks:\n  - name: whoami\n    secrets: [whoami_token]\n    versions: {web: false}\n    backup: {}"))
    app, _ = make_app(repo)
    async with app.run_test(size=(120, 40), notifications=True) as pilot:
        await pilot.press("s")
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmScreen) and not app.pending
        assert any("sert encore" in n.message for n in app._notifications)


async def test_sauvegardes(repo):
    clipboard = Clipboard()
    app, written = make_app(repo, clipboard)
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.press("b")
        screen = app.screen
        assert isinstance(screen, BackupScreen)
        fill(screen, "#repository", "s3:https://s3.gra.io.cloud.ovh.net/sauvegardes")
        await press_generate(pilot, screen.query_one("#password", ValueField))
        fill(screen, "#access", "AKIA")
        fill_value(screen.query_one("#secret", ValueField), "SK")
        await press_ok(pilot)
        assert [row[1:] for row in app.rows] == [
            ("backup_password", "variable", "+ ajouté ● générée"),
            ("backup_s3_access_key", "variable", "+ ajouté"),
            ("backup_s3_secret_key", "variable", "+ ajouté"),
            ("s3:https://s3.gra.io.cloud.ovh.net/sauvegardes", "backup", "+ ajouté"),
        ]
        assert clipboard.history[-1] == ""


async def test_ecriture_refusee_le_plan_reste(repo):
    app, _ = make_app(repo)

    def refuse(changes):
        raise VaultError("infra-mcp lint refuse le résultat :\nproblème")

    app.writer = refuse
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("a")
        fill(app.screen, "#name", "a_token")
        fill_value(app.screen.query_one(ValueField), "x")
        await press_ok(pilot)
        await pilot.press("w")
        await press_ok(pilot)
        assert isinstance(app.screen, ConfirmScreen)  # « Rien n'a été écrit »
        await press_ok(pilot)
        assert len(app.pending) == 1 and app.is_running


async def test_quitter_avec_des_changements_demande_confirmation(repo):
    app, written = make_app(repo)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("a")
        app.screen.query_one("#kind", Select).value = "generated"
        fill(app.screen, "#name", "app_key")
        await press_ok(pilot)
        await pilot.press("q")
        assert isinstance(app.screen, ConfirmScreen)
        await press_ok(pilot)
    assert app.return_value is None and not written


def test_lignes_du_tableau(repo):
    set_secret(repo, "a_token", "un", PASSWORD)
    r = VaultRepo(repo, PASSWORD)
    pending = {c.key: c for c in [Change("delete", "a_token"), Change("variable", "x_pw", "v", generated=True)]}
    assert [row[1:] for row in table_rows(r, pending)] == [
        ("a_token", "secret", "− supprimé"), ("x_pw", "variable", "+ ajouté ● générée")]


def test_generation():
    values = {vault_tui.generate() for _ in range(50)}
    assert len(values) == 50 and all(len(v) == 32 and v.isalnum() for v in values)
