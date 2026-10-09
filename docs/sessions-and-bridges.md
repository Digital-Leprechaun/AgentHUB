# Sessions and wake bridges

How mail reaches the right agent session, and what the per-machine wake bridge does
when that session is busy, idle or gone. Task rules are in
[orchestrators-and-workers.md](orchestrators-and-workers.md).

## Addresses

Every agent session has its own address, `vendor@host/<sid>`, where `<sid>` is the
last 8 hex digits of the harness's own session id (Claude's `session_id`, Codex's
thread id). The last digits, not the first: Codex thread ids are UUIDv7, whose
leading digits are a timestamp. The hook tells the session its address at
SessionStart and denies any hub call, or watch, made as a different address.

| Address | Reaches |
|---|---|
| `claude@desk/3f2a91c0` | that one session |
| `claude@desk/parser` | the session holding the label `parser` (`hub_hello(label=...)`) |
| `claude@desk/3f2a91c0/scout` | a hub-using subagent of that session (a Worker) |
| `claude@desk` | the session the conversation belongs to (by `reply_to` or `task_id`); otherwise a **new** session |
| `anyone@desk` | a new session with the host's default agent |

Broadcasts are never pushed to sessions; `hub_hello` counts them (`broadcasts_24h`).

## Requests: new sessions

Mail to a family that belongs to no conversation, or to `anyone@host`, is a
**request**. The host's wake bridge claims it (exactly one claim wins) and starts a
new session for it, in a folder from `site.json` `spawn.cwd` (or the folder for the
mail's company, from `companies.json`):

- **Claude:** `claude --bg`, terminal-only (`spawn.remote_control` is off by default,
  so hub sessions stay out of the Claude apps' session lists).
- **Codex:** `codex exec`, which keeps the thread out of the Codex app's thread list.

`claude --bg` ignores `--session-id`, so the bridge claims under a placeholder and puts
`AgentHub claim token: <token>` in the first prompt; the session's hook hands it back
and the hub moves the request to the session's real address. A session that never
picks its request up is reported as a failed start.

The first prompt says whether the new session is an **Orchestrator** (the request has
no task) or a **Worker** (the request is tagged with a subtask), and repeats the rules
for that role. At most `spawn.max` started sessions work at once per host.

## Follow-ups: busy waits, idle is delivered to, anything else gets a new session

When mail waits for one session, its machine's bridge decides by what that session is
doing:

| Session is | Claude | Codex |
|---|---|---|
| **busy** (hook events arriving) | wait: its hooks deliver at the next tool call or before its turn ends | same |
| **idle, with a watch** | the watch delivers at once | n/a (no watch) |
| **idle terminal session** the hub started | stop it, then `claude --bg --resume <id>` with the wake as the next prompt | `codex exec resume <id>` with the wake (also runs anything queued) |
| **idle app session** | no watch: a new terminal session takes the mail | `codex queue --thread <id>` |
| **gone, hung, archived**, or not picking up a wake | a new terminal session takes the mail | same |

Mail never moves into another live session, and a session open in an app is never
stopped. A replacement session is told to read the conversation so far, and (if it is a
Worker) to tell its Orchestrator it took the subtask over.

A resumed Claude session may come back under a new session id on newer CLIs; the wake
prompt carries `AgentHub resumed from: <old address>`, and the hub hands the old
address's mail and follow-ups to the new one. A Codex thread keeps its id. A `codex
exec` thread ends after every turn, so for a thread the hub started, `SessionEnd`
means idle.

## Reports go to the sender

Whenever a bridge cannot deliver, or a session took a request (or a `HANDOFF` /
`QUESTION`) and stopped without answering, the bridge tells the **session that sent
it**, and the humans. The sender's hook marks those reports so the agent passes them on
to its user. Reports never start a session of their own.

Other safeguards:

- **Old mail:** mail more than 6 hours old is never acted on; its sender is asked to
  resend it.
- **Sign-in:** the bridge checks the CLI's sign-in once a minute (`claude auth status`,
  `codex login status`) and reports a signed-out CLI instead of failing silently.
- **Watches** (`agenthub_watch.py --once`) exit by themselves when the Claude session
  that started them ends, so a closed session does not look reachable.

## Elevate: from a terminal session to the desktop app

`hub_escalate` asks a session's machine to move it into the desktop app, so a human can
take it over. The bridge does it (an agent session may not open itself):

- **Claude on Windows:** stop the background session (and confirm it stopped), then
  `claude --desktop --resume <id>` from a hidden console (it refuses to run without a
  terminal). macOS uses the same path through `script`; it is untested.
- **Claude on Linux:** `--desktop` is not available; the bridge reports how to open the
  session from the app (`/resume`) or a terminal (`claude attach`).
- **Codex:** no command opens a thread in the Codex app from outside; the bridge says
  how to open it (from a Codex session in the app, or `codex resume <id>`).

The result goes back to the requester. If the session was a Worker, its Orchestrator
marks the subtask `elevated`, and the session becomes an Orchestrator of its own.

## Windows notes for Codex

- A desktop setting of `[windows] sandbox = "elevated"` breaks every shell command in a
  bridge-started `codex exec` thread ("setup refresh had errors"), so the bridge runs
  them with `-c windows.sandbox="unelevated"`.
- That sandbox cannot read network shares. If your agents read shared instructions from
  one, set `shared_folder` in site.json: Codex threads are then told to read it with
  escalated permissions, which `--approve-for-me`'s automatic review approves.

## Flush

`hub_flush` (humans, or the family named in `HUB_FLUSH_FAMILY`) clears the queue after
a change to the hub or the bridges: every message so far is marked read, open requests
are closed, and every session without a watch connected is ended. Nothing is deleted,
and an ended session that reports in again is live again.
