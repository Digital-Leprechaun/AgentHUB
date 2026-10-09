#!/usr/bin/env python3
"""Installs AgentHub notifications for this machine's Claude Code and Codex.

  - copies agenthub_hook.py and agenthub_watch.py into ~/.agenthub/
  - registers agenthub_hook.py for SessionStart, UserPromptSubmit, PostToolUse, Stop,
    SessionEnd and PreToolUse (the session-address guard) in Claude Code
    (~/.claude/settings.json) and Codex ($CODEX_HOME/hooks.json). Codex runs a new or
    changed hook only after it is approved once with /hooks.

Only hook entries whose command runs agenthub_hook.py are touched: earlier ones are
replaced, everything else in those files is kept, and each file is backed up before
it changes. Safe to run again.

    python install_hooks.py --host desk [--dry-run]

Codex asks for hooks to be trusted before they run: open Codex once and approve them
with /hooks.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "PreToolUse", "SessionEnd")
# The guard only has work for hub calls and shell commands (a watch started with the
# wrong address); matching nothing else keeps it off every other tool call.
GUARD_MATCHER = "mcp__agenthub.*|Bash|PowerShell"
MARK = "agenthub_hook.py"
HERE = os.path.dirname(os.path.abspath(__file__))


def fwd(p: str) -> str:
    return p.replace("\\", "/")


_LAUNCHER: str | None = None


def python_cmd() -> str:
    """A launcher every hook shell (bash, cmd, PowerShell) runs as a bare command.

    Each candidate is actually run: on Windows `python` is often the Microsoft Store
    "app execution alias", which exists on PATH but only prints "Python was not found".
    A hook pointing at it would fail on every event."""
    global _LAUNCHER
    if _LAUNCHER:
        return _LAUNCHER
    import subprocess
    candidates = ["python", "py -3", "python3"] if os.name == "nt" else ["python3", "python"]
    for cand in candidates:
        try:
            r = subprocess.run(f"{cand} -c \"import sys; print(sys.version_info[0])\"", shell=True,
                               capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0 and r.stdout.strip() == "3":
            _LAUNCHER = cand
            return cand
    # The interpreter running this installer certainly works; quote it for spaces.
    _LAUNCHER = f'"{fwd(sys.executable)}"'
    return _LAUNCHER


def hook_command(home_dir: str, addr: str) -> str:
    script = fwd(os.path.join(home_dir, "agenthub_hook.py"))
    return f'{python_cmd()} "{script}" --as {addr}'


def load(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8-sig") as fh:
        text = fh.read().strip()
    return json.loads(text) if text else {}


def merge(doc: dict, command: str, claude: bool) -> dict:
    hooks = doc.setdefault("hooks", {})
    for event in EVENTS:
        groups = hooks.get(event) or []
        kept = []
        for g in groups:
            inner = [h for h in (g.get("hooks") or []) if MARK not in (h.get("command") or "")]
            if inner:
                kept.append(dict(g, hooks=inner))
        entry = {"type": "command", "command": command, "timeout": 10}
        if not claude:
            entry["commandWindows"] = command
        group = {"hooks": [entry]}
        if claude and event == "PostToolUse":
            group = {"matcher": "*", **group}
        if event == "PreToolUse":
            group = {"matcher": GUARD_MATCHER, **group}
        kept.append(group)
        hooks[event] = kept
    return doc


def write(path: str, doc: dict, dry: bool) -> str:
    new = json.dumps(doc, indent=2) + "\n"
    old = open(path, encoding="utf-8-sig").read() if os.path.exists(path) else None
    if old is not None and json.loads(old or "{}") == doc:
        return f"unchanged  {path}"
    if dry:
        return f"would update  {path}"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if old is not None:
        shutil.copy2(path, f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(new)
    os.replace(tmp, path)
    return f"{'updated' if old is not None else 'created'}  {path}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True, help="hub host part for this machine, e.g. desk")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    home = os.path.expanduser("~")
    agent_dir = os.path.join(home, ".agenthub")
    codex_home = os.environ.get("CODEX_HOME") or os.path.join(home, ".codex")

    for name in ("agenthub_hook.py", "agenthub_watch.py"):
        src, dst = os.path.join(HERE, name), os.path.join(agent_dir, name)
        if not os.path.exists(src):
            sys.exit(f"missing {src}")
        same = os.path.exists(dst) and open(src, "rb").read() == open(dst, "rb").read()
        if same:
            print(f"unchanged  {dst}")
        elif a.dry_run:
            print(f"would copy  {dst}")
        else:
            os.makedirs(agent_dir, exist_ok=True)
            shutil.copy2(src, dst)
            print(f"copied     {dst}")

    # The hook and watch read these when the harness's environment lacks them, which is
    # the norm for anything not started from an interactive shell.
    url, token = os.environ.get("AGENTHUB_URL", "").rstrip("/"), os.environ.get("AGENTHUB_TOKEN", "")
    creds = os.path.join(agent_dir, "credentials.json")
    if not (url and token):
        print(f"skipped    {creds} (AGENTHUB_URL / AGENTHUB_TOKEN not in this environment)")
    else:
        want = {"url": url, "token": token}
        try:
            have = json.load(open(creds, encoding="utf-8"))
        except (OSError, ValueError):
            have = None
        if have == want:
            print(f"unchanged  {creds}")
        elif a.dry_run:
            print(f"would write  {creds}")
        else:
            os.makedirs(agent_dir, exist_ok=True)
            fd = os.open(creds, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(want, fh)
            if os.name != "nt":
                os.chmod(creds, 0o600)
            print(f"wrote      {creds}")

    claude_settings = os.path.join(home, ".claude", "settings.json")
    print(write(claude_settings, merge(load(claude_settings), hook_command(agent_dir, f"claude@{a.host}"), True),
                a.dry_run))
    codex_hooks = os.path.join(codex_home, "hooks.json")
    print(write(codex_hooks, merge(load(codex_hooks), hook_command(agent_dir, f"codex@{a.host}"), False),
                a.dry_run))

    # Record the launcher that works here, so install-pointers can put the same one in
    # the watch command it writes into CLAUDE.md.
    launcher_file = os.path.join(agent_dir, "launcher.txt")
    if not a.dry_run:
        os.makedirs(agent_dir, exist_ok=True)
        with open(launcher_file, "w", encoding="utf-8") as fh:
            fh.write(python_cmd() + "\n")

    watch = fwd(os.path.join(agent_dir, "agenthub_watch.py"))
    print(f"\nwatch      {python_cmd()} \"{watch}\" --as claude@{a.host}/<session> --once"
          "\n           (each session's AgentHub hook tells it its own <session> at start)")


if __name__ == "__main__":
    main()
