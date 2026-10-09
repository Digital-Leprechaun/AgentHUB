#!/usr/bin/env python3
"""AgentHub watch: prints one line whenever a message or stop arrives for an agent.

Two ways to run it in Claude Code, both of which wake the session even when it is idle:

  --once, as a background Bash command (preferred). It exits after the first event, so
  the finished command is the wake; the agent reads the output and starts it again.
  Nothing else ever reaches the user's chat:

    Bash({command: "python agenthub_watch.py --as claude@desk --once 2>/dev/null",
          run_in_background: true})

  Under the Monitor tool, which turns each stdout line into a notification. Monitor
  expires every 30 minutes, and the desktop app posts a notice in the chat for every
  expiry, so this is noisy for a watch that runs all day:

    Monitor({command: "python agenthub_watch.py --as claude@desk",
             description: "AgentHub messages for claude@desk",
             timeout_ms: 1800000})

It holds the hub's wake socket open (format=text), so the hub pushes each event the
moment it happens; nothing is polled, and mail already waiting is pushed on connect.
It prints only real events -- connecting, reconnecting and keepalives stay silent
(diagnostics go to stderr), because every stdout line interrupts the agent. It
reconnects on its own if the hub restarts. With --once, losing the hub is not an event
(a relaunched watch would only report it again); the reconnect after an outage is.

Reads AGENTHUB_URL and AGENTHUB_TOKEN from the environment, or else from
~/.agenthub/credentials.json (written by install_hooks.py), so it works however the
harness was started. Standard library only.
"""
from __future__ import annotations

import argparse
import base64
import os
import secrets
import socket
import struct
import sys
import time
import urllib.parse


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


def connect(host: str, port: int, path: str, token: str):
    s = socket.create_connection((host, port), timeout=10)
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
           f"Authorization: Bearer {token}\r\n\r\n")
    s.sendall(req.encode())
    fh = s.makefile("rb")
    status = fh.readline().decode("latin-1").strip()
    while True:
        line = fh.readline()
        if line in (b"\r\n", b"\n", b""):
            break
    if " 101 " not in f" {status} ":
        s.close()
        raise ConnectionError(status or "no response")
    s.settimeout(None)
    return s, fh


def frames(s, fh):
    """Yield text payloads; answer pings; return on close."""
    while True:
        h = fh.read(2)
        if len(h) < 2:
            return
        op, ln = h[0] & 0x0F, h[1] & 0x7F
        if ln == 126:
            ln = struct.unpack("!H", fh.read(2))[0]
        elif ln == 127:
            ln = struct.unpack("!Q", fh.read(8))[0]
        mask = fh.read(4) if h[1] & 0x80 else b""
        data = fh.read(ln) if ln else b""
        if mask:
            data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        if op == 0x8:
            return
        if op == 0x9:  # ping -> masked pong
            m = secrets.token_bytes(4)
            s.sendall(struct.pack("!BB", 0x8A, 0x80 | len(data)) + m +
                      bytes(c ^ m[i % 4] for i, c in enumerate(data)))
            continue
        if op == 0x1:
            yield data.decode("utf-8", "replace")


def processes() -> dict[int, tuple[int, str]]:
    """pid -> (parent pid, lowercased executable name), for every process."""
    out: dict[int, tuple[int, str]] = {}
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Entry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]
        k32 = ctypes.windll.kernel32
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        snap = k32.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
        e = Entry()
        e.dwSize = ctypes.sizeof(Entry)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out[e.th32ProcessID] = (e.th32ParentProcessID, e.szExeFile.lower())
            ok = k32.Process32NextW(snap, ctypes.byref(e))
        k32.CloseHandle(snap)
        return out
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                with open(f"/proc/{d}/stat", encoding="utf-8", errors="replace") as fh:
                    stat = fh.read()
                name = stat[stat.index("(") + 1:stat.rindex(")")].lower()
                ppid = int(stat[stat.rindex(")") + 2:].split()[1])
                out[int(d)] = (ppid, name)
            except (OSError, ValueError):
                pass
    return out


def claude_owner() -> int | None:
    """The Claude Code process this watch was started from, if any (walks up the parents)."""
    procs, pid = processes(), os.getpid()
    for _ in range(12):
        ppid, _name = procs.get(pid, (0, ""))
        if not ppid or ppid not in procs:
            return None
        if procs[ppid][1].startswith("claude"):
            return ppid
        pid = ppid
    return None


def exit_with_owner(owner: int) -> None:
    """A background command outlives the session that started it, so a watch whose
    session has ended would sit connected forever, making a gone session look
    reachable. Leave quietly (nothing on stdout) once the owner is gone."""
    def loop() -> None:
        while True:
            time.sleep(20)
            p = processes().get(owner)
            if not p or not p[1].startswith("claude"):
                log(f"Claude process {owner} has ended; watch exiting")
                os._exit(0)
    import threading
    threading.Thread(target=loop, daemon=True).start()


def main() -> None:
    # Monitor reads our stdout as UTF-8. On Windows Python would otherwise write the
    # console code page, turning any non-ASCII character in a message into mojibake.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="AgentHub watch for Claude Code")
    ap.add_argument("--as", dest="addr", required=True)
    ap.add_argument("--once", action="store_true",
                    help="exit after the first event (for a background command instead of Monitor)")
    a = ap.parse_args()

    def event(text: str) -> None:
        print(text, flush=True)
        if a.once:
            sys.exit(0)

    url = os.environ.get("AGENTHUB_URL", "")
    token = os.environ.get("AGENTHUB_TOKEN", "")
    if not (url and token):
        try:
            import json
            with open(os.path.join(os.path.expanduser("~"), ".agenthub", "credentials.json"),
                      encoding="utf-8") as fh:
                saved = json.load(fh)
            url, token = url or saved.get("url", ""), token or saved.get("token", "")
        except (OSError, ValueError):
            pass
    url = url.rstrip("/")
    if not url or not token:
        print("[AgentHub] watch cannot start: no AGENTHUB_URL/AGENTHUB_TOKEN in the environment "
              "and no ~/.agenthub/credentials.json. Run the machine's register-agenthub script.",
              flush=True)
        sys.exit(1)
    owner = claude_owner()
    if owner:
        exit_with_owner(owner)
    u = urllib.parse.urlparse(url)
    host, port = u.hostname, u.port or 80
    path = f"/wake?{urllib.parse.urlencode({'as': a.addr, 'format': 'text'})}"

    backoff, failures, warned = 1, 0, False
    while True:
        try:
            s, fh = connect(host, port, path, token)
            log(f"connected as {a.addr}")
            backoff, failures = 1, 0
            if warned:
                warned = False
                event("[AgentHub] watch reconnected; notifications are flowing again.")
            for text in frames(s, fh):
                event(text)
            log("socket closed")
        except Exception as e:  # noqa: BLE001
            failures += 1
            log(f"connect failed: {e}")
            if "403" in str(e):
                print(f"[AgentHub] watch refused by the hub for {a.addr}: {e}. Check AGENTHUB_TOKEN.",
                      flush=True)
                sys.exit(1)
            # Tell the agent once if the hub stays unreachable, so silence is not mistaken
            # for an empty inbox.
            if failures == 5 and not warned:
                if not a.once:
                    print(f"[AgentHub] watch has lost the hub at {url}; retrying. Messages may be "
                          "waiting -- check hub_inbox when it returns.", flush=True)
                warned = True
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
