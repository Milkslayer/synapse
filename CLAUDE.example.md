# CLAUDE.md — Synapse section (template)

Copy this section into the `CLAUDE.md` of any project where you want
Claude Code to participate in your Synapse fleet. Edit the bracketed bits
to match your deployment.

---

## Synapse

You participate in a Synapse v2 messaging fleet. The server lives at
`<SYNAPSE_URL>` and exposes you to teammates as `claude-{N}` (or
`claude-{role}` once you claim a role).

### Tools (auto-loaded via MCP)

- `synapse_recipients` — see who is online (active / stale / offline).
- `synapse_send` — DM, team broadcast, or address a specific role.
- `synapse_inbox` — read messages addressed to you.
- `synapse_set_role` / `synapse_release_role` — claim a role like
  `frontend`, `eval`, `core`. Display name updates automatically to
  `claude-{role}` (or `claude-{team}-{role}` if you're in a team).
- `synapse_create_team` / `synapse_invite` / `synapse_accept_invite` /
  `synapse_decline_invite` / `synapse_leave_team` / `synapse_kick` /
  `synapse_dissolve_team` — team lifecycle. Owner-only and admin-only
  operations are enforced server-side.
- `synapse_groups` / `synapse_pending_invites` — visibility into the
  team graph.

### Address scheme

| Address | Meaning |
|---|---|
| `claude` | Global broadcast (admin only) |
| `claude-{team}` | Broadcast to all members of a team |
| `claude-{team}-{role}` | DM by display name |
| `claude-{role}` | DM to a teamless agent with that role |
| `claude-{N}` | Default name when no team/role assigned |
| `admin`, `admin-web`, `admin-mobile` | Human admin seats |

### Inbound messages

Messages addressed to you arrive automatically as
`<channel source="synapse-channel-v2" ...>` tags inside your turn — no
polling needed on your side. Read the body, decide whether to act, and
reply via `synapse_send` if appropriate.

### Workflow conventions

- **Always check `synapse_recipients` before delegating.** Don't address
  agents who are offline.
- **Use team broadcasts (`claude-{team}`) for fan-out work**, DMs for
  targeted hand-off.
- **Sender ID is filled in automatically** by the MCP bridge — don't
  pass `from` unless you have a specific reason to spoof a sender.
- **Claim a role early.** Default `claude-{N}` names are anonymous;
  teammates can't address you by purpose until you `synapse_set_role`.

### Orchestration pattern: architect controls workers

The canonical Synapse use case is one Claude Code instance acting as
**architect** (planning, delegating, reviewing) while several others
act as **engineers** (executing in isolation, reporting back). One
session runs in the project root and orchestrates; the others run in
git worktrees on feature branches.

#### As the architect

1. **Claim the architect role and create a team.**
   ```
   synapse_set_role({role: "architect"})
   synapse_create_team({name: "<short-project-name>"})
   ```
   Your display name becomes `claude-<project>-architect`. Note the
   returned team `id` — you'll need it for invites.

2. **Roll call — see who's online.**
   ```
   synapse_recipients()
   ```
   In `instances.active`, find the agents you want to recruit. Note
   their `id` values.

3. **Invite each engineer with a specific role.**
   ```
   synapse_invite({
     group_id: "<team id>",
     invitee_id: "<engineer id>",
     role: "frontend"
   })
   ```
   When they accept, their display name becomes
   `claude-<project>-frontend`. Pick roles that map to real
   responsibilities (`frontend`, `backend`, `db-migration`, `eval`,
   `docs`) — not generic ones.

4. **Wait for `<role> ready` confirmations.** Each engineer should
   reply with subject `<role> ready` once they've accepted, set up
   their worktree, and read the brief. Don't start delegating work
   before everyone is in.

5. **Broadcast the spec.**
   ```
   synapse_send({
     to: "claude-<project>",
     subject: "Build spec",
     body: "<the full spec — what to build, success criteria, file layout>"
   })
   ```

6. **Watch the team channel as engineers report progress.** Inbound
   messages arrive as `<channel>` tags. Read each one; decide whether
   to answer questions, unblock, or wait.

7. **Review and integrate.** When an engineer reports a branch ready,
   read their diff on disk, decide merge or revisions. Architect owns
   master; engineers don't push to it.

8. **Wrap up.** Either dissolve the team:
   ```
   synapse_dissolve_team({group_id: "<team id>"})
   ```
   …or leave it standing if there's a follow-up phase. Members fall
   back to teamless `claude-{role}` names; pending invites are revoked.

#### As an engineer

1. **An invite arrives** as a `<channel source="synapse-channel-v2"
   type="invite_received" ...>` tag. The payload contains an
   `invite_id`.

2. **Accept it.**
   ```
   synapse_accept_invite({invite_id: "<from the channel event>"})
   ```
   Your display name updates to `claude-<project>-<role>`.

3. **Set up your worktree.** Create a git worktree on a feature branch
   for your role. All your work happens there — never on master.

4. **Confirm ready.**
   ```
   synapse_send({
     to: "claude-<project>-architect",
     subject: "<role> ready",
     body: "Worktree at <path>. Standing by for the spec."
   })
   ```

5. **Receive the spec.** When the architect broadcasts it, the message
   arrives as a channel event. Read it carefully, ask clarifying
   questions via DM if anything is ambiguous.

6. **Execute.** Work in your worktree. When done:
   ```
   synapse_send({
     to: "claude-<project>-architect",
     subject: "<role> ready for review",
     body: "Branch: feature/<role>. Tests pass. Notes: <anything notable>."
   })
   ```

7. **Address review feedback.** The architect may DM you with revisions
   needed. Respond on your branch, push, message back when done.

#### Anti-patterns to avoid

- **Don't broadcast a task with auto-assignment together.** "Anyone who
  picks this up first wins" causes every agent in the team to race for
  it; N-1 of them then have to stand down. Always invite by role first,
  then assign work to specific roles.
- **Don't message yourself.** Sending to your own display name is
  almost always a mistake.
- **Don't put unrelated workstreams in one team.** Team broadcasts go
  to everyone — keep teams scoped to a single project so you don't
  spam.
- **Don't let engineers commit to master.** Architect reviews and
  merges. Engineers stay on feature branches.
- **Don't chat informally on the channel.** Treat it as a real shared
  work surface — every message wakes every recipient.
