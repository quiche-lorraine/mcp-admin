"""Ce qui tourne dans les stacks : outil versions (lecture).

Pour chaque service d'une stack de swarm_stacks, l'image du conteneur en cours
(référence, identifiant, digest, labels OCI : commit, version, date de build),
depuis quand il tourne, et la sortie de sa commande de version, déclarée sous la
clé versions de l'entrée (obligatoire, vérifiée par lint._versions_violations ;
false pour une image sans commande de version). Comme les commandes
d'application, la commande passe en liste à docker exec, sans shell, dans un
conteneur du service sur le premier manager.
"""

import base64
import json

import yaml
from fastmcp.exceptions import ToolError

from . import config, lint, maintenance, runner

VERSION_TIMEOUT = 15


def targets(stack: str | None) -> list[dict]:
    """Services à décrire : {service, argv} (argv None sans commande de version)."""
    stacks = [s for s in lint._stacks_config(config.ANSIBLE_DIR).get("swarm_stacks") or [] if isinstance(s, dict)]
    if stack is not None:
        maintenance.check_stack(stack)
        stacks = [s for s in stacks if s.get("name") == stack]
        if not stacks:
            raise ToolError(f"stack inconnue : {stack}")
    found = []
    for entry in stacks:
        compose_path = config.ANSIBLE_DIR / "stacks" / entry["name"] / "compose.yml"
        compose = yaml.safe_load(compose_path.read_text()) or {}
        versions = entry.get("versions") or {}
        for service in sorted(compose.get("services") or {}):
            argv = versions.get(service)
            found.append({"service": f"{entry['name']}_{service}", "argv": argv if isinstance(argv, list) else None})
    return found


async def run(stack: str | None) -> dict:
    # Mêmes garde-fous que les autres opérations avant de lire la déclaration.
    await runner.check_repo()
    services = targets(stack)
    if not services:
        return {}
    hosts = await maintenance.run("stack_versions", {
        # Encodé comme pour app_command : Ansible évalue les balises Jinja des extra vars.
        "maintenance_services_b64": base64.b64encode(json.dumps(services).encode()).decode(),
        "maintenance_timeout": VERSION_TIMEOUT,
    })
    (entry,) = hosts.values()
    if "error" in entry:
        raise ToolError(entry["error"])
    report = entry.get("report") or {}
    for svc in report.values():
        if svc.get("version") is not None:
            svc["version"] = runner._truncate("\n".join(maintenance.redact_log(line) for line in svc["version"]))
    return report
