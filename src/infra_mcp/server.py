"""Serveur MCP : lecture libre, plan en check mode, apply réservé au niveau write.

Volontairement absents : commande shell, tâche ad hoc, création de playbook, Vault.
Toute modification de l'infra passe par un commit dans le repo Ansible.
"""

from fastmcp import FastMCP
from fastmcp.server.auth import require_scopes

from . import config, runner
from .audit import AuditMiddleware
from .tokens import FileTokenVerifier

READ = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}

mcp = FastMCP(
    "infra",
    instructions=(
        "Pilote un parc de serveurs via un repo Ansible. Pour modifier la configuration, "
        "propose un changement dans le repo (group_vars / host_vars) ; utilise plan pour "
        "en voir l'effet. apply n'exécute que des playbooks existants."
    ),
    auth=FileTokenVerifier(config.TOKENS_FILE),
)
mcp.add_middleware(AuditMiddleware(config.AUDIT_LOG))


@mcp.tool(annotations=READ, auth=require_scopes("read"))
async def list_hosts() -> str:
    """Arborescence de l'inventaire : groupes et hôtes."""
    return await runner.inventory_graph()


@mcp.tool(annotations=READ, auth=require_scopes("read"))
async def host_vars(host: str) -> dict:
    """Variables effectives d'un hôte après fusion global → groupes → hôte (secrets masqués)."""
    return await runner.host_vars(host)


@mcp.tool(annotations=READ, auth=require_scopes("read"))
async def list_playbooks() -> list[str]:
    """Playbooks disponibles dans le repo (les seuls que plan et apply acceptent)."""
    return runner.list_playbooks()


@mcp.tool(annotations=READ, auth=require_scopes("read"))
async def repo_status() -> dict:
    """Commit du repo Ansible actuellement déployé sur le contrôleur."""
    return await runner.repo_status()


@mcp.tool(annotations=READ, auth=require_scopes("read"))
async def plan(playbook: str, limit: str) -> dict:
    """Montre ce que changerait un playbook, sans rien modifier (check mode + diff).

    Sert aussi à détecter la dérive : sur site, tout « changed » est un écart entre
    l'état réel et le repo. Attention, les tâches command/shell ne sont pas simulées.
    """
    return await runner.playbook(playbook, limit, check=True)


@mcp.tool(
    annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False},
    auth=require_scopes("write"),
)
async def apply(playbook: str, limit: str) -> dict:
    """Applique un playbook du repo sur les hôtes indiqués. Lance plan avant.

    `limit` est obligatoire : passe « all » explicitement pour tout le parc.
    """
    return await runner.playbook(playbook, limit, check=False)
