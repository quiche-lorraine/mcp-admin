"""infra-mcp vault : session interactive pour les secrets du repo de config, sur le poste de l'admin.

Depuis un clone du repo de config : passe sur main à jour (refuse s'il reste des
modifications non commitées), demande le mot de passe vault une fois, puis ajoute,
remplace, génère ou supprime des secrets les uns après les autres. Rien n'est écrit
avant la fin : récapitulatif, écriture, lint, commit sur une branche vault/<date>,
push. Ctrl-C à tout moment : rien n'est écrit.

Une valeur générée n'est jamais affichée : elle va dans le presse-papier (wl-copy,
xclip, xsel ou pbcopy), le temps de la ranger, puis le presse-papier est vidé.
"""

import os
import re
import secrets
import shutil
import string
import subprocess
import sys
from datetime import datetime
from getpass import getpass
from pathlib import Path

from . import lint
from .vault import BACKUP_FILE, BACKUP_VARS, VARS_DIR, Change, VaultError, VaultRepo, apply, restore, snapshot

MAIN = "main"
ALPHABET = string.ascii_letters + string.digits  # rien à échapper dans une URL, un DSN, un .env
GENERATED_LENGTH = 32
NO_CLIPBOARD = ("aucun presse-papier utilisable (wl-copy, xclip, xsel, pbcopy) : "
                "installe wl-clipboard (sudo apt install wl-clipboard), ou saisis la valeur")

MENU = """
  1  secret Docker pour une stack (swarm_secrets)
  2  variable vault seule (mot de passe des sauvegardes…)
  3  secret généré sur le serveur (swarm_secrets_generated : personne ne connaît la valeur)
  4  token d'un registre privé (ghcr.io…)
  5  sauvegardes restic : dépôt, mot de passe, accès S3
  6  supprimer un secret, une variable ou un registre
  7  revoir ce qui est prévu
  Entrée : terminer (récapitulatif, écriture, commit, push)"""


# --- Terminal et presse-papier ------------------------------------------------------


def _clipboard() -> tuple[list[str], list[str] | None] | None:
    """(commande pour copier, commande pour vider ou None : copier une valeur vide)."""
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        return ["wl-copy"], ["wl-copy", "--clear"]
    if os.environ.get("DISPLAY"):
        if shutil.which("xclip"):
            return ["xclip", "-selection", "clipboard"], None
        if shutil.which("xsel"):
            return ["xsel", "--clipboard", "--input"], ["xsel", "--clipboard", "--clear"]
    if shutil.which("pbcopy"):
        return ["pbcopy"], None
    return None


def _run_clipboard(cmd: list[str], value: str) -> bool:
    # Sorties vers /dev/null : wl-copy et xclip restent en arrière-plan pour servir le
    # presse-papier, run() attendrait sinon la fermeture de leurs tubes.
    try:
        res = subprocess.run(cmd, input=value, text=True, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return res.returncode == 0


class Terminal:
    def say(self, text: str = "") -> None:
        print(text)

    def ask(self, prompt: str, default: str = "") -> str:
        answer = input(f"{prompt} [{default}] : " if default else f"{prompt} : ").strip()
        return answer or default

    def hidden(self, prompt: str) -> str:
        return getpass(f"{prompt} (sans écho) : ")

    def can_copy(self) -> bool:
        return _clipboard() is not None

    def copy(self, value: str) -> bool:
        tool = _clipboard()
        return tool is not None and _run_clipboard(tool[0], value)

    def clear(self) -> None:
        tool = _clipboard()
        if tool:
            _run_clipboard(tool[1], "") if tool[1] else _run_clipboard(tool[0], "")


def _yes(term, prompt: str, default: bool) -> bool:
    answer = term.ask(f"{prompt} {'[O/n]' if default else '[o/N]'}").lower()
    return default if not answer else answer.startswith(("o", "y"))


# --- Git ----------------------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if res.returncode != 0:
        raise VaultError(f"git {' '.join(args)} : {(res.stderr or res.stdout).strip()}")
    return res.stdout.strip()


def prepare(start: Path, term) -> Path:
    """Racine du clone, sur main à jour. Refuse s'il reste des modifications non commitées."""
    try:
        root = Path(_git(start, "rev-parse", "--show-toplevel"))
    except (VaultError, OSError):
        raise VaultError(f"{start} : pas dans un clone git du repo de config") from None
    if _git(root, "status", "--porcelain", "--untracked-files=no"):
        raise VaultError("modifications non commitées dans le repo : committe-les ou mets-les de côté (git stash) d'abord")
    branch = subprocess.run(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], cwd=root,
                            capture_output=True, text=True).stdout.strip()
    if branch != MAIN:
        term.say(f"{branch or 'HEAD détachée'} → {MAIN}")
        _git(root, "switch", "--quiet", MAIN)
    term.say(f"mise à jour de {MAIN} (git pull --ff-only)…")
    _git(root, "pull", "--ff-only", "--quiet", "origin", MAIN)
    if not (root / VARS_DIR).is_dir():
        raise VaultError(f"{root / VARS_DIR} absent : ce n'est pas un repo de config infra-mcp")
    return root


