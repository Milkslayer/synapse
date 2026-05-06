#!/usr/bin/env python3
"""Synapse v2 MCP Bridge — stdio MCP server for Synapse v2 protocol.

Runs as a child process of Claude Code. Maintains a stable UUID across
Claude restarts via a per-project lease file. Heartbeats every 10s in a
background thread. Polls /events for identity_changed notifications and
emits MCP notifications/tools/list_changed when display name shifts so
Claude refreshes the tool descriptions automatically.

"""

import hashlib
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request

# --- Config ---------------------------------------------------------------

SYNAPSE_URL = os.environ.get("SYNAPSE_URL", "http://localhost:3004")
HEARTBEAT_INTERVAL_S = int(os.environ.get("SYNAPSE_HEARTBEAT_INTERVAL", "10"))

# Pair the bridge with its sibling channel plugin via the parent Claude PID.
# Each Claude Code session has a unique PPID, so concurrent sessions in the
# same cwd don't collide. Lose identity persistence across Claude restarts —
# acceptable trade for working multi-session.
PPID = os.getppid()
LEASE_DIR = os.environ.get(
    "SYNAPSE_LEASE_DIR",
    os.path.join(os.path.expanduser("~"), ".claude", "synapse-v2"),
)
LEASE_FILE = os.path.join(LEASE_DIR, f"ppid-{PPID}.json")

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, f"bridge-v2-ppid-{PPID}.log")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("synapse-bridge-v2")


# --- HTTP helpers ---------------------------------------------------------

def _http(method, path, body=None, timeout=5):
    url = f"{SYNAPSE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def http_get(path, timeout=5):
    try:
        return _http("GET", path, timeout=timeout)
    except Exception as e:
        log.warning(f"GET {path} failed: {e}")
        return {"error": str(e)}


def http_post(path, body, timeout=10):
    try:
        return _http("POST", path, body=body, timeout=timeout)
    except Exception as e:
        log.warning(f"POST {path} failed: {e}")
        return {"error": str(e)}


def http_delete(path, body=None, timeout=10):
    try:
        return _http("DELETE", path, body=body, timeout=timeout)
    except Exception as e:
        log.warning(f"DELETE {path} failed: {e}")
        return {"error": str(e)}


# --- Lease ----------------------------------------------------------------

def load_lease():
    try:
        with open(LEASE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return None


def save_lease(state):
    os.makedirs(LEASE_DIR, exist_ok=True)
    tmp = LEASE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, LEASE_FILE)


# --- State (mutated by background threads) -------------------------------

_state_lock = threading.Lock()
INSTANCE_ID = None
DISPLAY_NAME = None
LAST_EVENT_TS = None  # ISO timestamp; cursor for /events polling

# stdout writer needs its own lock since both main loop and background
# thread can emit notifications.
_stdout_lock = threading.Lock()


def stdout_send(obj):
    with _stdout_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


# --- Registration --------------------------------------------------------

def register_with_server():
    global INSTANCE_ID, DISPLAY_NAME, LAST_EVENT_TS
    lease = load_lease()
    body = {"kind": "claude"}
    if lease and lease.get("id"):
        body["preferred_id"] = lease["id"]
    result = http_post("/register", body, timeout=10)
    if result.get("error") == "instance_already_active":
        log.warning(f"Lease UUID still active on server; registering fresh. lease={lease}")
        body.pop("preferred_id", None)
        result = http_post("/register", body, timeout=10)
    if "error" in result:
        log.error(f"Registration failed: {result}")
        return False
    with _state_lock:
        INSTANCE_ID = result["id"]
        DISPLAY_NAME = result["display_name"]
        LAST_EVENT_TS = None
    save_lease({"id": INSTANCE_ID, "display_name": DISPLAY_NAME, "registered_at": time.time()})
    log.info(f"Registered: id={INSTANCE_ID} display_name={DISPLAY_NAME} reactivated={result.get('reactivated', False)}")
    return True


# --- Background loops ----------------------------------------------------

def heartbeat_loop():
    while True:
        try:
            with _state_lock:
                iid = INSTANCE_ID
            if iid:
                http_post("/heartbeat", {"id": iid}, timeout=3)
        except Exception as e:
            log.warning(f"heartbeat failed: {e}")
        time.sleep(HEARTBEAT_INTERVAL_S)


