#!/usr/bin/env python3
"""AgentHub notification hook for Claude Code and Codex.

One script, registered for these hook events, so an agent learns about waiting
messages at every point its harness gives us control:

  SessionStart      tells the session its own AgentHub address, plus anything that
                    arrived while no session was running (also fires after compaction)
  UserPromptSubmit  mail that arrived while the agent sat waiting for the user
  PostToolUse       mail that arrives mid-task, between tool calls
  Stop              the agent is about to end its turn with mail waiting: block the
                    stop so it deals with the mail first
  PreToolUse        the guard: a hub call (or an agenthub_watch.py command) made as
                    any address other than this session's own (or, for a subagent,
                    <session>/<role>) is denied, and so are task changes from inside a
                    subagent. Local only, no network.
  SessionEnd        marks the session ended on the hub

Every SESSION has its own address: the family from --as plus the last 8 hex digits
of the harness's session id, e.g. claude@desk/3f2a91c0. Mail to it reaches only that
session, so several sessions of one agent never answer the same message.

Subagents (Claude Code marks their hook events with agent_id): a helper that never
uses the hub needs nothing. One that does is a Worker for its parent session (the
Orchestrator): it talks on the hub as <session>/<role>, every message tagged with a
task, and never changes tasks. A subagent's events deliver STOPs but never mail; it
reads its own with hub_inbox.

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
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request

TIMEOUT = 3.0


def sid_of(session_id: str) -> str:
    """Last 8 hex digits of the session id. (Codex ids are UUIDv7: their leading
    digits are a timestamp, shared by sessions started close together.) Must match
    server.py's sid_of."""
    h = re.sub(r"[^0-9a-f]", "", (session_id or "").lower())
    return h[-8:] if len(h) >= 8 else ""


def session_address(family: str, session_id: str) -> str:
    sid = sid_of(session_id)
    return f"{family}/{sid}" if sid else family


# The wake bridge puts this in a session it starts, so the session can take over the
# request the bridge claimed for it (see server.py Store.adopt).
CLAIM_TOKEN = re.compile(r"AgentHub claim token: ([0-9a-fA-F-]{32,40})")
# The wake bridge puts this in the prompt that resumes a session: newer CLIs give the
# resumed conversation a new session id, so the hub hands the old address's mail on.
RESUMED_FROM = re.compile(r"AgentHub resumed from: ([A-Za-z0-9._-]+@[A-Za-z0-9._-]+/[0-9a-f]{8})")

WATCH_AS = re.compile(r"agenthub_watch\.py\S*\s.*?--as[=\s]+[\"']?([^\s\"']+)")


# Hub tools a subagent may still use: they only read.
READ_ONLY_TOOLS = {"hub_peek", "hub_search", "hub_who", "hub_tasks", "hub_topics", "hub_stops"}
# ...and, as a Worker under its own address (you/<role>), these: the hub requires every
# message to carry a task of its Orchestrator's (its parent session's).
WORKER_TOOLS = {"hub_say", "hub_inbox", "hub_hello"}