def _free_branch(root: Path) -> str:
    """vault/<date>, suivi de -2, -3… si le nom est déjà pris (ici ou sur origin)."""
    base = f"vault/{datetime.now():%Y%m%d-%H%M%S}"
    for n in range(1, 100):
        branch = base if n == 1 else f"{base}-{n}"
        taken = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=root,
                               capture_output=True).returncode == 0 or subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"], cwd=root,
            capture_output=True).returncode == 0
        if not taken:
            return branch
    raise VaultError(f"{base} : aucun nom de branche libre")


def _pr_url(root: Path, branch: str) -> str | None:
    try:
        url = _git(root, "remote", "get-url", "origin")
    except VaultError:
        return None
    m = re.fullmatch(r"(?:git@github\.com:|https://github\.com/)([^/]+)/(.+?)(?:\.git)?/?", url)
    return f"https://github.com/{m.group(1)}/{m.group(2)}/compare/{MAIN}...{branch}?expand=1" if m else None


# --- Session ------------------------------------------------------------------------


def open_vault(root: Path, term) -> VaultRepo:
    if path := os.environ.get("ANSIBLE_VAULT_PASSWORD_FILE"):
        try:
            password = Path(path).expanduser().read_text().strip()
        except OSError as exc:
            raise VaultError(f"ANSIBLE_VAULT_PASSWORD_FILE : {exc}") from None
        return VaultRepo(root, password)
    if not (root / VARS_DIR / "vault.yml").exists():
        password = term.hidden("Mot de passe du nouveau vault")
        if not password or term.hidden("Encore une fois") != password:
            raise VaultError("mot de passe vide ou les deux saisies diffèrent")
        return VaultRepo(root, password)
    for _ in range(3):
        try:
            return VaultRepo(root, term.hidden("Mot de passe vault"))
        except VaultError as exc:
            term.say(str(exc))
    raise VaultError("trois essais sans le bon mot de passe")


def _value(term, label: str, generate_default: bool = False) -> tuple[str, bool]:
    """(valeur, générée ?) : saisie deux fois sans écho, ou générée et passée par le presse-papier."""
    mode = term.ask(f"{label} : [s]aisir ou [g]énérer", "g" if generate_default else "s").lower()
    if mode.startswith("g"):
        if not term.can_copy():
            raise VaultError(NO_CLIPBOARD)
        length = term.ask("Longueur", str(GENERATED_LENGTH))
        if not length.isdigit() or not 16 <= int(length) <= 256:
            raise VaultError("longueur : un nombre entre 16 et 256")
        value = "".join(secrets.choice(ALPHABET) for _ in range(int(length)))
        if not term.copy(value):
            raise VaultError(NO_CLIPBOARD)
        term.ask("Valeur copiée dans le presse-papier : range-la (gestionnaire de mots de passe), puis Entrée")
        term.clear()
        term.say("presse-papier vidé")
        return value, True
    if not mode.startswith("s"):
        raise VaultError("réponds s ou g")
    value = term.hidden(label)
    if term.hidden("Encore une fois") != value:
        raise VaultError("les deux saisies diffèrent")
    return value, False


