"""Bout en bout à travers l'app HTTP : authentification, visibilité des outils, audit."""

import json
from contextlib import asynccontextmanager

import httpx2
import pytest
from fastmcp import Client
from fastmcp.client.auth import BearerAuth
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError

from infra_mcp import config, tokens
from infra_mcp.server import mcp


@asynccontextmanager
async def serve():
    # Un context manager plutôt qu'une fixture : le lifespan doit être ouvert et
    # fermé dans la même tâche asyncio.
    app = mcp.http_app(path="/mcp")

    def factory(headers=None, timeout=None, auth=None, **_):
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), headers=headers, timeout=timeout, auth=auth
        )

    def _connect(token):
        transport = StreamableHttpTransport("http://test/mcp", auth=BearerAuth(token), httpx_client_factory=factory)
        return Client(transport)

    async with app.router.lifespan_context(app):
        yield _connect


async def test_token_read_ne_voit_pas_apply():
    async with serve() as connect:
        async with connect(tokens.create(config.TOKENS_FILE, "read", "test")) as client:
            names = {t.name for t in await client.list_tools()}
            assert names == {"list_hosts", "host_vars", "list_playbooks", "repo_status", "plan",
                             "status", "logs", "stack_status", "health"}
            assert (await client.call_tool("list_playbooks", {})).data == ["site", "stacks", "swarm-join"]
            with pytest.raises(ToolError):
                await client.call_tool("apply", {"playbook": "site", "limit": "all"})


async def test_token_write_voit_apply():
    async with serve() as connect:
        async with connect(tokens.create(config.TOKENS_FILE, "write", "test")) as client:
            names = {t.name for t in await client.list_tools()}
            assert {"apply", "updates", "reboot", "restart_service", "stack_restart", "docker_prune"} <= names
            with pytest.raises(ToolError, match="playbook inconnu"):
                await client.call_tool("apply", {"playbook": "inconnu", "limit": "all"})


async def test_token_invalide_refuse():
    async with serve() as connect:
        with pytest.raises(Exception):
            async with connect("imcp_00000000_faux") as client:
                await client.list_tools()


async def test_audit_journalise_les_refus():
    async with serve() as connect:
        token = tokens.create(config.TOKENS_FILE, "read", "audit")
        token_id = token.split("_")[1]
        async with connect(token) as client:
            with pytest.raises(ToolError):
                await client.call_tool("apply", {"playbook": "site", "limit": "all"})
        entries = [json.loads(line) for line in config.AUDIT_LOG.read_text().splitlines()]
        entry = [e for e in entries if e["client"] == f"{token_id}:audit"][-1]
        assert entry["tool"] == "apply"
        assert entry["status"] == "error"
        assert entry["args"] == {"playbook": "site", "limit": "all"}
        assert token.split("_", 2)[2] not in config.AUDIT_LOG.read_text()