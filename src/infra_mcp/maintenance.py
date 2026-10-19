"""Opérations de maintenance : playbooks maintenance_* du socle (docs/maintenance.md).

Le code des opérations vit dans la collection infra_mcp.core, pas dans le repo de
config : celui-ci n'apporte que l'inventaire. Les paramètres sont validés ici,
passés en extra vars (le repo de config ne peut pas les surcharger) et revalidés
par les playbooks. Jamais de shell : arguments en liste.
"""

import json
import re

from fastmcp.exceptions import ToolError

from . import config, runner

_UNIT_RE = re.compile(r"^[A-Za-z0-9@_.:-]+$")
_STACK_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_SERVICE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_SINCE_RE = re.compile(r"^[0-9]+[smhd]$")
SSH_UNITS = {"ssh", "sshd", "ssh.service", "sshd.service", "ssh.socket", "sshd.socket"}
UPDATE_MODES = ("upgrade", "security", "dist-upgrade")
MAX_LOG_LINES = 500

# Secrets à masquer dans les logs. Le masquage limite les dégâts, il ne garantit rien.
_LOG_SECRETS = [
    (re.compile(r"imcp_[A-Za-z0-9_-]+"), "imcp_***"),
    (re.compile(r"SWMTKN-[A-Za-z0-9-]+"), "SWMTKN-***"),
    (re.compile(r"(?i)(authorization:\s*\S+\s+)\S+"), r"\1***"),
    (re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)(\s*[=:]\s*)\S+"), r"\1\2***"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     "[clé privée masquée]"),
]


# --- Validation -------------------------------------------------------------------


def check_host(host: str) -> str:
    """Un seul hôte (ou groupe) : les opérations ciblées n'acceptent pas de liste."""
    if "," in host:
        raise ToolError("un seul hôte attendu")
    return runner.check_limit(host)


def check_stack(stack: str) -> str:
    if not _STACK_RE.fullmatch(stack):
        raise ToolError("nom de stack invalide")
    return stack


def check_unit(unit: str) -> str:
    if not _UNIT_RE.fullmatch(unit):
        raise ToolError("nom d'unité invalide")
    if unit in SSH_UNITS:
        raise ToolError("ssh n'est jamais redémarré : un échec couperait l'accès à la machine")
    return unit


def parse_source(source: str) -> tuple[str, str]:
    """unit:<unité systemd> ou docker:<stack>_<service>."""
    kind, _, name = source.partition(":")
    if kind == "unit" and _UNIT_RE.fullmatch(name):
        return kind, name
    if kind == "docker" and _SERVICE_RE.fullmatch(name):
        return kind, name
    raise ToolError("source invalide : unit:<unité systemd> ou docker:<stack>_<service>")


def check_since(since: str) -> str:
    if not _SINCE_RE.fullmatch(since):
        raise ToolError("since invalide : un nombre suivi de s, m, h ou d (ex. 30m, 2h, 1d)")
    return since


def check_lines(lines: int) -> int:
    if not 1 <= lines <= MAX_LOG_LINES:
        raise ToolError(f"lines doit être entre 1 et {MAX_LOG_LINES}")
    return lines


# --- Exécution --------------------------------------------------------------------


def parse_json_output(stdout: str) -> dict:
    """Sortie du callback ansible.posix.json -> par hôte : rapport, erreur, stats."""
    start = stdout.find("{")
    try:
        data = json.loads(stdout[start:]) if start >= 0 else {}
    except json.JSONDecodeError:
        data = {}
    hosts: dict[str, dict] = {}
    for play in data.get("plays", []):
        for task in play.get("tasks", []):
            name = task.get("task", {}).get("name", "")
            for host, result in task.get("hosts", {}).items():
                entry = hosts.setdefault(host, {})
                if name == "Rapport" and "maintenance_report" in result:
                    entry["report"] = result["maintenance_report"]
                elif result.get("unreachable"):
                    entry.setdefault("error", f"injoignable : {result.get('msg', '')}"[:500])
                elif result.get("failed") and not result.get("ignore_errors"):
                    msg = result.get("msg") or result.get("stderr") or ""
                    entry.setdefault("error", f"« {name} » : {msg}"[:1000])
    for host, stats in data.get("stats", {}).items():
        hosts.setdefault(host, {})["stats"] = stats
    return hosts


