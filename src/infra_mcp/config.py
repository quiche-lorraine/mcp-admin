"""Configuration par variables d'environnement (voir deploy/infra-mcp.service)."""

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
MAX_OUTPUT = int(os.environ.get("INFRA_MCP_MAX_OUTPUT", "20000"))
