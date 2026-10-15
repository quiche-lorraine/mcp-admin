"""Appels à Ansible. Jamais de shell : arguments en liste, entrées validées."""

import asyncio
import json
import os
import re
import signal
import sys
from pathlib import Path

from fastmcp.exceptions import ToolError

from . import config

# Noms d'hôtes, de groupes, ou liste séparée par des virgules. Pas de « @fichier »,
# pas de motifs exotiques : on cible ce qui est déclaré dans l'inventaire, rien d'autre.
# Pas de tiret en tête de nom : « --list » serait lu comme une option par Ansible.
_NAME = r"[A-Za-z0-9_][A-Za-z0-9_.\-]*"
_LIMIT_RE = re.compile(rf"^{_NAME}(,{_NAME})*$")
_PLAYBOOK_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]*$")
_SECRET_KEY_RE = re.compile(r"(password|passwd|secret|token|private|vault)", re.I)

_apply_lock = asyncio.Lock()


def check_limit(limit: str) -> str:
    if not _LIMIT_RE.fullmatch(limit):
        raise ToolError("limit invalide : noms d'hôtes ou de groupes séparés par des virgules")
    return limit


def playbook_path(name: str) -> str:
    """Seuls les playbooks présents dans le repo sont exécutables : l'agent ne peut pas en créer."""
    if not _PLAYBOOK_RE.fullmatch(name):
        raise ToolError("nom de playbook invalide")
    path = (config.ANSIBLE_DIR / "playbooks" / f"{name}.yml").resolve()
    if path.parent != (config.ANSIBLE_DIR / "playbooks").resolve() or not path.is_file():
        raise ToolError(f"playbook inconnu : {name} (voir list_playbooks)")
    return str(path)


def list_playbooks() -> list[str]:
    return sorted(p.stem for p in (config.ANSIBLE_DIR / "playbooks").glob("*.yml"))


def redact(value):
    """Masque les valeurs dont la clé ressemble à un secret avant de les renvoyer à l'agent."""
    if isinstance(value, dict):
        return {
            k: "***" if _SECRET_KEY_RE.search(str(k)) else redact(v) for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def _truncate(text: str) -> str:
    # On garde la fin : c'est là que se trouvent le récapitulatif et les erreurs.
    if len(text) <= config.MAX_OUTPUT:
        return text
    return f"[… {len(text) - config.MAX_OUTPUT} caractères tronqués …]\n" + text[-config.MAX_OUTPUT:]


def _bin(name: str) -> str:
    """ansible-* du venv de l'outil : c'est là que vivent la collection
    infra_mcp.core et ses dépendances. Sous systemd, le PATH ne contient pas le venv."""
    candidate = Path(sys.executable).parent / name
    return str(candidate) if candidate.exists() else name


async def run(*args: str, timeout: int | None = None) -> dict:
    env = {
        **os.environ,
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_FORCE_COLOR": "0",
        "ANSIBLE_RETRY_FILES_ENABLED": "0",
    }
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=config.ANSIBLE_DIR,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        # Groupe de processus dédié : en cas de délai dépassé, on tue aussi les
        # workers et les connexions ssh qu'ansible-playbook a lancés.
        start_new_session=True,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout or config.PLAYBOOK_TIMEOUT)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()  # sinon le processus reste zombie
        raise ToolError("délai dépassé, exécution interrompue")
    return {"returncode": proc.returncode, "output": out.decode(errors="replace")}


def recap(output: str) -> dict:
    """Extrait le PLAY RECAP : ok / changed / failed / unreachable par hôte."""
    result = {}
    in_recap = False
    for line in output.splitlines():
        if line.startswith("PLAY RECAP"):
            in_recap = True
            continue
        if in_recap and ":" in line:
            host, _, stats = line.partition(":")
            result[host.strip()] = {
                k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", stats)
            }
    return result


async def inventory_graph() -> str:
    res = await run(_bin("ansible-inventory"), "--graph", timeout=60)
    return _truncate(res["output"])


async def host_vars(host: str) -> dict:
    check_limit(host)
    res = await run(_bin("ansible-inventory"), "--host", host, timeout=60)
    if res["returncode"] != 0:
        raise ToolError(_truncate(res["output"]))
    return redact(json.loads(res["output"]))


async def playbook(name: str, limit: str, check: bool) -> dict:
    args = [_bin("ansible-playbook"), playbook_path(name), "--limit", check_limit(limit), "--diff"]
    if check:
        args.append("--check")
        res = await run(*args)
    else:
        # Un seul apply à la fois : deux exécutions concurrentes sur le même parc = état imprévisible.
        if _apply_lock.locked():
            raise ToolError("un apply est déjà en cours, réessaie plus tard")
        async with _apply_lock:
            res = await run(*args)
    return {
        "returncode": res["returncode"],
        "recap": recap(res["output"]),
        "output": _truncate(res["output"]),
    }


async def repo_status() -> dict:
    head = await run("git", "log", "-1", "--format=%H %ci %s", timeout=30)
    dirty = await run("git", "status", "--porcelain", timeout=30)
    return {
        "commit": head["output"].strip(),
        "uncommitted_changes": bool(dirty["output"].strip()),
    }
