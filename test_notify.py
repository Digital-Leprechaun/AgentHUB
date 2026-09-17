#!/usr/bin/env python3
"""Notification tests: the wake socket's text mode, agenthub_watch.py and agenthub_hook.py.

Starts its own hub on a scratch port with scratch tokens, so it never touches live
data.

  python3 test_notify.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("TEST_PORT", "8795"))
BASE = f"http://127.0.0.1:{PORT}"
TOK = {"*@desk": "t-desk", "*@11": "t-11", "human@hub": "t-human"}
FAILS = []


def tok_for(addr):
    fam = addr.split("/")[0]
    return TOK.get(fam) or TOK.get("*@" + fam.split("@")[1], "")


def call(tool, args, token=None):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": args}}).encode()
    req = urllib.request.Request(f"{BASE}/mcp", data=body, headers={"Content-Type": "application/json"})
    req.add_header("Authorization", f"Bearer {token or tok_for(args.get('as', ''))}")
    with urllib.request.urlopen(req, timeout=20) as r:
        res = json.loads(r.read())["result"]
    text = res["content"][0]["text"]
    return {"_error": text} if res.get("isError") else json.loads(text)


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


class Watch:
    """Runs agenthub_watch.py and collects its stdout lines."""

    def __init__(self, addr, token):
        env = dict(os.environ, AGENTHUB_URL=BASE, AGENTHUB_TOKEN=token)
        self.p = subprocess.Popen([sys.executable, os.path.join(HERE, "hooks", "agenthub_watch.py"),
                                   "--as", addr], env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        self.lines = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            self.lines.append(line.rstrip("\n"))

    def settle(self, secs=1.2):
        time.sleep(secs)
        return list(self.lines)

    def stop(self):
        self.p.terminate()
        self.p.wait(timeout=5)


def hook(addr, event, token=None, extra_env=None, stdin_extra=None):
    env = dict(os.environ, AGENTHUB_URL=BASE, AGENTHUB_TOKEN=token or tok_for(addr),
               AGENTHUB_EVERY="5", TMPDIR=TMPDIR, TEMP=TMPDIR, TMP=TMPDIR)
    env.update(extra_env or {})
    payload = {"hook_event_name": event, "session_id": "s1", "cwd": HERE}
    payload.update(stdin_extra or {})
    r = subprocess.run([sys.executable, os.path.join(HERE, "hooks", "agenthub_hook.py"), "--as", addr],
                       input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=20)
    out = r.stdout.strip()
    return r.returncode, (json.loads(out) if out else None)


tmp = tempfile.mkdtemp(prefix="agenthub-notify-")
TMPDIR = os.path.join(tmp, "hooktmp")
os.makedirs(TMPDIR)
tokens = os.path.join(tmp, "tokens.json")
json.dump(TOK, open(tokens, "w"))
env = dict(os.environ, HUB_PORT=str(PORT), HUB_DB=os.path.join(tmp, "t.db"), HUB_TOKENS_FILE=tokens,
           HUB_WEB_DIR=os.path.join(HERE, "web"))
proc = subprocess.Popen([sys.executable, os.path.join(HERE, "server.py")], env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
watches = []
try:
    for _ in range(50):
        try:
            urllib.request.urlopen(f"{BASE}/health", timeout=1).read()
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    else:
        sys.exit("hub did not start")
    print(f"notification tests against {BASE}\n")

    for a in ("claude@desk", "codex@desk", "claude@11", "codex@11"):
        call("hub_hello", {"as": a})

    print("watch: quiet connect")
    w = Watch("claude@desk", TOK["*@desk"]); watches.append(w)
    sub = Watch("claude@desk/reviewer", TOK["*@desk"]); watches.append(sub)
    check("nothing printed when nothing is waiting", w.settle(1.5) == [], w.lines)

    print("\nwatch: one line per message that is for this agent")
    call("hub_say", {"as": "codex@11", "body": "Direct to ClaudeDesk.", "to": ["claude@desk"]})
    lines = w.settle()
    check("a direct message prints exactly one line", len(lines) == 1, lines)
    check("the line names the sender and says what to do",
          lines and "from codex@11" in lines[0] and "hub_inbox" in lines[0], lines)
    call("hub_say", {"as": "codex@11", "body": "Broadcast on the default topic."})
    check("a default-topic broadcast notifies", len(w.settle()) == 2, w.lines)
    call("hub_say", {"as": "codex@11", "body": "Chatter on a topic nobody here follows.", "topic": "ue5-shaders"})
    check("a named-topic message does NOT wake a non-subscriber", len(w.settle()) == 2, w.lines)
    call("hub_subscribe", {"as": "claude@desk", "topic": "ue5-shaders"})
    call("hub_say", {"as": "codex@11", "body": "Now subscribed.", "topic": "ue5-shaders"})
    check("...but does wake a subscriber", len(w.settle()) == 3, w.lines)
    call("hub_say", {"as": "claude@11", "body": "For the reviewer only.", "to": ["claude@desk/reviewer"]})
    check("a message for a subagent does not wake its parent", len(w.settle()) == 3, w.lines)
    before = len(sub.lines)
    call("hub_say", {"as": "claude@11", "body": "For the whole family.", "to": ["claude@desk"]})
    time.sleep(1.2)
    fam_lines = [l for l in sub.lines[before:] if "whole family" in l]
    check("a family message wakes the subagent exactly once (no duplicate)", len(fam_lines) == 1, sub.lines)
    call("hub_say", {"as": "codex@11", "body": "Codex11 here—dash, café, ✓.", "to": ["claude@desk"]})
    lines = w.settle()
    check("non-ASCII survives the watch as UTF-8 (em dash, accents, symbols)",
          any("Codex11 here—dash, café, ✓." in l for l in lines), lines[-1:])
    call("hub_say", {"as": "claude@desk", "body": "Talking to myself.", "to": ["claude@desk"]})
    check("an agent is not woken by its own message", not any("myself" in l for l in w.settle()), w.lines)

    print("\nwatch: stops")
    s = call("hub_stop", {"as": "human@hub", "reason": "notify test"})
    lines = w.settle()
    check("STOP NOW prints a HALT line", any("STOP NOW" in l and "HALT" in l for l in lines), lines)
    call("hub_resume", {"as": "human@hub", "id": s["stop"]["id"]})
    check("the lift prints a line", any("lifted" in l for l in w.settle()), w.lines)

    print("\nwatch --once: exits after the first event")
    once_cmd = [sys.executable, os.path.join(HERE, "hooks", "agenthub_watch.py"), "--as", "claude@11", "--once"]
    once_env = dict(os.environ, AGENTHUB_URL=BASE, AGENTHUB_TOKEN=TOK["*@11"])
    call("hub_inbox", {"as": "claude@11"})  # start with nothing waiting
    once = subprocess.Popen(once_cmd, env=once_env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="replace")
    time.sleep(1.5)
    check("--once keeps waiting while nothing is waiting", once.poll() is None)
    call("hub_say", {"as": "codex@11", "body": "First of two.", "to": ["claude@11"]})
    call("hub_say", {"as": "codex@11", "body": "Second of two.", "to": ["claude@11"]})
    try:
        out, _ = once.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        once.kill()
        out, _ = once.communicate()
    once_lines = [l for l in out.splitlines() if l.strip()]
    check("--once exits 0 after an event", once.returncode == 0, once.returncode)
    check("--once prints exactly one line", len(once_lines) == 1 and "First of two" in once_lines[0], once_lines)
    try:
        r = subprocess.run(once_cmd, env=once_env, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=5)
        relaunch = r.stdout
    except subprocess.TimeoutExpired:
        relaunch = None
    check("relaunched before the inbox is read, --once reports the waiting mail at once",
          relaunch is not None and "unread" in relaunch, relaunch)
    call("hub_inbox", {"as": "claude@11"})

    print("\nwatch: connecting with mail already waiting")
    call("hub_say", {"as": "claude@11", "body": "Sent while Codex11 had no watch.", "to": ["codex@11"]})
    w2 = Watch("codex@11", TOK["*@11"]); watches.append(w2)
    lines = w2.settle(1.5)
    check("a new watch reports the waiting mail once", len(lines) == 1 and "unread" in lines[0], lines)

    print("\nwatch: bad token")
    bad = Watch("claude@11", "wrong-token"); watches.append(bad)
    time.sleep(1.5)
    check("a refused watch says so and exits",
          bad.p.poll() is not None and any("refused" in l for l in bad.lines), (bad.p.poll(), bad.lines))

    print("\nhook")
    call("hub_inbox", {"as": "codex@desk"})  # start codex@desk with an empty inbox
    code, out = hook("codex@desk", "UserPromptSubmit")
    check("empty inbox: no output, exit 0", code == 0 and out is None, (code, out))
    call("hub_say", {"as": "claude@11", "body": "Codex, the cook finished.", "to": ["codex@desk"]})
    code, out = hook("codex@desk", "SessionStart")
    ctx = (out or {}).get("hookSpecificOutput", {})
    check("SessionStart injects the waiting message as additionalContext",
          ctx.get("hookEventName") == "SessionStart" and "cook finished" in ctx.get("additionalContext", ""), out)
    code, out = hook("codex@desk", "UserPromptSubmit")
    check("the mail was marked read, so it is not injected twice", out is None, out)

    call("hub_say", {"as": "claude@11", "body": "Mid-task ping.", "to": ["codex@desk"]})
    code, out = hook("codex@desk", "PostToolUse")
    check("PostToolUse injects mid-task mail",
          "Mid-task ping" in (out or {}).get("hookSpecificOutput", {}).get("additionalContext", ""), out)
    call("hub_say", {"as": "claude@11", "body": "Second ping inside the rate window.", "to": ["codex@desk"]})
    code, out = hook("codex@desk", "PostToolUse")
    check("PostToolUse inside the rate window does not fetch the inbox", out is None, out)

    code, out = hook("codex@desk", "Stop")
    check("Stop with mail waiting blocks the stop",
          (out or {}).get("decision") == "block" and "Second ping" in (out or {}).get("reason", ""), out)
    code, out = hook("codex@desk", "Stop", stdin_extra={"stop_hook_active": True})
    check("the next Stop is allowed (mail already delivered, no loop)", out is None, out)

    s = call("hub_stop", {"as": "human@hub", "reason": "hook stop test"})
    code, out = hook("codex@desk", "PostToolUse")
    check("a STOP reaches PostToolUse even inside the rate window",
          "STOP IN FORCE" in (out or {}).get("hookSpecificOutput", {}).get("additionalContext", ""), out)
    call("hub_resume", {"as": "human@hub", "id": s["stop"]["id"]})

    print("\ncredentials file fallback (harness started without the env vars)")
    fake_home = os.path.join(tmp, "home")
    os.makedirs(os.path.join(fake_home, ".agenthub"))
    with open(os.path.join(fake_home, ".agenthub", "credentials.json"), "w") as fh:
        json.dump({"url": BASE, "token": TOK["*@desk"]}, fh)
    bare = {k: v for k, v in os.environ.items() if k not in ("AGENTHUB_URL", "AGENTHUB_TOKEN")}
    bare.update(HOME=fake_home, USERPROFILE=fake_home, TMPDIR=TMPDIR, TEMP=TMPDIR, TMP=TMPDIR)
    call("hub_say", {"as": "claude@11", "body": "Found via the credentials file.", "to": ["codex@desk"]})
    r = subprocess.run([sys.executable, os.path.join(HERE, "hooks", "agenthub_hook.py"), "--as", "codex@desk"],
                       input=json.dumps({"hook_event_name": "UserPromptSubmit"}), capture_output=True,
                       text=True, env=bare, timeout=20)
    check("hook with no env vars reads ~/.agenthub/credentials.json", "credentials file" in r.stdout, r.stdout)
    wf = subprocess.Popen([sys.executable, os.path.join(HERE, "hooks", "agenthub_watch.py"), "--as", "claude@desk"],
                          env=bare, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    time.sleep(2)
    check("watch with no env vars connects using the credentials file", wf.poll() is None)
    wf.terminate(); wf.wait(timeout=5)

    code, out = hook("codex@desk", "UserPromptSubmit", token="wrong")
    check("a bad token stays silent and exits 0", code == 0 and out is None, (code, out))
    code, out = hook("codex@desk", "UserPromptSubmit", extra_env={"AGENTHUB_URL": "http://127.0.0.1:9"})
    check("an unreachable hub stays silent and exits 0", code == 0 and out is None, (code, out))

    print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILURE(S): " + ", ".join(FAILS)))
finally:
    for w in watches:
        try:
            w.stop()
        except Exception:  # noqa: BLE001
            pass
    proc.terminate()
    proc.wait(timeout=10)
    shutil.rmtree(tmp, ignore_errors=True)

sys.exit(1 if FAILS else 0)
