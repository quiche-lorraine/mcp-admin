"""infra-mcp vault : les secrets du repo de config, en plein écran, sur le poste de l'admin.

Depuis un clone du repo de config : passe sur main à jour (refuse s'il reste des
modifications non commitées) et demande le mot de passe vault, dans le terminal ;
puis l'interface plein écran (vault_tui.py) prépare les changements. Rien n'est
écrit avant d'y valider : écriture d'un coup, lint, commit sur une branche
vault/<date> (write_and_commit). L'interface fermée, la branche est poussée, dans
le terminal (git peut y demander ses identifiants), et le lien de la PR affiché.

Une valeur générée n'est jamais affichée : elle va dans le presse-papier (wl-copy,
xclip, xsel ou pbcopy), le temps de la ranger, puis le presse-papier est vidé.
"""

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from getpass import getpass
from pathlib import Path

from . import lint
from .vault import BACKUP_FILE, VARS_DIR, Change, VaultError, VaultRepo, apply, restore, snapshot

MAIN = "main"


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


class SystemClipboard:
    def available(self) -> bool:
        return _clipboard() is not None

    def copy(self, value: str) -> bool:
        tool = _clipboard()
        return tool is not None and _run_clipboard(tool[0], value)

    def clear(self) -> None:
        if tool := _clipboard():
            _run_clipboard(tool[1] or tool[0], "")


class Terminal:
    """Ce qui se passe hors de l'interface plein écran : avant (git, mot de passe) et après (push)."""

    def say(self, text: str = "") -> None:
        print(text)

    def ask(self, prompt: str, default: str = "") -> str:
        answer = input(f"{prompt} [{default}] : " if default else f"{prompt} : ").strip()
        return answer or default

    def hidden(self, prompt: str) -> str:
        return getpass(f"{prompt} (sans écho) : ")


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


# --- Écriture -------------------------------------------------------------------------


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


def _commit_message(repo: VaultRepo, changes: list[Change]) -> tuple[str, str]:
    lines = [repo.describe(c) for c in changes]
    title = "vault : " + ", ".join(c.name for c in changes)
    if len(title) > 72:
        title = f"vault : {len(changes)} changements"
    return title, "\n".join(f"- {line}" for line in lines) + "\n\nÉcrit par infra-mcp vault (aucune valeur dans ce message)."


def write_and_commit(repo: VaultRepo, changes: list[Change]) -> dict:
    """Écrit les changements, vérifie avec le lint, committe sur une nouvelle branche vault/<date>.

    Lève VaultError si rien n'a été écrit (lint, git) : les fichiers sont alors remis comme
    avant. Si seul le commit échoue, les fichiers restent écrits sur la branche et
    commit_error le dit."""
    root = repo.root
    branch = _free_branch(root)
    baseline = set(lint.run(root))  # n'arrêter que sur ce que ces changements ajoutent
    swarm_deleted = [c.name for c in changes if c.kind == "delete" and (c.name in repo.secrets or c.name in repo.generated)]
    title, body = _commit_message(repo, changes)  # avant apply : « ajouté » ou « remplacé » selon l'état de départ
    saved = snapshot(repo)
    done = apply(repo, changes)
    if new := [p for p in lint.run(root) if p not in baseline]:
        restore(saved)
        raise VaultError("infra-mcp lint refuse le résultat :\n" + "\n".join(new))
    try:
        _git(root, "switch", "--quiet", "-c", branch)
    except VaultError:
        restore(saved)
        raise
    result = {"branch": branch, "done": done, "swarm_deleted": swarm_deleted, "commit_error": None,
              "backup_created": any(c.kind == "backup_config" for c in changes)}
    try:
        _git(root, "add", "--", str(VARS_DIR))
        _git(root, "commit", "--quiet", "-m", title, "-m", body)
    except VaultError as exc:
        result["commit_error"] = str(exc)
    return result


def report_and_push(root: Path, result: dict, term) -> int:
    branch = result["branch"]
    if result["commit_error"]:
        term.say(f"fichiers écrits sur la branche {branch}, mais le commit a échoué ({result['commit_error']}) : "
                 "committe à la main")
        return 1
    term.say(f"commit sur {branch} :")
    for line in result["done"]:
        term.say(f"  {line}")
    if result["swarm_deleted"]:
        term.say(f"le swarm garde les secrets Docker retirés ({', '.join(result['swarm_deleted'])}) : "
                 "docker secret ls puis docker secret rm sur un manager, une fois les stacks redéployées")
    if result["backup_created"]:
        term.say(f"{BACKUP_FILE} créé : chaque entrée de swarm_stacks doit avoir sa clé backup (README, « Sauvegardes »)")
    if not _yes(term, f"Pousser {branch} ?", True):
        term.say(f"pour pousser plus tard : git push -u origin {branch}")
        return 0
    if subprocess.run(["git", "push", "--quiet", "-u", "origin", branch], cwd=root).returncode != 0:
        term.say(f"push raté : git push -u origin {branch}")
        return 1
    if url := _pr_url(root, branch):
        term.say(f"ouvre la PR : {url}")
    return 0


def run(start: Path = Path("."), term=None, app_class=None, clipboard=None) -> int:
    term = term or Terminal()
    try:
        root = prepare(start, term)
        repo = open_vault(root, term)
        if app_class is None:
            from .vault_tui import VaultApp as app_class
        app = app_class(repo, writer=lambda changes: write_and_commit(repo, changes),
                        clipboard=clipboard or SystemClipboard())
        result = app.run()
        if result is None:
            term.say("rien n'a été écrit")
            return 0
        return report_and_push(root, result, term)
    except VaultError as exc:
        term.say(f"erreur : {exc}")
        return 1
    except (KeyboardInterrupt, EOFError):
        term.say("\nabandon : rien n'a été écrit")
        return 130


def main(repo: Path) -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        sys.exit("infra-mcp vault est interactif : lance-le dans un terminal (ou utilise infra-mcp secret set)")
    sys.exit(run(repo))
