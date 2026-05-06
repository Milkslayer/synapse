#!/usr/bin/env node
/**
 * Synapse v2 Channel for Claude Code
 *
 * Polls Synapse v2 /events endpoint for the running session and pushes
 * three event types into Claude Code as <channel source="synapse"> tags:
 *
 *   - message_arrived  → "From: …\nSubject: …\n\n<body>"
 *   - identity_changed → "Your display name changed: <old> → <new>"
 *   - invite_received  → "Invite to team <name> as <role> from <inviter>"
 *   - group_ownerless  → admin notice (only delivered to admin-* instances)
 *
 * Reads the per-cwd lease written by the v2 MCP bridge to discover which
 * instance ID we belong to. Drains existing events on startup so a fresh
 * Claude restart doesn't replay old notifications.
 *
 */

import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { appendFileSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))

const SYNAPSE_URL = process.env.SYNAPSE_URL || 'http://localhost:3004'
const POLL_INTERVAL = parseInt(process.env.SYNAPSE_POLL_INTERVAL || '5000', 10)

// Pair with the bridge via the parent Claude PID. Same PPID = same Claude
// session = same lease file. Differentiates concurrent Claude sessions in
// the same cwd; each gets its own Claude identity.
const PPID = process.ppid
const LEASE_DIR = process.env.SYNAPSE_LEASE_DIR || join(homedir(), '.claude', 'synapse-v2')
const LEASE_FILE = join(LEASE_DIR, `ppid-${PPID}.json`)

const LOG_DIR = join(__dirname, 'logs')
try { mkdirSync(LOG_DIR, { recursive: true }) } catch {}
const LOG_FILE = join(LOG_DIR, `channel-v2-ppid-${PPID}.log`)

function flog(level, msg) {
  const ts = new Date().toISOString().replace('T', ' ').slice(0, 19)
  try { appendFileSync(LOG_FILE, `${ts} ${level} ${msg}\n`) } catch {}
  process.stderr.write(`[synapse-channel-v2] ${msg}\n`)
}

const mcp = new Server(
  { name: 'synapse-v2', version: '2.0.0' },
  {
    capabilities: { experimental: { 'claude/channel': {} } },
    instructions: [
      'Messages from the Synapse v2 messaging system arrive as <channel source="synapse-v2" ...>.',
      'Three event types: message_arrived (a teammate sent you something), identity_changed',
      '(your display name was updated server-side), invite_received (you have a pending team',
      'invite — accept or decline via synapse_accept_invite / synapse_decline_invite).',
      'Read the content and act on it. Use the synapse_v2 MCP tools to reply or take action.',
    ].join(' '),
  },
)

await mcp.connect(new StdioServerTransport())

let myInstanceId = null
let cursor = null  // ISO timestamp; only events created strictly after this are pushed
const pushedEventIds = new Set()  // explicit dedup: never push the same event id twice

function cursorFilePath(instanceId) {
  return join(LEASE_DIR, `cursor-${instanceId}.json`)
}

function loadCursor(instanceId) {
  try {
    const raw = readFileSync(cursorFilePath(instanceId), 'utf-8')
    const data = JSON.parse(raw)
    return data.cursor || null
  } catch { return null }
}

function saveCursor(instanceId, c) {
  try {
    writeFileSync(cursorFilePath(instanceId), JSON.stringify({ cursor: c, updated_at: new Date().toISOString() }))
  } catch (err) {
    flog('WARN', `cursor save failed: ${err.message}`)
  }
}

function readLease() {
  try {
    const data = JSON.parse(readFileSync(LEASE_FILE, 'utf-8'))
    if (data.id) {
      if (data.id !== myInstanceId) {
        flog('INFO', `lease changed: ${myInstanceId || '(none)'} -> ${data.id}`)
        // Bridge re-registered under a new UUID. Reset cursor; old-UUID
        // events belong to a different stream.
        cursor = loadCursor(data.id) || new Date().toISOString()
      }
      myInstanceId = data.id
      return data.id
    }
  } catch {}
  return null
}

async function fetchEvents() {
  // Re-read the lease every poll so we follow the bridge's current UUID
  // even if Claude Code respawned the bridge mid-session.
  readLease()
  if (!myInstanceId) return []
  const qs = new URLSearchParams({ mark: '0' })
  if (cursor) qs.set('since', cursor)
  try {
    const res = await fetch(`${SYNAPSE_URL}/events/${myInstanceId}?${qs}`)
    if (!res.ok) {
      flog('WARN', `events fetch returned ${res.status}`)
      return []
    }
    const events = await res.json()
    if (!Array.isArray(events)) return []
    return events
  } catch (err) {
    flog('ERROR', `events fetch failed: ${err.message}`)
    return []
  }
}

