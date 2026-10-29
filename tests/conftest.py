"""La configuration est lue à l'import : on la redirige vers un dossier temporaire
avant que le moindre module infra_mcp soit chargé."""

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="infra-mcp-tests-"))
ANSIBLE_DIR = Path(__file__).resolve().parent.parent / "examples" / "infra"

os.environ["INFRA_MCP_ANSIBLE_DIR"] = str(ANSIBLE_DIR)
os.environ["INFRA_MCP_TOKENS"] = str(_TMP / "tokens.json")
os.environ["INFRA_MCP_AUDIT_LOG"] = str(_TMP / "audit.jsonl")
os.environ["INFRA_MCP_AUTOUPDATE_INTERVAL"] = "0"
