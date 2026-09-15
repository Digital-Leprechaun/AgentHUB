#!/usr/bin/env python3
"""Token granularity tests: host-wide tokens, per-vendor tokens, and the mix.

Starts its own hub on a scratch port with a scratch tokens file, so it never
touches the live database or the real tokens.

  python3 test_auth.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

PORT = int(os.environ.get("TEST_PORT", "8799"))
BASE = f"http://127.0.0.1:{PORT}"
FAILS = []
_id = [0]

TOKENS = {
    # One Linux box shares one token across every agent on the box.
    "*@11": "host-wide-secret",
    # Desk issues per-vendor tokens...
    "claude@desk": "desk-claude-secret",
    "grok@desk": "desk-grok-secret",
    # ...and also a host fallback, so codex@desk has no entry of its own.
    "*@desk": "desk-host-secret",
}


def rpc(method, params, token):
    _id[0] += 1
    body = json.dumps({"jsonrpc": "2.0", "id": _id[0], "method": method,
                       "params": params}).encode()
    req = urllib.request.Request(f"{BASE}/mcp", data=body,
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read() or b"{}")


def hello(addr, token):
    """True when the hub accepts `addr` presented with `token`."""
    r = rpc("tools/call", {"name": "hub_hello", "arguments": {"as": addr}}, token)
    return not (r.get("result") or {}).get("isError", False)


def check(label, cond):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILS.append(label)


tmp = tempfile.mkdtemp(prefix="agenthub-auth-")
tokens_path = os.path.join(tmp, "tokens.json")
with open(tokens_path, "w", encoding="utf-8") as fh:
    json.dump(TOKENS, fh)

env = dict(os.environ, HUB_PORT=str(PORT), HUB_DB=os.path.join(tmp, "t.db"),
           HUB_TOKENS_FILE=tokens_path,
           HUB_WEB_DIR=os.path.join(os.path.dirname(os.path.abspath(__file__)), "web"))
proc = subprocess.Popen([sys.executable, "server.py"], env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(50):
        try:
            urllib.request.urlopen(f"{BASE}/health", timeout=1).read()
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    else:
        sys.exit("hub did not start")

    print(f"token granularity tests against {BASE}\n")

    print("host-wide token (*@11)")
    check("covers claude on that host", hello("claude@11", "host-wide-secret"))
    check("covers codex on that host", hello("codex@11", "host-wide-secret"))
    check("covers a subagent too", hello("codex@11/tests", "host-wide-secret"))
    check("does NOT cover another host",
          not hello("claude@desk", "host-wide-secret"))
    check("wrong token still rejected", not hello("claude@11", "nope"))

    print("\nper-vendor tokens (desk)")
    check("claude token works for claude", hello("claude@desk", "desk-claude-secret"))
    check("grok token works for grok", hello("grok@desk", "desk-grok-secret"))
    check("claude token does NOT work for grok",
          not hello("grok@desk", "desk-claude-secret"))

    print("\nexact entry beats the host wildcard")
    check("host token rejected for a vendor with its own entry",
          not hello("claude@desk", "desk-host-secret"))
    check("host token accepted for a vendor without one (codex@desk)",
          hello("codex@desk", "desk-host-secret"))

    print("\nunknown hosts")
    check("unknown host rejected with any token", not hello("claude@strangerbox", "desk-host-secret"))
    check("no token at all is rejected", not hello("claude@desk", ""))

    print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILURE(S): " + ", ".join(FAILS)))
finally:
    proc.terminate()
    proc.wait(timeout=10)
    shutil.rmtree(tmp, ignore_errors=True)

sys.exit(1 if FAILS else 0)
