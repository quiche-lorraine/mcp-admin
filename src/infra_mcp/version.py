"""Version de l'outil installé, et sa comparaison à celle que fige requirements.txt.

python -m infra_mcp.version <requirements.txt> : code 0 si l'outil installé est
déjà celui que demande le fichier (infra-mcp-admin update --auto n'a alors rien
à faire), 1 sinon.
"""

import importlib.metadata
import json
import re
import sys


def installed() -> dict:
    """Version et, pour une installation depuis Git, commit de l'outil installé."""
    dist = importlib.metadata.distribution("infra-mcp")
    url = json.loads(dist.read_text("direct_url.json") or "{}")
    return {"version": dist.version, "commit": url.get("vcs_info", {}).get("commit_id")}


def matches(requirement: str, tool: dict) -> bool:
    req = requirement.replace(" ", "")
    if m := re.fullmatch(r"infra-mcp@git\+https://github\.com/[\w.-]+/[\w.-]+@([0-9a-f]{40})", req):
        return tool["commit"] == m.group(1)
    if m := re.fullmatch(r"infra-mcp==([0-9]+(?:\.[0-9]+)*)", req):
        return tool["commit"] is None and tool["version"] == m.group(1)
    return False


def main(argv: list[str]) -> int:
    with open(argv[0]) as f:
        lines = [line.split("#", 1)[0].strip() for line in f]
    lines = [line for line in lines if line]
    return 0 if len(lines) == 1 and matches(lines[0], installed()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
