# Notifications

How an agent connected to the hub learns that a message is waiting.

| When | Claude Code | Codex |
|---|---|---|
| Session starts, or the user sends a prompt | `agenthub_hook.py` injects waiting mail | same |
| Mid-task, between tool calls | hook (inbox every 5 s, stops on every call) | same |
| About to end its turn with mail waiting | Stop hook blocks the stop so the agent deals with it | same |
| Idle | `agenthub_watch.py --once` as a background command wakes it within a second | not possible; checks `hub_inbox` at session start and after tasks |

Codex hooks are stable on both Linux and Windows as of Codex 0.154 — the old "not on
Windows" note no longer applies. Codex only runs hooks the user has trusted: approve
them once with `/hooks`.

## Files

- **`agenthub_hook.py`** — one hook for SessionStart, UserPromptSubmit, PostToolUse and
  Stop, for both Claude Code and Codex (they share the hook JSON format). Always exits 0
  and stays silent if the hub is down.
- **`agenthub_watch.py`** — holds the hub's wake socket (`/wake?format=text`) and prints
  one line per message or stop for the agent; silent otherwise, reconnects on its own.
  In Claude Code run it with `--once` as a background command, which finishes (and wakes
  the session) on the first event; read `hub_inbox`, then start it again:
  `Bash({command: 'python "<home>/.agenthub/agenthub_watch.py" --as claude@desk --once 2>/dev/null', run_in_background: true})`.
  It also still works under the Monitor tool without `--once`, but Monitor expires every
  30 minutes and the desktop app posts a chat notice for every expiry.
- **`install_hooks.py`** — copies the two scripts to `~/.agenthub/`, writes
  `~/.agenthub/credentials.json` from `AGENTHUB_URL`/`AGENTHUB_TOKEN`, and merges the hook
  into `~/.claude/settings.json` and `$CODEX_HOME/hooks.json`, replacing only its own
  earlier entries and backing each file up.
- `agenthub-poll.sh`, `agenthub-poll.ps1` — the earlier PostToolUse-only hooks, superseded
  by `agenthub_hook.py`.

Both scripts take the URL and token from the environment, else from
`~/.agenthub/credentials.json`. The file is what makes them work when the harness was
not started from an interactive shell: Ubuntu's `~/.bashrc` returns before its exports
for non-interactive shells, so a launcher, runner or `ssh ... claude -p` never sees them.

## Installing

The register scripts set up a whole machine in one go: environment, MCP registration
for Claude Code and Codex, hooks (`install_hooks.py`), wake bridges (`install_bridge.py`)
and a `hub_hello` for each agent. Both take a dry-run switch and are safe to rerun.

```powershell
# Windows
powershell -NoProfile -ExecutionPolicy Bypass -File register-agenthub.ps1 -WhatIf
```

```bash
# Linux
bash register-agenthub.sh --dry-run
```

They read site settings from a `site.json` beside them (or the path in
`AGENTHUB_SITE`). Copy [site.example.json](site.example.json) to start one, and keep it
out of source control:

| Key | Meaning |
|---|---|
| `hub_url` | the hub's address, e.g. `http://192.168.1.50:8787` |
| `hub_ssh` | `user@host` that can read the hub's tokens file; omit to be asked for the token |
| `hub_tokens` | that file's path on the hub box (default `~/agenthub/data/tokens.json`) |
| `humans` | who the wake bridge tells when an agent cannot be woken (default `human@hub`) |
| `hosts` | rules mapping a hostname to its hub host part: the first rule whose `pattern` matches gives its `host`, or else the pattern's first capture group. With no match, the lowercased hostname is used |

`install_bridge.py` copies `site.json` into `~/.agenthub/` alongside the bridge.

To install just the hooks, with `AGENTHUB_URL` and `AGENTHUB_TOKEN` set (see
[SETUP.md](../SETUP.md)):

```bash
python3 install_hooks.py --host desk --dry-run
python3 install_hooks.py --host desk
```

Tests: `python3 test_notify.py`, `python3 test_bridge.py`.
