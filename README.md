# fpl-strategy-mcp

A custom Fantasy Premier League MCP server, built specifically to operationalize
**our** squad-building framework — not a generic player-lookup tool. Every tool
maps to a named section of the strategy documents this project is built on:

| Tool | Framework section it implements |
|---|---|
| `fpl_search_players` | foundational data access |
| `fpl_defcon_profile` | Layer 1.1 — threshold-hit-rate, not raw totals |
| `fpl_fixture_outlook` | Layer 3.3 — weighted rolling window, not single FDR |
| `fpl_tier_classifier` | Build Strategy §2 — Core Anchor / Value Floor / Edge |
| `fpl_hit_math` | Build Strategy §4.3 — explicit hit-justification test |
| `fpl_blank_double_gameweeks` | Build Strategy §8 — chip timing |
| `fpl_get_team` | tracking our actual live squad, publicly, no login |
| `fpl_price_ownership_trends` | Layers 1.3 + 2.1 — price/EO signal |

No FPL email or password is ever required. Everything reads public, unauthenticated
endpoints only — this was a deliberate choice, partly because we don't need
anything else, and partly because handing real login credentials to any
third-party code (ours included) is a reasonable thing to be cautious about.

## What's been verified vs. what to check on first run

Built and tested in a sandboxed environment with **no access to the live FPL
API** (network allowlist restriction) — so testing here meant:
- Full unit tests against synthetic data shaped like real FPL API responses
  (`test_analysis.py`) — 18/18 passing.
- Full integration tests through the actual MCP tool-call path with a mocked
  API layer (`test_server_integration.py`) — 11/11 passing.
- Syntax and import verification for every file.

The one thing that's genuinely unverified against the real API is the exact
field names for defensive-contribution stats — that's the newest, least
publicly documented part of the FPL API. Run this once, after setup, before
trusting the DEFCON numbers for real decisions:

```bash
python check_live_api.py
```

It tells you plainly whether the field-name guesses in `analysis.py` matched
reality, and exactly what to edit if they didn't. If a field name is wrong, the
tool fails loudly with the actual available keys listed — it will never silently
return a wrong number.

## Setup — Option A: Claude Desktop or Claude Code (recommended, simplest)

This runs entirely on your own machine. Nothing is exposed to the internet,
nothing needs hosting, and it's the standard way MCP servers are used locally.

1. Install Python 3.10+ if you don't have it.
2. In this folder, install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Run the live field-name check once:
   ```bash
   python check_live_api.py
   ```
4. Add this to your Claude Desktop config file
   (macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`,
   Windows: `%APPDATA%\Claude\claude_desktop_config.json`):
   ```json
   {
     "mcpServers": {
       "fpl-strategy": {
         "command": "python",
         "args": ["/full/path/to/fpl-strategy-mcp/server.py"]
       }
     }
   }
   ```
   Use the **full absolute path** to `server.py` — relative paths cause silent
   failures.
5. Restart Claude Desktop completely. You should see the FPL tools available
   in the tools/hammer icon.

Claude Code works the same way — add the equivalent entry to its MCP config.

## Setup — Option B: remote, for use directly inside claude.ai web chat

This chat interface (claude.ai) only connects to MCP servers that are reachable
over the public internet — it cannot reach a server sitting on your laptop.
To use these tools directly in a claude.ai conversation (rather than Claude
Desktop/Code), you'd need to:

1. Deploy this server somewhere publicly reachable, using HTTP transport instead
   of stdio. Change the last line of `server.py` to:
   ```python
   mcp.run(transport="streamable-http", port=8000)
   ```
   (Small free-tier hosts work fine for this — Render, Railway, Fly.io, etc.)
2. In claude.ai: **Settings → Connectors → Add custom connector**, and paste
   your server's public URL.

This is more setup than Option A for the same result, so start with Option A
unless you specifically need it inside this exact chat interface.

## Files

- `server.py` — tool registrations (the MCP-facing layer)
- `fpl_client.py` — shared API client with caching, no business logic
- `analysis.py` — our custom calculations (DEFCON hit-rate, rolling fixtures,
  tier classification, hit-math) — this is the part that's genuinely ours
- `check_live_api.py` — one-time live field-name verification
- `test_analysis.py`, `test_server_integration.py` — the test suite; re-run
  either with `python test_analysis.py` any time you change `analysis.py`

## A note on trust

You mentioned not trusting pre-built MCPs for this — that's a reasonable
instinct, especially given at least one public FPL MCP server asks for your
real FPL email and password to unlock team-viewing features. This server never
asks for that, and every file is short enough to read end to end. That's the
actual point of building our own: not that public ones are malicious, but that
you shouldn't have to take it on faith.
