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
    inbox checks, default 5); AGENTHUB_STAKEHOLDERS (the master STAKEHOLDERS.md, or
    its folder; default from ~/.agenthub/site.json)

At SessionStart the hook also tells the session its stakeholders: the companies its
work is for, from a stakeholders.md in the folder it was opened on, checked against
the master file (see stakeholders_of). The first one listed is the session's company
on the hub.

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
import threading
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


# -- stakeholders -------------------------------------------------------------
# A stakeholder is the company a piece of work is for. The master file (STAKEHOLDERS.md,
# in the agents' shared folder) lists each one as a `## <Name>` section with bullets
# such as "- color: `#f5a26b`", "- hub: `acme`", "- laws: [..](..)", "- services: ...".
# A project binds itself to stakeholders with a stakeholders.md in the folder the
# session is opened on ("- <Name>" lines); only that folder counts, never its parents.
# No file means the session is open. The first stakeholder listed gives the session its
# company on the hub (its `hub` label), which colors the session's tasks on the board.
# (companies.json is no longer read here: it only tells the wake bridge which folder a
# new session for a company starts in.) server.py parses the same master file for the
# board: keep the two parsers in step.
PROJECT_FILE = "stakeholders.md"
MASTER_FILE = "STAKEHOLDERS.md"
MASTER_WAIT = 2.0  # seconds to wait for a shared folder that does not answer
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
BULLET = re.compile(r"^\s*[-*+]\s+(.*?)\s*$")


def norm_company(c: str | None) -> str:
    """A company label as the hub keeps it. Must match server.py's norm_company."""
    return re.sub(r"[^a-z0-9._-]", "", (c or "").strip().lower())[:64]


def parse_master(text: str) -> list[dict]:
    """The stakeholders in a master file: name, color, hub label, and its laws and
    services as link targets (relative to the master file) or plain text ("none yet")."""
    out: list[dict] = []
    cur: dict | None = None
    for line in (text or "").splitlines():
        if line.startswith("## "):
            cur = {"name": line[3:].strip(), "color": "", "hub": "", "laws": "", "services": ""}
            out.append(cur)
            continue
        if line.startswith("# "):
            cur = None
            continue
        m = BULLET.match(line)
        if not (cur is not None and m and ":" in m.group(1)):
            continue
        key, val = (x.strip() for x in m.group(1).split(":", 1))
        key = key.strip("*_ ").lower()
        if key in ("laws", "services"):
            cur[key] = LINK.findall(val) or val
        elif key in ("color", "hub"):
            cur[key] = val.strip("`'\" ")
    for s in out:
        if not re.fullmatch(r"#[0-9a-fA-F]{3,8}", s["color"]):
            s["color"] = ""
        s["hub"] = norm_company(s["hub"] or s["name"])
    return [s for s in out if s["name"]]


