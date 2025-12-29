# Infinite.com MCP Crawl Tool

Specialized MCP crawler for Infinite Computer Solutions. It crawls **https://www.infinite.com/** (same-origin only), summarizes top pages, and returns snippets/URLs for agents to answer from. The index is persisted to `src/mcp_server/data/infinite_index.json` when writes are allowed.

## Tools
- `crawl` (max_pages=40, max_depth=2) — crawl Infinite.com within conservative limits, respect robots.txt, persist the index, and return concise `{url, snippet}` entries for up to 10 pages.
- Core MCP set retained: `greet`, `secret`, `weather`, plus `crawl`. Other tools have been removed.

## How to run the MCP server
- Via Docker Compose (recommended in this repo): `docker-compose up mcp` from `open-agent-platform-infinite`.
- Locally: `uv run python -m mcp_server.server http --host 0.0.0.0 --port 8000` from `mcp/mcp_server`.

## Agent integration (OAP / LangGraph)
1. Point `mcp_config.url` to the MCP server (e.g., `http://localhost:8000`).
2. Include the tool names in `mcp_config.tools`:
   - `crawl`
   - `greet`
   - `secret`
   - `weather`
3. The tools agent system prompt nudges Infinite-related questions to `crawl` first; if results are insufficient, it asks before any broader web search.

## Crawl settings
- Defaults: `max_pages=40`, `max_depth=2`, `REQUEST_TIMEOUT=10s`. Inputs are clamped to these caps.
- Robots.txt is honored when reachable.
- Only same-origin links under `https://www.infinite.com/` are followed.
- The latest index is persisted to `src/mcp_server/data/infinite_index.json` for reuse across restarts (falls back to memory if disk write is unavailable).
- If a crawl request fails to fetch any pages, the server will return the last cached index (if available) so you still have snippets to answer with.

## Example prompts to trigger the tool
- “Crawl Infinite.com for their services”
- “Grab top pages from Infinite Computer Solutions site”
