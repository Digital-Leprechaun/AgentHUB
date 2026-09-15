#!/usr/bin/env python3
"""AgentHub notification hook for Claude Code and Codex.

One script, registered for four hook events, so an agent learns about waiting
messages at every point its harness gives us control:

  SessionStart      anything that arrived while no session was running
  UserPromptSubmit  mail that arrived while the agent sat waiting for the user
  PostToolUse       mail that arrives mid-task, between tool calls
  Stop              the agent is about to end its turn with mail waiting: block the
                    stop so it deals with the mail first

A STOP in force is reported on every event, with no rate limit.

Both harnesses feed the same JSON on stdin and accept the same JSON on stdout, so
this works unchanged for either. Standard library only. It always exits 0 and
stays silent on any failure: a hub that is down must never break a session.

    python agenthub_hook.py --as claude@desk
    env: AGENTHUB_URL, AGENTHUB_TOKEN; AGENTHUB_EVERY (seconds between mid-task
    inbox checks, default 5)

The URL and token come from the environment, or else from ~/.agenthub/credentials.json
(written by install_hooks.py). The file matters: a harness started outside an
interactive shell -- a desktop launcher, a runner, ssh -- never sees exports that
live in ~/.bashrc.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.parse
import urllib.request

TIMEOUT = 3.0


def poll(url: str, token: str, addr: str, stop_only: bool, event: str = "", session: str = "") -> str:
    # event + session let the hub know which session this agent runs and whether it is
    # busy -- the Codex wake bridge needs both.
    q = {"as": addr}
    if event:
        q["event"] = event
    if session:
        q["session"] = session
    if stop_only:
        q["stop_only"] = "1"
    req = urllib.request.Request(f"{url}/hook/poll?{urllib.parse.urlencode(q)}")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace").strip()


def due(addr: str, every: float) -> bool:
    """Rate-limit mid-task inbox checks per agent; stops are never rate-limited."""
    if every <= 0:
        return True
    safe = "".join(ch if ch.isalnum() else "_" for ch in addr)
    stamp = os.path.join(tempfile.gettempdir(), f".agenthub-hook-{safe}")
    now = time.time()
    try:
        if now - os.path.getmtime(stamp) < every:
            return False
    except OSError:
        pass
    try:
        with open(stamp, "w", encoding="ascii") as fh:
            fh.write(str(int(now)))
    except OSError:
        pass
    return True


def credentials() -> tuple[str, str]:
    url = os.environ.get("AGENTHUB_URL", "")
    token = os.environ.get("AGENTHUB_TOKEN", "")
    if not (url and token):
        try:
            path = os.path.join(os.path.expanduser("~"), ".agenthub", "credentials.json")
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)
            url = url or saved.get("url", "")
            token = token or saved.get("token", "")
        except (OSError, ValueError):
            pass
    return url.rstrip("/"), token


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as", dest="addr", default=os.environ.get("AGENTHUB_AS", ""))
    ap.add_argument("--every", type=float, default=float(os.environ.get("AGENTHUB_EVERY", "5")))
    a = ap.parse_args()

    url, token = credentials()
    if not (a.addr and url and token):
        return

    try:
        event = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        event = {}
    name = event.get("hook_event_name") or ""

    # Codex exposes the exact thread id that `codex queue --thread` needs; Claude Code
    # only has session_id in the hook payload.
    session = os.environ.get("CODEX_THREAD_ID") or event.get("session_id") or ""

    stop_only = name == "PostToolUse" and not due(a.addr, a.every)
    try:
        text = poll(url, token, a.addr, stop_only, name, session)
    except Exception:  # noqa: BLE001
        return
    if not text:
        return

    if name == "Stop":
        # Blocking the stop makes the agent continue with `reason` as its next
        # instruction. The poll above marked the mail read, so this cannot loop on
        # the same messages.
        emit({
            "decision": "block",
            "reason": text + "\n\nThese AgentHub messages arrived while you were working. "
                             "Handle them before ending your turn: reply with hub_say if "
                             "they concern you, otherwise carry on and finish.",
        })
        return

    if name in ("SessionStart", "UserPromptSubmit", "PostToolUse"):
        emit({"hookSpecificOutput": {"hookEventName": name, "additionalContext": text}})


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        pass
    sys.exit(0)
