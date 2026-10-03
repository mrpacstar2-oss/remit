# Hosted Remit MCP server (offline tools only: guide, check, format, authority diff, examples, fixture runs).
FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY deploy/new_key.py ./deploy/new_key.py
RUN pip install --no-cache-dir ".[mcp]" && useradd --create-home remit && mkdir -p /data && chown remit /data
USER remit
ENV REMIT_FREE_MONTHLY=2000
EXPOSE 8000
CMD ["sh", "-c", "remit mcp --http --host 0.0.0.0 --port 8000 --usage /data/usage.sqlite --free-monthly ${REMIT_FREE_MONTHLY} $( [ -f /data/keys.json ] && echo --keys /data/keys.json )"]
