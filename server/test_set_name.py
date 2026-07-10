"""Tests for set_name (session-name sync + stale reclaim + identity takeover).

Feature 2026-07-10: a Claude session's name (customTitle) syncs to the Synapse
display name as claude-{slug}. session_id is the proof of continuity that makes
named identities durable across process restarts.

Run: python test_set_name.py
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["SYNAPSE_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "synapse_test.db")

import synapse_v2 as s  # noqa: E402

s.init_db()
s.init_db()  # migration must be idempotent

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("ok   " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))


def set_status(iid, status):
    conn = s.get_db()
    conn.execute("UPDATE instances SET status = ? WHERE id = ?", (status, iid))
    conn.commit()
    conn.close()


def backdate(iid, seconds):
    ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    conn = s.get_db()
    conn.execute("UPDATE instances SET last_seen_at = ? WHERE id = ?", (ts, iid))
    conn.commit()
    conn.close()


def row(iid):
    conn = s.get_db()
    r = conn.execute("SELECT * FROM instances WHERE id = ?", (iid,)).fetchone()
    conn.close()
    return dict(r) if r else None


def undelivered_events(iid):
    return s.get_events(iid, mark_delivered=False)


m = s.register("admin", preferred_name="web")

# --- 1. fresh claim --------------------------------------------------------
a = s.register("claude")
r = s.set_name(a["id"], "housekeeping", session_id="sess-A")
check("fresh claim -> claude-housekeeping", r.get("display_name") == "claude-housekeeping", str(r))
check("fresh claim not adopted", r.get("adopted") is False, str(r))
check("session_id persisted", row(a["id"])["session_id"] == "sess-A", str(row(a["id"])))

# --- 2. idempotent re-claim (no rename event churn) -------------------------
before_events = len(undelivered_events(a["id"]))
r = s.set_name(a["id"], "housekeeping", session_id="sess-A")
check("re-claim keeps name", r.get("display_name") == "claude-housekeeping", str(r))
check("re-claim emits no identity_changed", len(undelivered_events(a["id"])) == before_events)

# --- 3. live collision -> suffix --------------------------------------------
b = s.register("claude")
r = s.set_name(b["id"], "housekeeping", session_id="sess-B")
check("live collision suffixed", r.get("display_name") == "claude-housekeeping-2", str(r))

# --- 4. name-only reclaim from a stale holder (different session) ----------
set_status(a["id"], "stale")
backdate(a["id"], 300)
s.send_message(m["id"], "claude-housekeeping", "pre-reclaim", "for the old holder")
c = s.register("claude")
r = s.set_name(c["id"], "housekeeping", session_id="sess-C")
check("stale holder reclaimed", r.get("display_name") == "claude-housekeeping", str(r))
check("reclaim is not adoption", r.get("adopted") is False, str(r))
a_row = row(a["id"])
check("old holder renamed to claude-N", a_row["display_name"].startswith("claude-") and
      a_row["display_name"].split("-")[-1].isdigit(), str(a_row))
check("old holder keeps identity+inbox", any(msg["subject"] == "pre-reclaim" for msg in s.get_inbox(a["id"])))
evs = [e for e in undelivered_events(a["id"]) if e["type"] == "identity_changed"]
check("old holder notified of demotion", any(
    e["payload"].get("reason") == "name_reclaimed_by_session" for e in evs), str(evs))

# --- 5. bare-name routing follows the reclaim -------------------------------
s.send_message(m["id"], "claude-housekeeping", "post-reclaim", "for the new holder")
check("bare name routes to new holder", any(msg["subject"] == "post-reclaim" for msg in s.get_inbox(c["id"])))
check("old holder did NOT get post-reclaim msg",
      not any(msg["subject"] == "post-reclaim" for msg in s.get_inbox(a["id"])))

# --- 6. identity takeover: same session resumes ------------------------------
s.send_message(m["id"], "claude-housekeeping", "while-offline", "delivered on resume")
set_status(c["id"], "offline")
backdate(c["id"], 300)
d = s.register("claude")  # the resumed session's fresh process
d_fresh_id = d["id"]
r = s.set_name(d["id"], "housekeeping", session_id="sess-C")
check("takeover adopts old UUID", r.get("id") == c["id"], str(r))
check("takeover flagged", r.get("adopted") is True, str(r))
check("takeover keeps the name", r.get("display_name") == "claude-housekeeping", str(r))
check("fresh row deleted", row(d_fresh_id) is None)
check("adopted row active again", row(c["id"])["status"] == "active")
check("offline-era DM waiting in adopted inbox",
      any(msg["subject"] == "while-offline" for msg in s.get_inbox(c["id"])))

# --- 7. takeover never fires against a LIVE row of the same session ---------
e2 = s.register("claude")
r = s.set_name(e2["id"], "housekeeping", session_id="sess-C")  # sess-C holder is live again
check("live same-session row not adopted", r.get("adopted") is False and r.get("id") == e2["id"], str(r))
check("live holder keeps name; newcomer suffixed", r.get("display_name") == "claude-housekeeping-3", str(r))

# --- 8. team/role precedence -------------------------------------------------
f = s.register("claude")
s.set_role(f["id"], "eval")
r = s.set_name(f["id"], "nightwatch", session_id="sess-F")
check("role holder refuses session name", r.get("error") == "team_role_name_precedence", str(r))
check("role name untouched", row(f["id"])["display_name"] == "claude-eval", str(row(f["id"])))

# --- 9. takeover of a seated row returns the seat, keeps team name ----------
g = s.register("claude")
grp = s.create_group("anvil", g["id"])
s.set_name(g["id"], "builder", session_id="sess-G")  # named while teamless? g owns team but no seat
# put g in the team properly via set_team (m action), then kill it
s.set_team(g["id"], grp["id"], "core", m["id"])
set_status(g["id"], "offline")
backdate(g["id"], 300)
h = s.register("claude")
r = s.set_name(h["id"], "builder", session_id="sess-G")
check("seated takeover returns adopted id", r.get("id") == g["id"], str(r))
check("seated takeover keeps team name", r.get("display_name") == "claude-anvil-core", str(r))
check("seated takeover flags precedence", r.get("error") == "team_role_name_precedence", str(r))

# --- 10. an 'active'-status corpse (sweep lag) is still reclaimable ----------
i2 = s.register("claude")
s.set_name(i2["id"], "corpse", session_id="sess-I")
backdate(i2["id"], 120)  # status still 'active', but 120s silent
j2 = s.register("claude")
r = s.set_name(j2["id"], "corpse", session_id="sess-J")
check("sweep-lagged corpse reclaimed", r.get("display_name") == "claude-corpse", str(r))

# --- 11. validation + kind guard ---------------------------------------------
r = s.set_name(j2["id"], "Bad Name!")
check("invalid name rejected", "error" in r and "invalid" in r["error"], str(r))
r = s.set_name(m["id"], "sneaky-m")
check("admin kind rejected", r.get("error") == "only_claude_can_set_names", str(r))
r = s.set_name("no-such-id", "ghost")
check("unknown instance rejected", r.get("error") == "unknown_instance", str(r))

# --- 12. recipients exposes session_id ---------------------------------------
snap = s.get_recipients()
all_entries = [i for b in ("active", "stale", "offline") for i in snap["instances"][b]]
check("recipients carries session_id", any(i.get("session_id") == "sess-C" for i in all_entries))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
