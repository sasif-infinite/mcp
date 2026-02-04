#!/usr/bin/env python3
"""mcp_server MCP server"""

import os
import sys
from typing import Any

import httpx
from arcade_mcp_server import MCPApp, mcp_app as arcade_mcp_app_module
from arcade_mcp_server.convert import (
    convert_content_to_structured_content,
    convert_to_mcp_content,
)
from arcade_mcp_server.server import MCPServer as ArcadeMCPServer
from arcade_mcp_server.transports.http_streamable import HTTPStreamableTransport
from arcade_mcp_server.types import (
    CallToolResult,
    JSONRPCError,
    JSONRPCResponse,
    ListToolsResult,
    MCPTool,
)
from arcade_mcp_server import worker as arcade_worker
from arcade_mcp_server.worker import create_arcade_mcp as _create_arcade_mcp
from starlette.requests import Request
from fastapi.middleware.cors import CORSMiddleware
from arcade_mcp_server import types as arcade_mcp_types

# Temporarily pin protocol to match the UI SDK (sdk supports up to 2025-03-26).
arcade_mcp_types.LATEST_PROTOCOL_VERSION = "2025-03-26"

# Capture request headers so custom tools can be scoped to the caller.
_original_handle_request = HTTPStreamableTransport.handle_request


async def _handle_request_with_headers(
    self: HTTPStreamableTransport,
    scope: dict[str, Any],
    receive: Any,
    send: Any,
) -> None:
    request = Request(scope, receive)
    if self.session is not None:
        headers = {key.lower(): value for key, value in request.headers.items()}
        self.session._session_data["request_headers"] = headers
    await _original_handle_request(self, scope, receive, send)


HTTPStreamableTransport.handle_request = _handle_request_with_headers


def _get_langconnect_base_url() -> str | None:
    for key in (
        "LANGCONNECT_API_URL",
        "INTERNAL_RAG_URL",
        "NEXT_PUBLIC_RAG_API_URL",
        "RAG_API_URL",
    ):
        value = os.getenv(key)
        if value:
            return value.rstrip("/")
    return None


def _extract_bearer_token(session: Any | None) -> str | None:
    if session is None:
        return None
    headers = {}
    try:
        headers = session._session_data.get("request_headers", {})  # type: ignore[attr-defined]
    except Exception:
        headers = {}
    if not headers:
        return None
    token = headers.get("x-keycloak-access-token") or headers.get("authorization")
    if not token:
        return None
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token or None


def _build_custom_mcp_tool(payload: dict[str, Any]) -> MCPTool:
    schema = payload.get("inputSchema") or {
        "type": "object",
        "properties": {},
        "required": [],
    }
    return MCPTool(
        name=payload.get("name", ""),
        title=payload.get("name", ""),
        description=payload.get("description") or "",
        inputSchema=schema,
        _meta={"source": "custom"},
    )


class CustomMCPServer(ArcadeMCPServer):
    async def _fetch_custom_tools(
        self, session: Any | None
    ) -> list[MCPTool]:
        token = _extract_bearer_token(session)
        base_url = _get_langconnect_base_url()
        if not token or not base_url:
            return []
        url = f"{base_url}/tools"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    url, headers={"Authorization": f"Bearer {token}"}
                )
        except httpx.HTTPError:
            return []
        if not response.is_success:
            return []
        try:
            payload = response.json()
        except ValueError:
            return []
        tools_payload = payload if isinstance(payload, list) else payload.get("tools", [])
        if not isinstance(tools_payload, list):
            return []
        return [
            _build_custom_mcp_tool(tool)
            for tool in tools_payload
            if isinstance(tool, dict) and tool.get("name")
        ]

    async def _invoke_custom_tool(
        self,
        name: str,
        args: dict[str, Any],
        session: Any | None,
    ) -> CallToolResult | None:
        token = _extract_bearer_token(session)
        base_url = _get_langconnect_base_url()
        if not token or not base_url:
            return None
        url = f"{base_url}/tools/invoke"
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(
                    url,
                    json={"name": name, "args": args},
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as exc:
            content = convert_to_mcp_content(f"Custom tool request failed: {exc}")
            structured = convert_content_to_structured_content(
                {"error": f"Custom tool request failed: {exc}"}
            )
            return CallToolResult(
                content=content,
                structuredContent=structured,
                isError=True,
            )

        if response.status_code == 404:
            return None
        try:
            payload = response.json()
        except ValueError:
            payload = response.text

        if not response.is_success:
            message = (
                payload.get("detail")
                if isinstance(payload, dict)
                else str(payload)
            )
            content = convert_to_mcp_content(message)
            structured = convert_content_to_structured_content({"error": message})
            return CallToolResult(
                content=content,
                structuredContent=structured,
                isError=True,
            )

        content = convert_to_mcp_content(payload)
        structured = convert_content_to_structured_content(payload)
        return CallToolResult(
            content=content,
            structuredContent=structured,
            isError=False,
        )

    async def _handle_list_tools(
        self,
        message: Any,
        session: Any | None = None,
    ) -> JSONRPCResponse[ListToolsResult] | JSONRPCError:
        base = await super()._handle_list_tools(message, session=session)
        if isinstance(base, JSONRPCError):
            return base
        custom_tools = await self._fetch_custom_tools(session)
        if not custom_tools:
            return base
        existing_names = {tool.name for tool in base.result.tools}
        merged = base.result.tools + [
            tool for tool in custom_tools if tool.name not in existing_names
        ]
        return JSONRPCResponse(
            id=message.id,
            result=ListToolsResult(tools=merged),
        )

    async def _handle_call_tool(
        self,
        message: Any,
        session: Any | None = None,
    ) -> JSONRPCResponse[CallToolResult] | JSONRPCError:
        tool_name = message.params.name
        args = message.params.arguments or {}
        custom_response = await self._invoke_custom_tool(tool_name, args, session)
        if custom_response is not None:
            return JSONRPCResponse(id=message.id, result=custom_response)
        return await super()._handle_call_tool(message, session=session)


# Ensure the MCP server uses the custom handler for tool listing/invocation.
arcade_worker.MCPServer = CustomMCPServer

# Patch the FastAPI app factory used by MCPApp to inject CORS support for the OAP web UI.
def create_arcade_mcp_with_cors(*args, **kwargs):
    fastapi_app = _create_arcade_mcp(*args, **kwargs)
    fastapi_app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["mcp-session-id"],
    )
    return fastapi_app


arcade_mcp_app_module.create_arcade_mcp = create_arcade_mcp_with_cors

app = MCPApp(name="mcp_server", version="1.0.0", log_level="DEBUG")

# Keep at least one tool registered so the MCP server can start.
@app.tool
def healthcheck() -> dict:
    """Minimal no-op tool to satisfy server startup requirements."""
    return {"status": "ok"}

# Run with specific transport
if __name__ == "__main__":
    # Decide transport/host/port from flags or environment so Docker can bind to 0.0.0.0.
    # Default transport keeps the existing "stdio" behaviour.
    transport = "stdio"
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        transport = sys.argv[1]

    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "8000"))

    # Lightweight flag parsing for --host/--port
    for idx, arg in enumerate(sys.argv[1:], start=1):
        if arg == "--host" and idx + 1 < len(sys.argv):
            host = sys.argv[idx + 1]
        if arg == "--port" and idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    # Run the server
    app.run(transport=transport, host=host, port=port)
