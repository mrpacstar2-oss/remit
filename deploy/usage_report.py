#!/usr/bin/env python3
"""Print requests per customer per month (the numbers you would invoice).
Usage on the VPS: docker compose exec mcp python -c "from remit.mcp_server import Usage; [print(*r) for r in Usage('/data/usage.sqlite').report()]"
"""
