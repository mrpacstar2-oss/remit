# Getting Remit in front of AI agents: checklist

Plain-language steps. **Done** items are in this repository. **You** items need your accounts, money or a decision.

## Where AI agents look, and what we have for each

| Channel | Why it matters | Status |
|---|---|---|
| **MCP tool server** | AI coding tools (Claude Code, Cursor, VS Code Copilot and others) connect to it and get the guide, checker and test runner directly. | **Done:** `remit mcp` (local) and the hosted version in `deploy/`. Tested over the real protocol. |
| **Official MCP Registry** (registry.modelcontextprotocol.io) | The public directory AI tools and aggregators search for servers. | **Prepared:** `deploy/server.json`. **You:** publish once the package and domain exist (step 5). |
| **llms.txt** on the website | Agents pointed at a docs site read `/llms.txt` first. | **Done:** `site/llms.txt`, `site/llms-full.txt`, built by `site/build.py`. |
| **Website** | For humans, and for search engines that AI tools query. | **Done:** `site/index.html`, `robots.txt` (AI crawlers allowed), `sitemap.xml`. |
| **Public GitHub repository** | Code, docs and examples. Indexers and future model training read GitHub. | **You:** choose a name. I can create and fill the repository once you confirm. |
| **PyPI package** | `pip install` and `uvx` make the local server one command. | **Prepared:** `pyproject.toml`. **You:** a PyPI account and token (step 4). |
| **Doc indexers** (for example Context7) | They feed up-to-date docs to coding agents. | **You:** submit the GitHub repository after it is public (free). |

## Steps

1. **Pick the name.** "Remit" is taken by several products, including another programming language. A distinct
   name also helps AI search find you.
2. **Domain.** Buy one, which costs roughly £10 a year. In your domain provider's DNS settings, add an **A record**
   pointing to your VPS's IP address.
3. **VPS.** Log in to the VPS and run:
   ```bash
   git clone https://github.com/mrpacstar2-oss/remit.git /opt/remit && cd /opt/remit/deploy
   SITE_URL=https://YOURDOMAIN SITE_NAME="YourName" python3 ../site/build.py
   DOMAIN=YOURDOMAIN bash setup.sh
   ```
   You then get the website at `https://YOURDOMAIN`, the AI tool server at `https://YOURDOMAIN/mcp`, and HTTPS
   set up automatically.
4. **PyPI** (optional but recommended). Create an account at pypi.org and an API token, then run
   `python -m build && twine upload dist/*`.
5. **MCP Registry.** Edit `deploy/server.json` (owner, domain), install `mcp-publisher`, log in with GitHub, and
   run `mcp-publisher publish`.
6. **Paid access** (later). Create a key per customer with
   `docker compose exec mcp python /app/deploy/new_key.py <customer> <monthly-limit>`, then restart the `mcp`
   service. Anonymous users get `REMIT_FREE_MONTHLY` requests per month per IP address.

## Not tested here

The Docker image and Caddy configuration could not be built in the development sandbox, because no Docker daemon
was available. The same install steps and the hosted server were tested without Docker. Expect to fix small
issues on first deploy.
