"""Mise à jour automatique des images : les services listés dans la clé auto_update
d'une entrée de swarm_stacks suivent le tag de leur image (playbook
maintenance_stack_autoupdate).

Lancée par le serveur toutes les config.AUTOUPDATE_INTERVAL secondes, avec le
verrou d'apply : un passage qui le trouve pris est simplement reporté au suivant.
Ce qui change est écrit dans le journal du service (journalctl -u infra-mcp) et
dans le journal d'audit ; un échec contient « ERROR », que health remonte.
"""

import asyncio
import json
import sys
from datetime import datetime, timezone

from fastmcp.exceptions import ToolError

from . import config, lint, maintenance, runner

_task: asyncio.Task | None = None


def _log(message: str) -> None:
    print(f"auto_update : {message}", file=sys.stderr, flush=True)


def _audit(entry: dict) -> None:
    config.AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with config.AUDIT_LOG.open("a") as f:
        f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "client": "auto_update", "tool": "auto_update", "args": {}, **entry},
                           ensure_ascii=False) + "\n")


def tracked_stacks() -> list[str]:
    stacks = lint._stacks_config(config.ANSIBLE_DIR).get("swarm_stacks") or []
    return [s["name"] for s in stacks if isinstance(s, dict) and s.get("auto_update")]


async def run_once() -> dict | None:
    """Un passage. None si aucune stack n'est suivie (Ansible n'est pas lancé)."""
    if not tracked_stacks():
        return None
    hosts = await maintenance.run("stack_autoupdate", {}, write=True)
    entry = next(iter(hosts.values()))
    if "error" in entry:
        raise ToolError(entry["error"])
    return entry.get("report") or {}


async def _pass() -> None:
    if runner._apply_lock.locked():
        return
    try:
        report = await run_once()
    except ToolError as exc:
        if "déjà en cours" not in str(exc):
            _log(f"ERROR {exc}")
            _audit({"status": "error", "error": str(exc)[:500]})
        return
    except Exception as exc:  # le prochain passage doit avoir lieu quoi qu'il arrive
        _log(f"ERROR {type(exc).__name__}: {exc}")
        _audit({"status": "error", "error": f"{type(exc).__name__}: {exc}"[:500]})
        return
    if not report:
        return
    for name in report.get("updated") or []:
        _log(f"{name} mis à jour : {(report.get('images') or {}).get(name)}")
    for name, err in (report.get("failed") or {}).items():
        _log(f"ERROR {name} : mise à jour en échec : {err}")
    for name in report.get("missing") or []:
        _log(f"{name} : service absent, rien à suivre")
    if report.get("updated") or report.get("failed"):
        _audit({"status": "error" if report.get("failed") else "ok",
                "result": {k: report.get(k) for k in ("updated", "failed")}})


async def _loop(interval: int) -> None:
    while True:
        await asyncio.sleep(interval)
        await _pass()


def start() -> asyncio.Task | None:
    """Démarre la boucle une seule fois par processus ; rien si l'intervalle vaut 0."""
    global _task
    if config.AUTOUPDATE_INTERVAL <= 0:
        return None
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(_loop(config.AUTOUPDATE_INTERVAL))
    return _task
