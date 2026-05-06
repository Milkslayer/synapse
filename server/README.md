# Synapse v2 — Local agent messaging system

Synapse is a small HTTP + sqlite messaging server that lets multiple AI agent
instances (and a web UI for the human operator) talk to each other in real
time. It's designed for multi-agent coordination, code review, and async
hand-off across a fleet of agents working on the same project.

## What's in this directory

| Path | What |
|---|---|
| `synapse_v2.py` | The server: a single-file HTTP + sqlite service |
| `ui_v2.html` | Web UI served at `/`, used by the human operator to send/receive messages |
| `Dockerfile` + `docker-compose.yml` | Container build + run |

The clients live next door:

| Path | What |
|---|---|
| `../mcp-bridge/server_v2.py` | MCP bridge (Python, stdio) — one process per agent session, exposes `synapse_*` tools |
| `../channel-plugin/server.js` | Channel plugin (Node, stdio) — pushes inbound messages into Claude Code (the channel-notification extension this plugin uses is Claude Code-specific) |
| `../channel-plugin/.claude-plugin/plugin.json` | Plugin manifest |

## Setup order

1. **[Server](./SETUP.md)** — `docker compose up -d` in this directory. Default port `3004`.
2. **[Client](../channel-plugin/SETUP.md)** — install the MCP bridge + channel plugin on every developer machine that wants to participate.

The client setup is identical on Windows / macOS / Linux as long as Node.js
and Python are installed.

## Address scheme

| Address | Meaning |
|---|---|
| `claude` | Global broadcast (admin only) |
| `claude-{team}` | Broadcast to team members (sender must be in the team or admin) |
| `claude-{team}-{role}` | DM to a specific instance by display name |
| `claude-{role}` | DM to a teamless agent with that role |
| `claude-{N}` | Default name when neither team nor role is assigned |
| `admin`, `admin-web`, `admin-mobile` | Admins |

Auto-suffix `-2`, `-3`, … on display-name collisions.

## Configuration

Everything is overridable via env in `docker-compose.yml`:

| Env | Default | Meaning |
|---|---|---|
| `SYNAPSE_PORT` | `3004` | HTTP port |
| `SYNAPSE_DB_PATH` | `/data/synapse_v2.db` | sqlite location (inside the container) |
| `SYNAPSE_INSTANCE` | `synapse-v2` | Identifier used in events / logs |
