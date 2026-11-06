"""Interface plein écran de infra-mcp vault (Textual).

Un tableau de ce que contient le repo (secrets Docker, variables vault, secrets
générés sur le serveur, registres, dépôt des sauvegardes) et de ce qui est prévu ;
des raccourcis pour ajouter, remplacer, supprimer, annuler ; des fenêtres de
saisie. Rien n'est écrit avant « w » : récapitulatif, puis writer(changements),
qui écrit, vérifie avec le lint et committe (vault_cli.write_and_commit). Si
writer refuse, le plan reste là et on peut le corriger.

L'application rend le résultat de writer, ou None si on quitte sans écrire.
"""

import secrets
import string

import yaml
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Select, Static

from .vault import BACKUP_VARS, Change, VaultError, VaultRepo

ALPHABET = string.ascii_letters + string.digits  # rien à échapper dans une URL, un DSN, un .env
GENERATED_LENGTH = 32
NO_CLIPBOARD = ("aucun presse-papier utilisable (wl-copy, xclip, xsel, pbcopy) : installe wl-clipboard "
                "(sudo apt install wl-clipboard), ou saisis la valeur")

KINDS = {
    "secret": "secret Docker",
    "variable": "variable vault",
    "generated": "généré (serveur)",
    "registry": "registre",
    "backup": "dépôt des sauvegardes",
}
ADDABLE = [
    ("Secret Docker pour une stack (swarm_secrets)", "secret"),
    ("Variable vault seule (backup_credentials…)", "variable"),
    ("Secret généré sur le serveur : personne ne connaît la valeur", "generated"),
    ("Token d'un registre privé (ghcr.io…)", "registry"),
]


def generate(length: int = GENERATED_LENGTH) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def table_rows(repo: VaultRepo, pending: dict) -> list[tuple[tuple, str, str, str]]:
    """(clé, nom, sorte, état) : l'existant, puis ce qui est ajouté, chacun avec ce qui est prévu."""
    rows: dict[tuple, list] = {}
    for name in repo.secrets:
        rows[("name", name)] = [name, "secret"]
    for key in repo.vault:
        name = key.removeprefix("vault_")
        if not key.startswith("vault_registry_") and name not in repo.secrets:
            rows[("name", name)] = [name, "variable"]
    for name in repo.generated:
        rows[("name", name)] = [name, "generated"]
    for host in repo.registries:
        rows[("registry", host)] = [host, "registry"]
    if where := repo.backup_defined():
        rows[("backup",)] = [str((yaml.safe_load(where.read_text()) or {}).get("backup_repository", "")), "backup"]
    existing = set(rows)
    for key, change in pending.items():
        if key not in rows:
            kind = "backup" if change.kind == "backup_config" else change.kind
            rows[key] = [change.name, kind]
    out = []
    for key, (name, kind) in rows.items():
        change = pending.get(key)
        if change is None:
            state = "inchangé"
        elif change.kind.startswith("delete"):
            state = "− supprimé"
        else:
            state = ("~ remplacé" if key in existing else "+ ajouté") + (" ● générée" if change.generated else "")
        out.append((key, name, kind, state))
    return out


# --- Fenêtres ---------------------------------------------------------------------


class ValueField(Vertical):
    """Valeur saisie deux fois sans écho, ou générée : copiée dans le presse-papier, jamais affichée."""

    def __init__(self, label: str, clipboard, can_generate: bool = True, **kwargs):
        super().__init__(classes="field", **kwargs)
        self.label, self.copier, self.can_generate = label, clipboard, can_generate
        self.generated: str | None = None

    def compose(self) -> ComposeResult:
        yield Static(Text(self.label), classes="label")
        with Horizontal(classes="line"):
            yield Input(password=True, placeholder="valeur (sans écho)", classes="value", compact=True)
            yield Input(password=True, placeholder="encore une fois", classes="confirm", compact=True)
            if self.can_generate:
                yield Button("Générer", classes="generate", compact=True)
        yield Static("", classes="status")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if not event.button.has_class("generate"):
            return
        event.stop()
        value = generate()
        if not self.copier.available() or not self.copier.copy(value):
            self.app.notify(NO_CLIPBOARD, severity="error", markup=False)
            return
        self.generated = value
        for widget in self.query(Input):
            widget.value, widget.disabled = "", True
        event.button.disabled = True
        status = self.query_one(".status", Static)
        status.update(f"✓ valeur générée ({GENERATED_LENGTH} caractères) et copiée dans le presse-papier : "
                      "range-la (gestionnaire de mots de passe) avant de valider")
        status.display = True

    def get(self) -> tuple[str, bool]:
        if self.generated is not None:
            return self.generated, True
        value, confirm = self.query_one(".value", Input).value, self.query_one(".confirm", Input).value
        if not value:
            raise VaultError(f"{self.label} : valeur vide")
        if value != confirm:
            raise VaultError(f"{self.label} : les deux saisies diffèrent")
        return value, False


