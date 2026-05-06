#!/usr/bin/env python3
"""Synapse v2 — agent messaging with first-class groups, role/team-derived
display names, and persistent active/stale/offline presence.

Runs in Docker container synapse-v2 on port 3004 by default.
"""

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

# --- Config ---------------------------------------------------------------

DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "synapse_v2.db")
DB_PATH = os.environ.get("SYNAPSE_DB_PATH", DEFAULT_DB_PATH)
PORT = int(os.environ.get("SYNAPSE_PORT", "3004"))
INSTANCE_NAME = os.environ.get("SYNAPSE_INSTANCE", "synapse-v2")

ACTIVE_TIMEOUT_S = 60          # active → stale
STALE_TIMEOUT_S = 3600         # stale → offline
CLEANUP_INTERVAL_S = 10        # presence sweep cadence

# Roles and team names: lowercase, alphanumeric + dashes, must start with a letter.
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")


# --- Helpers --------------------------------------------------------------

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def now_ts():
    return time.time()


def new_id():
    return uuid.uuid4().hex


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS instances (
            id            TEXT PRIMARY KEY,
            display_name  TEXT NOT NULL UNIQUE,
            team_id       TEXT NULL,
            role          TEXT NULL,
            kind          TEXT NOT NULL CHECK (kind IN ('claude', 'admin')),
            registered_at TEXT NOT NULL,
            last_seen_at  TEXT NOT NULL,
            status        TEXT NOT NULL CHECK (status IN ('active', 'stale', 'offline'))
        );
        CREATE INDEX IF NOT EXISTS idx_instances_team ON instances(team_id);
        CREATE INDEX IF NOT EXISTS idx_instances_status ON instances(status);

        CREATE TABLE IF NOT EXISTS groups (
            id           TEXT PRIMARY KEY,
            name         TEXT NOT NULL UNIQUE,
            owner_id     TEXT NULL,
            created_at   TEXT NOT NULL,
            dissolved_at TEXT NULL
        );

        CREATE TABLE IF NOT EXISTS invites (
            id          TEXT PRIMARY KEY,
            group_id    TEXT NOT NULL,
            invitee_id  TEXT NOT NULL,
            role        TEXT NOT NULL,
            invited_by  TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            status      TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'declined', 'revoked'))
        );
        CREATE INDEX IF NOT EXISTS idx_invites_invitee ON invites(invitee_id, status);

        CREATE TABLE IF NOT EXISTS messages (
            id             TEXT PRIMARY KEY,
            sender_id      TEXT NOT NULL,
            sender_name    TEXT NOT NULL,
            recipient_addr TEXT NOT NULL,
            recipient_kind TEXT NOT NULL CHECK (recipient_kind IN ('global', 'group', 'instance', 'admin')),
            resolved_ids   TEXT NOT NULL,
            subject        TEXT NOT NULL,
            body           TEXT NOT NULL,
            timestamp      TEXT NOT NULL,
            archived       INTEGER NOT NULL DEFAULT 0,
            archived_at    TEXT NULL
        );

        CREATE TABLE IF NOT EXISTS reads (
            message_id TEXT NOT NULL,
            reader_id  TEXT NOT NULL,
            read_at    TEXT NOT NULL,
            PRIMARY KEY (message_id, reader_id)
        );

        CREATE TABLE IF NOT EXISTS events (
            id           TEXT PRIMARY KEY,
            target_id    TEXT NOT NULL,
            type         TEXT NOT NULL,
            payload      TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            delivered_at TEXT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_events_target ON events(target_id, delivered_at);
    """)
    conn.commit()
    conn.close()


# --- Display-name resolution ---------------------------------------------

def _claim_name(conn, base, exclude_id=None):
    """Return an available display name based on `base`, suffixing -2/-3/... on collision."""
    rows = conn.execute(
        "SELECT id, display_name FROM instances WHERE display_name = ? OR display_name LIKE ?",
        (base, f"{base}-%")
    ).fetchall()
    taken = set()
    for r in rows:
        if r["id"] == exclude_id:
            continue
        taken.add(r["display_name"])
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def _next_unassigned_name(conn, exclude_id=None):
    """Find next free `claude-N` for an unassigned Claude."""
    rows = conn.execute(
        "SELECT id, display_name FROM instances WHERE display_name GLOB 'claude-[0-9]*'"
    ).fetchall()
    taken = set()
    for r in rows:
        if r["id"] == exclude_id:
            continue
        taken.add(r["display_name"])
    n = 1
    while f"claude-{n}" in taken:
        n += 1
    return f"claude-{n}"


def _next_admin_name(conn, exclude_id=None, hint=None):
    """Pick a name for an `admin` registration. hint='web' → admin-web; otherwise admin-N."""
    if hint:
        base = f"admin-{hint}"
        rows = conn.execute(
            "SELECT id, display_name FROM instances WHERE display_name = ? OR display_name LIKE ?",
            (base, f"{base}-%")
        ).fetchall()
        taken = set(r["display_name"] for r in rows if r["id"] != exclude_id)
        if base not in taken:
            return base
        n = 2
        while f"{base}-{n}" in taken:
            n += 1
        return f"{base}-{n}"
    return _claim_name(conn, "admin", exclude_id=exclude_id)


def _compute_display_name(conn, instance_id, role, team_id):
    """Compute the canonical display name for an instance given its role/team."""
    if team_id:
        team = conn.execute("SELECT name FROM groups WHERE id = ?", (team_id,)).fetchone()
        if not team:
            raise ValueError(f"team {team_id} not found")
        if not role:
            # Team membership without role would create an ambiguous claude-{team} address.
            # Force a default role of "member" so display_name stays disambiguated.
            role = "member"
        base = f"claude-{team['name']}-{role}"
    elif role:
        base = f"claude-{role}"
    else:
        return _next_unassigned_name(conn, exclude_id=instance_id)
    return _claim_name(conn, base, exclude_id=instance_id)


def _validate_name_token(token, what):
    if not NAME_RE.match(token):
        raise ValueError(f"invalid {what}: must match {NAME_RE.pattern}")


def _check_role_team_disjoint(conn, role=None, team_name=None, exclude_team_id=None, exclude_role_holder=None):
    """Enforce that team names and role names don't collide."""
    if role:
        clash = conn.execute(
            "SELECT id FROM groups WHERE name = ? AND dissolved_at IS NULL",
            (role,)
        ).fetchone()
        if clash and clash["id"] != exclude_team_id:
            raise ValueError(f"role '{role}' collides with existing team name")
    if team_name:
        clash = conn.execute(
            "SELECT id FROM instances WHERE role = ? AND id != COALESCE(?, '')",
            (team_name, exclude_role_holder)
        ).fetchone()
        if clash:
            raise ValueError(f"team name '{team_name}' collides with existing role")


# --- Instance lifecycle --------------------------------------------------

def register(kind, preferred_id=None, preferred_name=None):
    if kind not in ("claude", "admin"):
        return {"error": f"invalid kind: {kind}"}
    conn = get_db()
    try:
        if preferred_id:
            existing = conn.execute("SELECT * FROM instances WHERE id = ?", (preferred_id,)).fetchone()
            if existing:
                # Always take over a matching UUID — presenting the UUID proves
                # ownership of the lease, and a respawn from the same cwd is
                # the same logical Claude as before. The old process (if any)
                # will see its heartbeats overwritten harmlessly until it dies.
                conn.execute(
                    "UPDATE instances SET status = 'active', last_seen_at = ? WHERE id = ?",
                    (now_iso(), preferred_id)
                )
                conn.commit()
                return {"id": preferred_id, "display_name": existing["display_name"], "reactivated": True}
            iid = preferred_id  # use it as the new UUID
        else:
            iid = new_id()

        # Pick display name
        if preferred_name and NAME_RE.match(preferred_name):
            display = _claim_name(conn, preferred_name)
        elif kind == "admin":
            hint = preferred_name.split("-", 1)[1] if (preferred_name and preferred_name.startswith("admin-")) else None
            display = _next_admin_name(conn, hint=hint)
        else:
            display = _next_unassigned_name(conn)

        ts = now_iso()
        conn.execute(
            "INSERT INTO instances (id, display_name, team_id, role, kind, registered_at, last_seen_at, status) VALUES (?, ?, NULL, NULL, ?, ?, ?, 'active')",
            (iid, display, kind, ts, ts)
        )
        conn.commit()
        return {"id": iid, "display_name": display, "reactivated": False}
    finally:
        conn.close()


def heartbeat(iid):
    conn = get_db()
    try:
        row = conn.execute("SELECT status FROM instances WHERE id = ?", (iid,)).fetchone()
        if not row:
            return {"error": "unknown_instance"}
        new_status = "active"
        conn.execute(
            "UPDATE instances SET last_seen_at = ?, status = ? WHERE id = ?",
            (now_iso(), new_status, iid)
        )
        conn.commit()
        return {"status": new_status}
    finally:
        conn.close()


def unregister(iid):
    conn = get_db()
    try:
        conn.execute(
            "UPDATE instances SET status = 'offline', last_seen_at = ? WHERE id = ?",
            (now_iso(), iid)
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def forget(iid, by_id):
    if not _is_admin(by_id):
        return {"error": "forbidden_admin_only"}
    conn = get_db()
    try:
        conn.execute("DELETE FROM instances WHERE id = ?", (iid,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# --- Identity ------------------------------------------------------------

def set_role(iid, role):
    _validate_name_token(role, "role")
    conn = get_db()
    try:
        inst = conn.execute("SELECT * FROM instances WHERE id = ?", (iid,)).fetchone()
        if not inst:
            return {"error": "unknown_instance"}
        if inst["kind"] != "claude":
            return {"error": "only_claude_can_have_roles"}
        _check_role_team_disjoint(conn, role=role, exclude_role_holder=iid)
        new_display = _compute_display_name(conn, iid, role, inst["team_id"])
        conn.execute(
            "UPDATE instances SET role = ?, display_name = ? WHERE id = ?",
            (role, new_display, iid)
        )
        _emit_event(conn, iid, "identity_changed", {"display_name": new_display, "role": role, "team_id": inst["team_id"]})
        conn.commit()
        return {"display_name": new_display, "role": role}
    except ValueError as e:
        return {"error": str(e)}
    finally:
        conn.close()


def release_role(iid):
    conn = get_db()
    try:
        inst = conn.execute("SELECT * FROM instances WHERE id = ?", (iid,)).fetchone()
        if not inst:
            return {"error": "unknown_instance"}
        new_display = _compute_display_name(conn, iid, None, inst["team_id"])
        conn.execute(
            "UPDATE instances SET role = NULL, display_name = ? WHERE id = ?",
            (new_display, iid)
        )
        _emit_event(conn, iid, "identity_changed", {"display_name": new_display, "role": None, "team_id": inst["team_id"]})
        conn.commit()
        return {"display_name": new_display, "role": None}
    finally:
        conn.close()


def set_team(iid, team_id, role, by_id):
    """M-only direct team assignment (bypasses invite flow)."""
    if not _is_admin(by_id):
        return {"error": "forbidden_admin_only"}
    if role:
        _validate_name_token(role, "role")
    conn = get_db()
    try:
        inst = conn.execute("SELECT * FROM instances WHERE id = ?", (iid,)).fetchone()
        if not inst:
            return {"error": "unknown_instance"}
        if inst["kind"] != "claude":
            return {"error": "only_claude_can_join_teams"}
        if team_id is not None:
            team = conn.execute("SELECT * FROM groups WHERE id = ? AND dissolved_at IS NULL", (team_id,)).fetchone()
            if not team:
                return {"error": "team_not_found"}
            _check_role_team_disjoint(conn, role=role, exclude_role_holder=iid)
        new_display = _compute_display_name(conn, iid, role, team_id)
        conn.execute(
            "UPDATE instances SET team_id = ?, role = ?, display_name = ? WHERE id = ?",
            (team_id, role, new_display, iid)
        )
        _emit_event(conn, iid, "identity_changed", {"display_name": new_display, "role": role, "team_id": team_id})
        conn.commit()
        return {"display_name": new_display, "team_id": team_id, "role": role}
    except ValueError as e:
        return {"error": str(e)}
    finally:
        conn.close()


# --- Groups --------------------------------------------------------------

def _is_admin(iid):
    if not iid:
        return False
    conn = get_db()
    try:
        row = conn.execute("SELECT kind FROM instances WHERE id = ?", (iid,)).fetchone()
        return bool(row and row["kind"] == "admin")
    finally:
        conn.close()


def create_group(name, owner_id):
    try:
        _validate_name_token(name, "team_name")
    except ValueError as e:
        return {"error": str(e)}
    conn = get_db()
    try:
        owner = conn.execute("SELECT id FROM instances WHERE id = ?", (owner_id,)).fetchone()
        if not owner:
            return {"error": "owner_not_found"}
        try:
            _check_role_team_disjoint(conn, team_name=name)
        except ValueError as e:
            return {"error": str(e)}
        existing = conn.execute("SELECT id FROM groups WHERE name = ? AND dissolved_at IS NULL", (name,)).fetchone()
        if existing:
            return {"error": "team_name_taken"}
        gid = new_id()
        conn.execute(
            "INSERT INTO groups (id, name, owner_id, created_at) VALUES (?, ?, ?, ?)",
            (gid, name, owner_id, now_iso())
        )
        conn.commit()
        return {"id": gid, "name": name, "owner_id": owner_id}
    finally:
        conn.close()


def dissolve_group(gid, by_id):
    conn = get_db()
    try:
        group = conn.execute("SELECT * FROM groups WHERE id = ? AND dissolved_at IS NULL", (gid,)).fetchone()
        if not group:
            return {"error": "team_not_found"}
        if by_id != group["owner_id"] and not _is_admin(by_id):
            return {"error": "forbidden"}
        # Move members out, recompute their display names
        members = conn.execute("SELECT id, role FROM instances WHERE team_id = ?", (gid,)).fetchall()
        for m in members:
            new_display = _compute_display_name(conn, m["id"], m["role"], None)
            conn.execute(
                "UPDATE instances SET team_id = NULL, display_name = ? WHERE id = ?",
                (new_display, m["id"])
            )
            _emit_event(conn, m["id"], "identity_changed", {"display_name": new_display, "role": m["role"], "team_id": None, "reason": "team_dissolved"})
        # Revoke pending invites
        conn.execute("UPDATE invites SET status = 'revoked' WHERE group_id = ? AND status = 'pending'", (gid,))
        # Soft-delete the group
        conn.execute("UPDATE groups SET dissolved_at = ? WHERE id = ?", (now_iso(), gid))
        conn.commit()
        return {"ok": True, "id": gid, "members_freed": len(members)}
    finally:
        conn.close()


def transfer_owner(gid, new_owner_id, by_id):
    if not _is_admin(by_id):
        return {"error": "forbidden_admin_only"}
    conn = get_db()
    try:
        group = conn.execute("SELECT id FROM groups WHERE id = ? AND dissolved_at IS NULL", (gid,)).fetchone()
        if not group:
            return {"error": "team_not_found"}
        new_owner = conn.execute("SELECT id FROM instances WHERE id = ?", (new_owner_id,)).fetchone()
        if not new_owner:
            return {"error": "owner_not_found"}
        conn.execute("UPDATE groups SET owner_id = ? WHERE id = ?", (new_owner_id, gid))
        conn.commit()
        return {"ok": True, "id": gid, "owner_id": new_owner_id}
    finally:
        conn.close()


def list_groups():
    conn = get_db()
    try:
        out = []
        for g in conn.execute("SELECT * FROM groups ORDER BY created_at"):
            members = conn.execute("SELECT COUNT(*) c, SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) a FROM instances WHERE team_id = ?", (g["id"],)).fetchone()
            out.append({
                "id": g["id"],
                "name": g["name"],
                "owner_id": g["owner_id"],
                "created_at": g["created_at"],
                "dissolved_at": g["dissolved_at"],
                "members": members["c"] or 0,
                "active_members": members["a"] or 0,
            })
        return out
    finally:
        conn.close()


def get_group(gid):
    conn = get_db()
    try:
        g = conn.execute("SELECT * FROM groups WHERE id = ?", (gid,)).fetchone()
        if not g:
            return {"error": "team_not_found"}
        members = [dict(r) for r in conn.execute("SELECT id, display_name, role, status FROM instances WHERE team_id = ?", (gid,))]
        return {**dict(g), "members": members}
    finally:
        conn.close()


# --- Invites & membership ------------------------------------------------

def invite(gid, invitee_id, role, by_id):
    _validate_name_token(role, "role")
    conn = get_db()
    try:
        group = conn.execute("SELECT * FROM groups WHERE id = ? AND dissolved_at IS NULL", (gid,)).fetchone()
        if not group:
            return {"error": "team_not_found"}
        if by_id != group["owner_id"] and not _is_admin(by_id):
            return {"error": "forbidden_owner_or_admin_only"}
        invitee = conn.execute("SELECT id, kind FROM instances WHERE id = ?", (invitee_id,)).fetchone()
        if not invitee:
            return {"error": "invitee_not_found"}
        if invitee["kind"] != "claude":
            return {"error": "only_claude_can_join_teams"}
        try:
            _check_role_team_disjoint(conn, role=role, exclude_role_holder=invitee_id)
        except ValueError as e:
            return {"error": str(e)}
        # Cancel any prior pending invites for this invitee+group
        conn.execute(
            "UPDATE invites SET status = 'revoked' WHERE invitee_id = ? AND group_id = ? AND status = 'pending'",
            (invitee_id, gid)
        )
        invite_id = new_id()
        conn.execute(
            "INSERT INTO invites (id, group_id, invitee_id, role, invited_by, created_at, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
            (invite_id, gid, invitee_id, role, by_id, now_iso())
        )
        _emit_event(conn, invitee_id, "invite_received", {
            "invite_id": invite_id, "group_id": gid, "group_name": group["name"], "role": role, "invited_by": by_id,
        })
        conn.commit()
        return {"id": invite_id, "group_id": gid, "invitee_id": invitee_id, "role": role, "status": "pending"}
    finally:
        conn.close()


def accept_invite(invite_id, by_id):
    conn = get_db()
    try:
        inv = conn.execute("SELECT * FROM invites WHERE id = ?", (invite_id,)).fetchone()
        if not inv:
            return {"error": "invite_not_found"}
        if inv["status"] != "pending":
            return {"error": f"invite_status_{inv['status']}"}
        if by_id != inv["invitee_id"]:
            return {"error": "forbidden_invitee_only"}
        group = conn.execute("SELECT * FROM groups WHERE id = ? AND dissolved_at IS NULL", (inv["group_id"],)).fetchone()
        if not group:
            conn.execute("UPDATE invites SET status = 'revoked' WHERE id = ?", (invite_id,))
            conn.commit()
            return {"error": "team_dissolved"}
        # If invitee was already in a team, they leave first (one team max)
        new_display = _compute_display_name(conn, inv["invitee_id"], inv["role"], inv["group_id"])
        conn.execute(
            "UPDATE instances SET team_id = ?, role = ?, display_name = ? WHERE id = ?",
            (inv["group_id"], inv["role"], new_display, inv["invitee_id"])
        )
        conn.execute("UPDATE invites SET status = 'accepted' WHERE id = ?", (invite_id,))
        _emit_event(conn, inv["invitee_id"], "identity_changed", {
            "display_name": new_display, "role": inv["role"], "team_id": inv["group_id"], "reason": "invite_accepted",
        })
        conn.commit()
        return {"ok": True, "display_name": new_display, "team_id": inv["group_id"], "role": inv["role"]}
    finally:
        conn.close()


def decline_invite(invite_id, by_id):
    conn = get_db()
    try:
        inv = conn.execute("SELECT * FROM invites WHERE id = ?", (invite_id,)).fetchone()
        if not inv:
            return {"error": "invite_not_found"}
        if inv["status"] != "pending":
            return {"error": f"invite_status_{inv['status']}"}
        if by_id != inv["invitee_id"]:
            return {"error": "forbidden_invitee_only"}
        conn.execute("UPDATE invites SET status = 'declined' WHERE id = ?", (invite_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def revoke_invite(invite_id, by_id):
    conn = get_db()
    try:
        inv = conn.execute("SELECT * FROM invites WHERE id = ?", (invite_id,)).fetchone()
        if not inv:
            return {"error": "invite_not_found"}
        if inv["status"] != "pending":
            return {"error": f"invite_status_{inv['status']}"}
        group = conn.execute("SELECT owner_id FROM groups WHERE id = ?", (inv["group_id"],)).fetchone()
        if by_id != inv["invited_by"] and (not group or by_id != group["owner_id"]) and not _is_admin(by_id):
            return {"error": "forbidden"}
        conn.execute("UPDATE invites SET status = 'revoked' WHERE id = ?", (invite_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def leave_group(gid, member_id, by_id):
    if member_id != by_id and not _is_admin(by_id):
        return {"error": "forbidden_self_leave_only"}
    conn = get_db()
    try:
        inst = conn.execute("SELECT * FROM instances WHERE id = ?", (member_id,)).fetchone()
        if not inst:
            return {"error": "unknown_instance"}
        if inst["team_id"] != gid:
            return {"error": "not_a_member"}
        group = conn.execute("SELECT * FROM groups WHERE id = ?", (gid,)).fetchone()
        new_display = _compute_display_name(conn, member_id, inst["role"], None)
        conn.execute(
            "UPDATE instances SET team_id = NULL, display_name = ? WHERE id = ?",
            (new_display, member_id)
        )
        _emit_event(conn, member_id, "identity_changed", {
            "display_name": new_display, "role": inst["role"], "team_id": None, "reason": "left_team",
        })
        # If owner left, mark team ownerless and notify M
        if group and group["owner_id"] == member_id:
            conn.execute("UPDATE groups SET owner_id = NULL WHERE id = ?", (gid,))
            for m in conn.execute("SELECT id FROM instances WHERE kind = 'admin'"):
                _emit_event(conn, m["id"], "group_ownerless", {"group_id": gid, "group_name": group["name"]})
        conn.commit()
        return {"ok": True, "display_name": new_display}
    finally:
        conn.close()


def kick_member(gid, member_id, by_id):
    conn = get_db()
    try:
        group = conn.execute("SELECT * FROM groups WHERE id = ? AND dissolved_at IS NULL", (gid,)).fetchone()
        if not group:
            return {"error": "team_not_found"}
        if by_id != group["owner_id"] and not _is_admin(by_id):
            return {"error": "forbidden_owner_or_admin_only"}
        if member_id == group["owner_id"]:
            return {"error": "cannot_kick_owner"}
        inst = conn.execute("SELECT * FROM instances WHERE id = ?", (member_id,)).fetchone()
        if not inst or inst["team_id"] != gid:
            return {"error": "not_a_member"}
        new_display = _compute_display_name(conn, member_id, inst["role"], None)
        conn.execute(
            "UPDATE instances SET team_id = NULL, display_name = ? WHERE id = ?",
            (new_display, member_id)
        )
        _emit_event(conn, member_id, "identity_changed", {
            "display_name": new_display, "role": inst["role"], "team_id": None, "reason": "kicked_from_team",
        })
        conn.commit()
        return {"ok": True, "display_name": new_display}
    finally:
        conn.close()


# --- Routing and messages ------------------------------------------------

def _resolve_recipients(conn, sender, to_addr):
    """Return (recipient_kind, [instance_ids])."""
    if not to_addr:
        raise ValueError("empty recipient")

    if to_addr == "claude":
        if sender["kind"] != "admin":
            raise PermissionError("only_m_can_send_global_broadcast")
        rows = conn.execute(
            "SELECT id FROM instances WHERE kind = 'claude' AND status != 'offline' AND id != ?",
            (sender["id"],)
        ).fetchall()
        return "global", [r["id"] for r in rows]

    if to_addr == "admin":
        rows = conn.execute(
            "SELECT id FROM instances WHERE kind = 'admin' AND id != ?",
            (sender["id"],)
        ).fetchall()
        return "admin", [r["id"] for r in rows]

    # Specific display name (DM) — checked before team broadcast so direct
    # addressing always wins over a coincidental team-name match.
    row = conn.execute("SELECT id FROM instances WHERE display_name = ?", (to_addr,)).fetchone()
    if row:
        return "instance", [row["id"]]

    # claude-{team} broadcast
    if to_addr.startswith("claude-"):
        team_name = to_addr[len("claude-"):]
        team = conn.execute(
            "SELECT id FROM groups WHERE name = ? AND dissolved_at IS NULL",
            (team_name,)
        ).fetchone()
        if team:
            if sender["team_id"] != team["id"] and sender["kind"] != "admin":
                raise PermissionError("sender_not_in_team")
            rows = conn.execute(
                "SELECT id FROM instances WHERE team_id = ? AND kind = 'claude' AND status != 'offline' AND id != ?",
                (team["id"], sender["id"])
            ).fetchall()
            return "group", [r["id"] for r in rows]
        # Team existed once but was dissolved
        dissolved = conn.execute(
            "SELECT id FROM groups WHERE name = ? AND dissolved_at IS NOT NULL",
            (team_name,)
        ).fetchone()
        if dissolved:
            raise ValueError("team_dissolved")

    raise ValueError(f"unknown_recipient: {to_addr}")


def send_message(sender_id, to_addr, subject, body):
    conn = get_db()
    try:
        sender = conn.execute("SELECT * FROM instances WHERE id = ?", (sender_id,)).fetchone()
        if not sender:
            return {"error": "unknown_sender"}
        try:
            kind, ids = _resolve_recipients(conn, sender, to_addr)
        except PermissionError as e:
            return {"error": str(e)}
        except ValueError as e:
            return {"error": str(e)}
        msg_id = new_id()[:12]
        ts = now_iso()
        conn.execute(
            "INSERT INTO messages (id, sender_id, sender_name, recipient_addr, recipient_kind, resolved_ids, subject, body, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (msg_id, sender_id, sender["display_name"], to_addr, kind, json.dumps(ids), subject, body, ts)
        )
        # Push message_arrived events with full payload so the channel
        # plugin can surface body preview without an extra fetch
        for rid in ids:
            _emit_event(conn, rid, "message_arrived", {
                "message_id": msg_id,
                "from": sender["display_name"],
                "subject": subject,
                "body": body,
                "recipient_addr": to_addr,
                "recipient_kind": kind,
            })
        conn.commit()
        return {"id": msg_id, "status": "sent", "to": to_addr, "kind": kind, "delivered_to": len(ids), "timestamp": ts, "sent_as": sender["display_name"]}
    finally:
        conn.close()


def get_inbox(instance_id, unread_only=False, limit=50):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM messages WHERE archived = 0 ORDER BY timestamp DESC LIMIT 200"
        ).fetchall()
        out = []
        for r in rows:
            ids = json.loads(r["resolved_ids"])
            if instance_id not in ids:
                continue
            d = dict(r)
            d["resolved_ids"] = ids
            read_row = conn.execute(
                "SELECT read_at FROM reads WHERE message_id = ? AND reader_id = ?",
                (r["id"], instance_id)
            ).fetchone()
            d["read_by_me"] = 1 if read_row else 0
            d["read_at_by_me"] = read_row["read_at"] if read_row else None
            if unread_only and d["read_by_me"]:
                continue
            out.append(d)
            if len(out) >= limit:
                break
        return out
    finally:
        conn.close()


def get_all_messages(limit=100, offset=0):
    """Admin view: every message on the network, newest first."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM messages WHERE archived = 0 ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM messages WHERE archived = 0").fetchone()[0]
        out = []
        for r in rows:
            d = dict(r)
            d["resolved_ids"] = json.loads(r["resolved_ids"])
            out.append(d)
        return {"messages": out, "total": total}
    finally:
        conn.close()


def get_sent(instance_id, limit=50):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM messages WHERE sender_id = ? ORDER BY timestamp DESC LIMIT ?",
            (instance_id, limit)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["resolved_ids"] = json.loads(r["resolved_ids"])
            out.append(d)
        return out
    finally:
        conn.close()


def mark_read(msg_ids, reader_id):
    conn = get_db()
    try:
        ts = now_iso()
        for mid in msg_ids:
            conn.execute(
                "INSERT OR REPLACE INTO reads (message_id, reader_id, read_at) VALUES (?, ?, ?)",
                (mid, reader_id, ts)
            )
        conn.commit()
        return {"ok": True, "count": len(msg_ids)}
    finally:
        conn.close()


def archive_message(msg_id):
    conn = get_db()
    try:
        conn.execute("UPDATE messages SET archived = 1, archived_at = ? WHERE id = ?", (now_iso(), msg_id))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def unarchive_message(msg_id):
    conn = get_db()
    try:
        conn.execute("UPDATE messages SET archived = 0, archived_at = NULL WHERE id = ?", (msg_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def delete_message(msg_id):
    conn = get_db()
    try:
        conn.execute("DELETE FROM reads WHERE message_id = ?", (msg_id,))
        conn.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# --- Channel events ------------------------------------------------------

def _emit_event(conn, target_id, event_type, payload):
    """Record an event for a target instance. Caller must commit()."""
    conn.execute(
        "INSERT INTO events (id, target_id, type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
        (new_id(), target_id, event_type, json.dumps(payload), now_iso())
    )


def get_events(instance_id, since=None, mark_delivered=True):
    """Return undelivered events for an instance. If since is given, return events created after that ISO timestamp."""
    conn = get_db()
    try:
        if since:
            rows = conn.execute(
                "SELECT * FROM events WHERE target_id = ? AND created_at > ? ORDER BY created_at",
                (instance_id, since)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM events WHERE target_id = ? AND delivered_at IS NULL ORDER BY created_at",
                (instance_id,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(r["payload"])
            out.append(d)
        if mark_delivered and out:
            ts = now_iso()
            ids = [r["id"] for r in out]
            placeholders = ",".join("?" * len(ids))
            conn.execute(f"UPDATE events SET delivered_at = ? WHERE id IN ({placeholders})", [ts, *ids])
            conn.commit()
        return out
    finally:
        conn.close()


# --- Discovery -----------------------------------------------------------

def get_recipients():
    """Full snapshot for /recipients endpoint."""
    conn = get_db()
    try:
        ts = now_ts()
        instances_by_status = {"active": [], "stale": [], "offline": []}
        for r in conn.execute("SELECT * FROM instances ORDER BY status, display_name"):
            try:
                last_seen = datetime.fromisoformat(r["last_seen_at"]).timestamp()
                last_seen_ago = round(ts - last_seen, 1)
            except Exception:
                last_seen_ago = None
            entry = {
                "id": r["id"],
                "display_name": r["display_name"],
                "kind": r["kind"],
                "team_id": r["team_id"],
                "role": r["role"],
                "last_seen_ago": last_seen_ago,
            }
            instances_by_status[r["status"]].append(entry)

        groups_out = []
        for g in conn.execute("SELECT * FROM groups WHERE dissolved_at IS NULL ORDER BY name"):
            counts = conn.execute(
                "SELECT COUNT(*) c, SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) a FROM instances WHERE team_id = ?",
                (g["id"],)
            ).fetchone()
            groups_out.append({
                "id": g["id"],
                "name": g["name"],
                "owner_id": g["owner_id"],
                "members": counts["c"] or 0,
                "active_members": counts["a"] or 0,
            })

        return {"instances": instances_by_status, "groups": groups_out}
    finally:
        conn.close()


def get_presence():
    """Lightweight presence-only view."""
    snap = get_recipients()
    return snap["instances"]


# --- Cleanup loop --------------------------------------------------------

def cleanup_loop():
    while True:
        try:
            conn = get_db()
            ts = now_ts()
            for r in conn.execute("SELECT id, status, last_seen_at FROM instances WHERE status != 'offline'"):
                try:
                    last_seen = datetime.fromisoformat(r["last_seen_at"]).timestamp()
                except Exception:
                    continue
                age = ts - last_seen
                new_status = None
                if r["status"] == "active" and age > ACTIVE_TIMEOUT_S:
                    new_status = "stale"
                elif r["status"] == "stale" and age > STALE_TIMEOUT_S:
                    new_status = "offline"
                if new_status:
                    conn.execute("UPDATE instances SET status = ? WHERE id = ?", (new_status, r["id"]))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[cleanup] error: {e}")
        time.sleep(CLEANUP_INTERVAL_S)


# --- MCP tool descriptors (served at /mcp/tools) -------------------------

MCP_TOOLS = [
    {"name": "synapse_register", "description": "Register this session with Synapse v2. Returns stable id + display_name.",
     "inputSchema": {"type": "object", "properties": {"kind": {"type": "string", "enum": ["claude", "admin"]}, "preferred_id": {"type": "string"}, "preferred_name": {"type": "string"}}, "required": ["kind"]}},
    {"name": "synapse_send", "description": "Send a message. `to` may be: 'claude' (admin only, global), 'claude-{team}' (team broadcast), 'claude-{team}-{role}' or 'claude-{role}' (DM by display name), 'admin' (admins), or any specific display name.",
     "inputSchema": {"type": "object", "properties": {"from": {"type": "string"}, "to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}}, "required": ["from", "to", "subject", "body"]}},
    {"name": "synapse_inbox", "description": "Your inbox (keyed by stable instance id).",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}, "unread_only": {"type": "boolean"}}, "required": ["id"]}},
    {"name": "synapse_recipients", "description": "Snapshot of all instances and groups on the network.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "synapse_set_role", "description": "Claim or change your role. Display name updates to claude-{role} (or claude-{team}-{role}) with auto -2 suffix on collision.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}, "role": {"type": "string"}}, "required": ["id", "role"]}},
    {"name": "synapse_create_team", "description": "Create a team. Caller becomes owner.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "owner_id": {"type": "string"}}, "required": ["name", "owner_id"]}},
    {"name": "synapse_invite", "description": "Invite a Claude to your team. Owner only (or admin).",
     "inputSchema": {"type": "object", "properties": {"group_id": {"type": "string"}, "invitee_id": {"type": "string"}, "role": {"type": "string"}, "by_id": {"type": "string"}}, "required": ["group_id", "invitee_id", "role", "by_id"]}},
    {"name": "synapse_accept_invite", "description": "Accept a pending invite.",
     "inputSchema": {"type": "object", "properties": {"invite_id": {"type": "string"}, "by_id": {"type": "string"}}, "required": ["invite_id", "by_id"]}},
    {"name": "synapse_leave_team", "description": "Leave your current team.",
     "inputSchema": {"type": "object", "properties": {"group_id": {"type": "string"}, "member_id": {"type": "string"}}, "required": ["group_id", "member_id"]}},
]


# --- HTTP handler --------------------------------------------------------

def _load_ui():
    ui_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_v2.html")
    if os.path.exists(ui_path):
        with open(ui_path, "r", encoding="utf-8") as f:
            return f.read()
    return f"<html><body><h1>{INSTANCE_NAME}</h1><p>Synapse v2 server. UI placeholder — ui_v2.html not yet present.</p></body></html>"


class V2Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode())

    def _html(self, html):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/":
            return self._html(_load_ui())
        if path == "/ping":
            return self._json({"status": "ok", "service": INSTANCE_NAME, "version": "2"})
        if path == "/recipients":
            return self._json(get_recipients())
        if path == "/presence":
            return self._json(get_presence())
        if path == "/groups":
            return self._json(list_groups())
        if path.startswith("/groups/"):
            return self._json(get_group(path.split("/groups/", 1)[1]))
        if path.startswith("/inbox/"):
            iid = path.split("/inbox/", 1)[1]
            unread = "unread" in qs
            return self._json(get_inbox(iid, unread_only=unread))
        if path.startswith("/sent/"):
            return self._json(get_sent(path.split("/sent/", 1)[1]))
        if path == "/messages":
            limit = int(qs.get("limit", ["100"])[0])
            offset = int(qs.get("offset", ["0"])[0])
            return self._json(get_all_messages(limit=limit, offset=offset))
        if path.startswith("/events/"):
            iid = path.split("/events/", 1)[1]
            since = qs.get("since", [None])[0]
            mark = qs.get("mark", ["1"])[0] != "0"
            return self._json(get_events(iid, since=since, mark_delivered=mark))
        if path == "/mcp/tools":
            return self._json({"tools": MCP_TOOLS})
        return self._json({"error": "not_found"}, 404)

    def do_POST(self):
        body = self._body()
        path = self.path

        if path == "/register":
            return self._json(register(body.get("kind"), preferred_id=body.get("preferred_id"), preferred_name=body.get("preferred_name")))
        if path == "/heartbeat":
            return self._json(heartbeat(body.get("id")))
        if path == "/unregister":
            return self._json(unregister(body.get("id")))
        if path == "/forget":
            return self._json(forget(body.get("id"), body.get("by_id")))
        if path == "/identity/set-role":
            return self._json(set_role(body.get("id"), body.get("role")))
        if path == "/identity/release-role":
            return self._json(release_role(body.get("id")))
        if path == "/identity/set-team":
            return self._json(set_team(body.get("id"), body.get("team_id"), body.get("role"), body.get("by_id")))
        if path == "/groups":
            return self._json(create_group(body.get("name"), body.get("owner_id")))
        if path.startswith("/groups/") and path.endswith("/transfer-owner"):
            gid = path[len("/groups/"):-len("/transfer-owner")]
            return self._json(transfer_owner(gid, body.get("new_owner_id"), body.get("by_id")))
        if path.startswith("/groups/") and path.endswith("/invite"):
            gid = path[len("/groups/"):-len("/invite")]
            return self._json(invite(gid, body.get("invitee_id"), body.get("role"), body.get("by_id")))
        if path.startswith("/groups/") and path.endswith("/leave"):
            gid = path[len("/groups/"):-len("/leave")]
            return self._json(leave_group(gid, body.get("member_id"), body.get("member_id")))
        if path.startswith("/groups/") and path.endswith("/kick"):
            gid = path[len("/groups/"):-len("/kick")]
            return self._json(kick_member(gid, body.get("member_id"), body.get("by_id")))
        if path.startswith("/invites/") and path.endswith("/accept"):
            iid = path[len("/invites/"):-len("/accept")]
            return self._json(accept_invite(iid, body.get("by_id")))
        if path.startswith("/invites/") and path.endswith("/decline"):
            iid = path[len("/invites/"):-len("/decline")]
            return self._json(decline_invite(iid, body.get("by_id")))
        if path.startswith("/invites/") and path.endswith("/revoke"):
            iid = path[len("/invites/"):-len("/revoke")]
            return self._json(revoke_invite(iid, body.get("by_id")))
        if path == "/send":
            return self._json(send_message(body.get("from"), body.get("to"), body.get("subject", ""), body.get("body", "")))
        if path == "/mark-read":
            return self._json(mark_read(body.get("ids", []), body.get("reader_id")))
        if path.startswith("/archive/"):
            return self._json(archive_message(path.split("/archive/", 1)[1]))
        if path.startswith("/unarchive/"):
            return self._json(unarchive_message(path.split("/unarchive/", 1)[1]))
        if path.startswith("/delete/"):
            return self._json(delete_message(path.split("/delete/", 1)[1]))
        if path == "/mcp/call":
            return self._json(_dispatch_mcp(body.get("tool"), body.get("arguments", {})))
        return self._json({"error": "not_found"}, 404)

    def do_DELETE(self):
        path = self.path
        body = self._body()
        if path.startswith("/groups/"):
            gid = path[len("/groups/"):]
            return self._json(dissolve_group(gid, body.get("by_id")))
        return self._json({"error": "not_found"}, 404)


def _dispatch_mcp(name, args):
    if name == "synapse_register":
        return register(args.get("kind"), preferred_id=args.get("preferred_id"), preferred_name=args.get("preferred_name"))
    if name == "synapse_send":
        return send_message(args.get("from"), args.get("to"), args.get("subject", ""), args.get("body", ""))
    if name == "synapse_inbox":
        return get_inbox(args.get("id"), unread_only=args.get("unread_only", False))
    if name == "synapse_recipients":
        return get_recipients()
    if name == "synapse_set_role":
        return set_role(args.get("id"), args.get("role"))
    if name == "synapse_create_team":
        return create_group(args.get("name"), args.get("owner_id"))
    if name == "synapse_invite":
        return invite(args.get("group_id"), args.get("invitee_id"), args.get("role"), args.get("by_id"))
    if name == "synapse_accept_invite":
        return accept_invite(args.get("invite_id"), args.get("by_id"))
    if name == "synapse_leave_team":
        return leave_group(args.get("group_id"), args.get("member_id"), args.get("member_id"))
    return {"error": f"unknown_tool: {name}"}


# --- Main ----------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    threading.Thread(target=cleanup_loop, daemon=True).start()
    server = HTTPServer(("0.0.0.0", PORT), V2Handler)
    print(f"{INSTANCE_NAME} running on http://0.0.0.0:{PORT} (db={DB_PATH})")
    server.serve_forever()
