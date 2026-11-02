"""Sauvegarde des stacks : outils backup_run (écriture) et backups (lecture), et
passage planifié chaque jour à config.BACKUP_AT.

Ce qui est sauvegardé vient de la clé backup des entrées de swarm_stacks
(obligatoire, vérifiée par lint._backup_violations) : des dumps, sortie d'une
commande d'un service (pg_dump…), et des volumes copiés tels quels. Le dépôt
restic et ses accès sont des variables du repo de config (backup_repository,
backup_credentials), lues par le playbook : voir maintenance_backup.yml.

Comme la mise à jour automatique, le passage planifié prend le verrou d'apply ;
s'il est pris, il réessaie chaque minute pendant une heure. Ce qui échoue est
écrit dans le journal du service et dans le journal d'audit, et health alerte
quand une stack n'a pas de sauvegarde réussie de moins de 26 heures.
"""

import asyncio
import base64
import json
import sys
from datetime import datetime, timedelta, timezone

import yaml
from fastmcp.exceptions import ToolError

from . import config, lint, maintenance, runner

TIMEOUT = 3600
_task: asyncio.Task | None = None


def plan(stack: str | None) -> dict:
    """{dumps: [{stack, service, argv}], volumes: [{stack, volume}]} : ce que le
    playbook sauvegarde, pour une stack ou pour toutes."""
    stacks = [s for s in lint._stacks_config(config.ANSIBLE_DIR).get("swarm_stacks") or [] if isinstance(s, dict)]
    if stack is not None:
        maintenance.check_stack(stack)
        stacks = [s for s in stacks if s.get("name") == stack]
        if not stacks:
            raise ToolError(f"stack inconnue : {stack}")
    found = {"dumps": [], "volumes": []}
    for entry in stacks:
        backup = entry.get("backup") or {}
        compose_path = config.ANSIBLE_DIR / "stacks" / entry["name"] / "compose.yml"
        volumes = (yaml.safe_load(compose_path.read_text()) or {}).get("volumes") or {}
        for service, argv in sorted((backup.get("dumps") or {}).items()):
            found["dumps"].append({"stack": entry["name"], "service": service, "argv": argv})
        for name in backup.get("volumes") or []:
            # Nom Docker : celui du compose (name:), sinon préfixé par la stack.
            docker_name = (volumes.get(name) or {}).get("name") or f"{entry['name']}_{name}"
            found["volumes"].append({"stack": entry["name"], "volume": docker_name})
    return found


def _entry(hosts: dict) -> dict:
    (entry,) = hosts.values()
    if "error" in entry:
        raise ToolError(entry["error"])
    return entry.get("report") or {}


async def run(stack: str | None, check: bool = False) -> dict:
    await runner.check_repo()
    todo = plan(stack)
    if not todo["dumps"] and not todo["volumes"]:
        return {"stacks": {}, "info": "rien à sauvegarder (clé backup des stacks)"}
    hosts = await maintenance.run("backup", {
        # Encodé : Ansible évalue les balises Jinja des extra vars (voir appcmd.py).
        "maintenance_plan_b64": base64.b64encode(json.dumps(todo).encode()).decode(),
        "maintenance_check": check,
        "maintenance_timeout": TIMEOUT,
    }, write=True)
    return _entry(hosts)


async def snapshots() -> dict:
    await runner.check_repo()
    report = _entry(await maintenance.run("backup_list", {}))
    if report.get("error"):
        report["error"] = maintenance.redact_log(report["error"])
    return report


# --- Passage planifié --------------------------------------------------------------


def _log(message: str) -> None:
    print(f"backup : {message}", file=sys.stderr, flush=True)


def _audit(entry: dict) -> None:
    config.AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with config.AUDIT_LOG.open("a") as f:
        f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "client": "backup", "tool": "backup", "args": {}, **entry},
                           ensure_ascii=False) + "\n")


def next_run(now: datetime, at: str) -> datetime:
    """Prochaine occurrence de l'heure HH:MM, strictement après now."""
    hour, minute = (int(x) for x in at.split(":"))
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)


async def _pass() -> None:
    todo = plan(None)
    if not todo["dumps"] and not todo["volumes"]:
        return
    for _ in range(60):
        if not runner._apply_lock.locked():
            break
        await asyncio.sleep(60)
    try:
        report = await run(None, check=True)
    except ToolError as exc:
        if "sauvegardes non configurées" in str(exc):
            return  # health le signale déjà, chaque jour
        _log(f"ERROR {str(exc)[:500]}")
        _audit({"status": "error", "error": str(exc)[:500]})
        return
    except Exception as exc:  # le passage du lendemain doit avoir lieu quoi qu'il arrive
        message = f"{type(exc).__name__}: {exc}"
        _log(f"ERROR {message[:500]}")
        _audit({"status": "error", "error": message[:500]})
        return
    failed = {name: s for name, s in (report.get("stacks") or {}).items() if not s.get("ok")}
    for name, s in failed.items():
        errors = {k: v["error"] for k, v in s["items"].items() if "error" in v}
        _log(f"ERROR {name} : sauvegarde en échec : {errors}")
    if report.get("check") not in (None, "ok"):
        _log(f"ERROR restic check : {report['check']}")
    _audit({"status": "error" if failed or report.get("check") not in (None, "ok") else "ok",
            "result": {"stacks": {n: s.get("ok") for n, s in (report.get("stacks") or {}).items()},
                       "check": report.get("check")}})


async def _loop(at: str) -> None:
    while True:
        now = datetime.now().astimezone()
        await asyncio.sleep((next_run(now, at) - now).total_seconds())
        await _pass()


def start() -> asyncio.Task | None:
    """Démarre la boucle une seule fois par processus ; rien si BACKUP_AT est vide."""
    global _task
    if not config.BACKUP_AT:
        return None
    next_run(datetime.now(), config.BACKUP_AT)  # heure invalide : erreur au démarrage
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(_loop(config.BACKUP_AT))
    return _task