class Dialog(ModalScreen):
    """Fenêtre centrée, Échap pour annuler. Le presse-papier est vidé si une valeur y est passée."""

    BINDINGS = [Binding("escape", "cancel", "annuler")]

    def __init__(self, clipboard=None):
        super().__init__()
        self.copier = clipboard

    def error(self, message: str) -> None:
        self.query_one("#error", Static).update(Text(message))

    def _done(self, result) -> None:
        if self.copier and any(f.generated for f in self.query(ValueField)):
            self.copier.clear()
            self.app.notify("presse-papier vidé")
        self.dismiss(result)

    def action_cancel(self) -> None:
        self._done(None)

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(Text(self.heading()), classes="title")
            with VerticalScroll(classes="body"):
                yield from self.body()
            yield Static("", id="error", classes="error")
            with Horizontal(classes="buttons"):
                yield Button(self.ok_label, variant="primary", id="ok", compact=True)
                yield Button("Annuler", id="cancel", compact=True)

    ok_label = "OK"

    def heading(self) -> str:
        return ""

    def body(self) -> ComposeResult:
        yield from ()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self._done(None)
        elif event.button.id == "ok":
            try:
                self._done(self.submit())
            except VaultError as exc:
                self.error(str(exc))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.query_one("#ok", Button).press()  # Entrée dans un champ : comme le bouton principal

    def submit(self):
        return True


class AddScreen(Dialog):
    def __init__(self, repo: VaultRepo, clipboard):
        super().__init__(clipboard)
        self.repo = repo

    ok_label = "Ajouter"

    def heading(self) -> str:
        return "Ajouter"

    def body(self) -> ComposeResult:
        yield Select(ADDABLE, value="secret", allow_blank=False, id="kind", compact=True)
        yield Static("Nom", classes="label")
        yield Input(placeholder="minuscules, chiffres, _ : ex. app_mailer_dsn", id="name", compact=True)
        yield Static("Compte qui possède le token", classes="label", id="username-label")
        yield Input(placeholder="ex. quiche-lorraine", id="username", compact=True)
        yield ValueField("Valeur", self.copier, id="value")
        yield Static("", id="help", classes="help")

    def on_mount(self) -> None:
        self._kind_changed("secret")
        self.query_one("#name", Input).focus()

    def on_select_changed(self, event: Select.Changed) -> None:
        self._kind_changed(event.value)

    def _kind_changed(self, kind) -> None:
        self.query_one("#username").display = self.query_one("#username-label").display = kind == "registry"
        self.query_one("#value").display = kind != "generated"
        self.query_one("#name", Input).placeholder = (
            "nom d'hôte du registre : ex. ghcr.io" if kind == "registry" else "minuscules, chiffres, _ : ex. app_mailer_dsn")
        self.query_one("#help", Static).update({
            "secret": "Déclaré dans swarm_secrets. Reste à le lister dans la stack et son compose (external: true). "
                      "Si personne n'a besoin de connaître la valeur, préfère « secret généré sur le serveur ».",
            "variable": "Seulement dans vault.yml, sous vault_<nom> : pour backup_credentials ou un mot de passe de registre.",
            "generated": "Créé au prochain apply stacks avec une valeur aléatoire, jamais modifié ensuite. "
                         "La valeur ne passe ni par le repo ni par toi.",
            "registry": "Le manager s'y connecte avant de déployer. Pour ghcr.io : token classic, scope read:packages.",
        }[kind])

    def submit(self) -> Change:
        kind = self.query_one("#kind", Select).value
        name = self.query_one("#name", Input).value.strip()
        username = self.query_one("#username", Input).value.strip()
        value, generated = ("", False) if kind == "generated" else self.query_one(ValueField).get()
        change = Change(kind, name, value, username=username, generated=generated)
        self.repo.check(change)
        return change


class ValueScreen(Dialog):
    def __init__(self, title: str, clipboard, can_generate: bool = True):
        super().__init__(clipboard)
        self.title_text, self.can_generate = title, can_generate

    ok_label = "Remplacer"

    def heading(self) -> str:
        return self.title_text

    def body(self) -> ComposeResult:
        yield ValueField("Nouvelle valeur", self.copier, can_generate=self.can_generate)

    def submit(self) -> tuple[str, bool]:
        return self.query_one(ValueField).get()


