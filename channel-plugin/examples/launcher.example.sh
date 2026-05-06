#!/usr/bin/env bash
# Launch Claude Code with Synapse v2 channel auto-fire enabled.
#
# Place anywhere on PATH (e.g. ~/bin/claude) and `chmod +x` it.
#
# Required flags:
#   --channels                                arms auto-fire for the plugin
#   --dangerously-load-development-channels   bypasses Claude Code's allowlist
#                                             check for locally-developed
#                                             channel servers
#
# Synapse works best when the agent isn't asked to approve every action.
# Inbound messages drive turns automatically; per-tool approval prompts
# break that flow. To smooth the multi-agent loop, add
# `--dangerously-skip-permissions` to the exec line below. Recommended for
# any seriously-used Synapse fleet.
#
# Usage:
#   claude                    — new session
#   claude --resume <id>      — resume session
#   claude -c                 — continue last session
#   claude <any claude flags> — passed through

exec claude \
  --channels plugin:synapse-channel-v2@claude-local \
  --dangerously-load-development-channels server:synapse-channel-v2 \
  "$@"
