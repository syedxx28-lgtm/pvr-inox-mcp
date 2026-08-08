# Remote MCP endpoint. Only the read-only lookup tools are registered when
# PVR_MCP_TRANSPORT is not stdio - see mcp_server.py.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PVR_MCP_TRANSPORT=streamable-http \
    PVR_MCP_HOST=0.0.0.0 \
    PVR_MCP_PATH=/mcp

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only what the server needs. watch.py, notify.py and the watch config stay out
# of the image - the cron watcher is a separate concern and its tools are not
# registered remotely anyway.
COPY core.py mcp_server.py icon.svg ./

# Cloud Run injects PORT; the server prefers it over PVR_MCP_PORT.
EXPOSE 8080
CMD ["python3", "mcp_server.py"]
