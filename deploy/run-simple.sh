#!/usr/bin/env bash
# Simple mode: one container serves the website and the AI tool server on a single port, plain HTTP.
# Use this when ports 80/443 are taken or blocked. (Automatic HTTPS needs ports 80/443; see setup.sh.)
# Usage, from the deploy/ folder:  PORT=8787 bash run-simple.sh
set -euo pipefail
PORT="${PORT:-8787}"
cd "$(dirname "$0")"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
IP="$(curl -fsS https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"
SITE_URL="http://$IP:$PORT" SITE_NAME=Remit MCP_URL="http://$IP:$PORT/mcp" python3 ../site/build.py
docker build -t remit-mcp -f Dockerfile ..
docker rm -f remit >/dev/null 2>&1 || true
docker run -d --name remit --restart unless-stopped -p "$PORT:8000" \
  -v remit-data:/data -v "$(cd .. && pwd)/site:/site:ro" -e REMIT_SITE_DIR=/site remit-mcp
sleep 3
curl -fsS "http://127.0.0.1:$PORT/health" && echo
echo "Website:      http://$IP:$PORT/"
echo "AI index:     http://$IP:$PORT/llms.txt"
echo "Tool server:  http://$IP:$PORT/mcp"
