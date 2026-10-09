#!/usr/bin/env python3
"""AgentHub wake bridge: wakes a sleeping agent when mail needs it.

One process per agent it serves, on that agent's own machine, because both wake
mechanisms are local:

  Codex   `codex queue --thread <id> --message <text>` drops a follow-up into the
          desktop app's queue, and an idle session starts a turn within seconds. A
          thread Codex reports gone (archived) is ended on the hub, which hands its mail
          to the family's next live session; an ended session, or a family-level thread
          silent for a day, is reported instead of queued into.
  Claude  `claude --bg --resume <id> "<text>"` continues that conversation as a
          background session. Only when the session is NOT already running: resuming a
          live session would start a duplicate copy. A Claude whose AgentHub watch is
          connected needs no waking at all, and one sitting in an open session without
          a watch is reported to the humans rather than duplicated.

Sessions: every session of the agent has its own address (claude@desk/<sid>), and the
bridge wakes each one with ITS session id, only for mail aimed at it.

Starting new sessions: mail to the family (claude@desk) or to anyone@host that the
hub cannot route to an existing conversation is a request. A bridge that starts
sessions (site.json `spawn`; Claude only for now) claims it on the hub -- exactly one
claim wins -- and runs `claude --bg --session-id <new uuid>` in the configured folder
and permission mode. The new session reads the request under its own address. At
most `spawn.max` started sessions work at once; the rest wait, and the hub tells the
humans about any request nobody claims within 60 seconds.

Common rules: only mail addressed to the agent wakes it (never a broadcast); never
while a STOP applies; one wake per batch; the wake says only that mail is waiting, and
carries no content and no authority; if the agent shows no activity within 60 seconds,
the humans and the senders are told once. The humans are site.json `humans`, else
AGENTHUB_HUMANS, else human@hub.

The hub learns each agent's session id and busy/idle state from agenthub_hook.py.

    python agenthub_wake_bridge.py --as codex@desk
    python agenthub_wake_bridge.py --as claude@25 --claude C:/path/claude.exe

Credentials come from AGENTHUB_URL / AGENTHUB_TOKEN or ~/.agenthub/credentials.json.
Installed and auto-started by install_bridge.py. Standard library only.
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_DIR = os.path.join(HOME, ".agenthub")
BUSY_EVENTS = {"UserPromptSubmit", "PostToolUse", "PreToolUse"}
LOG_MAX = 1_000_000
STALE_MAIL = 6 * 3600  # seconds: older undelivered mail is reported, never acted on
SIGNED_OUT = ("cannot be woken: the {vendor} CLI on {host} is signed out, so it waits. "
              "Run `{login}` on {host}")
LOGIN = {"claude": "claude auth login", "codex": "codex login"}
# A running `codex exec` with no hook events for this long is taken as hung (a long tool
# call fires none, so this is far longer than an app session's busy_stale).
CODEX_HUNG = 900


# ----------------------------------------------------------------------------
# plumbing
# ----------------------------------------------------------------------------
class Log:
    def __init__(self, path: str | None):
        self.path = path

    def __call__(self, msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        if self.path:
            try:
                if os.path.exists(self.path) and os.path.getsize(self.path) > LOG_MAX:
                    os.replace(self.path, self.path + ".1")
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass
        try:
            print(line, file=sys.stderr, flush=True)
        except Exception:  # noqa: BLE001  (pythonw has no stderr)
            pass


def credentials() -> tuple[str, str]:
    url = os.environ.get("AGENTHUB_URL", "")
    token = os.environ.get("AGENTHUB_TOKEN", "")
    if not (url and token):
        try:
            with open(os.path.join(AGENT_DIR, "credentials.json"), encoding="utf-8") as fh:
                saved = json.load(fh)
            url, token = url or saved.get("url", ""), token or saved.get("token", "")
        except (OSError, ValueError):
            pass
    return url.rstrip("/"), token


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


def load_site() -> dict:
    """Site settings kept out of source control (see site.example.json): the first
    site.json found in $AGENTHUB_SITE, next to this script, or in ~/.agenthub/."""
    for path in (os.environ.get("AGENTHUB_SITE", ""), os.path.join(HERE, "site.json"),
                 os.path.join(AGENT_DIR, "site.json")):
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    return json.load(fh)
            except (OSError, ValueError):
                pass
    return {}


def default_host() -> str:
    """The hub host part for this machine: the first site.json `hosts` rule whose
    `pattern` matches the hostname gives its `host`, or else its first capture group;
    with no matching rule, the lowercased hostname."""
    name = socket.gethostname()
    for rule in load_site().get("hosts", []):
        m = re.match(rule.get("pattern", "(?!)"), name, re.I)
        if m:
            return (rule.get("host") or m.group(1)).lower()
    if re.match(r"^[A-Za-z0-9-]+$", name):
        return name.lower()
    raise SystemExit(f"cannot derive the host part from hostname {name!r}; pass --as")


def humans() -> list[str]:
    """Who hears about an agent that could not be woken."""
    listed = load_site().get("humans") or os.environ.get("AGENTHUB_HUMANS", "").split(",")
    return [h.strip().lower() for h in listed if h.strip()] or ["human@hub"]


def native(path: str | None) -> str | None:
    """`path` if it is a real executable. On Windows a .cmd/.bat shim runs through
    cmd.exe, whose parsing ignores Python's argument quoting, so text from a message
    could run commands: discovery never returns one (see argv() for an explicit one)."""
    if not path:
        return None
    if os.name == "nt" and os.path.splitext(path)[1].lower() in (".cmd", ".bat"):
        return None
    return path


def find_codex(explicit: str | None) -> str | None:
    """Resolved on every wake: the desktop app's codex.exe lives in a folder whose name
    changes with each update."""
    if explicit:
        return explicit
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", os.path.join(HOME, "AppData", "Local"))
        hits = glob.glob(os.path.join(local, "OpenAI", "Codex", "bin", "*", "codex.exe"))
        if hits:
            return max(hits, key=os.path.getmtime)
    return native(shutil.which("codex"))


def find_claude(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    hit = native(shutil.which("claude"))
    if hit:
        return hit
    # A bridge run as a service (systemd, a scheduled task) does not get the login
    # shell's PATH, which is where ~/.local/bin usually comes from.
    for cand in (os.path.join(HOME, ".local", "bin", "claude.exe"),
                 os.path.join(HOME, ".local", "bin", "claude"),
                 os.path.join(HOME, ".claude", "local", "claude")):
        if cand and os.path.exists(cand):
            return cand
    return None


BATCH_UNSAFE = re.compile(r'["%\r\n]')


def argv(cmd: list[str]):
    """What to hand to subprocess for `cmd`. A native executable gets the list as is.
    A .cmd/.bat shim (only ever given explicitly, e.g. by the tests) gets a command line
    built here: every argument double-quoted, with the characters that can end a quoted
    string or expand inside one (`"`, `%`, newlines) replaced, because cmd.exe parses
    the line itself and Python's quoting does not protect against that."""
    if os.name != "nt" or os.path.splitext(cmd[0])[1].lower() not in (".cmd", ".bat"):
        return cmd
    return " ".join('"' + BATCH_UNSAFE.sub("'", str(c)) + '"' for c in cmd)


def claude_sessions(claude: str) -> list[dict] | None:
    """Active Claude sessions on this machine (desktop app ones included), or None if
    the list could not be read."""
    try:
        flags = {"creationflags": 0x08000000} if os.name == "nt" else {}
        r = subprocess.run(argv([claude, "agents", "--json"]), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=60, **flags)
        if r.returncode != 0:
            return None
        return json.loads(r.stdout or "[]")
    except Exception:  # noqa: BLE001
        return None


# Setup steps that stop a background Claude before its first turn. Each needs a human at a
# terminal, so a failed start names the step and what to run, not just "exit 1".
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
GATES = (
    ("home directory is trusted one session at a time",
     "`claude --bg` will not start in the home folder: set site.json spawn.cwd for {host} "
     "to a trusted project folder"),
    ("Workspace not trusted",
     "Open a terminal in {cwd}, run `claude`, and accept the trust prompt"),
    ("Login expired", "Run `claude auth login` on {host}"),
    ("Not logged in", "Run `claude auth login` on {host}"),
    ("Failed to authenticate", "Run `claude auth login` on {host}"),
    ("Choose the text style",
     "Run `claude` once in a terminal on {host} and finish the first-run setup"),
    ("New MCP server found in this project",
     "Open a terminal in {cwd}, run `claude`, and answer the project MCP server prompt "
     "(use it, or continue without it)"),
)