class Session:
    def __init__(self, repo: VaultRepo, term):
        self.repo, self.term = repo, term
        self.pending: dict[tuple, Change] = {}

    def add(self, change: Change) -> None:
        self.repo.check(change)
        self.pending.pop(change.key, None)
        self.pending[change.key] = change
        self.term.say(f"prévu : {self.repo.describe(change)}")

    def _name(self, prompt: str, kind: str) -> str:
        name = self.term.ask(prompt)
        self.repo.check(Change(kind, name, value="-", username="-"))  # le nom, avant de demander la valeur
        return name

    def secret(self):
        name = self._name("Nom du secret Docker (ex. app_mailer_dsn)", "secret")
        self.term.say("si personne n'a besoin de connaître la valeur, le choix 3 évite qu'elle passe par toi")
        value, generated = _value(self.term, "Valeur")
        self.add(Change("secret", name, value, generated=generated))

    def variable(self):
        name = self._name("Nom de la variable (vault_<nom>, ex. backup_password)", "variable")
        value, generated = _value(self.term, "Valeur")
        self.add(Change("variable", name, value, generated=generated))

    def generated(self):
        self.add(Change("generated", self._name("Nom du secret généré (ex. app_key)", "generated")))

    def registry(self):
        host = self.term.ask("Registre (nom d'hôte, ex. ghcr.io)")
        username = self.term.ask("Compte qui possède le token")
        self.repo.check(Change("registry", host, value="-", username=username))
        token, _ = _value(self.term, "Token")
        self.add(Change("registry", host, token, username=username))

    def backup(self):
        where = self.repo.backup_defined()
        if where is None:
            repository = self.term.ask("Dépôt restic (ex. s3:https://s3.<région>.io.cloud.ovh.net/<bucket>)")
            self.repo.check(Change("backup_config", repository))
        else:
            repository = None
            self.term.say(f"dépôt déjà configuré dans {where.relative_to(self.repo.root)} : seules les valeurs changent")
            if not self.repo.backup_credentials_ok(where):
                self.term.say("attention : son backup_credentials ne lit pas vault_backup_password, "
                              "vault_backup_s3_access_key, vault_backup_s3_secret_key")
        self.term.say("sans le mot de passe du dépôt, les sauvegardes sont illisibles : garde-le hors du serveur")
        changes = []
        for key, var in BACKUP_VARS.items():
            label = {"password": "Mot de passe du dépôt", "s3_access_key": "Clé d'accès S3",
                     "s3_secret_key": "Clé secrète S3"}[key]
            value, generated = _value(self.term, label, generate_default=key == "password")
            changes.append(Change("variable", var, value, generated=generated))
        if repository is not None:
            changes.append(Change("backup_config", repository))
        for change in changes:
            self.add(change)

    def delete(self):
        target = self.term.ask("Nom à supprimer (secret, variable, ou registre comme ghcr.io)")
        change = self.repo.deletion(target)
        if _yes(self.term, f"Supprimer {target} ?", False):
            self.add(change)

    def review(self):
        if not self.pending:
            self.term.say("rien de prévu pour l'instant")
        for change in self.pending.values():
            self.term.say(f"  - {self.repo.describe(change)}")

    def loop(self) -> None:
        actions = {"1": self.secret, "2": self.variable, "3": self.generated, "4": self.registry,
                   "5": self.backup, "6": self.delete, "7": self.review}
        self.term.say(MENU)
        while True:
            choice = self.term.ask("\nChoix (1-7, ? : menu, Entrée : terminer)")
            if not choice:
                return
            if choice not in actions:
                self.term.say(MENU if choice == "?" else "choix inconnu (? pour le menu)")
                continue
            try:
                actions[choice]()
            except VaultError as exc:
                self.term.say(f"refusé : {exc}")


