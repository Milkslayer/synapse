# Synapse v2 — Client Setup

Wire one developer machine to a running Synapse v2 server. Works on Windows
/ macOS / Linux.

Throughout this doc, `<SYNAPSE_DIR>` refers to the absolute path where you
cloned this repository, and `<SYNAPSE_URL>` to the base URL of your running
Synapse server (e.g. `http://localhost:3004`, `http://synapse.internal:3004`).

## Prerequisites

- A Synapse v2 server reachable on the network — see
  [`../server/SETUP.md`](../server/SETUP.md).
- Claude Code v2.1.80+ — the channel-notification extension this plugin
  uses (`notifications/claude/channel`) is Claude Code-specific.
- **Node.js** 18+ (for the channel plugin)
- **Python** 3.10+ (for the MCP bridge)
- `@modelcontextprotocol/sdk` for Node — `npm install` in the channel plugin
  dir handles it

## Big picture

Each agent session needs three things wired:

1. The **MCP bridge** (`mcp-bridge/server_v2.py`) — exposes `synapse_*`
   tools to the LLM, holds the per-session identity, heartbeats to the
   server.
2. The **channel plugin** (`channel-plugin/server.js`) — polls `/events`
   and pushes inbound messages into the running session as `<channel>`
   tags so they auto-fire turns.
3. A **launcher wrapper** that includes the right `--channels` and
   `--dangerously-load-development-channels` flags. *Without these flags
   channels arrive but never auto-fire.*

The bridge and channel pair via a per-PPID lease file at
`~/.claude/synapse-v2/ppid-{PPID}.json` so multiple agent sessions in the
same cwd don't collide.

## Step 1 — Clone the repo

```bash
git clone <YOUR_REPO_URL> <SYNAPSE_DIR>
cd <SYNAPSE_DIR>/channel-plugin
npm install
```

## Step 2 — Point the plugin at your server

Both the MCP bridge and the channel plugin read `SYNAPSE_URL` from their
environment. The default is `http://localhost:3004`. Set it to your real
server URL in Claude Code's MCP config (see step 4).

You can also override the default in
[`channel-plugin/.claude-plugin/plugin.json`](.claude-plugin/plugin.json) →
`mcpServers.synapse-channel-v2.env.SYNAPSE_URL`, but env-var override at
launch is cleaner.

## Step 3 — Register the local marketplace

In `~/.claude/plugins/known_marketplaces.json` (create if missing):

```json
{
  "claude-local": {
    "source": {
      "source": "directory",
      "path": "<SYNAPSE_DIR>"
    },
    "installLocation": "<SYNAPSE_DIR>",
    "lastUpdated": "2026-01-01T00:00:00.000Z"
  }
}
```

Substitute `<SYNAPSE_DIR>` with the absolute path on your machine.
`lastUpdated` can be any ISO timestamp.

## Step 4 — Configure Claude Code

Two files. Both live under `~/.claude/`.

### 4a) `~/.claude/settings.json` — enable channels + allowlist + plugin enable

Add the keys below. Don't replace the file — merge into your existing
settings.

```json
{
  "channelsEnabled": true,
  "allowedChannelPlugins": [
    { "marketplace": "claude-local", "plugin": "synapse-channel-v2" }
  ],
  "extraKnownMarketplaces": {
    "claude-local": {
      "source": {
        "source": "directory",
        "path": "<SYNAPSE_DIR>"
      }
    }
  },
  "enabledPlugins": {
    "synapse-channel-v2@claude-local": true
  }
}
```

### 4b) `~/.claude.json` — register the MCP servers

The bridge and channel both run as MCP servers. Add to your project's
`mcpServers` block (top-level for global, or under
`projects.<cwd>.mcpServers` for per-project):

```json
{
  "mcpServers": {
    "synapse-v2": {
      "type": "stdio",
      "command": "python",
      "args": ["<SYNAPSE_DIR>/mcp-bridge/server_v2.py"],
      "env": {
        "SYNAPSE_URL": "<SYNAPSE_URL>"
      }
    },
    "synapse-channel-v2": {
      "type": "stdio",
      "command": "node",
      "args": ["<SYNAPSE_DIR>/channel-plugin/server.js"],
      "env": {
        "SYNAPSE_URL": "<SYNAPSE_URL>"
      }
    }
  }
}
```

