"""Commandes d'application : outils app_query (lecture) et app_command (écriture).

Une commande n'existe que si le repo de config la déclare, sous la clé commands
d'une entrée de swarm_stacks : service, liste d'arguments fixe, paramètres
validés par une expression. Pas de shell : les arguments sont passés en liste à
docker exec, dans un conteneur du service sur le premier manager. Le lint
vérifie la déclaration (lint.command_violations), ce module les valeurs.
"""

import base64
import json
import re

from fastmcp.exceptions import ToolError

from . import config, lint, maintenance, runner


def find(stack: str, command: str) -> dict:
    stacks = {s.get("name"): s for s in lint._stacks_config(config.ANSIBLE_DIR).get("swarm_stacks") or []}
    if stack not in stacks:
        raise ToolError(f"stack inconnue : {stack}")
    commands = stacks[stack].get("commands") or {}
    if command not in commands:
        raise ToolError(f"commande inconnue pour {stack} : {command} (déclarées : {', '.join(sorted(commands)) or 'aucune'})")
    return commands[command]


def build_argv(cmd: dict, params: dict) -> list[str]:
    """argv de la commande, paramètres validés puis substitués."""
    declared = cmd.get("params") or {}
    if unknown := set(params) - set(declared):
        raise ToolError(f"paramètres inconnus : {', '.join(sorted(unknown))}")
    if missing := set(declared) - set(params):
        raise ToolError(f"paramètres manquants : {', '.join(sorted(missing))}")
    for name, value in params.items():
        if not isinstance(value, str) or not re.fullmatch(declared[name], value):
            raise ToolError(f"{name} : valeur refusée (attendu : {declared[name]})")
        # Les arguments passent à Ansible en extra vars, qu'il évalue comme des
        # templates : une valeur ne doit jamais contenir de balise Jinja.
        if any(tag in value for tag in ("{{", "{%", "{#")):
            raise ToolError(f"{name} : balise Jinja refusée (Ansible l'évaluerait)")
        if value.startswith("-"):
            raise ToolError(f"{name} : une valeur ne commence pas par - (elle serait lue comme une option)")
    argv = [lint.PLACEHOLDER_RE.sub(lambda m: params[m.group(1)], arg) for arg in cmd["argv"]]
    if any(tag in arg for arg in argv for tag in ("{{", "{%", "{#")):
        raise ToolError("argv : balise Jinja refusée (Ansible l'évaluerait)")
    return argv


async def run(stack: str, command: str, params: dict, *, write: bool) -> dict:
    # Mêmes garde-fous que les autres opérations avant de lire la déclaration.
    await runner.check_repo()
    maintenance.check_stack(stack)
    cmd = find(stack, command)
    level = cmd.get("level", "write")
    if level == "write" and not write:
        raise ToolError(f"{command} modifie l'application : passer par app_command (token write)")
    argv = build_argv(cmd, params)
    secret = bool(cmd.get("secret_output"))
    hosts = await maintenance.run("app_command", {
        "maintenance_service": f"{stack}_{cmd['service']}",
        # Encodé : Ansible évalue les balises Jinja des extra vars, pas le résultat
        # d'un filtre (b64decode). Seconde barrière après build_argv.
        "maintenance_argv_b64": base64.b64encode(json.dumps(argv).encode()).decode(),
        "maintenance_timeout": int(cmd.get("timeout", 60)),
        "maintenance_secret_output": secret,
    }, write=write)
    (entry,) = hosts.values()
    if "error" in entry:
        raise ToolError(entry["error"])
    report = entry.get("report") or {}
    rc = report.get("rc")
    result = {"stack": stack, "command": command, "rc": rc}
    if rc == 124:
        result["error"] = f"délai dépassé ({cmd.get('timeout', 60)} s)"
    if secret:
        result["output"] = "masquée (secret_output) : la commande a tourné, sa sortie n'est renvoyée nulle part"
    else:
        lines = [maintenance.redact_log(line) for line in (report.get("stdout") or []) + (report.get("stderr") or [])]
        result["output"] = runner._truncate("\n".join(lines))
    return result
