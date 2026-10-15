"""Tokens à niveaux. Le fichier ne contient que des empreintes, jamais le token en clair.

Format du token : imcp_<id>_<secret>. L'id sert à retrouver l'entrée, le secret
est comparé en temps constant à son empreinte SHA-256 (suffisant ici : le secret
fait 256 bits d'entropie, ce n'est pas un mot de passe qu'on pourrait deviner).
"""

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from fastmcp.server.auth import AccessToken, TokenVerifier

# Chaque niveau inclut les scopes des niveaux inférieurs.
LEVELS = {
    "read": ["read"],
    "write": ["read", "write"],
}


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def create(path: Path, level: str, label: str) -> str:
    if level not in LEVELS:
        raise ValueError(f"niveau inconnu : {level} (attendu : {', '.join(LEVELS)})")
    data = _load(path)
    token_id = secrets.token_hex(4)
    while token_id in data:
        token_id = secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)
    data[token_id] = {
        "hash": _digest(secret),
        "level": level,
        "label": label,
        "created": int(time.time()),
    }
    _save(path, data)
    return f"imcp_{token_id}_{secret}"


def revoke(path: Path, token_id: str) -> bool:
    data = _load(path)
    if data.pop(token_id, None) is None:
        return False
    _save(path, data)
    return True


def listing(path: Path) -> dict:
    return {k: {kk: vv for kk, vv in v.items() if kk != "hash"} for k, v in _load(path).items()}


class FileTokenVerifier(TokenVerifier):
    """Relit le fichier à chaque requête : une révocation est effective immédiatement."""

    def __init__(self, path: Path):
        super().__init__()
        self.path = path

    async def verify_token(self, token: str) -> AccessToken | None:
        parts = token.split("_", 2)
        if len(parts) != 3 or parts[0] != "imcp":
            return None
        _, token_id, secret = parts
        entry = _load(self.path).get(token_id)
        if entry is None:
            return None
        if not hmac.compare_digest(entry["hash"], _digest(secret)):
            return None
        return AccessToken(
            token=token,
            client_id=f"{token_id}:{entry['label']}",
            scopes=LEVELS.get(entry["level"], []),
            expires_at=None,
        )