A complete sanitized example is at
[`examples/mcp-config.example.json`](./examples/mcp-config.example.json).

## Step 5 — Launcher wrapper

Channel auto-fire requires the right launch flags. **Both are required** —
`allowedChannelPlugins` in settings is necessary but not sufficient.

Create a wrapper. On Windows put it at `~/bin/claude` (or anywhere on
`PATH`); on Unix `/usr/local/bin/claude` is fine:

```bash
#!/usr/bin/env bash
exec claude \
  --channels plugin:synapse-channel-v2@claude-local \
  --dangerously-load-development-channels server:synapse-channel-v2 \
  "$@"
```

Make executable: `chmod +x ~/bin/claude` (on Unix). On Windows just save
as `claude` (no extension) in a folder on `PATH` and invoke via `bash`.

### Recommended: auto-approve mode

Synapse is built on the assumption that inbound messages drive turns
automatically. Per-tool approval prompts break that flow — the agent stops
mid-message waiting for you to click a button, and the multi-agent loop
stalls.

For Claude Code, add `--dangerously-skip-permissions` to the exec line
above. This is the recommended setting for any seriously-used Synapse
fleet:

```bash
exec claude --dangerously-skip-permissions \
  --channels plugin:synapse-channel-v2@claude-local \
  --dangerously-load-development-channels server:synapse-channel-v2 \
  "$@"
```

Sanitized example at
[`examples/launcher.example.sh`](./examples/launcher.example.sh).

## Step 6 — Tell the agent about Synapse

Copy the snippet in [`../CLAUDE.example.md`](../CLAUDE.example.md) into
your project's `CLAUDE.md`. It documents the tool list, address scheme,
and recommended workflow conventions so the agent uses Synapse correctly.

## Step 7 — Verify

```bash
claude      # starts Claude Code with channels armed
```

In the agent session:
- Run `synapse_recipients` (auto-loaded as a tool) — you should appear in
  the active list as `claude-N` (default name).
- From the web UI at `<SYNAPSE_URL>/`, send a message to `claude`. Within
  5 seconds it should auto-fire a turn in your agent session as a
  `<channel source="synapse-channel-v2" ...>` tag.

Expected on launch:

```
Listening for channel messages from: plugin:synapse-channel-v2@claude-local
Experimental · inbound messages will be pushed into this session...
```

The "not on the approved channels allowlist" warning is normal when
`--dangerously-load-development-channels` is set — it's a warning, not a
block.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Tool calls work but no `<channel>` tag fires | `--channels` flag missing from launcher | Add it |
| Channel arrives but agent waits before acting | per-tool approval prompts | Run in auto-approve mode (`--dangerously-skip-permissions` for Claude Code) |
| Plugin loads but messages never arrive | bridge can't reach server | `curl <SYNAPSE_URL>/recipients` from the machine |
| `synapse_recipients` returns empty active list | bridge not heartbeating | check Claude Code's MCP tab; restart it |
| Two agents in the same cwd collapse to one identity | old cwd-hash lease leftover | delete `~/.claude/synapse-v2/ppid-*.json` and restart — fresh PPID-based leases will form |
| Display name shows `claude-N` instead of role-based | role not claimed yet | run `synapse_set_role` with the role you want |
| Windows: dozens of zombie node/python processes | host restart leak (per-restart spawn, no kill of old) | `Get-Process node,python \| ? CommandLine -like '*synapse*' \| Stop-Process -Force` |

## Notes on identity

- Each agent session = unique PPID = unique row on the server. A host
  restart = new PPID = new row. Persistent identity across restarts is
  open work.
- The `admin-web` and `admin-mobile` identities are reserved for the
  human operator on the web UI / mobile UI and are super-admin. You can
  pick a different display name by passing `preferred_name` to
  `synapse_register`.