def name_gate(text: str, host: str, cwd: str) -> str:
    """What a human has to do, if `text` shows a known setup step; else ''."""
    plain = ANSI.sub("", text or "")
    for needle, todo in GATES:
        if needle.lower() in plain.lower():
            return todo.format(host=host, cwd=cwd)
    return ""


def is_delivery_notice(frm: str) -> bool:
    """Mail from a wake bridge (exactly vendor@host/bridge) or from the hub itself
    (server.py has the twin)."""
    f = (frm or "").lower()
    return f == "agenthub@hub" or (f.count("/") == 1 and f.endswith("/bridge"))


def parse_ts(s: str | None) -> float:
    if not s:
        return 0.0
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


class SingleInstance:
    """One bridge per Codex address per machine, or wakes would be queued twice."""

    def __init__(self, addr: str):
        os.makedirs(AGENT_DIR, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9]", "_", addr)
        self.fh = open(os.path.join(AGENT_DIR, f"codex_bridge_{safe}.lock"), "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit(f"another bridge for {addr} is already running on this machine")


# ----------------------------------------------------------------------------
# hub access
# ----------------------------------------------------------------------------
class Hub:
    def __init__(self, url: str, token: str, addr: str, spawn: bool = False):
        self.url, self.token, self.addr, self.spawn = url, token, addr, spawn
        self.bridge_addr = f"{addr}/bridge"

    def pending(self) -> dict:
        q = urllib.parse.urlencode({"as": self.addr})
        req = urllib.request.Request(f"{self.url}/api/pending?{q}")
        req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(f"{self.url}{path}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def claim(self, msg_id: int, target: str, session_id: str, cwd: str) -> str:
        """Claim a request for a new session; the hub answers 400 if another took it."""
        return self.post("/api/claim", {"as": self.bridge_addr, "msg_id": msg_id, "target": target,
                                        "session": session_id, "cwd": cwd})["session"]

    def fail(self, msg_id: int, note: str, target: str = "") -> None:
        self.post("/api/request/fail", {"as": self.bridge_addr, "msg_id": msg_id, "note": note,
                                        "target": target})

    def session_dead(self, session: str, note: str) -> list[dict]:
        """End a session that no longer exists; the hub hands its mail on."""
        return self.post("/api/session/dead", {"as": self.bridge_addr, "session": session,
                                               "note": note})["moved"]

    def say(self, body: str, to: list[str]) -> None:
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "hub_say", "arguments": {"as": self.bridge_addr, "body": body, "to": to,
                                              "topic": "agenthub"}}}
        req = urllib.request.Request(f"{self.url}/mcp", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            res = json.loads(r.read()).get("result", {})
        if res.get("isError"):
            raise RuntimeError(res.get("content", [{}])[0].get("text", "hub_say failed"))


def wake_socket_loop(hub: Hub, kick: threading.Event, log: Log, stop: threading.Event) -> None:
    """Hold the hub's wake socket so a new message triggers a check at once. The main
    loop also polls, so a dropped socket only costs latency, never a missed wake."""
    u = urllib.parse.urlparse(hub.url)
    host, port = u.hostname, u.port or 80
    # spawn=1 tells the hub this bridge starts sessions, so mail to the family becomes
    # a request for a new session rather than going to an existing one.
    q = {"as": hub.bridge_addr}
    if hub.spawn:
        q["spawn"] = "1"
    path = "/wake?" + urllib.parse.urlencode(q)
    backoff = 1
    while not stop.is_set():
        try:
            s = socket.create_connection((host, port), timeout=10)
            key = base64.b64encode(secrets.token_bytes(16)).decode()
            s.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                       f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                       f"Authorization: Bearer {hub.token}\r\n\r\n").encode())
            fh = s.makefile("rb")
            status = fh.readline().decode("latin-1")
            while fh.readline() not in (b"\r\n", b"\n", b""):
                pass
            if " 101 " not in status:
                raise ConnectionError(status.strip())
            s.settimeout(None)
            log("wake socket connected")
            backoff = 1
            kick.set()
            while True:
                h = fh.read(2)
                if len(h) < 2:
                    break
                op, ln = h[0] & 0x0F, h[1] & 0x7F
                if ln == 126:
                    ln = struct.unpack("!H", fh.read(2))[0]
                elif ln == 127:
                    ln = struct.unpack("!Q", fh.read(8))[0]
                mask = fh.read(4) if h[1] & 0x80 else b""
                data = fh.read(ln) if ln else b""
                if op == 0x8:
                    break
                if op == 0x9:
                    m = secrets.token_bytes(4)
                    s.sendall(struct.pack("!BB", 0x8A, 0x80 | len(data)) + m +
                              bytes(c ^ m[i % 4] for i, c in enumerate(data)))
                elif op == 0x1:
                    kick.set()
            log("wake socket closed")
        except Exception as e:  # noqa: BLE001
            log(f"wake socket unavailable: {e}")
        stop.wait(backoff)
        backoff = min(backoff * 2, 30)


# ----------------------------------------------------------------------------
# the bridge
# ----------------------------------------------------------------------------
class WakeRefused(Exception):
    """Why this agent cannot be woken right now; the text is reported to the humans."""


class SessionGone(WakeRefused):
    """The session no longer exists (Codex archived or deleted the thread). The hub ends
    it and hands its mail to another live session rather than reporting a failure."""


# What `codex queue` says about a thread that is gone for good.
GONE = re.compile(r"is archived|no (such )?(thread|session) (found|with)|(thread|session) not found", re.I)
# A family-level (pre-session) Codex thread silent this long is not open in the app:
# queuing into it would only run the request whenever someone next opens it.
LEGACY_STALE = 86400