def _commit_message(repo: VaultRepo, changes: list[Change]) -> tuple[str, str]:
    lines = [repo.describe(c) for c in changes]
    title = "vault : " + ", ".join(c.name for c in changes)
    if len(title) > 72:
        title = f"vault : {len(changes)} changements"
    return title, "\n".join(f"- {line}" for line in lines) + "\n\nÉcrit par infra-mcp vault (aucune valeur dans ce message)."


def run(start: Path = Path("."), term=None) -> int:
    term = term or Terminal()
    written = False
    try:
        root = prepare(start, term)
        repo = open_vault(root, term)
        term.say("\nDans le repo :")
        for line in repo.summary():
            term.say(f"  {line}")
        session = Session(repo, term)
        session.loop()
        changes = list(session.pending.values())
        if not changes:
            term.say("rien à écrire")
            return 0
        term.say("\nÀ écrire :")
        session.review()
        if not _yes(term, "Écrire et committer sur une branche vault/… ?", True):
            term.say("rien n'a été écrit")
            return 1

        branch = _free_branch(root)
        baseline = set(lint.run(root))  # n'arrêter que sur ce que ces changements ajoutent
        swarm_deleted = [c.name for c in changes
                         if c.kind == "delete" and (c.name in repo.secrets or c.name in repo.generated)]
        saved = snapshot(repo)
        done = apply(repo, changes)
        written = True
        if new := [p for p in lint.run(root) if p not in baseline]:
            restore(saved)
            written = False
            raise VaultError("infra-mcp lint refuse le résultat, rien n'a été écrit :\n" + "\n".join(new))

        try:
            _git(root, "switch", "--quiet", "-c", branch)
        except VaultError:
            restore(saved)
            written = False
            raise
        _git(root, "add", "--", str(VARS_DIR))
        title, body = _commit_message(repo, changes)
        try:
            _git(root, "commit", "--quiet", "-m", title, "-m", body)
        except VaultError as exc:
            raise VaultError(f"fichiers écrits sur la branche {branch}, mais le commit a échoué ({exc}) : "
                             "committe à la main") from None
        term.say(f"\ncommit sur {branch} :")
        for line in done:
            term.say(f"  {line}")
        if swarm_deleted:
            term.say(f"le swarm garde les secrets Docker retirés ({', '.join(swarm_deleted)}) : "
                     "docker secret ls puis docker secret rm sur un manager, une fois les stacks redéployées")

        if _yes(term, f"Pousser {branch} ?", True):
            if subprocess.run(["git", "push", "--quiet", "-u", "origin", branch], cwd=root).returncode != 0:
                term.say(f"push raté : git push -u origin {branch}")
                return 1
            if url := _pr_url(root, branch):
                term.say(f"ouvre la PR : {url}")
        else:
            term.say(f"pour pousser plus tard : git push -u origin {branch}")
        if any(c.kind == "backup_config" for c in changes):
            term.say(f"{BACKUP_FILE} créé : chaque entrée de swarm_stacks doit avoir sa clé backup (README, « Sauvegardes »)")
        return 0
    except VaultError as exc:
        term.say(f"erreur : {exc}")
        return 1
    except (KeyboardInterrupt, EOFError):
        term.say("\nabandon" + ("" if written else " : rien n'a été écrit"))
        return 130


def main(repo: Path) -> None:
    if not sys.stdin.isatty():
        sys.exit("infra-mcp vault est interactif : lance-le dans un terminal (ou utilise infra-mcp secret set)")
    sys.exit(run(repo))
