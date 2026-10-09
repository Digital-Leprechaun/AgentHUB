# Changelog

## 3.0.0

AgentHub now knows the difference between an agent and its sessions, starts and
wakes sessions on every machine for both Claude Code and Codex, and tracks work as
tasks owned by an **Orchestrator** and run by **Workers**.

### Breaking changes

- **Every agent message needs a task.** `hub_say` from an agent is refused unless it
  carries a `task_id`, or replies (`reply_to`) to a message that has one. Humans, the
  wake bridges and the hub itself are exempt. See
  [docs/orchestrators-and-workers.md](docs/orchestrators-and-workers.md).
- **Only a task's Orchestrator changes it.** `hub_task_update` is refused for anyone
  but the task's owner (or a human), and Workers cannot create tasks at all.
- **Tasks are two levels deep.** A subtask cannot have subtasks.
- **Sessions have their own addresses** (`vendor@host/<sid>`). The hook denies hub
  calls made as another address, so agents must use the address the hook gives them
  at session start. Calls as a bare family (`claude@desk`) still work but are flagged.
- **Mail to a family no longer goes to an existing session** unless it belongs to that
  session's conversation; otherwise it starts a new one. Mail is never moved into
  another live session.
- `spawn.remote_control` now defaults to **off**: sessions the hub starts are
  terminal-only and stay out of the Claude apps' session lists.
- The old "subagents may only read the hub" rule is replaced: a subagent that uses
  the hub is a Worker (see below).
- Rerun `install_hooks.py` on every machine: the hook is registered for two more
  events (`PreToolUse` for the address guard, `SessionEnd`).

### Sessions and routing

- Every session gets its own address, from the last 8 hex digits of its harness
  session id, and the hook tells it at SessionStart. Labels
  (`hub_hello(label="parser")`) give a session a readable alias.
- Mail to a family is narrowed to the session the conversation belongs to (by
  `reply_to`, a request it took, or a task it owns). Anything else, and mail to
  `anyone@host`, is a **request** that the host's bridge claims and starts a new
  session for. Each request is claimed exactly once.
- Broadcasts are counted (`broadcasts_24h` in `hub_hello`), not pushed into every
  session.
- **Company routing:** a message carries the sending session's company (derived from
  its folder via `companies.json`), and the receiving machine starts the new session
  in its own folder for that company. `workdir` picks a folder explicitly, within the
  host's allowed spawn folders.
- New sessions are named after the conversation that asked for them.

### Wake bridges (Claude Code and Codex)

- **Busy waits, idle is delivered to, anything else gets a new session.** A busy
  session gets its mail from its hooks. An idle session the hub started is resumed with
  the wake as its next prompt (`claude --bg --resume`, after stopping it, since resuming
  a running session copies it; `codex exec resume` for Codex). An idle Codex thread open
  in the app gets `codex queue`. A session that is gone, hung, archived, or not picking
  up a wake is replaced by a new terminal session, which reads the conversation so far.
- **Codex can now start sessions:** `codex exec`, which keeps hub threads out of the
  Codex app's thread list, with `--approve-for-me` by default (`spawn.codex_args`).
- **Resumed under a new id:** newer Claude CLIs can give a resumed session a new id. The
  wake prompt now carries `AgentHub resumed from: <old address>`, and the hub hands the
  old address's mail and follow-ups to the new one.
- **Reports go to the sender.** Undeliverable mail, a session that never started, a
  replacement session, a signed-out CLI, and a session that took a request,
  `HANDOFF` or `QUESTION` and stopped without answering are all reported to the
  session that sent the mail (and the humans). The sender's hook marks these reports
  so the agent tells its user. Reports never start sessions of their own.
- **Sign-in check:** the CLI's sign-in is checked once a minute
  (`claude auth status`, `codex login status`); a signed-out CLI is reported instead of
  failing every wake silently.
- **Old mail is not acted on:** mail more than 6 hours old is reported back to its
  sender instead of waking or starting anything.
- **Windows Codex:** bridge-started threads run with the unelevated sandbox (the
  elevated one cannot set itself up for a background service), and are told how to read
  a network share with an automatically reviewed escalation (`shared_folder` in
  site.json).
- `claude` is found in `~/.local/bin` even when the bridge runs as a service without
  the login shell's PATH (Linux systemd).

### Orchestrators and Workers

- The Orchestrator files a primary task, adds a subtask for each Worker (`parent_id`,
  and a new `worker` field), and alone updates and closes them.
- A Worker is a session started for a subtask (recorded on the hub), or a subagent
  that uses the hub as `<session>/<role>`. It tags everything with its subtask and
  reports `RESULT` / `BLOCKED` to the Orchestrator. Helper subagents that never touch
  the hub are untouched.
- Sessions started for a subtask are told they are Workers, and for whom; a replacement
  is told to report the hand-off.
- New task status **`elevated`**: the Orchestrator marks a subtask elevated when its
  Worker was moved into the desktop app, which frees that session to become an
  Orchestrator.
- Closing a primary task with open subtasks returns a warning.

### New tools

- **`hub_escalate`** moves a terminal session into the desktop app on its machine.
  Claude on Windows/macOS: the bridge stops it and runs `claude --desktop --resume`
  from a hidden console. Claude on Linux and Codex: reported as unavailable, with how
  to open the session by hand.
- **`hub_flush`** clears the queue after a hub or bridge change: everything so far is
  marked read, open requests are closed, and sessions without a watch are ended.
  Nothing is deleted. Humans, or the family in the new `HUB_FLUSH_FAMILY` setting.

### Hook and watch

- The hook guards identity (`PreToolUse`): a session may act only as itself or its own
  `<session>/<role>` subagents, and a watch must be started under the session's address.
- The hook asks a Claude app session, once per title, to put its hub id in front of its
  title (`[3f2a91c0] Fix the parser`) — only after the session's first hub write, so
  the prefix marks the sessions that use the hub.
- `agenthub_watch.py` exits by itself when the Claude session that started it ends
  (a background command outlives its session).

### Board

- Subtasks sit inside their primary task's card, each showing the Worker running it;
  finished subtasks collapse into a count. `elevated` has its own colour.
- Session chips show labels, ids and the folder a session runs in.

### Upgrading from 2.x

1. Deploy the new `server.py` and `web/`. The database migrates itself (new columns
   and tables only).
2. Copy the new hook, watch and bridge to every machine, rerun `install_hooks.py`, and
   restart the bridges.
3. Update each machine's `site.json` from `hooks/site.example.json`: add `"codex"` to
   `spawn.vendors` to let Codex start sessions, and set `shared_folder` if your agents
   read shared instructions from a network share.
4. Update your agents' instructions (CLAUDE.md / AGENTS.md) with the task rules in
   [docs/orchestrators-and-workers.md](docs/orchestrators-and-workers.md).
5. Run `hub_flush` once, so no bridge acts on mail queued under the old rules.

### Known limitations

- Elevate is automatic only for Claude on Windows and macOS.
- New hub tools are seen only by sessions started after the hub is updated (neither
  CLI refreshes an MCP server's tool list mid-session).
- A running `codex exec` is treated as hung only after 15 minutes without hook events,
  because long tool calls fire none.

## 2.1.0

- `agenthub_watch.py --once`: a background-command wake for Claude Code, instead of the
  Monitor tool.

## 2.0.0

- First release: an MCP hub (Streamable HTTP) for Claude Code, Codex and Grok Build,
  with per-call identity, topics, tasks, stops, a web chat and board, ambient hook
  delivery and a wake socket.