def read_project(folder: str) -> list[str] | None:
    """The stakeholder names in <folder>/stakeholders.md, in order; None when there is
    no such file (an open session). Parent folders are never searched."""
    try:
        with open(os.path.join(folder, PROJECT_FILE), encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read(65536)
    except (OSError, TypeError):
        return None
    names: list[str] = []
    for line in text.splitlines():
        m = BULLET.match(line)
        name = m.group(1).strip("`*_ ") if m else ""
        if name and name.lower() not in (n.lower() for n in names):
            names.append(name)
    return names


def load_site() -> dict:
    """site.json: $AGENTHUB_SITE, else the copy install_bridge.py keeps in ~/.agenthub/."""
    for path in (os.environ.get("AGENTHUB_SITE", ""),
                 os.path.join(os.path.expanduser("~"), ".agenthub", "site.json")):
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8-sig") as fh:
                    return json.load(fh)
            except (OSError, ValueError):
                pass
    return {}


def master_path(family: str) -> str:
    """Where this machine finds the master STAKEHOLDERS.md: $AGENTHUB_STAKEHOLDERS (the
    file, or the folder holding it), else site.json "stakeholders" (likewise), else
    STAKEHOLDERS.md in site.json "shared_folder". Either site.json value may be per host,
    e.g. {"lab": "/mnt/agents", "*": "//fileserver/share/Agents"}. '' when none is set."""
    host = family.split("@", 1)[1] if "@" in family else ""
    want = os.environ.get("AGENTHUB_STAKEHOLDERS", "")
    if not want:
        site = load_site()
        for key in ("stakeholders", "shared_folder"):
            v = site.get(key) or ""
            if isinstance(v, dict):
                v = v.get(host) or v.get("*") or ""
            if v:
                want = str(v)
                break
    if not want:
        return ""
    want = os.path.expanduser(want)
    return os.path.normpath(want if want.lower().endswith(".md") else os.path.join(want, MASTER_FILE))


def master_cache() -> str:
    return os.path.join(tempfile.gettempdir(), ".agenthub-stakeholders.json")


def read_master(path: str) -> tuple[list[dict] | None, bool]:
    """(stakeholders, from_cache). The file is read on a helper thread, so a shared
    folder that does not answer costs MASTER_WAIT seconds, not the session; then the
    copy last read on this machine is used. (None, False) when there is neither."""
    got: list[str] = []

    def read() -> None:
        try:
            with open(path, encoding="utf-8-sig", errors="replace") as fh:
                got.append(fh.read(262144))
        except OSError:
            pass

    if path:
        t = threading.Thread(target=read, daemon=True)
        t.start()
        t.join(MASTER_WAIT)
    if got:
        parsed = parse_master(got[0])
        try:
            with open(master_cache(), "w", encoding="utf-8") as fh:
                json.dump({"path": path, "stakeholders": parsed}, fh)
        except OSError:
            pass
        return parsed, False
    try:
        with open(master_cache(), encoding="utf-8") as fh:
            cached = json.load(fh)
        if path and cached.get("path") == path:
            return list(cached.get("stakeholders") or []), True
    except (OSError, ValueError, AttributeError):
        pass
    return None, False


def stakeholders_of(cwd: str, family: str) -> dict:
    """The stakeholders of a session opened on `cwd`: {"company", "note"}. `company` is
    the hub label of the first stakeholder listed ('' for an open session, or when that
    one is not in the master file); `note` is what the session is told at SessionStart."""
    names = read_project(cwd)
    if names is None:
        return {"company": "", "note": "[AgentHub] This session is open: its folder has no "
                                       "stakeholders.md, so only the shared laws apply."}
    where = os.path.normpath(os.path.join(cwd, PROJECT_FILE))
    if not names:
        return {"company": "", "note": f"[AgentHub] WARNING: {where} lists no stakeholders. Tell "
                                       "the human before doing any work."}
    path = master_path(family)
    master, stale = read_master(path)
    if master is None:
        why = (f"{path} cannot be read" if path else "this machine has no master file configured "
               "(AGENTHUB_STAKEHOLDERS, or site.json \"stakeholders\" or \"shared_folder\")")
        return {"company": "", "note": f"[AgentHub] WARNING: {where} names {', '.join(names)}, but "
                                       f"{why}, so they cannot be checked. Tell the human before "
                                       "doing any work."}
    by_name = {s["name"].lower(): s for s in master}
    known = [by_name[n.lower()] for n in names if n.lower() in by_name]
    unknown = [n for n in names if n.lower() not in by_name]
    first = by_name.get(names[0].lower())
    base = os.path.dirname(path)

    def files(v) -> str:
        if isinstance(v, list):
            return ", ".join(os.path.normpath(os.path.join(base, x)) for x in v) or "none"
        return v or "none"

    lines = []
    if known:
        each = "; ".join(f"{s['name']} (laws: {files(s['laws'])}; services: {files(s['services'])})"
                         for s in known)
        lines.append(f"[AgentHub] This session's stakeholders: {each}. Their laws and services add "
                     "to the shared ones: read them before working for them."
                     + (f" Your tasks are filed for {first['name']} (hub company {first['hub']})."
                        if first else ""))
    if unknown:
        lines.append(f"[AgentHub] WARNING: {where} names {', '.join(unknown)}, which "
                     f"{'is' if len(unknown) == 1 else 'are'} not in the master stakeholder file "
                     f"{path}. Tell the human before doing any work.")
    if stale:
        lines.append(f"[AgentHub] {path} could not be read just now; the stakeholders above come "
                     "from the copy last read on this machine.")
    return {"company": first["hub"] if first else "", "note": "\n".join(lines)}


def stake_cache(sid: str) -> str:
    return os.path.join(tempfile.gettempdir(), f".agenthub-stake-{sid}.json")


def session_stakeholders(event: dict, me: str, family: str, cwd: str) -> dict:
    """stakeholders_of() the folder the session was opened on: worked out at SessionStart
    and remembered for the session's later events, whose cwd may have moved. After a
    compaction (SessionStart again) the remembered answer is repeated."""
    sid = me.rsplit("/", 1)[-1]
    fresh = event.get("hook_event_name") == "SessionStart" and event.get("source") != "compact"
    if not fresh:
        try:
            with open(stake_cache(sid), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            pass  # a session older than this hook: work it out now, once
    try:
        info = stakeholders_of(cwd, family)
    except Exception:  # noqa: BLE001
        info = {"company": "", "note": ""}
    try:
        with open(stake_cache(sid), "w", encoding="utf-8") as fh:
            json.dump(info, fh)
    except OSError:
        pass
    return info


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

    # The stakeholders of the folder the session was opened on, worked out even when the
    # hub cannot be reached: an unknown stakeholder must stop the agent either way.
    cwd = event.get("cwd") or os.getcwd()
    stake = session_stakeholders(event, me, family, cwd) if me and me != family else {}
    url, token = credentials()
    if not (family and url and token):
        if name == "SessionStart" and stake.get("note") and not event.get("agent_id"):
            emit({"hookSpecificOutput": {"hookEventName": name, "additionalContext": stake["note"]}})
        return

    # Mail is for the parent session, never a subagent: from inside a subagent only
    # STOPs are delivered, and the mail stays unread for the parent.
    stop_only = bool(event.get("agent_id")) or (name == "PostToolUse" and not due(me, a.every))
    title = session_title(event.get("transcript_path") or "") if me != family else ""
    try:
        m = CLAIM_TOKEN.search(event.get("prompt") or "") if name == "UserPromptSubmit" else None
        r = RESUMED_FROM.search(event.get("prompt") or "") if name == "UserPromptSubmit" else None
        text = poll(url, token, me, stop_only, name, session, cwd,
                    m.group(1) if m else "", stake.get("company", ""),
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
        text = "\n".join(t for t in (ident, stake.get("note"), text) if t)
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