class BackupScreen(Dialog):
    def __init__(self, repo: VaultRepo, clipboard):
        super().__init__(clipboard)
        self.repo = repo
        self.where = repo.backup_defined()

    ok_label = "Prévoir"

    def heading(self) -> str:
        return "Sauvegardes restic"

    def body(self) -> ComposeResult:
        if self.where is None:
            yield Static("Dépôt restic, dans une autre région que le serveur", classes="label")
            yield Input(placeholder="s3:https://s3.<région>.io.cloud.ovh.net/<bucket>", id="repository", compact=True)
        else:
            note = f"Dépôt déjà configuré dans {self.where.relative_to(self.repo.root)} : seules les valeurs changent."
            if not self.repo.backup_credentials_ok(self.where):
                note += (" Attention : son backup_credentials ne lit pas vault_backup_password, "
                         "vault_backup_s3_access_key et vault_backup_s3_secret_key.")
            yield Static(Text(note), classes="help")
        yield ValueField("Mot de passe du dépôt : sans lui, les sauvegardes sont illisibles. Garde-le hors du serveur.",
                         self.copier, id="password")
        yield Static("Clé d'accès S3 (un identifiant, affiché)", classes="label")
        yield Input(placeholder="clé d'accès", id="access", compact=True)
        yield ValueField("Clé secrète S3", self.copier, can_generate=False, id="secret")

    def submit(self) -> list[Change]:
        password, generated = self.query_one("#password", ValueField).get()
        access = self.query_one("#access", Input).value.strip()
        if not access:
            raise VaultError("clé d'accès S3 vide")
        secret, _ = self.query_one("#secret", ValueField).get()
        changes = [
            Change("variable", BACKUP_VARS["password"], password, generated=generated),
            Change("variable", BACKUP_VARS["s3_access_key"], access),
            Change("variable", BACKUP_VARS["s3_secret_key"], secret),
        ]
        if self.where is None:
            changes.append(Change("backup_config", self.query_one("#repository", Input).value.strip()))
        for change in changes:
            self.repo.check(change)
        return changes


class ConfirmScreen(Dialog):
    def __init__(self, title: str, lines: list[str], ok: str):
        super().__init__()
        self.title_text, self.lines, self.ok_label = title, lines, ok

    def heading(self) -> str:
        return self.title_text

    def body(self) -> ComposeResult:
        for line in self.lines:
            yield Static(Text(line))

    def on_mount(self) -> None:
        self.query_one("#ok", Button).focus()  # Entrée confirme, Échap annule


# --- Application ------------------------------------------------------------------