def guard(event: dict, me: str) -> str | None:
    """Why this tool call must be denied, or None. A session may act only as itself
    (or as its own subagents, me/<role>).

    Inside a Claude Code subagent (the hook payload carries agent_id): a helper that
    does not use the hub needs nothing. One that does is a Worker for its parent
    session (the Orchestrator): it may read the hub and talk on it as me/<role>, every
    message tagged with a task of the Orchestrator's, but it never manages tasks (that
    is the Orchestrator's job) and never runs a watch."""
    tool = event.get("tool_name") or ""
    inp = event.get("tool_input") or {}
    if not isinstance(inp, dict):
        return None
    if event.get("agent_id"):
        hub_tool = tool.split("__")[-1] if tool.startswith("mcp__agenthub") else ""
        cmd = inp.get("command")
        cmd = " ".join(str(c) for c in cmd) if isinstance(cmd, list) else str(cmd or "")
        if "agenthub_watch" in cmd:
            return f"A subagent does not run an AgentHub watch: its parent session ({me}) does."
        if hub_tool.startswith("hub_task_") or hub_tool in ("hub_flush", "hub_escalate"):
            return (f"You are a Worker for {me}, your Orchestrator: it creates and updates tasks. "
                    f"Tell it what you need (a task change, or another Worker).")
        if hub_tool in WORKER_TOOLS:
            claimed = str(inp.get("as") or "").strip().lower()
            if not claimed.startswith(me + "/") or claimed.split("/")[-1] == "bridge":
                return (f"As a Worker for {me}, use your own address {me}/<role> as `as` (for "
                        f"example {me}/{(event.get('agent_type') or 'worker').lower()}), and tag "
                        "every message with your subtask's task_id.")
            return None
        if hub_tool and hub_tool not in READ_ONLY_TOOLS:
            return (f"Subagents may read the hub and, as Workers, use "
                    f"{', '.join(sorted(WORKER_TOOLS))}; not {hub_tool}.")
        if hub_tool:
            return None
    claimed = ""
    if tool.startswith("mcp__agenthub"):
        claimed = str(inp.get("as") or "").strip()
    else:
        cmd = inp.get("command")
        if isinstance(cmd, list):
            cmd = " ".join(str(c) for c in cmd)
        m = WATCH_AS.search(cmd) if isinstance(cmd, str) and "agenthub_watch" in cmd else None
        claimed = m.group(1) if m else ""
    c = claimed.lower()
    # <session>/bridge is reserved: only exactly vendor@host/bridge is a wake bridge.
    if not c or c == me or (c.startswith(me + "/") and tool.startswith("mcp__agenthub")
                            and c.split("/")[-1] != "bridge"):
        return None
    return (f"You are AgentHub session {me}. This call uses {claimed!r}, which is not you. "
            f"Use {me} (or {me}/<role> for a subagent) as `as` on hub calls and as --as for "
            "agenthub_watch.py. Answer only mail delivered to your own session.")


def companies() -> dict[str, list[str]]:
    """This machine's company -> project folders, e.g. {"acme": ["D:/Work/Acme"]}.
    Each machine keeps its own (folders differ per machine), in $AGENTHUB_COMPANIES or
    ~/.agenthub/companies.json; the setup kit never overwrites it."""
    path = os.environ.get("AGENTHUB_COMPANIES") or os.path.join(
        os.path.expanduser("~"), ".agenthub", "companies.json")
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    out: dict[str, list[str]] = {}
    for name, dirs in (raw.items() if isinstance(raw, dict) else []):
        dirs = [dirs] if isinstance(dirs, str) else list(dirs or [])
        out[str(name).strip().lower()] = [os.path.normpath(os.path.expanduser(d)) for d in dirs if d]
    return out


def company_of(cwd: str, comps: dict[str, list[str]]) -> str:
    """The company whose folder holds `cwd` (the deepest match wins), or ''."""
    if not cwd:
        return ""
    want = os.path.normcase(os.path.abspath(cwd))
    best, depth = "", -1
    for name, dirs in comps.items():
        for d in dirs:
            root = os.path.normcase(os.path.abspath(d))
            try:
                inside = os.path.commonpath([want, root]) == root
            except ValueError:  # different drives
                inside = False
            if inside and len(root) > depth:
                best, depth = name, len(root)
    return best


def session_title(transcript: str) -> str:
    """The session's current title (as named in the Claude app, or by --name): the last
    custom-title record in the tail of its transcript. '' when there is none."""
    try:
        with open(transcript, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 262144))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    for line in reversed(tail.splitlines()):
        if '"custom-title"' in line:
            try:
                return str(json.loads(line).get("customTitle") or "")[:120]
            except ValueError:
                continue
    return ""


TITLE_ID = re.compile(r"^\[[0-9a-f]{8}\]\s*")


# Hub calls that make a session a hub participant (reading, or starting the watch
# every session runs, does not).
HUB_WRITES = {"hub_say", "hub_task_create", "hub_task_update", "hub_escalate", "hub_flush"}


def hub_used_marker(sid: str) -> str:
    return os.path.join(tempfile.gettempdir(), f".agenthub-hubused-{sid}")


def note_hub_use(event: dict, me: str) -> None:
    """Remember that this session has started using the hub (its first hub write)."""
    tool = event.get("tool_name") or ""
    if event.get("agent_id") or not tool.startswith("mcp__agenthub"):
        return
    if tool.split("__")[-1] in HUB_WRITES:
        try:
            open(hub_used_marker(me.rsplit("/", 1)[-1]), "a").close()
        except OSError:
            pass


