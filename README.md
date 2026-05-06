# Synapse v2

> 💬 **Easiest setup:** clone this repo into a project of yours and ask
> Claude Code to set Synapse up for you. The agent reads the SETUP docs in
> `server/` and `channel-plugin/`, picks the right host/port for your
> network, edits your `~/.claude.json` and `~/.claude/settings.json`,
> writes the launcher wrapper, and walks you through the smoke test.

Real-time messaging system for fleets of AI agents and the humans who
orchestrate them. A single small server keeps a roster of who is online,
routes addressed messages, and pushes inbound traffic into agent sessions
through an MCP channel plugin so the agent picks up the message
automatically — no polling required from the agent's side.

Built around three primitives:

- **Identities** — every agent session and human seat is a row on the
  server, addressable by display name (`claude-myteam-core`, `admin-web`).
- **Teams + roles** — agents claim roles (`core`, `frontend`, `eval`) and
  optionally join teams. Display names and broadcast scopes derive
  automatically from the team/role pair.
- **Real-time push** — the channel plugin polls a per-instance event stream
  and pushes inbound messages into Claude Code as `<channel>` tags, which
  triggers an agent turn within seconds.

## Repo layout

```
synapse/
├── server/                  # HTTP + sqlite server (Docker), port 3004 default
│   ├── synapse_v2.py
│   ├── ui_v2.html           — web UI for the human operator
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── .env.example
│   ├── README.md
│   └── SETUP.md
├── mcp-bridge/              # Stdio MCP server — exposes synapse_* tools
│   └── server_v2.py
├── channel-plugin/          # Stdio MCP plugin — pushes /events into Claude Code
│   ├── server.js
│   ├── package.json
│   ├── .claude-plugin/
│   ├── examples/
│   └── SETUP.md
└── CLAUDE.example.md        # Drop-in CLAUDE.md section for your projects
```

## Quick start

### 1. Stand up the server

```bash
git clone https://github.com/Milkslayer/synapse.git
cd synapse/server
docker compose up -d synapse-v2
```

Server listens on `:3004`. Open `http://localhost:3004/` in a browser to
see the operator UI. Override any of `SYNAPSE_PORT`, `SYNAPSE_DB_PATH`,
`SYNAPSE_INSTANCE` via env vars or a `.env` file (see
[`server/.env.example`](./server/.env.example)). Full server docs in
[`server/SETUP.md`](./server/SETUP.md).

### 2. Wire up an agent client

```bash
cd ../channel-plugin
npm install
```

Then point Claude Code at the bridge + channel plugin and add a launcher
wrapper that arms channel auto-fire. Full instructions in
[`channel-plugin/SETUP.md`](./channel-plugin/SETUP.md).

### 3. Tell the agent about Synapse

Copy the section in [`CLAUDE.example.md`](./CLAUDE.example.md) into the
`CLAUDE.md` of any project where you want the agent to participate. It
documents the tool list, address scheme, and recommended workflow
conventions so the agent uses Synapse correctly.

## Recommended: run the agent in auto-approve mode

Synapse is built on the assumption that inbound messages drive turns
automatically. Per-tool approval prompts break that flow — the agent stops
mid-message waiting for you to click a button, and the multi-agent loop
stalls.

The simplest fix is to launch Claude Code with
`--dangerously-skip-permissions`. This is the recommended setting for any
seriously-used Synapse fleet. The launcher example in
[`channel-plugin/examples/launcher.example.sh`](./channel-plugin/examples/launcher.example.sh)
explains where to add it.

## How it's meant to be used

The canonical pattern is one Claude Code instance acting as **architect**
(planning, delegating, reviewing) while several others act as **engineers**
(executing in isolation, reporting back). One session runs in the project
root and orchestrates; the others run in git worktrees on feature branches.

A typical run looks like this:

1. **Architect** claims a role, creates a team, and runs roll call:
   ```
   synapse_set_role({role: "architect"})
   synapse_create_team({name: "myproject"})
   synapse_recipients()
   ```
2. **Architect** invites each available engineer by role:
   `frontend`, `backend`, `eval`, `docs`. Their display names become
   `claude-myproject-frontend`, etc.
3. **Engineers** accept the invite (delivered as a `<channel>` event),
   set up their worktrees, and reply `<role> ready`.
4. **Architect** broadcasts the spec to `claude-myproject`. Every engineer
   receives it via channel auto-fire, no polling.
5. **Engineers** work in their worktrees, message progress and questions
   back to `claude-myproject-architect`.
6. **Architect** reviews diffs on disk, merges to master, dissolves the
   team when done.

Full walkthrough with concrete tool calls, conventions, and anti-patterns
in [`CLAUDE.example.md`](./CLAUDE.example.md). Drop that file's snippet
into your project's `CLAUDE.md` and Claude Code will follow the pattern
on its own.

## Address scheme

| Address | Meaning |
|---|---|
| `claude` | Global broadcast (admin only) |
| `claude-{team}` | Broadcast to all members of a team |
| `claude-{team}-{role}` | DM to a specific role in a team |
| `claude-{role}` | DM to a teamless agent with that role |
| `claude-{N}` | Default name when no team/role assigned (`claude-1`, `claude-2`, …) |
| `admin`, `admin-web`, `admin-mobile` | Human admin seats |

Display name collisions auto-suffix `-2`, `-3`, …

## Configuration

The server side is fully env-configurable. `docker-compose.yml` references
each setting through a `${VAR:-default}` so you can override without
editing the file:

| Env | Default | Meaning |
|---|---|---|
| `SYNAPSE_PORT` | `3004` | HTTP port (also the published Docker port) |
| `SYNAPSE_DB_PATH` | `/data/synapse_v2.db` | sqlite location inside the container |
| `SYNAPSE_INSTANCE` | `synapse-v2` | Identifier used in events / logs / container name |

Set any of them on the host shell, or copy
[`server/.env.example`](./server/.env.example) to `server/.env` and edit
there — docker-compose picks the file up automatically.

The client side (bridge + channel plugin) is configured per-session via
Claude Code's MCP config. Both processes read `SYNAPSE_URL` from their
environment (default `http://localhost:3004`). See
[`channel-plugin/SETUP.md`](./channel-plugin/SETUP.md) for the full
picture.

Display names are configurable per-registration via `preferred_name` —
nothing in this codebase hardcodes a personal handle. The `admin` /
`admin-web` defaults are conventions, not requirements.

## Security note

Synapse v2 has no auth. Don't expose `:3004` to the public internet.
Recommended deployments: LAN, VPN, or Tailscale.

## License

Apache 2.0 — see [`LICENSE`](./LICENSE).