class Bridge:
    def __init__(self, a, hub: Hub, log: Log):
        self.a, self.hub, self.log = a, hub, log
        self.vendor = a.addr.split("@")[0]
        self.host = a.addr.split("@")[1]
        self.woken: dict[tuple, float] = {}      # (address, message id) -> wake time
        self.escalated: set[tuple] = set()
        self.first_seen: dict[tuple, float] = {}
        self.spawn_failed: set[tuple] = set()    # (message id, target) we could not start
        self.bg_ids: dict[str, str] = {}         # placeholder address -> `claude --bg` id
        self.cwd_of: dict[str, str] = {}         # placeholder address -> folder it started in
        self.full_logged = False
        self.respawned: set[tuple] = set()       # (address, message id) handed to a new session
        self.stop_logged: set[str] = set()
        self._agents: list[dict] | None = None   # `claude agents --json`, read once per check
        self._login: tuple[float, bool] = (0.0, True)
        self.login_escalated = False
        self.req_from: dict[str, str] = {}       # placeholder address -> who sent its request
        self.req_told: set[tuple] = set()        # (message id, why) already reported for a request
        self.handed_on: set[tuple] = set()       # (address, message id) given to a new session
        self.unanswered: set[tuple] = set()      # (address, message id) reported as not answered
        self.resumed: set[tuple] = set()         # (address, message id) we stopped and resumed for
        self.procs: dict[str, subprocess.Popen] = {}  # Codex: thread id or "#<msg id>" -> its `codex exec`

    def addressed(self, m: dict) -> bool:
        to = [t.lower() for t in (m.get("to") or [])]
        return self.a.addr in to

    def check(self) -> None:
        state = self.hub.pending()
        now = time.time()
        self._agents = None

        # Who may need a wake: the family address itself (agents whose hook predates
        # session addresses) and every session of the family, each with the mail that
        # is really for it.
        targets = [(self.a.addr, state.get("agent") or {},
                    [m for m in state.get("unread", []) if self.addressed(m)],
                    bool(state.get("stops")))]
        for s in state.get("sessions") or []:
            if s.get("state") == "starting":
                # A placeholder for a session we started: its first prompt adopts the
                # request. One that never does within 2 x grace did not start.
                if now - parse_ts(s.get("last_seen")) >= 2 * self.a.grace and s.get("spawned_for"):
                    self.never_started(s)
                continue
            targets.append((s["address"], s, [m for m in s.get("unread", []) if m.get("direct")],
                            bool(s.get("stops"))))

        keys = {(addr, m["id"]) for addr, _, mail, _ in targets for m in mail}
        for store in (self.woken, self.first_seen):   # forget anything already read
            for k in [k for k in store if k not in keys]:
                store.pop(k, None)
        self.escalated &= keys
        self.respawned &= keys
        for addr, agent, mail, stopped in targets:
            if mail:
                self.check_one(addr, agent, mail, stopped, now)
            elif agent.get("owes_reply"):
                self.check_answered(addr, agent, now)

        for e in state.get("escalations") or []:
            self.to_desktop(e, state.get("sessions") or [])

        if self.a.spawn:
            self.spawn_requests(state.get("requests") or [], state.get("sessions") or [],
                                bool(state.get("stops")), now)

    def check_one(self, addr: str, agent: dict, mail: list[dict], stopped: bool, now: float) -> None:
        for m in mail:
            self.first_seen.setdefault((addr, m["id"]), now)
        if self.vendor == "claude" and "/" in addr:
            self.check_claude(addr, agent, mail, stopped, now)
            return
        if self.vendor == "codex" and "/" in addr:
            self.check_codex(addr, agent, mail, stopped, now)
            return
        # A family address (an agent whose hook predates session addresses) keeps the
        # older rules below.

        # Acknowledgement window for wakes already queued (60 seconds).
        last_activity = parse_ts(agent.get("last_event_at"))
        overdue = [k[1] for k, t in self.woken.items()
                   if k[0] == addr and now - t >= self.a.grace and last_activity < t
                   and k not in self.escalated]
        if overdue:
            self.escalate(addr, mail, overdue, f"did not acknowledge a wake within {int(self.a.grace)} s; "
                                               f"the {self.vendor} app may be closed or stuck")
            return

        # (Bursts are already settled by the main loop, which pauses after the wake
        # socket fires before it checks.)
        fresh = [m for m in mail if (addr, m["id"]) not in self.woken
                 and (addr, m["id"]) not in self.escalated]
        if not fresh:
            return

        if self.vendor == "codex" and agent.get("state") == "ended":
            # A Claude conversation can be resumed after it ends; a closed or archived
            # Codex thread cannot be queued into.
            self.escalate(addr, mail, [m["id"] for m in fresh],
                          "has ended (its Codex thread was closed or archived), so it cannot be woken")
            return

        if self.vendor == "claude" and agent.get("ws_open"):
            return  # its AgentHub watch is connected and delivers without any wake

        if stopped:
            self.log(f"{len(fresh)} message(s) waiting, but a STOP applies to {addr}; not waking")
            return

        event = agent.get("last_event") or ""
        if event in BUSY_EVENTS and now - last_activity < self.a.busy_stale:
            return  # it is working; its hooks deliver the mail at the next event.

        session = agent.get("session_id")
        if not session:
            opened = "Codex" if self.vendor == "codex" else "Claude"
            self.escalate(addr, mail, [m["id"] for m in fresh],
                          f"has no registered session, so it cannot be woken (prompt {opened} once "
                          "so its AgentHub hook reports the session)")
            return
        if self.vendor == "codex" and "/" not in addr and now - last_activity > LEGACY_STALE:
            self.escalate(addr, mail, [m["id"] for m in fresh],
                          f"has no live session: its last known Codex thread ({session}) has been silent "
                          f"since {agent.get('last_event_at') or 'an unknown time'}, so it is not queued "
                          "there. Open Codex on that machine and send it again")
            return

        text = (f"AgentHub: {len(mail)} message(s) waiting for {addr}. "
                f"Call hub_inbox as {addr} and handle them.")
        try:
            detail = (self.wake_codex(session, text) if self.vendor == "codex"
                      else self.wake_claude(session, text, agent.get("cwd") or ""))
        except SessionGone as e:
            if "/" not in addr:
                self.escalate(addr, mail, [m["id"] for m in fresh], str(e))
                return
            try:
                moved = self.hub.session_dead(addr, str(e))
            except Exception as ex:  # noqa: BLE001
                self.escalate(addr, mail, [m["id"] for m in fresh], f"{e} (and the hub could not "
                                                                    f"hand its mail on: {ex})")
                return
            self.log(f"{addr} is gone ({e}); the hub ended it and handed on: "
                     + (", ".join(f"#{mv['msg_id']} -> {mv['to'] or 'nobody (humans told)'}" for mv in moved)
                        or "nothing"))
            return
        except WakeRefused as e:
            self.escalate(addr, mail, [m["id"] for m in fresh], str(e))
            return
        except Exception as e:  # noqa: BLE001
            self.escalate(addr, mail, [m["id"] for m in fresh], f"could not be woken: {e}")
            return
        if detail is None:
            return  # nothing to do; another path is already delivering
        for m in fresh:
            self.woken[(addr, m["id"])] = now
        self.log(f"woke {addr} (session {session}) for message(s) "
                 f"{', '.join('#' + str(m['id']) for m in fresh)}: {detail}")

    # -- Claude: busy waits, idle is delivered to, anything else gets a new session --
    def check_claude(self, addr: str, agent: dict, mail: list[dict], stopped: bool, now: float) -> None:
        """Mail is waiting for one Claude session. Decide by what the session is doing:

          busy    hook events still arriving: its hooks deliver at the next tool call
                  or before its turn ends, so wait
          idle    its AgentHub watch delivers at once; a terminal (background) session
                  without one is stopped and resumed with the wake as its next prompt
                  (same session, same history: resuming a RUNNING session copies it)
          gone    a session the hub started is resumed the same way, keeping its context
          other   hung (busy, hooks silent), gone (not hub-started), open in an app with
                  no watch, or not picking up a wake or its watch: the hub reopens the
                  mail as a request and this bridge starts a NEW terminal session, which
                  reads the conversation so far from the hub. Mail never moves into
                  another live session, and a session open in an app is never stopped.
        """
        open_ = self.prelude(addr, agent, mail, stopped, now,
                             lambda: None if agent.get("state") == "ended"
                             else self.agent_entry(agent.get("session_id") or ""))
        if open_ is None:
            return

        session = agent.get("session_id") or ""
        last = parse_ts(agent.get("last_event_at"))
        ended = agent.get("state") == "ended"
        entry = self.agent_entry(session) if session else None
        if entry is False:
            return  # `claude agents` failed this time; decide on the next check
        status = ((entry or {}).get("status") or "").lower()
        background = (entry or {}).get("kind") == "background"

        # Busy: wait for its hooks, unless they have gone quiet for too long.
        if entry and not ended and ((agent.get("last_event") or "") in BUSY_EVENTS
                                    or status in ("busy", "running")):
            if now - last < self.a.busy_stale:
                return
            return self.respawn(addr, entry if background else None, open_,
                                f"looks hung: busy with no hook activity for {int(now - last)} s")

        # A wake already sent: give it the grace window, then give up on this session.
        woken = [self.woken[(addr, m["id"])] for m in open_ if (addr, m["id"]) in self.woken]
        if woken:
            if now - max(woken) < self.a.grace or last >= min(woken):
                return
            return self.respawn(addr, entry if background else None, open_,
                                f"did not pick up a wake within {int(self.a.grace)} s")

        if entry and not ended:  # idle
            if agent.get("ws_open"):
                first = min(self.first_seen[(addr, m["id"])] for m in open_)
                if now - first < self.a.grace:
                    return  # its watch delivers
                return self.respawn(addr, None, open_,
                                    f"has an AgentHub watch that did not deliver within {int(self.a.grace)} s")
            if background:
                return self.resume(addr, session, entry, open_, agent, now)
            return self.respawn(addr, None, open_, "is open in an app, idle, with no AgentHub watch")

        # Gone.
        if agent.get("spawned_for"):
            return self.resume(addr, session, None, open_, agent, now)
        self.respawn(addr, None, open_, "has ended" if ended else "is no longer running")

    def prelude(self, addr: str, agent: dict, mail: list[dict], stopped: bool, now: float,
                running) -> list[dict] | None:
        """What both vendors check before deciding how to deliver: the mail still to
        deliver, or None for nothing to do now. `running()` gives the session's running
        process (None if it is not running, False if that cannot be told now)."""
        open_ = [m for m in mail if (addr, m["id"]) not in self.respawned]
        # Mail that sat undelivered for hours (a bridge that was down, a session long
        # gone) is not worked on blind: the work may be done, moot or superseded.
        stale = [m for m in open_ if now - parse_ts(m.get("ts")) > STALE_MAIL]
        if stale:
            self.respawned.update((addr, m["id"]) for m in stale)
            self.escalate(addr, stale, [m["id"] for m in stale],
                          f"was not woken for mail more than {STALE_MAIL // 3600} hours old; "
                          "send it again if it is still needed")
            open_ = [m for m in open_ if m not in stale]
        # A report from a bridge or the hub is for the session that sent the mail, to
        # pass on to the user; the humans are copied on it already. Waiting for that session
        # is fine, but it never wakes a gone session or starts a new one.
        notices = [m for m in open_ if is_delivery_notice(m.get("from") or "")]
        if notices and len(notices) == len(open_) and not agent.get("ws_open"):
            entry = running()
            if entry is False:
                return None
            if entry is None:
                self.respawned.update((addr, m["id"]) for m in notices)
                self.log(f"{addr} is gone; not starting a session for delivery notice(s) "
                         f"{', '.join('#' + str(m['id']) for m in notices)} (the humans have them)")
                return None
        if not open_ or "/" not in addr:
            return None  # family-level mail is a request now: spawn_requests handles it
        if stopped:
            if addr not in self.stop_logged:
                self.stop_logged.add(addr)
                self.log(f"{len(open_)} message(s) waiting, but a STOP applies to {addr}; not waking")
            return None
        self.stop_logged.discard(addr)
        if not self.cli_ready():
            self.escalate(addr, open_, [m["id"] for m in open_], SIGNED_OUT.format(host=self.host, vendor=self.vendor.capitalize(), login=LOGIN[self.vendor]))
            return None  # the mail waits for the login
        return open_

    # -- Codex: the same rules, with Codex's own commands --------------------------------
    def check_codex(self, addr: str, agent: dict, mail: list[dict], stopped: bool, now: float) -> None:
        """Mail is waiting for one Codex thread (tested 2026-10-09, task 618):

          busy    a `codex exec` of ours is running it, or its hooks are firing: they
                  deliver at the next tool call or before its turn ends, so wait
          idle    a thread the hub started (terminal-only, run by `codex exec`):
                  `codex exec resume <id>` with the wake, which also runs anything queued.
                  A thread open in the desktop app: `codex queue`, which the app runs
                  now if it is idle, or after its current turn
          other   hung, archived, closed, or not picking up a wake: a NEW thread, as for
                  Claude. A `codex exec` thread ends after every turn, so for a thread the
                  hub started "ended" just means idle.
        """
        session = agent.get("session_id") or ""
        proc = self.procs.get(session) or self.procs.get(f"#{agent.get('spawned_for')}")
        running = proc is not None and proc.poll() is None
        hub_started = bool(agent.get("spawned_for"))
        ended = agent.get("state") == "ended" and not hub_started
        open_ = self.prelude(addr, agent, mail, stopped, now,
                             lambda: proc if running else (None if ended else {"kind": "thread"}))
        if open_ is None:
            return
        last = parse_ts(agent.get("last_event_at"))
        busy_event = (agent.get("last_event") or "") in BUSY_EVENTS

        if running or (busy_event and not ended):
            # A long tool call fires no hook events, so a running `codex exec` gets
            # more time than an app thread whose hooks went quiet.
            limit = max(self.a.busy_stale, CODEX_HUNG) if running else self.a.busy_stale
            if now - last < limit:
                return
            if running:
                proc.kill()
            return self.respawn(addr, None, open_,
                                f"looks hung: busy with no hook activity for {int(now - last)} s")

        woken = [self.woken[(addr, m["id"])] for m in open_ if (addr, m["id"]) in self.woken]
        if woken:
            if now - max(woken) < self.a.grace or last >= min(woken):
                return
            return self.respawn(addr, None, open_, f"did not pick up a wake within {int(self.a.grace)} s")
        if not session:
            return self.respawn(addr, None, open_, "has no registered Codex thread")
        if hub_started:
            return self.resume_codex(addr, session, open_, agent, now)
        if ended:
            return self.respawn(addr, None, open_, "has ended (its Codex thread was closed or archived)")
        text = (f"AgentHub: {len(open_)} message(s) waiting for {addr}. "
                f"Call hub_inbox as {addr} and handle them.")
        try:
            detail = self.wake_codex(session, text)
        except SessionGone as e:
            return self.respawn(addr, None, open_, str(e))
        except Exception as e:  # noqa: BLE001
            return self.respawn(addr, None, open_, f"could not be woken ({e})")
        for m in open_:
            self.woken[(addr, m["id"])] = now
        self.log(f"woke {addr} (thread {session}) for message(s) "
                 f"{', '.join('#' + str(m['id']) for m in open_)}: {detail}")

    def codex_exec(self, args: list[str], cwd: str, key: str) -> subprocess.Popen:
        """Start `codex exec ...` in the background (a turn can run for a long time),
        output to ~/.agenthub/codex-exec/<key>.log, and remember it under `key`."""
        codex = find_codex(self.a.codex)
        if not codex:
            raise WakeRefused("the codex executable was not found")
        logdir = os.path.join(AGENT_DIR, "codex-exec")
        os.makedirs(logdir, exist_ok=True)
        log = open(os.path.join(logdir, re.sub(r"[^A-Za-z0-9_.#-]", "_", key) + ".log"), "ab")
        flags = {"creationflags": 0x08000000} if os.name == "nt" else {"start_new_session": True}
        # Windows: the "elevated" sandbox (the desktop app's setting) needs a helper that
        # cannot set itself up for a thread started by this background service, so every
        # shell command fails ("setup refresh had errors"). The unelevated sandbox works;
        # anything outside it goes through --approve-for-me's automatic review.
        win = ["-c", 'windows.sandbox="unelevated"'] if os.name == "nt" else []
        # That sandbox cannot read a network share at all (granting the folder does not
        # help: it has no network credentials), so the prompt tells the thread to read
        # the shared Agents folder outside the sandbox (see codex_note).
        cmd = [codex, *win, "exec", "--skip-git-repo-check", *self.a.codex_args, "-C", cwd] + args
        p = subprocess.Popen(argv(cmd), cwd=cwd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **flags)
        log.close()
        self.procs[key] = p
        return p

    def codex_note(self) -> str:
        """A line for a Codex thread's prompt on Windows (tested 2026-10-09): reads of the
        shared Agents folder are denied inside the sandbox; asking to run that read
        outside it is approved by --approve-for-me's automatic review."""
        if self.vendor != "codex" or os.name != "nt":
            return ""
        share = self.a.shared_folder or "the shared Agents folder"
        return (f" On this machine your sandbox cannot read network shares: if reading {share} "
                "(your shared instructions) is denied, rerun that read with escalated sandbox "
                "permissions; it is reviewed automatically.")

    def resume_codex(self, addr: str, session: str, mail: list[dict], agent: dict, now: float) -> None:
        """Deliver to a thread the hub started by resuming it with the wake as its next
        prompt. The thread keeps its id, and anything queued for it runs too."""
        text = (f"AgentHub: {len(mail)} message(s) are waiting for you. Call hub_inbox with "
                f"your own session address (the one the AgentHub hook gave you) and handle them."
                + self.codex_note())
        cwd = agent.get("cwd") or self.a.spawn_cwd
        try:
            p = self.codex_exec(["resume", session, text], cwd, session)
        except Exception as e:  # noqa: BLE001
            return self.respawn(addr, None, mail, f"could not be resumed ({e})")
        for m in mail:
            self.woken[(addr, m["id"])] = now
        self.log(f"woke {addr} (thread {session}) for message(s) "
                 f"{', '.join('#' + str(m['id']) for m in mail)}: codex exec resume, pid {p.pid}")

    def agent_entry(self, session: str):
        """This session's `claude agents --json` row; None if it is not running; False
        if the list could not be read."""
        if self._agents is None:
            claude = find_claude(self.a.claude)
            got = claude_sessions(claude) if claude else None
            if got is None:
                self.log("`claude agents --json` failed; cannot tell busy from idle this time")
                return False
            self._agents = got
        hit = [s for s in self._agents if (s.get("sessionId") or "").lower() == session.lower()]
        return hit[0] if hit else None

    def cli_ready(self) -> bool:
        """Is the CLI signed in? Checked at most once a minute. Every wake and every new
        session runs through the CLI, which signs out separately from the desktop app;
        a signed-out CLI fails each one silently ("Login expired"). Claude reports JSON
        from `claude auth status`; Codex prints "Logged in using ..." from `codex login
        status` and exits non-zero when signed out."""
        t, ok = self._login
        if time.time() - t < 60:
            return ok
        ok = True
        try:
            if self.vendor == "codex":
                codex = find_codex(self.a.codex)
                if codex:
                    r = self.run([codex, "login", "status"])
                    ok = r.returncode == 0 and "logged in" in (r.stdout + r.stderr).lower()
            else:
                claude = find_claude(self.a.claude)
                if claude:
                    r = self.run([claude, "auth", "status"])
                    ok = not re.search(r'"loggedIn"\s*:\s*false', r.stdout or "")
        except Exception:  # noqa: BLE001  (an older CLI without a status command: assume fine)
            ok = True
        self._login = (time.time(), ok)
        if not ok and not self.login_escalated:
            self.login_escalated = True
            self.log(f"the {self.vendor} CLI on {self.host} is signed out; wakes and new sessions wait")
        elif ok and self.login_escalated:
            self.login_escalated = False
            self.log(f"the {self.vendor} CLI on {self.host} is signed in again")
        return ok

    def stop_claude(self, entry: dict, session: str) -> bool:
        """Stop a background Claude session and confirm it is gone. Resuming, replacing
        or elevating a session that is still running would leave two copies working."""
        claude = find_claude(self.a.claude)
        try:
            r = self.run([claude, "stop", entry.get("id") or session[:8]])
        except Exception as e:  # noqa: BLE001
            self.log(f"could not stop {entry.get('id')}: {e}")
            return False
        live = claude_sessions(claude)
        still = live is None or any((x.get("sessionId") or "").lower() == session.lower()
                                    and (x.get("status") or "").lower() in ("busy", "running", "idle")
                                    for x in live)
        if r.returncode != 0 and still:
            self.log(f"`claude stop {entry.get('id')}` exited {r.returncode} and the session is still "
                     f"running: {(r.stdout + r.stderr).strip()[:200]}")
            return False
        return True

    def resume(self, addr: str, session: str, entry: dict | None, mail: list[dict], agent: dict,
               now: float) -> None:
        """Deliver to a terminal session by resuming it with the wake as its next prompt.
        A running one is stopped first, since resuming it while it runs makes a copy."""
        claude = find_claude(self.a.claude)
        # No address to call as: a resumed conversation may have a new one, and the
        # hook refuses hub calls made under another session's address. The last line
        # lets the hub hand the old address's mail to whichever address it now has.
        text = (f"AgentHub: {len(mail)} message(s) are waiting for you. Call hub_inbox with "
                f"your own session address (the one the AgentHub hook gave you) and handle them. "
                f"AgentHub resumed from: {addr}")
        cwd = agent.get("cwd") or ""
        if entry and not self.stop_claude(entry, session):
            self.escalate(addr, mail, [m["id"] for m in mail],
                          f"could not be stopped to deliver to it, so its mail waits (a copy must not run "
                          f"beside it). Stop it by hand on {self.host}: `claude stop {entry.get('id')}`")
            return
        try:
            r = self.run([claude, "--bg", "--resume", session, text], cwd=cwd)
            out = (r.stdout + r.stderr).strip()
            gate = name_gate(out, self.host, cwd)
            if r.returncode != 0 or gate:
                raise WakeRefused(gate or f"claude --bg --resume exited {r.returncode}: {out[:200]}")
        except Exception as e:  # noqa: BLE001
            return self.respawn(addr, None, mail, f"could not be resumed ({e})")
        for m in mail:
            self.woken[(addr, m["id"])] = now
        # The resumed conversation may come back under a NEW session id (newer CLIs):
        # this address then ends because we stopped it, and the answer comes from the
        # other one. check_answered() does not count that ending against it.
        self.resumed.update((addr, m["id"]) for m in mail)
        self.log(f"woke {addr} (session {session}) for message(s) "
                 f"{', '.join('#' + str(m['id']) for m in mail)}: {' '.join(out.split())[:160]}")

    def respawn(self, addr: str, stop_entry: dict | None, mail: list[dict], why: str) -> None:
        """Give this session's waiting mail to a new terminal session. `stop_entry` is a
        background session of ours to stop first (hung, or failing its wakes), so two
        sessions never work the same message."""
        mids = [m["id"] for m in mail]
        if not self.a.spawn:
            self.escalate(addr, mail, mids, f"{why}, and this bridge does not start sessions")
            return
        if stop_entry and not self.stop_claude(stop_entry, stop_entry.get("sessionId") or ""):
            self.escalate(addr, mail, mids, f"{why}, but it could not be stopped, so no second session "
                                            f"is started beside it. Stop it by hand on {self.host}: "
                                            f"`claude stop {stop_entry.get('id')}`")
            return
        try:
            reopened = self.hub.post("/api/session/respawn", {
                "as": self.hub.bridge_addr, "session": addr, "msg_ids": mids, "note": why})["reopened"]
        except Exception as e:  # noqa: BLE001
            self.escalate(addr, mail, mids, f"{why}, and the hub could not reopen its mail: {e}")
            return
        self.respawned.update((addr, mid) for mid in mids)
        self.handed_on.update((addr, mid) for mid in mids)
        self.log(f"{addr} {why}: a new session will take "
                 f"{', '.join('#' + str(m) for m in reopened) or 'nothing'}")
        self.escalate(addr, mail, mids, f"{why}; a new terminal session on {self.host} is taking it over")

    def check_answered(self, addr: str, agent: dict, now: float) -> None:
        """A session that took mail it should act on (a request, a HANDOFF or a QUESTION)
        and then stopped -- went idle, hung, or ended -- without answering the sender:
        tell the sender once, so the sending session can tell the user."""
        owed = agent["owes_reply"]
        key = (addr, owed["id"])
        if key in self.unanswered or key in self.handed_on or "/" not in addr:
            return
        if not agent.get("last_event"):
            return  # it never ran (a session that failed to start is reported as that)
        last = parse_ts(agent.get("last_event_at"))
        event = agent.get("last_event") or ""
        if agent.get("state") == "ended" or event == "SessionEnd":
            how = "ended"
        elif now - last < self.a.busy_stale:
            return  # still working, or only just stopped
        elif event in BUSY_EVENTS:
            how = f"looks hung (busy, no hook activity for {int(now - last)} s)"
        elif event == "Stop":
            if self.vendor == "codex":
                how = "went idle"  # a Codex turn that ended; its thread can be resumed
            else:
                entry = self.agent_entry(agent.get("session_id") or "")
                if entry is False:
                    return
                how = "went idle" if entry else "stopped running"
        else:
            return
        if key in self.resumed and how in ("ended", "stopped running"):
            return  # we stopped it to resume it; the resumed session answers
        self.unanswered.add(key)
        self.escalate_to(owed.get("from") or "", owed["id"],
                         f"Wake bridge: {addr} took message #{owed['id']} from {owed.get('from')} but "
                         f"{how} without answering it. "
                         + (f"`claude attach` on {self.host} shows what it did, " if self.vendor == "claude"
                            else f"`codex resume {agent.get('session_id')}` on {self.host} shows what it did, ")
                         + "or send it again to start fresh.")

    def to_desktop(self, e: dict, sessions: list[dict]) -> None:
        """Move a session into the Claude desktop app (hub_escalate). An agent session may
        not open one itself; this bridge runs outside any session, as the user. Stop the
        background copy first, or the app would open a second copy of it."""
        addr = e["session"]
        s = next((x for x in sessions if x["address"] == addr), None)
        session = (s or {}).get("session_id") or ""
        claude = find_claude(self.a.claude)
        ok, note = False, ""
        try:
            if not session:
                raise WakeRefused(f"{addr} has no registered session id")
            if not claude:
                raise WakeRefused("the claude executable was not found")
            if self.vendor == "codex":
                # Tested 2026-10-09: no command or link opens a thread in the Codex app
                # from outside; a session inside the app can (it has a tool for it).
                raise WakeRefused(
                    f"Codex cannot open a thread in its desktop app from outside. Ask a Codex "
                    f"session in the app on {self.host} to open thread {session}, or run "
                    f"`codex resume {session}` in a terminal there")
            entry = self.agent_entry(session)
            if entry and entry.get("kind") != "background":
                ok, note = True, f"{addr} is already open in an app"
            elif sys.platform.startswith("linux"):
                # `claude --desktop` exists only on macOS and Windows, and the Linux app
                # cannot load a terminal session from a claude:// link (it reports the
                # session "not found" and misbehaves). Leave the session as it is.
                name = (entry or {}).get("name") or ""
                raise WakeRefused(
                    f"the Linux desktop app cannot take a terminal session from outside. The session "
                    f"is still running; on {self.host}, open it from the app with /resume"
                    + (f" (it is named \"{name}\")" if name else "")
                    + f", or in a terminal with `claude attach {(entry or {}).get('id') or session[:8]}`")
            else:
                if entry and not self.stop_claude(entry, session):
                    raise WakeRefused(f"its background session could not be stopped, and opening it "
                                      f"in the app beside it would make a copy. Stop it by hand on "
                                      f"{self.host}: `claude stop {entry.get('id')}`")
                code = self.run_on_terminal([claude, "--desktop", "--resume", session],
                                            (s or {}).get("cwd") or "")
                if code != 0:
                    raise WakeRefused(f"claude --desktop --resume exited {code}")
                ok, note = True, f"opened {addr} (session {session}) in the Claude desktop app on {self.host}"
                if (s or {}).get("worker_of"):
                    note += (f". Orchestrator: mark subtask #{s['worker_of']} elevated, and tell {addr} "
                             "that it now owns that work and files its own primary task for it")
        except Exception as ex:  # noqa: BLE001
            note = f"could not move {addr} into the desktop app: {ex}"
        try:
            self.hub.post("/api/escalation/done", {"as": self.hub.bridge_addr, "id": e["id"],
                                                   "ok": ok, "note": note})
        except Exception as ex:  # noqa: BLE001
            self.log(f"could not settle escalation #{e['id']}: {ex}")
            return  # it stays open; the next check tries again
        self.log(f"escalation #{e['id']}: {note}")
        self.escalate_to(e.get("frm") or "", e["id"],
                         f"Wake bridge: {'RESULT' if ok else 'FAILED'} escalation #{e['id']}: {note}.")

    def run_on_terminal(self, cmd: list[str], cwd: str = "") -> int:
        """Run `cmd` attached to a terminal of its own: `claude --desktop` refuses to run
        with its output redirected. Windows: a new, hidden console. Elsewhere: a
        pseudo-terminal from `script`. Returns the exit code."""
        cwd = os.path.normpath(cwd) if cwd and os.path.isdir(cwd) else None
        if os.name == "nt":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0  # SW_HIDE
            p = subprocess.Popen(argv(cmd), cwd=cwd, startupinfo=si,
                                 creationflags=subprocess.CREATE_NEW_CONSOLE)
        else:
            script = shutil.which("script")
            if not script:
                raise WakeRefused("`script` is needed to give claude a terminal")
            if sys.platform == "darwin":
                # BSD script: script [-q] file command [args...] (untested on a real Mac)
                pty = [script, "-q", "/dev/null", *cmd]
            else:
                # util-linux script: the command is one shell string
                line = " ".join("'" + c.replace("'", "'\\''") + "'" for c in cmd)
                pty = [script, "-qec", line, "/dev/null"]
            p = subprocess.Popen(pty, cwd=cwd, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            return p.wait(timeout=90)
        except subprocess.TimeoutExpired:
            p.kill()
            raise WakeRefused("claude --desktop --resume did not finish within 90 s")

    def tell_request(self, mid: int, target: str, frm: str, why: str) -> None:
        """Report a problem with a request (mail to a family) once, to its sender and
        the humans."""
        if (mid, why) in self.req_told:
            return
        self.req_told.add((mid, why))
        self.escalate_to(frm, mid, f"Wake bridge: request #{mid} to {target} {why}.")

    def escalate_to(self, frm: str, mid: int, body: str) -> None:
        to = sorted(({frm} if "@" in frm else set()) | set(humans()))
        self.log(f"ESCALATE to {', '.join(to)}: {body}")
        try:
            self.hub.say(body, to)
        except Exception as e:  # noqa: BLE001
            self.log(f"escalation could not be posted: {e}")

    def never_started(self, s: dict) -> None:
        mid = int(s["spawned_for"])
        key = (mid, "never-started")
        if key in self.spawn_failed:
            return
        self.spawn_failed.add(key)
        why = f"the session started for it never picked it up within {int(2 * self.a.grace)} s"
        p = self.procs.get(f"#{mid}")
        if p is not None and p.poll() is not None:
            tail = ""
            try:
                with open(os.path.join(AGENT_DIR, "codex-exec", f"#{mid}.log"), encoding="utf-8",
                          errors="replace") as fh:
                    tail = " ".join(fh.read().split())[-240:]
            except OSError:
                pass
            why += f" (codex exec exited {p.returncode}: {tail})"
        bg = self.bg_ids.get(s["address"])
        if bg:
            # The reason is usually on the session's own screen: read it from there.
            why += f" (claude logs {bg})"
            claude = find_claude(self.a.claude)
            try:
                r = self.run([claude, "logs", bg]) if claude else None
                gate = name_gate((r.stdout + r.stderr) if r else "", self.host,
                                 self.cwd_of.get(s["address"], self.a.spawn_cwd))
            except Exception:  # noqa: BLE001
                gate = ""
            if gate:
                why += f". {gate}"
        self.log(f"request #{mid}: {why}")
        try:
            self.hub.fail(mid, why)
        except Exception as e:  # noqa: BLE001
            self.log(f"could not report the failure to the hub: {e}")
        self.escalate(s["address"], [{"id": mid, "from": self.req_from.get(s["address"], "")}], [mid],
                      f"could not start a session: {why}")

    def pick_cwd(self, workdir: str | None, company: str | None = None) -> tuple[str, str, str]:
        """Where to start a session for a request: (folder, focus, note).

        Only the host's allowed spawn folders (site.json spawn.cwd, plus this machine's
        company folders) are ever used. A `workdir` that is one of them starts there;
        one inside one starts in that folder, with the session told to work in
        `workdir` (it inherits the folder's workspace trust). Without a usable workdir,
        the request's company picks this machine's folder for it (companies.json).
        Anything else starts in the default, and the note says why."""
        note = ""
        if workdir:
            got = self._workdir_cwd(workdir)
            if got:
                return got
            note = (f"the requested folder {workdir} is not one of {self.host}'s allowed "
                    f"spawn folders ({', '.join(self.a.spawn_dirs)}) or does not exist")
        company = (company or "").strip().lower()
        if company:
            dirs = [d for d in self.a.companies.get(company, []) if os.path.isdir(d)]
            if dirs:
                return dirs[0], "", note
            note = "; ".join(filter(None, [note, (
                f"this machine has no folder for company {company!r} "
                f"(~/.agenthub/companies.json: {', '.join(self.a.companies) or 'none set'})")]))
        return self.a.spawn_cwd, "", note

    def _workdir_cwd(self, workdir: str) -> tuple[str, str, str] | None:
        want = os.path.realpath(os.path.expanduser(workdir))
        for root in self.a.spawn_dirs:
            r = os.path.realpath(root)
            try:
                inside = os.path.commonpath([os.path.normcase(want), os.path.normcase(r)]) == os.path.normcase(r)
            except ValueError:  # different drives
                inside = False
            if inside and os.path.isdir(want):
                # realpath only decides containment; tell the session the path as asked.
                asked = os.path.normpath(os.path.expanduser(workdir))
                return root, ("" if os.path.normcase(want) == os.path.normcase(r) else asked), ""
        return None

    # -- starting new sessions for family / anyone@host requests ----------------
    def spawn_requests(self, requests: list[dict], sessions: list[dict], stopped: bool,
                       now: float) -> None:
        mine = [r for r in requests
                if (r["msg_id"], r["target"]) not in self.spawn_failed
                and (not r["target"].startswith("anyone@") or self.a.anyone_vendor == self.vendor)]
        if not mine:
            self.full_logged = False
            return
        if stopped:
            return  # never start work under a STOP; the hub escalates unclaimed requests
        for rq in [r for r in mine if now - parse_ts(r.get("ts")) > STALE_MAIL]:
            mine.remove(rq)
            self.spawn_failed.add((rq["msg_id"], rq["target"]))
            why = f"is more than {STALE_MAIL // 3600} hours old, so no session was started for it"
            try:
                self.hub.fail(rq["msg_id"], why, rq["target"])
            except Exception as e:  # noqa: BLE001
                self.log(f"could not report the failure to the hub: {e}")
            self.tell_request(rq["msg_id"], rq["target"], rq.get("frm") or "", why + "; send it again if it is still needed")
        if not mine:
            return
        if not self.cli_ready():
            for rq in mine:
                self.tell_request(rq["msg_id"], rq["target"], rq.get("frm") or "",
                                  SIGNED_OUT.format(host=self.host, vendor=self.vendor.capitalize(), login=LOGIN[self.vendor]))
            return  # the requests wait for the login
        # Hub-started sessions still working count against the cap; idle ones do not.
        working = [s for s in sessions if s.get("spawned_for") and (
            s.get("state") == "starting" or (
                s.get("state") == "live" and s.get("last_event") not in ("Stop", "SessionEnd")
                and now - parse_ts(s.get("last_event_at")) < self.a.busy_stale))]
        room = self.a.spawn_max - len(working)
        for rq in mine:
            if room <= 0:
                if not self.full_logged:
                    self.log(f"{len(working)} started session(s) still working (max {self.a.spawn_max}); "
                             f"request(s) {', '.join('#' + str(r['msg_id']) for r in mine)} wait")
                    self.full_logged = True
                return
            key = (rq["msg_id"], rq["target"])
            session_id = str(uuid.uuid4())
            cwd, focus, note = self.pick_cwd(rq.get("workdir"), rq.get("company"))
            try:
                address = self.hub.claim(rq["msg_id"], rq["target"], session_id, focus or cwd)
            except urllib.error.HTTPError as e:
                self.log(f"request #{rq['msg_id']} not claimed: {e.read().decode('utf-8', 'replace')[:200]}")
                continue
            try:
                detail = (self.spawn_codex(session_id, address, rq, cwd, focus, note)
                          if self.vendor == "codex" else
                          self.spawn_claude(session_id, address, rq, cwd, focus, note))
            except Exception as e:  # noqa: BLE001
                self.spawn_failed.add(key)
                why = str(e)
                gate = name_gate(why, self.host, cwd)
                if gate and gate not in why:
                    why += f". {gate}"
                self.log(f"could not start a session for request #{rq['msg_id']}: {why}")
                try:
                    self.hub.fail(rq["msg_id"], why)
                except Exception as e2:  # noqa: BLE001
                    self.log(f"could not report the failure to the hub: {e2}")
                self.escalate(rq["target"], [{"id": rq["msg_id"], "from": rq.get("frm", "")}],
                              [rq["msg_id"]], f"could not start a new session: {why}")
                continue
            room -= 1
            m = re.search(r"backgrounded[^0-9a-f]{1,8}([0-9a-f]{8})", detail)
            if m:
                self.bg_ids[address] = m.group(1)
            self.cwd_of[address] = cwd
            self.req_from[address] = rq.get("frm") or ""
            self.log(f"started {address} for request #{rq['msg_id']} to {rq['target']} "
                     f"from {rq.get('frm')}: {detail}")

    def spawn_claude(self, session_id: str, address: str, rq: dict, cwd: str = "",
                     focus: str = "", note: str = "") -> str:
        cwd = cwd or self.a.spawn_cwd
        if self.vendor != "claude":
            raise WakeRefused(f"starting new {self.vendor} sessions is not supported yet")
        claude = find_claude(self.a.claude)
        if not claude:
            raise WakeRefused("the claude executable was not found")
        # Known setup gates, checked up front so the user hears the cause at once instead of
        # after a start that hangs on a screen nobody sees.
        if os.path.normcase(os.path.normpath(cwd or HOME)) == os.path.normcase(HOME):
            raise WakeRefused("the spawn folder is the home folder. "
                              + GATES[0][1].format(host=self.host, cwd=cwd))
        try:
            auth = self.run([claude, "auth", "status"])
            if re.search(r'"loggedIn"\s*:\s*false', auth.stdout or ""):
                raise WakeRefused(f"the Claude CLI on {self.host} is not signed in. "
                                  f"Run `claude auth login` on {self.host}")
        except WakeRefused:
            raise
        except Exception:  # noqa: BLE001  (an older CLI without `auth status`: just try)
            pass
        mid = rq["msg_id"]
        prompt = self.request_prompt(rq, cwd, focus, note, session_id)
        # Named after the conversation it continues: the sender session's title, without
        # the sender's own "[hub-id]" prefix (or an old "[HUB]" marker). Its own id is
        # added by its hook once it is in the app, where sessions can rename themselves.
        title = re.sub(r"^(?:\s*(?:\[HUB\]|\[[0-9a-f]{8}\]))+\s*", "", (rq.get("title") or "").strip())
        name = title or f"#{mid} from {rq.get('frm')}"
        cmd = [claude, "--bg", "--session-id", session_id, "--name", name]
        if self.a.remote_control:
            # Remote Control lists the session in the user's Claude apps (under "Other"), so a
            # session stuck on a prompt can be seen and answered without `claude attach`.
            cmd += ["--remote-control", name]
        if self.a.permission_mode:
            cmd += ["--permission-mode", self.a.permission_mode]
        cmd.append(prompt)
        flags = {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW
        r = subprocess.run(argv(cmd), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=120, cwd=cwd or None, **flags)
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0:
            raise WakeRefused(f"claude --bg exited {r.returncode}: {out[:300]}")
        return out.replace("\n", " ")[:200]

    def spawn_codex(self, session_id: str, address: str, rq: dict, cwd: str = "",
                    focus: str = "", note: str = "") -> str:
        """A new Codex thread for a request: `codex exec`, which keeps it out of the
        desktop app's thread list. It runs one turn and exits; later mail resumes it."""
        cwd = cwd or self.a.spawn_cwd
        if os.path.normcase(os.path.normpath(cwd or HOME)) == os.path.normcase(HOME):
            raise WakeRefused("the spawn folder is the home folder: set site.json spawn.cwd for "
                              f"{self.host} to a project folder")
        p = self.codex_exec([self.request_prompt(rq, cwd, focus, note, session_id)], cwd,
                            f"#{rq['msg_id']}")
        time.sleep(2)  # a start that fails at once (bad flag, no sign-in) says so here
        if p.poll() not in (None, 0):
            raise WakeRefused(f"codex exec exited {p.returncode} at once; see "
                              f"~/.agenthub/codex-exec/#{rq['msg_id']}.log")
        return f"codex exec, pid {p.pid}"

    def request_prompt(self, rq: dict, cwd: str, focus: str, note: str, session_id: str) -> str:
        """The first prompt of a session started for a request (both vendors). Like a
        wake, it carries no message content and no authority: the session reads the
        request from the hub under its own address. The bridge only knows a placeholder
        address for it, so the claim token at the end lets the session's hook adopt the
        request under its real address when this prompt arrives (server.py Store.adopt)."""
        mid = rq["msg_id"]
        frm, tid = rq.get("frm"), rq.get("task_id")
        worker = tid is not None and rq.get("task_parent") is not None
        respawn = (rq.get("note") or "").startswith("respawn:")
        if worker:
            # A Worker: it runs one subtask for the Orchestrator that sent it, which owns
            # every task change (see docs/orchestrators-and-workers.md).
            role = (f"You are a Worker on task #{tid} (\"{(rq.get('task_title') or '')[:80]}\") for its "
                    f"Orchestrator {rq.get('task_owner') or frm}. Tag every hub message with "
                    f"task_id={tid}. Do not create or update hub tasks: when you finish, or are "
                    f"blocked, tell {frm} (RESULT [{tid}] or BLOCKED [{tid}]) and it updates the "
                    f"board. If the work needs another Worker, ask {frm} to add one. Helper "
                    f"subagents that do not use the hub are fine. ")
        else:
            role = ("You are the Orchestrator for this request: open a primary task for it with "
                    "hub_task_create and tag your hub messages with it; if you ask another machine "
                    "or a hub-using subagent for help, add a subtask for that Worker first. ")
        return (f"AgentHub request #{mid} from {frm} (sent to {rq['target']}) is yours: "
                  f"this session was started to handle it. Use the AgentHub address the hook gave "
                  f"you at session start as `as` on every hub call. The request is delivered to you "
                  f"with this prompt (else read it with hub_inbox). " + role +
                  f"Do the work and answer with hub_say(to=[\"{frm}\"], reply_to={mid}). Handle only this "
                  f"request. You started in {cwd}"
                  + (f" (this machine's folder for company {rq['company']})"
                     if rq.get("company") and not focus and not note else "")
                  + (f"; the request is about {focus}, so work there" if focus else "")
                  + (f" (note: {note})" if note else "")
                  + (f". It continues a conversation whose session could not take it "
                     f"({(rq.get('note') or '')[len('respawn: '):][:160]}): read that conversation "
                     f"first with hub_peek around message #{rq.get('reply_to') or mid}"
                     + (f", then tell {frm} you are taking over task #{tid} so it can update the "
                        f"task's worker" if worker else "")
                     if respawn else "")
                  + ". Work wherever the request needs, as far as your access allows. "
                  "If you lack a path, a checkout, access or permission, "
                  "do not work around it -- answer BLOCKED (reply_to the request) saying exactly "
                  "what the user needs to set up." + self.codex_note() + " "
                  # One line: cmd.exe cuts a .cmd shim's arguments at a newline.
                  f"AgentHub claim token: {session_id}")

    def run(self, cmd: list[str], cwd: str = "") -> subprocess.CompletedProcess:
        # stdin=DEVNULL: under pythonw (the Windows autostart) there is no console, and
        # inheriting its invalid stdin handle makes process creation fail.
        flags = {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW
        return subprocess.run(argv(cmd), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=120, cwd=os.path.normpath(cwd) if cwd and os.path.isdir(cwd) else None,
                              **flags)

    def wake_codex(self, session: str, text: str) -> str | None:
        codex = find_codex(self.a.codex)
        if not codex:
            raise WakeRefused("cannot be woken: the codex executable was not found")
        r = self.run([codex, "queue", "--thread", session, "--message", text])
        if r.returncode != 0:
            out = (r.stderr or r.stdout).strip()
            if GONE.search(out):
                raise SessionGone(f"its Codex thread {session} is gone: {out[:200]}")
            raise WakeRefused(f"could not be woken: codex queue exited {r.returncode}: {out[:300]}")
        return r.stdout.strip()[:200]

    def wake_claude(self, session: str, text: str, cwd: str = "") -> str | None:
        claude = find_claude(self.a.claude)
        if not claude:
            raise WakeRefused("cannot be woken: the claude executable was not found")
        # Resuming a session that is already running starts a COPY of it. So only wake a
        # session this machine is not running; an open one that simply is not watching is
        # for a human to prompt.
        host = self.a.addr.split("@")[-1]
        sessions = claude_sessions(claude)
        if sessions is None:
            raise WakeRefused(
                "cannot be woken: `claude agents --json` failed. Check on that machine whether the "
                f"Claude CLI is signed in (claude auth status; fix with claude auth login) and new "
                f"enough (claude --version; waking needs the --bg option). Host: {host}")
        live = [s for s in sessions if (s.get("sessionId") or "").lower() == session.lower()]
        if live:
            s = live[0]
            if (s.get("status") or "").lower() in ("busy", "running"):
                return None  # working; its hooks deliver the mail
            raise WakeRefused(f"has session {session[:8]} open but idle with no AgentHub watch, so "
                              "it cannot be woken without starting a duplicate copy -- send it a "
                              "prompt, or have it arm its watch")
        # From the session's own folder: Claude finds a session by its project folder,
        # and the bridge's own folder (~/.agenthub) is not a trusted workspace.
        r = self.run([claude, "--bg", "--resume", session, text], cwd=cwd)
        out = (r.stdout + r.stderr).strip()
        if "Failed to authenticate" in out or "loggedIn\": false" in out:
            raise WakeRefused(f"could not be woken: the Claude CLI on {host} is not signed in "
                              "(run: claude auth login)")
        if re.search(r"unknown option|unknown argument|unrecognized", out, re.I):
            raise WakeRefused(f"could not be woken: the Claude CLI on {host} is too old for waking "
                              "(it needs --bg / --resume). Update it: npm install -g "
                              f"@anthropic-ai/claude-code. It reported: {out[:160]}")
        if r.returncode != 0:
            raise WakeRefused(f"could not be woken: claude --bg --resume exited {r.returncode}: "
                              f"{out[:300]}")
        return out.replace("\n", " ")[:200]

    def escalate(self, addr: str, mail: list[dict], mids: list[int], why: str) -> None:
        mids = [mid for mid in mids if (addr, mid) not in self.escalated]
        if not mids:
            return
        self.escalated.update((addr, mid) for mid in mids)
        senders = sorted({m["from"] for m in mail if m["id"] in mids and "@" in (m.get("from") or "")})
        to = sorted(set(senders) | set(humans()))
        refs = ", ".join(f"#{mid}" for mid in sorted(mids))
        body = f"Wake bridge: {addr} {why}. Waiting message(s): {refs}."
        self.log(f"ESCALATE to {', '.join(to)}: {body}")
        try:
            self.hub.say(body, to)
        except Exception as e:  # noqa: BLE001
            self.log(f"escalation could not be posted: {e}")


def spawn_conf(a, vendor: str) -> None:
    """Fill in how this bridge starts new sessions, from site.json `spawn`:

      "spawn": {"vendors": ["claude"], "max": 3, "permission_mode": "auto",
                "cwd": {"desk": "D:/Work", "*": "~"}, "anyone": {"*": "claude"}}

    vendors  whose bridges start sessions (Claude: `claude --bg`; Codex: `codex exec`)
    max      hub-started sessions per host still working at once
    cwd      where a new session starts, per host; default the home folder
    anyone   which vendor answers anyone@host, per host
    remote_control  start sessions with Remote Control, so they show in the Claude
             apps (default false: hub sessions are terminal-only and stay out of the
             apps' session lists; `claude agents` / `claude attach` reach them, and
             `claude --desktop --resume <id>` moves one into the desktop app); true/false,
             or per host, e.g. {"desk": true, "*": false}
    """
    conf = load_site().get("spawn") or {}
    host = a.addr.split("@")[1]

    def per_host(d, default):
        if isinstance(d, dict):
            return d.get(host, d.get("*", default))
        return d if d else default

    a.spawn = (not a.no_spawn) and vendor in (conf.get("vendors") or ["claude"])
    # Extra `codex exec` options for the threads it starts: by default approvals are
    # reviewed automatically inside the workspace-write sandbox (Codex's "auto" mode).
    # The agents' shared folder (shared instructions, setup kit), per host; site.json
    # "shared_folder", e.g. "\\\\fileserver\\share\\Agents" or {"lab": "/mnt/agents", "*": ...}.
    sf = load_site().get("shared_folder", "")
    a.shared_folder = per_host(sf, "") if isinstance(sf, dict) else sf
    ca = conf.get("codex_args", ["--approve-for-me"])
    a.codex_args = list(per_host(ca, ["--approve-for-me"]) if isinstance(ca, dict) else ca)
    a.spawn_max = int(conf.get("max") or 3)
    a.permission_mode = conf.get("permission_mode", "auto") or ""
    rc = conf.get("remote_control", False)
    a.remote_control = bool(per_host(rc, False) if isinstance(rc, dict) else rc)
    # normpath: the Claude CLI keys workspace trust by the exact path text, and on
    # Windows only the backslash form (D:\Work) may be the one the user has trusted.
    dirs = per_host(conf.get("cwd"), "~")
    a.spawn_dirs = [os.path.normpath(os.path.expanduser(d)) for d in ([dirs] if isinstance(dirs, str) else dirs)]
    a.spawn_cwd = a.spawn_dirs[0]  # the default; the others may be asked for by `workdir`
    # This machine's company folders (companies.json) pick the folder for a request by
    # its company, and are allowed spawn folders too.
    a.companies = companies()
    for dirs in a.companies.values():
        for d in dirs:
            if os.path.normcase(d) not in {os.path.normcase(x) for x in a.spawn_dirs}:
                a.spawn_dirs.append(d)
    a.anyone_vendor = per_host(conf.get("anyone"), "claude").lower()


def main() -> None:
    ap = argparse.ArgumentParser(description="AgentHub wake bridge")
    ap.add_argument("--as", dest="addr", required=True,
                    help="the agent this bridge wakes, e.g. codex@desk or claude@25")
    ap.add_argument("--codex", help="path to the codex executable; default: found automatically")
    ap.add_argument("--claude", help="path to the claude executable; default: found automatically")
    ap.add_argument("--grace", type=float, default=60.0, help="seconds to wait for acknowledgement")
    ap.add_argument("--debounce", type=float, default=2.0, help="seconds to let a burst of mail settle")
    ap.add_argument("--poll", type=float, default=5.0, help="seconds between state checks")
    ap.add_argument("--busy-stale", type=float, default=120.0,
                    help="treat a 'busy' Codex with no hook activity for this long as idle")
    ap.add_argument("--log", default="", help="default: ~/.agenthub/<vendor>_bridge.log")
    ap.add_argument("--no-spawn", action="store_true",
                    help="never start new sessions for family / anyone@host mail")
    a = ap.parse_args()
    a.addr = a.addr.lower()
    if "@" not in a.addr:
        a.addr = f"{a.addr}@{default_host()}"
    vendor = a.addr.split("@")[0]
    if vendor not in ("codex", "claude"):
        raise SystemExit(f"no wake mechanism for {vendor!r}; --as must name a codex or claude agent")

    log = Log(a.log or os.path.join(AGENT_DIR, f"{vendor}_bridge.log"))
    url, token = credentials()
    if not (url and token):
        log("cannot start: no AGENTHUB_URL/AGENTHUB_TOKEN and no ~/.agenthub/credentials.json")
        sys.exit(1)
    _lock = SingleInstance(a.addr)  # noqa: F841  (held for the life of the process)

    spawn_conf(a, vendor)
    hub = Hub(url, token, a.addr, a.spawn)
    bridge = Bridge(a, hub, log)
    kick, stop = threading.Event(), threading.Event()
    threading.Thread(target=wake_socket_loop, args=(hub, kick, log, stop), daemon=True).start()
    exe = find_codex(a.codex) if vendor == "codex" else find_claude(a.claude)
    log(f"bridge started for {a.addr} against {url}; {vendor}: {exe or 'NOT FOUND'}; "
        + (f"starts sessions (max {a.spawn_max} working, {a.permission_mode or 'default'} mode, "
           f"in {', '.join(a.spawn_dirs)})" if a.spawn else "does not start sessions"))

    try:
        while True:
            try:
                bridge.check()
            except Exception as e:  # noqa: BLE001
                log(f"check failed: {e}")
            kick.wait(a.poll)
            if kick.is_set():
                kick.clear()
                time.sleep(min(a.debounce, 2.0) + 0.1)  # let the burst and the debounce settle
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()