def title_nudge(me: str, title: str) -> str:
    """Ask a Claude session, once per title, to put its hub id in front of its title
    ("[3f2a91c0] Fix the parser"), so the user can read the address off the session list --
    but only once it uses the hub, so the prefix tells hub sessions from the rest. The
    app titles a session after its first prompt and may retitle it later; each new
    title is asked about once."""
    sid = me.rsplit("/", 1)[-1]
    if not title or not re.fullmatch(r"[0-9a-f]{8}", sid) or title.startswith(f"[{sid}]"):
        return ""
    if not os.path.exists(hub_used_marker(sid)):
        return ""
    title = re.sub(r"^\s*\[HUB\]\s*", "", title)  # the old marker for hub-started sessions
    want = f"[{sid}] {TITLE_ID.sub('', title)}"
    marker = os.path.join(tempfile.gettempdir(), f".agenthub-title-{sid}")
    try:
        with open(marker, encoding="utf-8") as fh:
            if fh.read() == title:
                return ""
    except OSError:
        pass
    try:
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(title)
    except OSError:
        pass
    return (f"[AgentHub] Put this session's hub id in its title: if you have the "
            f"mcp__ccd_session_mgmt__set_session_title tool (load it with ToolSearch if it is "
            f"deferred), call it with session_id \"self\" and title {json.dumps(want)}. Do it "
            "silently, without mentioning it. Without that tool, ignore this.")


def poll(url: str, token: str, addr: str, stop_only: bool, event: str = "", session: str = "",
         cwd: str = "", adopt: str = "", company: str = "", title: str = "", succeed: str = "") -> str:
    # event + session let the hub register this session and know whether it is busy;
    # the wake bridge needs both.
    q = {"as": addr}
    if event:
        q["event"] = event
    if session:
        q["session"] = session
    if cwd and event == "SessionStart":
        q["cwd"] = cwd
    if company:
        # Every event, not just SessionStart, so sessions older than the mapping pick it up.
        q["company"] = company
    if title:
        q["title"] = title
    if adopt:
        q["adopt"] = adopt
    if succeed:
        q["succeed"] = succeed
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

    try:
        event = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        event = {}
    name = event.get("hook_event_name") or ""

    # Codex exposes the exact thread id that `codex queue --thread` needs; Claude Code
    # only has session_id in the hook payload.
    session = os.environ.get("CODEX_THREAD_ID") or event.get("session_id") or ""
    family = a.addr.split("/")[0].lower()
    me = session_address(family, session) if family else ""

    if name == "PreToolUse":
        why = guard(event, me) if me and me != family else None
        if why:
            emit({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                         "permissionDecision": "deny",
                                         "permissionDecisionReason": why}})
        elif me and me != family:
            note_hub_use(event, me)
        return

    url, token = credentials()
    if not (family and url and token):
        return

    # Mail is for the parent session, never a subagent: from inside a subagent only
    # STOPs are delivered, and the mail stays unread for the parent.
    stop_only = bool(event.get("agent_id")) or (name == "PostToolUse" and not due(me, a.every))
    title = session_title(event.get("transcript_path") or "") if me != family else ""
    try:
        m = CLAIM_TOKEN.search(event.get("prompt") or "") if name == "UserPromptSubmit" else None
        cwd = event.get("cwd") or os.getcwd()
        r = RESUMED_FROM.search(event.get("prompt") or "") if name == "UserPromptSubmit" else None
        text = poll(url, token, me, stop_only, name, session, cwd,
                    m.group(1) if m else "", company_of(cwd, companies()) if me != family else "",
                    title, r.group(1) if r else "")
    except Exception:  # noqa: BLE001
        text = ""
    if (family.startswith("claude@") and me != family and not event.get("agent_id")
            and name in ("UserPromptSubmit", "PostToolUse")):
        nudge = title_nudge(me, title)
        text = "\n".join(t for t in (text, nudge) if t)
    if name == "SessionStart" and me != family:
        family_mail = ("Mail to the family address reaches this session only when it belongs to "
                       "your conversation; otherwise it starts a new session.")
        watch = f", and start your watch with --as {me}" if family.startswith("claude@") else ""
        ident = (f"[AgentHub] Your AgentHub address for this session is {me}. Use it as `as` on "
                 f"every hub call{watch}. Mail to it reaches only this session: answer only mail "
                 f"delivered to this session, never another session's. {family_mail} Give "
                 "yourself a readable alias with hub_hello(label=...) if others need one.")
        text = ident + ("\n" + text if text else "")
    if not text or name == "SessionEnd":
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
