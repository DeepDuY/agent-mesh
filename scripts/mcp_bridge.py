#!/usr/bin/env python3
"""MCP stdio-to-streamable-http bridge for agent-mesh orchestrator."""
from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx2
from mcp import ClientSession
from mcp.client.sse import sse_client


async def main():
    url = os.environ.get("AGENT_MESH_MCP_URL", "http://127.0.0.1:8001")
    # The MCP channel only accepts user tokens (session or API). Use a user token here.
    token = os.environ.get("AGENT_MESH_USER_TOKEN", "")
    if not token:
        print(
            "AGENT_MESH_USER_TOKEN is required: pass a user token "
            "(login session token or the API token returned at user creation/rotation).",
            file=sys.stderr,
        )
        sys.exit(2)
    http_client = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"} if token else {},
        timeout=httpx2.Timeout(60.0),
    )
    async with sse_client(url, headers={"Authorization": f"Bearer {token}"} if token else {}) as (
        read_stream,
        write_stream,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            # Read JSON-RPC messages from stdin and forward them through the session.
            while True:
                line = await asyncio.get_event_loop().run_in_executor(
                    None, sys.stdin.readline
                )
                if not line:
                    break
                msg = json.loads(line)
                method = msg.get("method")
                params = msg.get("params", {})
                req_id = msg.get("id")
                if method == "tools/list":
                    result = await session.list_tools()
                    response = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "tools": [
                                t.model_dump(exclude_unset=True)
                                for t in result.tools
                            ]
                        },
                    }
                elif method == "tools/call":
                    name = params.get("name")
                    arguments = params.get("arguments", {})
                    tool_result = await session.call_tool(name, arguments=arguments)
                    content = []
                    for item in tool_result.content:
                        if hasattr(item, "text"):
                            content.append({"type": "text", "text": item.text})
                        else:
                            content.append({"type": "text", "text": str(item)})
                    response = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"content": content},
                    }
                else:
                    response = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601, "message": "method not found"},
                    }
                print(json.dumps(response), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