class VaultApp(App):
    TITLE = "infra-mcp vault"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    #table { height: 1fr; }
    #hint { height: auto; padding: 0 1; color: $text-muted; }
    Dialog { align: center middle; }
    .dialog { width: 96; max-width: 100%; height: auto; max-height: 100%;
              border: round $primary; background: $surface; padding: 0 1; }
    .dialog .body { height: auto; max-height: 1fr; }
    .title { text-style: bold; }
    .label { margin-top: 1; color: $text-muted; }
    .field { height: auto; }
    .line { height: auto; }
    .line Input { width: 1fr; margin-right: 1; }
    .status { color: $success; height: auto; display: none; }
    .dialog Input { background: $boost; }
    .dialog Input:focus { background: $primary 20%; }
    .dialog Select > SelectCurrent { background: $boost; }
    .help { color: $text-muted; height: auto; margin-top: 1; }
    .error { color: $error; height: auto; }
    .buttons { height: auto; margin-top: 1; align-horizontal: right; }
    .buttons Button { margin-left: 2; }
    """
    BINDINGS = [
        Binding("a", "add", "ajouter"),
        Binding("r", "replace", "remplacer"),
        Binding("s", "delete", "supprimer"),
        Binding("u", "undo", "annuler"),
        Binding("b", "backup", "sauvegardes"),
        Binding("w", "write", "écrire"),
        Binding("q", "leave", "quitter"),
    ]

    def __init__(self, repo: VaultRepo, writer, clipboard):
        super().__init__()
        self.repo, self.writer, self.copier = repo, writer, clipboard
        self.pending: dict[tuple, Change] = {}
        self.rows: list[tuple] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
        yield Static(id="hint")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = f"{self.repo.root.name} (main)"
        self.query_one(DataTable).add_columns("Nom", "Sorte", "État")
        self.refresh_table()

    def refresh_table(self) -> None:
        table = self.query_one(DataTable)
        cursor = table.cursor_row
        table.clear()
        self.rows = table_rows(self.repo, self.pending)
        for key, name, kind, state in self.rows:
            style = "bold green" if state.startswith("+") else "bold yellow" if state.startswith("~") \
                else "bold red" if state.startswith("−") else ""
            table.add_row(Text(name), Text(KINDS[kind]), Text(state, style=style))
        if self.rows:
            table.move_cursor(row=min(cursor, len(self.rows) - 1))
        count = len(self.pending)
        self.query_one("#hint", Static).update(
            f"{count} changement(s) prévu(s) · w : écrire, committer, pousser" if count
            else "Rien de prévu · a : ajouter · sur une ligne : r remplacer, s supprimer")

    def selected(self) -> tuple | None:
        if not self.rows:
            return None
        return self.rows[self.query_one(DataTable).cursor_row]

    def plan(self, change: Change | None) -> None:
        if change is None:
            return
        self.pending.pop(change.key, None)
        self.pending[change.key] = change
        self.refresh_table()
        self.notify(f"prévu : {self.repo.describe(change)}", markup=False)

    def fail(self, message: str) -> None:
        self.notify(message, severity="error", markup=False)

    # Actions
    def action_add(self) -> None:
        self.push_screen(AddScreen(self.repo, self.copier), self.plan)

    def action_replace(self) -> None:
        if not (row := self.selected()):
            return
        key, name, kind, _ = row
        if kind not in ("secret", "variable", "registry"):
            self.fail("seuls un secret Docker, une variable vault ou un token de registre ont une valeur à remplacer")
            return
        username = ""
        if kind == "registry":
            planned = self.pending.get(key)
            username = planned.username if planned and planned.kind == "registry" else \
                (self.repo.registries.get(name) or {}).get("username", "")

        def done(result):
            if result:
                value, generated = result
                change = Change(kind, name, value, username=username, generated=generated)
                try:
                    self.repo.check(change)
                except VaultError as exc:
                    self.fail(str(exc))
                    return
                self.plan(change)

        self.push_screen(ValueScreen(f"Remplacer {name} ({KINDS[kind]})", self.copier), done)

    def action_delete(self) -> None:
        if not (row := self.selected()):
            return
        key, name, kind, state = row
        if state.startswith("+"):
            self.pending.pop(key, None)  # ajout pas encore écrit : on l'annule
            self.refresh_table()
            self.notify(f"{name} : ajout annulé", markup=False)
            return
        if kind == "backup":
            self.fail("le dépôt des sauvegardes se retire à la main (backup.yml)")
            return
        try:
            change = self.repo.deletion(name)
        except VaultError as exc:
            self.fail(str(exc))
            return
        note = ["Le swarm garde le secret Docker : docker secret rm sur un manager, une fois les stacks redéployées."] \
            if kind in ("secret", "generated") else []
        self.push_screen(ConfirmScreen(f"Supprimer {name} ({KINDS[kind]}) ?", note, "Supprimer"),
                         lambda ok: self.plan(change) if ok else None)

    def action_undo(self) -> None:
        if (row := self.selected()) and self.pending.pop(row[0], None):
            self.refresh_table()
            self.notify(f"{row[1]} : changement annulé", markup=False)

    def action_backup(self) -> None:
        def done(changes):
            for change in changes or []:
                self.plan(change)

        self.push_screen(BackupScreen(self.repo, self.copier), done)

    def action_write(self) -> None:
        if not self.pending:
            self.notify("rien de prévu")
            return
        changes = list(self.pending.values())
        lines = [f"• {self.repo.describe(c)}" for c in changes]

        def done(ok):
            if not ok:
                return
            try:
                result = self.writer(changes)
            except VaultError as exc:
                self.push_screen(ConfirmScreen("Rien n'a été écrit", str(exc).splitlines(), "OK"))
                return
            self.exit(result)

        self.push_screen(ConfirmScreen("Écrire, vérifier (lint) et committer sur une branche vault/… :",
                                       lines, "Écrire et committer"), done)

    def action_leave(self) -> None:
        if not self.pending:
            self.exit(None)
            return
        self.push_screen(ConfirmScreen(f"Quitter sans rien écrire ? {len(self.pending)} changement(s) seront perdus.",
                                       [], "Quitter"), lambda ok: self.exit(None) if ok else None)