def event_poll_loop():
    """Poll /events for identity_changed; update local display name and notify Claude."""
    global DISPLAY_NAME, LAST_EVENT_TS
    while True:
        try:
            with _state_lock:
                iid = INSTANCE_ID
                cursor = LAST_EVENT_TS
            if not iid:
                time.sleep(2)
                continue
            qs = f"?mark=0"
            if cursor:
                qs += f"&since={cursor}"
            events = http_get(f"/events/{iid}{qs}", timeout=5)
            if isinstance(events, list):
                for ev in events:
                    with _state_lock:
                        LAST_EVENT_TS = ev["created_at"]
                    if ev["type"] == "identity_changed":
                        new_name = ev["payload"].get("display_name")
                        if new_name:
                            with _state_lock:
                                if new_name != DISPLAY_NAME:
                                    log.info(f"identity_changed: {DISPLAY_NAME} -> {new_name}")
                                    DISPLAY_NAME = new_name
                                    save_lease({"id": INSTANCE_ID, "display_name": new_name, "registered_at": time.time()})
                            # Notify Claude that tool descriptions have changed
                            stdout_send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        except Exception as e:
            log.warning(f"event poll failed: {e}")
        time.sleep(5)


# --- Tool definitions (descriptions reference current DISPLAY_NAME) ------

