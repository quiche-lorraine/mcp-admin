"""Journal d'audit : une ligne JSON par appel d'outil, refus compris."""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware


class AuditMiddleware(Middleware):
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, entry: dict) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    async def on_call_tool(self, context, call_next):
        token = get_access_token()
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "client": token.client_id if token else None,
            "tool": context.message.name,
            "args": context.message.arguments or {},
        }
        start = time.monotonic()
        try:
            result = await call_next(context)
            entry["status"] = "ok"
            return result
        except Exception as exc:
            entry["status"] = "error"
            entry["error"] = f"{type(exc).__name__}: {exc}"[:500]
            raise
        finally:
            entry["duration_s"] = round(time.monotonic() - start, 2)
            self._write(entry)
