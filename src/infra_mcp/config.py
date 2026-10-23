"""Configuration par variables d'environnement (voir src/infra_mcp/deploy/infra-mcp.service)."""

import os
from pathlib import Path

ANSIBLE_DIR = Path(os.environ.get("INFRA_MCP_ANSIBLE_DIR", "/opt/infra")).resolve()
TOKENS_FILE = Path(os.environ.get("INFRA_MCP_TOKENS", "/etc/infra-mcp/tokens.json"))
AUDIT_LOG = Path(os.environ.get("INFRA_MCP_AUDIT_LOG", "/var/log/infra-mcp/audit.jsonl"))

# Écoute : par défaut en local uniquement. En prod, l'IP de la passerelle Docker
# pour que Traefik puisse joindre le service sans l'exposer sur Internet.
HOST = os.environ.get("INFRA_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("INFRA_MCP_PORT", "8765"))

PLAYBOOK_TIMEOUT = int(os.environ.get("INFRA_MCP_TIMEOUT", "1800"))
# plan est accessible avec un token read et tourne en root sur le parc : on borne
# le nombre d'exécutions simultanées pour qu'il ne sature ni le contrôleur ni le parc.
MAX_PLANS = int(os.environ.get("INFRA_MCP_MAX_PLANS", "2"))
# Une lecture qui trouve toutes les places prises attend qu'une se libère, au plus
# ce délai (secondes), avant d'être refusée.
SLOT_WAIT = int(os.environ.get("INFRA_MCP_SLOT_WAIT", "300"))
MAX_INVENTORY = int(os.environ.get("INFRA_MCP_MAX_INVENTORY", "4"))
MAX_OUTPUT = int(os.environ.get("INFRA_MCP_MAX_OUTPUT", "20000"))
