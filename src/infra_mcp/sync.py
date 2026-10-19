"""Synchronisation du repo de config, conditionnée par infra-mcp lint.

Le contrôleur ne suit que sa branche, en avance rapide. Le nouveau commit est
d'abord extrait à part et vérifié : s'il ne passe pas les garde-fous, le repo
déployé reste où il est, et la commande échoue (infra-mcp-sync.service apparaît
alors en échec dans systemctl, et le journal dit pourquoi).
"""

import io
import subprocess
import tarfile
import tempfile
from pathlib import Path

from . import lint


class SyncError(Exception):
    pass


def _git(repo: Path, *args: str, binary: bool = False):
    res = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=not binary)
    if res.returncode != 0:
        err = res.stderr.decode(errors="replace") if binary else res.stderr
        raise SyncError(f"git {' '.join(args)} : {err.strip()}")
    return res.stdout


def sync(repo: Path) -> str:
    """Avance le repo au dernier commit de sa branche s'il passe le lint. Renvoie un résumé."""
    branch = _git(repo, "symbolic-ref", "--short", "HEAD").strip()
    _git(repo, "fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
    head = _git(repo, "rev-parse", "HEAD").strip()
    target = _git(repo, "rev-parse", f"refs/remotes/origin/{branch}").strip()
    if target == head:
        return f"à jour ({head[:7]})"
    if subprocess.run(["git", "merge-base", "--is-ancestor", head, target], cwd=repo).returncode != 0:
        raise SyncError(f"origin/{branch} ({target[:7]}) ne descend pas de {head[:7]} : historique réécrit, rien n'est appliqué")

    # Le commit est vérifié tel quel, hors du repo déployé.
    archive = _git(repo, "archive", "--format=tar", target, binary=True)
    with tempfile.TemporaryDirectory(prefix="infra-mcp-sync-") as tmp:
        try:
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                tar.extractall(tmp, filter="data")
        except tarfile.TarError as e:
            # Lien vers l'extérieur du repo, chemin absolu… : refusé comme le reste.
            raise SyncError(f"{target[:7]} : contenu refusé ({e}), le repo reste à {head[:7]}") from e
        problems = lint.run(Path(tmp))
    if problems:
        shown = "\n".join(p.replace(tmp, f"<{target[:7]}>") for p in problems[:20])
        hint = ""
        if subprocess.run(["git", "diff", "--quiet", head, target, "--", "requirements.txt"], cwd=repo).returncode:
            # Le commit change de version d'outil : son lint est peut-être plus récent.
            hint = "\nrequirements.txt change : sudo infra-mcp-admin update (vérifie avec l'outil à la nouvelle version)"
        raise SyncError(
            f"{target[:7]} ne passe pas infra-mcp lint ({len(problems)} problème(s)), le repo reste à {head[:7]} :\n{shown}{hint}"
        )

    _git(repo, "merge", "--ff-only", "--quiet", target)
    subject = _git(repo, "log", "-1", "--format=%s", target).strip()
    return f"{head[:7]} → {target[:7]} : {subject}"
