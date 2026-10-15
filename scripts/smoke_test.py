"""Vérifie qu'un déploiement répond et montre les outils visibles pour un token.

INFRA_MCP_URL=https://mcp.example.fr/mcp INFRA_MCP_TOKEN=imcp_... python scripts/smoke_test.py
"""

import asyncio
import os

from fastmcp import Client
from fastmcp.client.auth import BearerAuth


async def main() -> None:
    url, token = os.environ["INFRA_MCP_URL"], os.environ["INFRA_MCP_TOKEN"]
    async with Client(url, auth=BearerAuth(token)) as client:
        tools = sorted(t.name for t in await client.list_tools())
        print("Outils visibles :", ", ".join(tools))
        print((await client.call_tool("repo_status", {})).data)
        print((await client.call_tool("list_hosts", {})).data)


asyncio.run(main())