def build_tools():
    name = DISPLAY_NAME or "(unregistered)"
    iid_hint = INSTANCE_ID or "(unregistered)"
    return [
        {
            "name": "synapse_send",
            "description": f"Send a message via Synapse v2. Your current display name is **{name}** (id={iid_hint}). `to` may be: 'claude' (admin only, global broadcast), 'claude-{{team}}' (team broadcast — sender must be in the team or admin), 'claude-{{team}}-{{role}}' or 'claude-{{role}}' (DM by display name), 'admin' (admins), or any specific display name.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient address — see description"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "from": {"type": "string", "description": f"Optional. Defaults to your id ({iid_hint})."},
                },
                "required": ["to", "subject", "body"],
            },
        },
        {
            "name": "synapse_inbox",
            "description": f"Your inbox. Returns messages routed to your instance. Auto-uses your id ({iid_hint}) unless you pass one explicitly.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "unread_only": {"type": "boolean"},
                },
            },
        },
        {
            "name": "synapse_recipients",
            "description": "Snapshot of all instances and groups on the network (active / stale / offline).",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "synapse_set_role",
            "description": f"Claim or change your role. Display name updates to claude-{{role}} (or claude-{{team}}-{{role}}) with auto -2 suffix on collision. You are currently **{name}**.",
            "inputSchema": {
                "type": "object",
                "properties": {"role": {"type": "string"}},
                "required": ["role"],
            },
        },
        {
            "name": "synapse_release_role",
            "description": "Drop your role. Display name reverts to claude-N (or claude-{team}-member if you're in a team).",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "synapse_create_team",
            "description": "Create a team. You become the owner. Team name must be lowercase alphanumeric (with dashes), and must not collide with any existing role name.",
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
        {
            "name": "synapse_dissolve_team",
            "description": "Dissolve a team you own. Members fall back to no-team; team broadcasts stop working. Owner or admin only.",
            "inputSchema": {
                "type": "object",
                "properties": {"group_id": {"type": "string"}},
                "required": ["group_id"],
            },
        },
        {
            "name": "synapse_invite",
            "description": "Invite a Claude to your team with a specific role. Owner only (or admin).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "group_id": {"type": "string"},
                    "invitee_id": {"type": "string"},
                    "role": {"type": "string"},
                },
                "required": ["group_id", "invitee_id", "role"],
            },
        },
        {
            "name": "synapse_accept_invite",
            "description": "Accept a pending invite. Your display name updates to claude-{team}-{role}.",
            "inputSchema": {
                "type": "object",
                "properties": {"invite_id": {"type": "string"}},
                "required": ["invite_id"],
            },
        },
        {
            "name": "synapse_decline_invite",
            "description": "Decline a pending invite.",
            "inputSchema": {
                "type": "object",
                "properties": {"invite_id": {"type": "string"}},
                "required": ["invite_id"],
            },
        },
        {
            "name": "synapse_leave_team",
            "description": "Leave your current team. Display name recomputes to claude-{role} or claude-N.",
            "inputSchema": {
                "type": "object",
                "properties": {"group_id": {"type": "string"}},
                "required": ["group_id"],
            },
        },
        {
            "name": "synapse_kick",
            "description": "Kick a member from your team. Owner or admin only.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "group_id": {"type": "string"},
                    "member_id": {"type": "string"},
                },
                "required": ["group_id", "member_id"],
            },
        },
        {
            "name": "synapse_groups",
            "description": "List all teams (active and dissolved) with member counts.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "synapse_pending_invites",
            "description": "List your pending invites (you as invitee).",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


# --- Tool dispatch -------------------------------------------------------

def dispatch_tool(name, args):
    with _state_lock:
        iid = INSTANCE_ID
        my_name = DISPLAY_NAME
    if not iid:
        return {"error": "not_registered"}

    if name == "synapse_send":
        sender_id = args.get("from") or iid
        result = http_post("/send", {
            "from": sender_id,
            "to": args.get("to"),
            "subject": args.get("subject", ""),
            "body": args.get("body", ""),
        })
        if isinstance(result, dict):
            result["you_are"] = my_name
        return result

    if name == "synapse_inbox":
        target = args.get("id") or iid
        unread_qs = "?unread" if args.get("unread_only") else ""
        return http_get(f"/inbox/{target}{unread_qs}")

    if name == "synapse_recipients":
        snap = http_get("/recipients")
        if isinstance(snap, dict):
            snap["you_are"] = {"id": iid, "display_name": my_name}
        return snap

    if name == "synapse_set_role":
        return http_post("/identity/set-role", {"id": iid, "role": args.get("role")})

    if name == "synapse_release_role":
        return http_post("/identity/release-role", {"id": iid})

    if name == "synapse_create_team":
        return http_post("/groups", {"name": args.get("name"), "owner_id": iid})

    if name == "synapse_dissolve_team":
        return http_delete(f"/groups/{args.get('group_id')}", {"by_id": iid})

    if name == "synapse_invite":
        return http_post(f"/groups/{args.get('group_id')}/invite", {
            "invitee_id": args.get("invitee_id"),
            "role": args.get("role"),
            "by_id": iid,
        })

    if name == "synapse_accept_invite":
        return http_post(f"/invites/{args.get('invite_id')}/accept", {"by_id": iid})

    if name == "synapse_decline_invite":
        return http_post(f"/invites/{args.get('invite_id')}/decline", {"by_id": iid})

    if name == "synapse_leave_team":
        return http_post(f"/groups/{args.get('group_id')}/leave", {"member_id": iid})

    if name == "synapse_kick":
        return http_post(f"/groups/{args.get('group_id')}/kick", {
            "member_id": args.get("member_id"),
            "by_id": iid,
        })

    if name == "synapse_groups":
        return http_get("/groups")

    if name == "synapse_pending_invites":
        # Filter from /events (invite_received entries) — quick implementation;
        # could be a dedicated endpoint later.
        ev = http_get(f"/events/{iid}?mark=0")
        if not isinstance(ev, list):
            return ev
        return [e["payload"] for e in ev if e["type"] == "invite_received"]

    return {"error": f"unknown_tool: {name}"}


# --- MCP main loop -------------------------------------------------------

def main():
    if not register_with_server():
        log.error("Could not register; bridge will keep retrying in heartbeat loop.")

    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=event_poll_loop, daemon=True).start()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = request.get("method")
        req_id = request.get("id")

        if method == "initialize":
            stdout_send({
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {"listChanged": True}},
                    "serverInfo": {"name": "synapse-v2", "version": "2.0.0"},
                },
            })

        elif method == "notifications/initialized":
            pass

        elif method == "tools/list":
            stdout_send({
                "jsonrpc": "2.0", "id": req_id,
                "result": {"tools": build_tools()},
            })

        elif method == "tools/call":
            tool_name = request["params"]["name"]
            arguments = request["params"].get("arguments", {})
            result = dispatch_tool(tool_name, arguments)
            stdout_send({
                "jsonrpc": "2.0", "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]},
            })

        elif method == "ping":
            stdout_send({"jsonrpc": "2.0", "id": req_id, "result": {}})

        else:
            stdout_send({
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            })


if __name__ == "__main__":
    log.info(f"Bridge v2 starting: pid={os.getpid()} ppid={os.getppid()} cwd={os.getcwd()} synapse={SYNAPSE_URL} lease={LEASE_FILE}")
    try:
        main()
    except KeyboardInterrupt:
        log.info("Bridge stopped (keyboard interrupt)")
    except BrokenPipeError:
        log.info("Bridge stopped (broken pipe — Claude Code closed)")
    except Exception as e:
        log.critical(f"Bridge crashed: {e}", exc_info=True)
        raise
    finally:
        try:
            with _state_lock:
                iid = INSTANCE_ID
            if iid:
                http_post("/unregister", {"id": iid}, timeout=2)
                log.info(f"Bridge unregistered: {iid}")
        except Exception:
            pass