async def run(name: str, extra: dict, *, limit: str | None = None, check: bool = False,
              write: bool = False) -> dict:
    """Lance infra_mcp.core.maintenance_<name> et renvoie le résultat par hôte."""
    args = [runner._bin("ansible-playbook"), f"infra_mcp.core.maintenance_{name}",
            "--extra-vars", json.dumps(extra)]
    if limit is not None:
        args += ["--limit", runner.check_limit(limit)]
    if check:
        args.append("--check")
    env = {"ANSIBLE_STDOUT_CALLBACK": "ansible.posix.json"}
    if write:
        # Une action à la fois sur le parc, verrou partagé avec apply.
        if runner._apply_lock.locked():
            raise ToolError("une action est déjà en cours, réessaie plus tard")
        async with runner._apply_lock:
            res = await runner.run(*args, env=env, separate_stderr=True)
    else:
        if runner._plan_slots.locked():
            raise ToolError(f"déjà {config.MAX_PLANS} lectures en cours, réessaie plus tard")
        async with runner._plan_slots:
            res = await runner.run(*args, env=env, separate_stderr=True)
    hosts = parse_json_output(res["output"])
    if not hosts:
        raise ToolError(runner._truncate(res["stderr"] or res["output"] or "aucune sortie d'Ansible"))
    return hosts


# --- Mise en forme ------------------------------------------------------------------


def redact_log(text: str) -> str:
    for pattern, replacement in _LOG_SECRETS:
        text = pattern.sub(replacement, text)
    return text


def summarize_status(report: dict) -> dict:
    mem = report.get("memory_mb") or {}
    disks = []
    for m in report.get("mounts") or []:
        total, avail = m.get("size_total") or 0, m.get("size_available") or 0
        if total <= 0 or m.get("fstype") in {"tmpfs", "devtmpfs", "squashfs", "overlay", "nsfs"}:
            continue
        used = round(100 * (1 - avail / total))
        disks.append({"mount": m.get("mount"), "size_gb": round(total / 1e9, 1), "used_pct": used,
                      **({"alert": True} if used >= 85 else {})})
    upgradable = [line for line in report.get("upgradable") or [] if "/" in line]
    docker = report.get("docker")
    swarm = (docker or {}).get("Swarm") or {}
    uptime = report.get("uptime_seconds")
    return {
        "uptime_hours": round(float(uptime) / 3600, 1) if uptime not in (None, "") else None,
        "load": (report.get("loadavg") or "").split()[:3],
        "memory_mb": {"total": (mem.get("real") or {}).get("total"),
                      "available": (mem.get("nocache") or {}).get("free")},
        "disks": disks,
        "failed_units": [line.split()[0] for line in report.get("failed_units") or [] if line.strip()],
        "updates": {"pending": len(upgradable),
                    "security": sum("-security" in line for line in upgradable),
                    "packages": [line.split("/")[0] for line in upgradable][:50]},
        "reboot_required": bool(report.get("reboot_required")),
        "reboot_packages": (report.get("reboot_pkgs") or "").split(),
        "docker": None if docker is None else {
            "containers_running": docker.get("ContainersRunning"),
            "swarm": swarm.get("LocalNodeState"),
            "manager": swarm.get("ControlAvailable"),
            "nodes": swarm.get("Nodes"),
        },
    }


def summarize_stack(tasks: list[dict]) -> dict:
    """Tâches d'une stack -> par service : voulues, en cours, dernières erreurs."""
    services: dict[str, dict] = {}
    for t in tasks:
        name = re.sub(r"\.[^.]+$", "", t.get("Name", ""))
        svc = services.setdefault(name, {"desired": 0, "running": 0, "errors": []})
        if t.get("DesiredState") == "Running":
            svc["desired"] += 1
        if str(t.get("CurrentState", "")).startswith("Running"):
            svc["running"] += 1
        if t.get("Error") and len(svc["errors"]) < 3:
            svc["errors"].append({"state": t.get("CurrentState"), "node": t.get("Node"),
                                  "error": t.get("Error")})
    return services


_APT_LINE_RE = re.compile(r"^(?:Inst|Setting up) (\S+)")
# unattended-upgrade --verbose ne reprend pas la sortie de dpkg, seulement cette liste.
_UU_LINE_RE = re.compile(r"^Packages that will be upgraded: (.*)$")


def summarize_updates(report: dict) -> dict:
    packages = set()
    for line in report.get("output") or []:
        if m := _APT_LINE_RE.match(line):
            packages.add(m.group(1))
        elif m := _UU_LINE_RE.match(line):
            packages.update(m.group(1).split())
    packages = sorted(packages)
    return {"mode": report.get("mode"), "simulation": report.get("simulation"),
            "packages": packages, "count": len(packages),
            "reboot_required": report.get("reboot_required")}


def filter_log_lines(lines: list[str], grep: str | None) -> list[str]:
    # Les lignes docker commencent par un horodatage ISO : le tri les remet dans l'ordre
    # quand stdout et stderr ont été lus séparément.
    lines = sorted(lines) if lines and re.match(r"^\d{4}-\d\d-\d\dT", lines[0]) else list(lines)
    if grep:
        needle = grep.lower()
        lines = [line for line in lines if needle in line.lower()]
    return [redact_log(line) for line in lines]
