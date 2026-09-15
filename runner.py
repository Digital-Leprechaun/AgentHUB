#!/usr/bin/env python3
"""AgentHub runner: the piece that lets the hub wake an IDLE agent (tier 3).

Runs on each PC, one per agent family. It holds a wake socket open to the hub and,
when a message arrives for that agent while no session is working, runs a command
of your choosing. That command is what actually starts or resumes the CLI -- the
runner deliberately does not manage agent processes itself, it just pulls the
trigger you configure.

Standard library only, so it runs anywhere the CLIs do.

  # just watch and print (safe first run)
  python runner.py --as claude@desk --hub http://HUB_HOST:8787

  # wake a headless Claude Code session with the message on stdin
  python runner.py --as claude@desk --hub http://HUB_HOST:8787 \
      --exec "claude -p --permission-mode acceptEdits"

  # Windows, resuming the most recent session in a new terminal
  python runner.py --as claude@desk --hub http://HUB_HOST:8787 ^
      --exec "wt.exe -w 0 nt claude --continue"

The message JSON is passed on stdin and in AGENTHUB_* environment variables, so
the command can use either. Bursts inside --debounce seconds fire once.

Transport is a WebSocket by default; --poll falls back to HTTP long-polling for
networks where the socket will not stay up.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


# ----------------------------------------------------------------------------
# minimal websocket client
# ----------------------------------------------------------------------------
class WsClient:
    def __init__(self, url: str, token: str = ""):
        u = urllib.parse.urlparse(url)
        self.secure = u.scheme in ("wss", "https")
        self.host = u.hostname or "127.0.0.1"
        self.port = u.port or (443 if self.secure else 80)
        self.path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        self.token = token
        self.sock: socket.socket | None = None
        self.fh = None

    def connect(self, timeout: float = 10) -> None:
        s = socket.create_connection((self.host, self.port), timeout=timeout)
        if self.secure:
            s = ssl.create_default_context().wrap_socket(s, server_hostname=self.host)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        req = [
            f"GET {self.path} HTTP/1.1",
            f"Host: {self.host}:{self.port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        if self.token:
            req.append(f"Authorization: Bearer {self.token}")
        s.sendall(("\r\n".join(req) + "\r\n\r\n").encode())
        fh = s.makefile("rb")
        status = fh.readline().decode("latin-1").strip()
        if "101" not in status:
            rest = b""
            while True:
                line = fh.readline()
                if line in (b"\r\n", b"", b"\n"):
                    break
                rest += line
            raise ConnectionError(f"upgrade refused: {status} {rest.decode('latin-1', 'replace')[:200]}")
        while True:
            line = fh.readline()
            if line in (b"\r\n", b"", b"\n"):
                break
        s.settimeout(None)
        self.sock, self.fh = s, fh

    def send(self, text: str) -> None:
        """Client frames must be masked (RFC 6455 5.3)."""
        data = text.encode()
        mask = secrets.token_bytes(4)
        n = len(data)
        if n < 126:
            hdr = struct.pack("!BB", 0x81, 0x80 | n)
        elif n < 65536:
            hdr = struct.pack("!BBH", 0x81, 0x80 | 126, n)
        else:
            hdr = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
        payload = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        self.sock.sendall(hdr + mask + payload)

    def recv(self) -> tuple[int, bytes] | None:
        try:
            h = self.fh.read(2)
            if len(h) < 2:
                return None
            opcode = h[0] & 0x0F
            masked = h[1] & 0x80
            ln = h[1] & 0x7F
            if ln == 126:
                ln = struct.unpack("!H", self.fh.read(2))[0]
            elif ln == 127:
                ln = struct.unpack("!Q", self.fh.read(8))[0]
            mask = self.fh.read(4) if masked else b""
            payload = self.fh.read(ln) if ln else b""
            if masked:
                payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
            return opcode, payload
        except Exception:  # noqa: BLE001
            return None

    def pong(self) -> None:
        mask = secrets.token_bytes(4)
        self.sock.sendall(struct.pack("!BB", 0x8A, 0x80) + mask)

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:  # noqa: BLE001
            pass


# ----------------------------------------------------------------------------
# runner
# ----------------------------------------------------------------------------
class Runner:
    def __init__(self, args):
        self.a = args
        self.pending: list[dict] = []
        self.lock = threading.Lock()
        self.timer: threading.Timer | None = None
        self.child: subprocess.Popen | None = None
        self.stops: dict[int, dict] = {}  # stops in force for this agent

    def busy(self) -> bool:
        return self.child is not None and self.child.poll() is None

    def on_stop(self, stop: dict) -> None:
        """A stop applies to us: halt what we launched and wake nothing until it lifts."""
        self.stops[stop["id"]] = stop
        what = "STOP NOW" if stop.get("scope") == "all" else f"stop on {stop.get('target')}"
        log(f"!! stop #{stop['id']} {what} by {stop.get('issuer')}: {stop.get('reason')}")
        with self.lock:
            self.pending.clear()
            if self.timer:
                self.timer.cancel()
                self.timer = None
        if self.busy() and not self.a.no_kill_on_stop:
            log(f"   terminating the session this runner launched (pid {self.child.pid}) and its children")
            self.kill_tree(self.child)

    @staticmethod
    def kill_tree(child: subprocess.Popen) -> None:
        """--exec runs through a shell, so the agent is a grandchild: killing only the
        shell would orphan it. The session is started as its own process group so the
        whole tree can be ended."""
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(child.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            import signal
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass

    def on_resume(self, stop: dict) -> None:
        self.stops.pop(stop["id"], None)
        log(f"-- stop #{stop['id']} lifted by {stop.get('lifted_by')}"
            + ("" if self.stops else "; wakes re-enabled"))

    def on_message(self, msg: dict) -> None:
        who, body = msg.get("from", "?"), (msg.get("body") or "").replace("\n", " ")
        log(f"<- #{msg.get('id')} {who}: {body[:110]}")
        with self.lock:
            self.pending.append(msg)
            if self.timer:
                self.timer.cancel()
            self.timer = threading.Timer(self.a.debounce, self.fire)
            self.timer.daemon = True
            self.timer.start()

    def fire(self) -> None:
        with self.lock:
            batch, self.pending, self.timer = self.pending, [], None
        if not batch:
            return
        if self.stops:
            log(f"   ({len(batch)} message(s); a stop is in force, not waking)")
            return
        if not self.a.exec:
            log(f"   ({len(batch)} message(s); no --exec configured, nothing launched)")
            return
        if self.busy() and not self.a.allow_concurrent:
            log(f"   ({len(batch)} message(s); a session is already running, skipping wake)")
            return
        lines = [f"[AgentHub] {len(batch)} message(s) for {self.a.addr}:"]
        for m in batch:
            topic = f" #{m['topic']}" if m.get("topic") else ""
            lines.append(f"  #{m.get('id')} {m.get('from')}{topic}: {m.get('body')}")
        lines.append("Use hub_peek for context and hub_say to reply.")
        text = "\n".join(lines)
        env = dict(os.environ)
        env.update({
            "AGENTHUB_URL": self.a.hub,
            "AGENTHUB_AS": self.a.addr,
            "AGENTHUB_COUNT": str(len(batch)),
            "AGENTHUB_FROM": batch[-1].get("from", ""),
            "AGENTHUB_TEXT": text,
        })
        log(f"-> waking: {self.a.exec}")
        try:
            group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                     else {"start_new_session": True})
            self.child = subprocess.Popen(
                self.a.exec, shell=True, env=env,
                stdin=subprocess.PIPE if self.a.stdin else None, **group,
            )
            if self.a.stdin and self.child.stdin:
                self.child.stdin.write(text.encode())
                self.child.stdin.close()
        except Exception as e:  # noqa: BLE001
            log(f"   wake command failed: {e}")

    # -- transports ---------------------------------------------------------
    def run_ws(self) -> None:
        url = self.a.hub.replace("http://", "ws://").replace("https://", "wss://")
        url = f"{url}/wake?as={urllib.parse.quote(self.a.addr)}"
        backoff = 1
        while True:
            ws = WsClient(url, self.a.token)
            try:
                ws.connect()
                log(f"connected to {self.a.hub} as {self.a.addr}")
                backoff = 1
                while True:
                    frame = ws.recv()
                    if frame is None:
                        break
                    opcode, payload = frame
                    if opcode == 0x9:
                        ws.pong()
                        continue
                    if opcode == 0x8:
                        break
                    if opcode != 0x1:
                        continue
                    try:
                        ev = json.loads(payload.decode())
                    except json.JSONDecodeError:
                        continue
                    if ev.get("type") == "message":
                        self.on_message(ev["message"])
                    elif ev.get("type") == "backlog":
                        for m in ev.get("messages", []):
                            self.on_message(m)
                    elif ev.get("type") == "ready":
                        log(f"ready; {ev.get('unread', 0)} unread")
                        self.stops.clear()
                        for st in ev.get("stops", []):
                            self.on_stop(st)
                    elif ev.get("type") == "stop":
                        self.on_stop(ev["stop"])
                    elif ev.get("type") == "resume":
                        self.on_resume(ev["stop"])
            except Exception as e:  # noqa: BLE001
                log(f"connection failed: {e}")
            finally:
                ws.close()
            log(f"reconnecting in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)

    def run_poll(self) -> None:
        log(f"long-polling {self.a.hub} as {self.a.addr}")
        url = (f"{self.a.hub}/hook/poll?as={urllib.parse.quote(self.a.addr)}"
               f"&wait={self.a.wait}")
        while True:
            try:
                req = urllib.request.Request(url)
                if self.a.token:
                    req.add_header("Authorization", f"Bearer {self.a.token}")
                with urllib.request.urlopen(req, timeout=self.a.wait + 15) as r:
                    text = r.read().decode("utf-8", "replace").strip()
                # Polling has no structured stop events, so read the hub's STOP banner.
                stopped = "STOP IN FORCE" in text
                if stopped and not self.stops:
                    self.on_stop({"id": 0, "scope": "?", "issuer": "hub", "reason": text})
                elif not stopped and self.stops:
                    self.on_resume({"id": 0, "lifted_by": "hub"})
                if stopped:
                    time.sleep(5)  # the hub answers at once while stopped; don't spin
                elif text:
                    self.on_message({"from": "hub", "body": text, "id": "-", "topic": ""})
            except Exception as e:  # noqa: BLE001
                log(f"poll failed: {e}; retrying in 5s")
                time.sleep(5)


def main() -> None:
    p = argparse.ArgumentParser(description="AgentHub wake runner")
    p.add_argument("--as", dest="addr", required=True, help="this agent's address, e.g. claude@desk")
    p.add_argument("--hub", default=os.environ.get("AGENTHUB_URL", "http://127.0.0.1:8787"))
    p.add_argument("--token", default=os.environ.get("AGENTHUB_TOKEN", ""))
    p.add_argument("--exec", default="", help="command to run when a message arrives (omit to just watch)")
    p.add_argument("--debounce", type=float, default=3.0, help="seconds to batch a burst, default 3")
    p.add_argument("--wait", type=int, default=55, help="long-poll seconds when --poll, default 55")
    p.add_argument("--poll", action="store_true", help="use HTTP long-polling instead of a websocket")
    p.add_argument("--stdin", action="store_true", default=True, help="pipe the message text to the command")
    p.add_argument("--no-stdin", dest="stdin", action="store_false")
    p.add_argument("--allow-concurrent", action="store_true",
                   help="wake even when a previous wake is still running")
    p.add_argument("--no-kill-on-stop", action="store_true",
                   help="on a STOP, stop waking but leave an already-launched session running")
    a = p.parse_args()
    a.hub = a.hub.rstrip("/")
    r = Runner(a)
    try:
        (r.run_poll if a.poll else r.run_ws)()
    except KeyboardInterrupt:
        log("bye")
        sys.exit(0)


if __name__ == "__main__":
    main()
