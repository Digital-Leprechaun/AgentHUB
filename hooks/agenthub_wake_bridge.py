#!/usr/bin/env python3
"""AgentHub wake bridge: wakes a sleeping agent when mail needs it.

One process per agent it serves, on that agent's own machine, because both wake
mechanisms are local:

  Codex   `codex queue --thread <id> --message <text>` drops a follow-up into the
          desktop app's queue, and an idle session starts a turn within seconds.
  Claude  `claude --bg --resume <id> "<text>"` continues that conversation as a
          background session. Only when the session is NOT already running: resuming a
          live session would start a duplicate copy. A Claude whose AgentHub watch is
          connected needs no waking at all, and one sitting in an open session without
          a watch is reported to the humans rather than duplicated.

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
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_DIR = os.path.join(HOME, ".agenthub")
BUSY_EVENTS = {"UserPromptSubmit", "PostToolUse", "PreToolUse"}
LOG_MAX = 1_000_000


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
    return shutil.which("codex")


def find_claude(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    hit = shutil.which("claude")
    if hit:
        return hit
    for cand in (os.path.join(HOME, ".local", "bin", "claude.exe"),
                 os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd")):
        if cand and os.path.exists(cand):
            return cand
    return None


def claude_sessions(claude: str) -> list[dict] | None:
    """Active Claude sessions on this machine (desktop app ones included), or None if
    the list could not be read."""
    try:
        flags = {"creationflags": 0x08000000} if os.name == "nt" else {}
        r = subprocess.run([claude, "agents", "--json"], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=60, **flags)
        if r.returncode != 0:
            return None
        return json.loads(r.stdout or "[]")
    except Exception:  # noqa: BLE001
        return None


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
    def __init__(self, url: str, token: str, addr: str):
        self.url, self.token, self.addr = url, token, addr
        self.bridge_addr = f"{addr}/bridge"

    def pending(self) -> dict:
        q = urllib.parse.urlencode({"as": self.addr})
        req = urllib.request.Request(f"{self.url}/api/pending?{q}")
        req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

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
    path = "/wake?" + urllib.parse.urlencode({"as": hub.bridge_addr})
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


class Bridge:
    def __init__(self, a, hub: Hub, log: Log):
        self.a, self.hub, self.log = a, hub, log
        self.vendor = a.addr.split("@")[0]
        self.woken: dict[int, float] = {}      # message id -> wake time
        self.escalated: set[int] = set()
        self.first_seen: dict[int, float] = {}

    def addressed(self, m: dict) -> bool:
        to = [t.lower() for t in (m.get("to") or [])]
        return self.a.addr in to

    def check(self) -> None:
        state = self.hub.pending()
        agent = state.get("agent") or {}
        mail = [m for m in state.get("unread", []) if self.addressed(m)]
        ids = {m["id"] for m in mail}
        now = time.time()

        # Forget anything Codex has read.
        for store in (self.woken, self.first_seen):
            for mid in [k for k in store if k not in ids]:
                store.pop(mid, None)
        self.escalated &= ids
        if not mail:
            return
        for mid in ids:
            self.first_seen.setdefault(mid, now)

        # Acknowledgement window for wakes already queued (60 seconds).
        last_activity = parse_ts(agent.get("last_event_at"))
        overdue = [mid for mid, t in self.woken.items()
                   if now - t >= self.a.grace and last_activity < t and mid not in self.escalated]
        if overdue:
            self.escalate(mail, overdue, f"did not acknowledge a wake within {int(self.a.grace)} s; "
                                         "the Codex app may be closed or stuck")
            return

        # (Bursts are already settled by the main loop, which pauses after the wake
        # socket fires before it checks.)
        fresh = [m for m in mail if m["id"] not in self.woken and m["id"] not in self.escalated]
        if not fresh:
            return

        if self.vendor == "claude" and agent.get("ws_open"):
            return  # its AgentHub watch is connected and delivers without any wake

        if state.get("stops"):
            self.log(f"{len(fresh)} message(s) waiting, but a STOP applies to {self.a.addr}; not waking")
            return

        event = agent.get("last_event") or ""
        if event in BUSY_EVENTS and now - last_activity < self.a.busy_stale:
            return  # Codex is working; its hooks deliver the mail at the next event.

        session = agent.get("session_id")
        if not session:
            opened = "Codex" if self.vendor == "codex" else "Claude"
            self.escalate(mail, [m["id"] for m in fresh],
                          f"has no registered session, so it cannot be woken (prompt {opened} once "
                          "so its AgentHub hook reports the session)")
            return

        n = len(mail)
        text = (f"AgentHub: {n} message(s) waiting for {self.a.addr}. "
                "Call hub_inbox and handle them.")
        try:
            detail = (self.wake_codex(session, text) if self.vendor == "codex"
                      else self.wake_claude(session, text))
        except WakeRefused as e:
            self.escalate(mail, [m["id"] for m in fresh], str(e))
            return
        except Exception as e:  # noqa: BLE001
            self.escalate(mail, [m["id"] for m in fresh], f"could not be woken: {e}")
            return
        if detail is None:
            return  # nothing to do; another path is already delivering
        for m in fresh:
            self.woken[m["id"]] = now
        self.log(f"woke {self.a.addr} (session {session}) for message(s) "
                 f"{', '.join('#' + str(m['id']) for m in fresh)}: {detail}")

    def run(self, cmd: list[str]) -> subprocess.CompletedProcess:
        # stdin=DEVNULL: under pythonw (the Windows autostart) there is no console, and
        # inheriting its invalid stdin handle makes process creation fail.
        flags = {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW
        return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=120, **flags)

    def wake_codex(self, session: str, text: str) -> str | None:
        codex = find_codex(self.a.codex)
        if not codex:
            raise WakeRefused("cannot be woken: the codex executable was not found")
        r = self.run([codex, "queue", "--thread", session, "--message", text])
        if r.returncode != 0:
            raise WakeRefused(f"could not be woken: codex queue exited {r.returncode}: "
                              f"{(r.stderr or r.stdout).strip()[:300]}")
        return r.stdout.strip()[:200]

    def wake_claude(self, session: str, text: str) -> str | None:
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
        r = self.run([claude, "--bg", "--resume", session, text])
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

    def escalate(self, mail: list[dict], mids: list[int], why: str) -> None:
        mids = [mid for mid in mids if mid not in self.escalated]
        if not mids:
            return
        self.escalated.update(mids)
        senders = sorted({m["from"] for m in mail if m["id"] in mids and "@" in m["from"]})
        to = sorted(set(senders) | set(humans()))
        refs = ", ".join(f"#{mid}" for mid in sorted(mids))
        body = f"Wake bridge: {self.a.addr} {why}. Waiting message(s): {refs}."
        self.log(f"ESCALATE to {', '.join(to)}: {body}")
        try:
            self.hub.say(body, to)
        except Exception as e:  # noqa: BLE001
            self.log(f"escalation could not be posted: {e}")


def main() -> None:
    ap = argparse.ArgumentParser(description="AgentHub wake bridge")
    ap.add_argument("--as", dest="addr", required=True,
                    help="the agent this bridge wakes, e.g. codex@desk or claude@25")
    ap.add_argument("--codex", help="path to the codex executable; default: found automatically")
    ap.add_argument("--claude", help="path to the claude executable; default: found automatically")
    ap.add_argument("--grace", type=float, default=60.0, help="seconds to wait for acknowledgement")
    ap.add_argument("--debounce", type=float, default=2.0, help="seconds to let a burst of mail settle")
    ap.add_argument("--poll", type=float, default=5.0, help="seconds between state checks")
    ap.add_argument("--busy-stale", type=float, default=300.0,
                    help="treat a 'busy' Codex with no hook activity for this long as idle")
    ap.add_argument("--log", default="", help="default: ~/.agenthub/<vendor>_bridge.log")
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

    hub = Hub(url, token, a.addr)
    bridge = Bridge(a, hub, log)
    kick, stop = threading.Event(), threading.Event()
    threading.Thread(target=wake_socket_loop, args=(hub, kick, log, stop), daemon=True).start()
    exe = find_codex(a.codex) if vendor == "codex" else find_claude(a.claude)
    log(f"bridge started for {a.addr} against {url}; {vendor}: {exe or 'NOT FOUND'}")

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
