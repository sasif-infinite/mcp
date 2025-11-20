FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

WORKDIR /app

# System dependencies for building wheels
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential && \
    rm -rf /var/lib/apt/lists/*

# Copy project metadata and source
COPY mcp_server/pyproject.toml mcp_server/uv.lock ./mcp_server/
COPY mcp_server/src ./mcp_server/src

# Install the MCP server package
RUN pip install --upgrade pip && \
    pip install --no-cache-dir /app/mcp_server

EXPOSE 8000

CMD ["python", "-m", "mcp_server.server", "http", "--host", "0.0.0.0", "--port", "8000"]
