#!/usr/bin/env python3
"""mcp_server MCP server"""

import os
import sys
from typing import Annotated

import httpx
from arcade_mcp_server import Context, MCPApp, mcp_app as arcade_mcp_app_module
from arcade_mcp_server.auth import Reddit
from arcade_mcp_server.worker import create_arcade_mcp as _create_arcade_mcp
from fastapi.middleware.cors import CORSMiddleware
from arcade_mcp_server import types as arcade_mcp_types
from mcp_server.infinite_site import register_infinite_tools

# Temporarily pin protocol to match the UI SDK (sdk supports up to 2025-03-26).
arcade_mcp_types.LATEST_PROTOCOL_VERSION = "2025-03-26"

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
register_infinite_tools(app)


@app.tool
def greet(name: Annotated[str, "The name of the person to greet"]) -> str:
    """Greet a person by name."""
    return f"Hello, {name}!"


# To use this tool locally, you need to either set the secret in the .env file or as an environment variable.
# We avoid `requires_secrets` so the tool remains callable over the HTTP transport used by OAP.
@app.tool
def whisper_secret(
    context: Context,
) -> Annotated[str, "The last 4 characters of the secret"]:
    """Reveal the last 4 characters of the secret (or explain how to configure it)."""
    # First try the MCP secret store (available when authenticated)
    secret_keys = ["MY_SECRET_KEY", "MCP_SECRET"]
    secret = None

    # Try each key in the MCP secret store (when available)
    for key in secret_keys:
        try:
            secret = context.get_secret(key)
            if secret:
                break
        except Exception:
            continue

    # Fall back to environment for unauthenticated HTTP usage
    if not secret:
        for key in secret_keys:
            secret = os.getenv(key)
            if secret:
                break

    if not secret:
        return "Secret not set. Set MY_SECRET_KEY or MCP_SECRET in your env/.env and retry."

    return "The last 4 characters of the secret are: " + secret[-4:]

# To use this tool locally, you need to install the Arcade CLI (uv tool install arcade-mcp)
# and then run 'arcade login' to authenticate.
@app.tool(requires_auth=Reddit(scopes=["read"]))
async def get_posts_in_subreddit(
    context: Context, subreddit: Annotated[str, "The name of the subreddit"]
) -> dict:
    """Get posts from a specific subreddit"""
    # Normalize the subreddit name
    subreddit = subreddit.lower().replace("r/", "").replace(" ", "")

    # Prepare the httpx request
    # OAuth token is injected into the context at runtime.
    # LLMs and MCP clients cannot see or access your OAuth tokens.
    oauth_token = context.get_auth_token_or_empty()
    headers = {
        "Authorization": f"Bearer {oauth_token}",
        "User-Agent": "mcp_server-mcp-server",
    }
    params = {"limit": 5}
    url = f"https://oauth.reddit.com/r/{subreddit}/hot"

    # Make the request
    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers, params=params)
        response.raise_for_status()

        # Return the response
        return response.json()

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