function formatEvent(ev) {
  const p = ev.payload || {}
  switch (ev.type) {
    case 'message_arrived': {
      // Notification shape: content "From:\nSubject:\n\nbody" with meta
      // containing sender/subject/message_id/timestamp. Empirically,
      // deviating from this shape (extra meta keys, missing timestamp) can
      // disable host-side channel auto-fire on Claude Code.
      const content = `From: ${p.from || '?'}\nSubject: ${p.subject || '(no subject)'}\n\n${p.body || ''}`
      return {
        content,
        meta: {
          sender: p.from,
          subject: p.subject,
          message_id: p.message_id,
          timestamp: ev.created_at,
        },
      }
    }
    case 'identity_changed': {
      const reason = p.reason ? ` (${p.reason})` : ''
      return {
        content: `Your Synapse identity has been updated${reason}: you are now **${p.display_name}**.`,
        meta: { type: 'identity_changed', display_name: p.display_name, reason: p.reason || null },
      }
    }
    case 'invite_received': {
      return {
        content: [
          `Pending invite: team **${p.group_name}** as **${p.role}**, from ${p.invited_by}.`,
          `Use synapse_accept_invite({invite_id: "${p.invite_id}"}) to accept,`,
          `or synapse_decline_invite({invite_id: "${p.invite_id}"}) to decline.`,
        ].join('\n'),
        meta: { type: 'invite_received', invite_id: p.invite_id, group_name: p.group_name, role: p.role },
      }
    }
    case 'group_ownerless': {
      return {
        content: `Team **${p.group_name}** (id ${p.group_id}) is now ownerless. As admin, transfer ownership or dissolve.`,
        meta: { type: 'group_ownerless', group_id: p.group_id, group_name: p.group_name },
      }
    }
    default:
      return {
        content: `Unhandled event type: ${ev.type}\n${JSON.stringify(p, null, 2)}`,
        meta: { type: ev.type },
      }
  }
}

async function drainExisting() {
  // First-run vs respawn:
  //  - Respawn after a crash or kill: persisted cursor exists → resume from
  //    where we left off, don't drain (otherwise we silently drop messages
  //    that arrived during the gap).
  //  - Genuine first run on this instance UUID: no persisted cursor → drain
  //    all current events (mark as seen) and set cursor to "now" so we don't
  //    replay history for an instance that's been around a while.
  if (!myInstanceId) {
    cursor = new Date().toISOString()
    flog('INFO', `no instance id yet; deferring drain`)
    return
  }
  const persisted = loadCursor(myInstanceId)
  if (persisted) {
    cursor = persisted
    flog('INFO', `resumed from persisted cursor=${cursor}`)
    return
  }
  const events = await fetchEvents()
  for (const ev of events) {
    pushedEventIds.add(ev.id)
  }
  if (events.length > 0) {
    cursor = events[events.length - 1].created_at
  } else {
    cursor = new Date().toISOString()
  }
  saveCursor(myInstanceId, cursor)
  flog('INFO', `drained ${events.length} existing events; cursor=${cursor}`)
}

async function pollEvents() {
  const events = await fetchEvents()
  for (const ev of events) {
    cursor = ev.created_at
    if (myInstanceId) saveCursor(myInstanceId, cursor)
    if (pushedEventIds.has(ev.id)) {
      continue  // dedup against same-process re-fetches and lease-cursor resets
    }
    pushedEventIds.add(ev.id)
    // Bound the set so it doesn't grow forever in long-running sessions.
    if (pushedEventIds.size > 1000) {
      const drop = pushedEventIds.size - 800
      let i = 0
      for (const id of pushedEventIds) { if (i++ < drop) pushedEventIds.delete(id); else break }
    }
    const formatted = formatEvent(ev)
    try {
      await mcp.notification({
        method: 'notifications/claude/channel',
        params: { content: formatted.content, meta: formatted.meta },
      })
      flog('INFO', `pushed ${ev.type} ${ev.id}`)
    } catch (err) {
      flog('ERROR', `push failed: ${err.message}`)
    }
  }
}

readLease()
await drainExisting()
setInterval(pollEvents, POLL_INTERVAL)

flog('INFO', `v2.0 | instance: ${myInstanceId || '(no lease)'} | poll: ${POLL_INTERVAL}ms | synapse: ${SYNAPSE_URL} | lease: ${LEASE_FILE}`)

process.on('uncaughtException', (err) => {
  flog('CRITICAL', `uncaught: ${err.message}\n${err.stack}`)
  process.exit(1)
})
process.on('unhandledRejection', (reason) => {
  flog('CRITICAL', `unhandled rejection: ${reason}`)
})
process.on('exit', (code) => {
  flog('INFO', `exiting code=${code}`)
})
