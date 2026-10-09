#!/usr/bin/env python3
"""Codex wake bridge tests, against a scratch hub and a fake `codex`.

The fake records every `codex queue` call instead of touching a real Codex. Runs on
Linux and Windows. Never touches live data.

  python3 test_bridge.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("TEST_PORT", "8793"))
BASE = f"http://127.0.0.1:{PORT}"
TOK = {"*@desk": "t-desk", "*@11": "t-11", "human@hub": "t-human"}
GRACE = 3.0
FAILS = []


def tok_for(addr):
    if "@" not in (addr or ""):
        return ""
    fam = addr.split("/")[0]
    return TOK.get(fam) or TOK.get("*@" + fam.split("@")[1], "")


def call(tool, args):
    if tool == "hub_say" and "task_id" not in args and "reply_to" not in args and not args.pop("_notask", False):
        # Every hub message needs a task: file untagged test mail under one shared task.
        args = dict(args, task_id=_test_task())
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": args}}).encode()
    req = urllib.request.Request(f"{BASE}/mcp", data=body, headers={"Content-Type": "application/json"})
    req.add_header("Authorization", f"Bearer {tok_for(args.get('as', ''))}")
    with urllib.request.urlopen(req, timeout=20) as r:
        res = json.loads(r.read())["result"]
    text = res["content"][0]["text"]
    return {"_error": text} if res.get("isError") else json.loads(text)


_TASK = []


def _test_task():
    """A task for test mail that does not care which task it is under (made once, by a human)."""
    if not _TASK:
        _TASK.append(call("hub_task_create", {"as": "human@hub", "title": "test traffic"})["task"]["id"])
    return _TASK[0]


def hook_event(addr, event, session="", consume=False):
    """What agenthub_hook.py sends. consume=False reports the event without reading mail."""
    q = {"as": addr, "event": event}
    if session:
        q["session"] = session
    if not consume:
        q["stop_only"] = "1"
    req = urllib.request.Request(f"{BASE}/hook/poll?{urllib.parse.urlencode(q)}")
    req.add_header("Authorization", f"Bearer {tok_for(addr)}")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode()


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def codex_calls():
    try:
        with open(CALLS, encoding="utf-8") as fh:
            return [json.loads(l) for l in fh if l.strip()]
    except FileNotFoundError:
        return []


def queue_calls():
    return [c for c in codex_calls() if c[:1] == ["queue"]]


def exec_calls():
    # On Windows the bridge puts `-c windows.sandbox="unelevated"` before `exec`.
    out = []
    for c in codex_calls():
        if c[:1] == ["-c"] and len(c) > 1 and c[1].replace("'", '"') == 'windows.sandbox="unelevated"':
            c = c[2:]
        if c[:1] == ["exec"]:
            out.append(c)
    return out


def escalations(who="codex@desk/bridge"):
    msgs = call("hub_peek", {"limit": 200})["messages"]
    return [m for m in msgs if m["from"] == who]


def wait_for(pred, secs):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.2)
    return pred()


def say_to_codex(body, sender="claude@11"):
    return call("hub_say", {"as": sender, "body": body, "to": ["codex@desk"]})["posted"]


tmp = tempfile.mkdtemp(prefix="agenthub-bridge-")
home = os.path.join(tmp, "home")
os.makedirs(home)
CALLS = os.path.join(tmp, "codex_calls.jsonl")
EXITFILE = os.path.join(tmp, "codex_exit")
GONEFILE = os.path.join(tmp, "codex_gone")  # thread ids Codex reports archived, one per line

fake_py = os.path.join(tmp, "fakecodex.py")
with open(fake_py, "w", encoding="utf-8") as fh:
    fh.write(
        "import json, os, sys\n"
        "if sys.argv[1:3] == ['login', 'status']:\n"
        "    sys.stderr.write('Logged in using ChatGPT'); sys.exit(0)\n"
        f"open({CALLS!r}, 'a', encoding='utf-8').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:2] == ['exec']:\n"
        "    sys.exit(0)\n"
        "thread = sys.argv[3] if len(sys.argv) > 3 else ''\n"
        f"if os.path.exists({GONEFILE!r}) and thread in open({GONEFILE!r}).read().split():\n"
        "    sys.stderr.write('Error: failed to queue session message: thread/queue/add failed: '\n"
        "                     f'session {thread} is archived. Run `codex unarchive {thread}` first.')\n"
        "    sys.exit(1)\n"
        f"code = int(open({EXITFILE!r}).read()) if os.path.exists({EXITFILE!r}) else 0\n"
        "print('Queued message fake for thread ' + (sys.argv[3] if len(sys.argv) > 3 else '?'))\n"
        "sys.exit(code)\n")
if os.name == "nt":
    fake = os.path.join(tmp, "codex.cmd")
    with open(fake, "w") as fh:
        fh.write(f'@"{sys.executable}" "{fake_py}" %*\n')
else:
    fake = os.path.join(tmp, "codex")
    with open(fake, "w") as fh:
        fh.write(f'#!/bin/sh\nexec "{sys.executable}" "{fake_py}" "$@"\n')
    os.chmod(fake, 0o755)

CLAUDE_CALLS = os.path.join(tmp, "claude_calls.jsonl")
SESSIONS = os.path.join(tmp, "claude_sessions.json")
AUTHFAIL = os.path.join(tmp, "claude_authfail")
json.dump([], open(SESSIONS, "w"))

fake_claude_py = os.path.join(tmp, "fakeclaude.py")
with open(fake_claude_py, "w", encoding="utf-8") as fh:
    fh.write(
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "if args[:1] == ['agents']:\n"
        f"    sys.stdout.write(open({SESSIONS!r}).read()); sys.exit(0)\n"
        f"open({CLAUDE_CALLS!r}, 'a', encoding='utf-8').write(json.dumps(args) + '\\n')\n"
        f"if os.path.exists({AUTHFAIL!r}):\n"
        "    print('Failed to authenticate: OAuth session expired and could not be refreshed')\n"
        "    sys.exit(1)\n"
        "print('backgrounded - fake')\n")
if os.name == "nt":
    fake_claude = os.path.join(tmp, "claude.cmd")
    with open(fake_claude, "w") as fh:
        fh.write(f'@"{sys.executable}" "{fake_claude_py}" %*\n')
else:
    fake_claude = os.path.join(tmp, "claude")
    with open(fake_claude, "w") as fh:
        fh.write(f'#!/bin/sh\nexec "{sys.executable}" "{fake_claude_py}" "$@"\n')
    os.chmod(fake_claude, 0o755)

procs = []
tokens = os.path.join(tmp, "tokens.json")
json.dump(TOK, open(tokens, "w"))
hub = subprocess.Popen([sys.executable, os.path.join(HERE, "server.py")],
                       env=dict(os.environ, HUB_PORT=str(PORT), HUB_DB=os.path.join(tmp, "t.db"),
                                HUB_TOKENS_FILE=tokens, HUB_WEB_DIR=os.path.join(HERE, "web")),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
benv = dict(os.environ, AGENTHUB_URL=BASE, AGENTHUB_TOKEN=TOK["*@desk"], HOME=home, USERPROFILE=home)
bridge_cmd = [sys.executable, os.path.join(HERE, "hooks", "agenthub_wake_bridge.py"), "--as", "codex@desk",
              "--codex", fake, "--grace", str(GRACE), "--debounce", "0.3", "--poll", "0.5",
              "--log", os.path.join(tmp, "codex_bridge.log")]
bridge = None
try:
    for _ in range(50):
        try:
            urllib.request.urlopen(f"{BASE}/health", timeout=1).read()
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    else:
        sys.exit("hub did not start")
    print(f"codex wake bridge tests against {BASE}\n")
    for a in ("claude@11", "codex@desk", "human@hub"):
        call("hub_hello", {"as": a})

    bridge = subprocess.Popen(bridge_cmd, env=benv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)

    print("single instance")
    second = subprocess.run(bridge_cmd, env=benv, capture_output=True, text=True, timeout=20)
    check("a second bridge for the same Codex refuses to start",
          second.returncode != 0 and "already running" in (second.stderr + second.stdout),
          (second.returncode, second.stderr[-200:]))

    print("\nno registered session")
    m = say_to_codex("Before Codex has ever reported a session.")
    check("escalates instead of waking", wait_for(lambda: len(escalations()) == 1, 6), escalations())
    e = escalations()[-1] if escalations() else {}
    check("the escalation goes to the humans and the sender",
          set(e.get("to", [])) == {"human@hub", "claude@11"}, e.get("to"))
    check("...and says there is no registered session", "no registered session" in e.get("body", ""), e.get("body"))
    check("no codex queue call was made", queue_calls() == [], queue_calls())
    time.sleep(1.5)
    check("it escalates only once for that message", len(escalations()) == 1, len(escalations()))
    hook_event("codex@desk", "UserPromptSubmit", consume=True)

    print("\nidle Codex with a registered session")
    hook_event("codex@desk", "SessionStart", session="thr-1")
    who_before = {a["address"]: a for a in call("hub_who", {})["agents"]}["codex@desk"]["last_seen"]
    m = say_to_codex("Please review my change.")
    check("the bridge queues a wake", wait_for(lambda: len(queue_calls()) == 1, 6), queue_calls())
    c = queue_calls()[0] if queue_calls() else []
    check("into exactly the registered session", c[:3] == ["queue", "--thread", "thr-1"], c)
    check("the wake says mail is waiting and carries no content",
          len(c) > 4 and "1 message(s) waiting for codex@desk" in c[4] and "review" not in c[4], c)
    hook_event("codex@desk", "UserPromptSubmit", consume=True)  # Codex wakes and reads it
    time.sleep(GRACE + 1.5)
    check("an acknowledged wake is not escalated", len(escalations()) == 1, escalations()[-1:])
    check("and is not repeated", len(queue_calls()) == 1, queue_calls())

    print("\nbridge polling does not count as Codex activity")
    hook_event("codex@desk", "Stop", session="thr-1")
    seen = {a["address"]: a for a in call("hub_who", {})["agents"]}["codex@desk"]["last_seen"]
    time.sleep(2.5)
    after = {a["address"]: a for a in call("hub_who", {})["agents"]}["codex@desk"]["last_seen"]
    check("codex@desk last_seen unchanged while only the bridge polls", seen == after, (seen, after))

    print("\nbusy Codex")
    hook_event("codex@desk", "UserPromptSubmit")  # busy, mail left unread
    say_to_codex("Sent while Codex is working.")
    time.sleep(2.5)
    check("a busy Codex is not woken (its hooks will deliver)", len(queue_calls()) == 1, queue_calls())
    hook_event("codex@desk", "Stop")  # turn ended without reading it
    check("once it goes idle with the mail still unread, it is woken",
          wait_for(lambda: len(queue_calls()) == 2, 6), queue_calls())
    hook_event("codex@desk", "UserPromptSubmit", consume=True)
    hook_event("codex@desk", "Stop")

    print("\nbroadcasts")
    call("hub_say", {"as": "claude@11", "body": "General announcement for everyone."})
    time.sleep(2.5)
    check("a broadcast does not wake Codex", len(queue_calls()) == 2, queue_calls())
    hook_event("codex@desk", "UserPromptSubmit", consume=True)
    hook_event("codex@desk", "Stop")

    print("\nSTOP")
    s = call("hub_stop", {"as": "human@hub", "reason": "bridge stop test"})
    say_to_codex("Sent during a STOP.")
    time.sleep(2.5)
    check("no wake while a STOP applies", len(queue_calls()) == 2, queue_calls())
    call("hub_resume", {"as": "human@hub", "id": s["stop"]["id"]})
    check("the wake happens once the STOP is lifted", wait_for(lambda: len(queue_calls()) == 3, 6), queue_calls())
    hook_event("codex@desk", "UserPromptSubmit", consume=True)
    hook_event("codex@desk", "Stop")

    print("\nno acknowledgement")
    before = len(escalations())
    say_to_codex("Nobody is at the Codex window.")
    check("wake queued", wait_for(lambda: len(queue_calls()) == 4, 6), queue_calls())
    check("after the grace period the humans and the sender are told",
          wait_for(lambda: len(escalations()) == before + 1, GRACE + 6), escalations()[-1:])
    e = escalations()[-1] if escalations() else {}
    check("the escalation says Codex did not acknowledge", "did not acknowledge" in e.get("body", ""), e.get("body"))
    time.sleep(GRACE + 1)
    check("exactly one escalation, and no further wakes for that message",
          len(escalations()) == before + 1 and len(queue_calls()) == 4, (len(escalations()), len(queue_calls())))
    hook_event("codex@desk", "UserPromptSubmit", consume=True)
    hook_event("codex@desk", "Stop")

    print("\ncodex queue fails")
    with open(EXITFILE, "w") as fh:
        fh.write("3")
    before = len(escalations())
    say_to_codex("The codex queue command will fail.")
    check("a failing codex queue is escalated", wait_for(lambda: len(escalations()) == before + 1, 6),
          escalations()[-1:])
    check("with the exit code in the report", "exited 3" in (escalations()[-1]["body"] if escalations() else ""),
          escalations()[-1:])

    # ---------------------------------------------------------------- Claude
    print("\n=== Claude wake (claude --bg --resume) ===")
    claude_bridge = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "hooks", "agenthub_wake_bridge.py"), "--as", "claude@desk",
         "--no-spawn",  # these tests cover the pre-session wake of a family address
         "--claude", fake_claude, "--grace", str(GRACE), "--debounce", "0.3", "--poll", "0.5",
         "--log", os.path.join(tmp, "claude_bridge.log")],
        env=benv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(claude_bridge)
    time.sleep(1.5)
    hook_event("claude@desk", "Stop", session="thr-c1")

    def claude_calls():
        try:
            with open(CLAUDE_CALLS, encoding="utf-8") as fh:
                return [json.loads(l) for l in fh if l.strip()]
        except FileNotFoundError:
            return []

    def say_to_claude(body, sender="codex@11"):
        return call("hub_say", {"as": sender, "body": body, "to": ["claude@desk"]})["posted"]

    print("asleep: no session running")
    json.dump([], open(SESSIONS, "w"))
    say_to_claude("ClaudeDesk, please look at this.")
    check("the bridge resumes the registered session in the background",
          wait_for(lambda: len(claude_calls()) == 1, 6), claude_calls())
    c = claude_calls()[0] if claude_calls() else []
    check("with --bg --resume and the registered session id", c[:3] == ["--bg", "--resume", "thr-c1"], c)
    check("the wake says mail is waiting and carries no content",
          len(c) > 3 and "1 message(s) waiting for claude@desk" in c[3] and "look at this" not in c[3], c)
    hook_event("claude@desk", "UserPromptSubmit", consume=True)  # it woke and read the mail
    hook_event("claude@desk", "Stop", session="thr-c1")

    print("\nits AgentHub watch is connected")
    watch = subprocess.Popen([sys.executable, os.path.join(HERE, "hooks", "agenthub_watch.py"),
                              "--as", "claude@desk"], env=benv,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(watch)
    time.sleep(2)
    say_to_claude("Watch is connected, so no wake is needed.")
    time.sleep(3)
    check("a watched Claude is not woken", len(claude_calls()) == 1, claude_calls())
    # Read the mail first: dropping the watch with mail still unread is a sleeping
    # Claude with waiting mail, which the bridge would rightly wake.
    hook_event("claude@desk", "UserPromptSubmit", consume=True)
    hook_event("claude@desk", "Stop", session="thr-c1")
    watch.terminate(); watch.wait(timeout=10)
    time.sleep(1.5)

    print("\nsession already running")
    json.dump([{"sessionId": "thr-c1", "kind": "interactive", "status": "busy"}], open(SESSIONS, "w"))
    say_to_claude("Sent while its session is busy.")
    time.sleep(3)
    check("a busy session is left alone", len(claude_calls()) == 1, claude_calls())
    before = len(escalations("claude@desk/bridge"))
    json.dump([{"sessionId": "thr-c1", "kind": "interactive", "status": "idle"}], open(SESSIONS, "w"))
    check("an idle session with no watch is reported, not duplicated",
          wait_for(lambda: len(escalations("claude@desk/bridge")) == before + 1, 8),
          escalations("claude@desk/bridge")[-1:])
    e = escalations("claude@desk/bridge")[-1] if escalations("claude@desk/bridge") else {}
    check("the report explains the duplicate risk", "duplicate" in e.get("body", ""), e.get("body"))
    check("still no claude call", len(claude_calls()) == 1, claude_calls())
    hook_event("claude@desk", "UserPromptSubmit", consume=True)
    hook_event("claude@desk", "Stop", session="thr-c1")

    print("\nthe Claude CLI is signed out")
    json.dump([], open(SESSIONS, "w"))
    open(AUTHFAIL, "w").close()
    before = len(escalations("claude@desk/bridge"))
    say_to_claude("Sent while the CLI is signed out.")
    check("a signed-out CLI is escalated",
          wait_for(lambda: len(escalations("claude@desk/bridge")) == before + 1, 8),
          escalations("claude@desk/bridge")[-1:])
    e = escalations("claude@desk/bridge")[-1] if escalations("claude@desk/bridge") else {}
    check("the report names the machine and says to sign in",
          "not signed in" in e.get("body", "") and "claude auth login" in e.get("body", ""), e.get("body"))

    # ------------------------------------------------------- Codex sessions
    print("\n=== Codex sessions: busy waits, idle is delivered to, anything else gets a new thread ===")
    os.remove(EXITFILE)
    hook_event("codex@11", "UserPromptSubmit", consume=True)  # it was a sender above; clear its mail
    work11 = os.path.join(tmp, "work11")
    os.makedirs(work11, exist_ok=True)
    site11 = os.path.join(tmp, "site11.json")
    json.dump({"humans": ["human@hub"], "spawn": {"vendors": ["claude", "codex"], "cwd": {"*": work11}}},
              open(site11, "w"))
    codex11 = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "hooks", "agenthub_wake_bridge.py"), "--as", "codex@11",
         "--codex", fake, "--grace", str(GRACE), "--debounce", "0.3", "--poll", "0.5", "--busy-stale", "6",
         "--log", os.path.join(tmp, "codex11_bridge.log")],
        env=dict(benv, AGENTHUB_TOKEN=TOK["*@11"], AGENTHUB_SITE=site11),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(codex11)
    wait_for(lambda: "codex@11/bridge" in call("hub_who", {})["wake_sockets"], 8)
    TA, TB = "01a00000-0000-7000-8000-0000aaaa0001", "01a00000-0000-7000-8000-0000bbbb0002"
    A, B = "codex@11/aaaa0001", "codex@11/bbbb0002"
    hook_event(A, "Stop", session=TA)
    hook_event(B, "Stop", session=TB)

    def live():
        return {a["address"] for a in call("hub_who", {})["agents"]}

    def say11(body, to, **kw):
        return call("hub_say", dict({"as": "claude@desk", "body": body, "to": to}, **kw))

    def notes11():
        return [m for m in call("hub_peek", {"limit": 300})["messages"]
                if m["from"] == "codex@11/bridge" and "claude@desk" in m["to"]]

    print("an idle thread open in the app gets its mail through codex queue")
    base = len(queue_calls())
    say11("For A", [A])
    check("queued into A's own thread", wait_for(lambda: any(c[2] == TA for c in queue_calls()[base:]), 8),
          queue_calls()[base:])
    hook_event(A, "UserPromptSubmit", session=TA, consume=True)
    hook_event(A, "Stop", session=TA)

    print("a busy thread is left alone")
    hook_event(B, "PreToolUse", session=TB)
    base = len(codex_calls())
    say11("For busy B", [B])
    time.sleep(3)
    check("nothing runs while it works: its hooks deliver", codex_calls()[base:] == [], codex_calls()[base:])
    hook_event(B, "UserPromptSubmit", session=TB, consume=True)
    hook_event(B, "Stop", session=TB)

    print("an archived thread: a new terminal thread takes its mail")
    with open(GONEFILE, "w") as fh:
        fh.write(TB + "\n")
    base_e = len(exec_calls())
    res = say11("For archived B", [B])
    tag = f"AgentHub request #{res['posted']} "
    check("a new thread is started with codex exec",
          wait_for(lambda: any(tag in c[-1] for c in exec_calls()[base_e:]), 10), exec_calls()[base_e:])
    e = next((c for c in exec_calls()[base_e:] if tag in c[-1]), [""])
    check("terminal-only, past the git-repo check, in auto mode, in the spawn folder",
          e[:2] == ["exec", "--skip-git-repo-check"] and "--approve-for-me" in e
          and "-C" in e and os.path.normcase(e[e.index("-C") + 1]) == os.path.normcase(work11), e[:8])
    check("the sender is told a new session took it over",
          wait_for(lambda: any(f"#{res['posted']}" in m["body"] and "new terminal session" in m["body"]
                               for m in notes11()), 8), notes11()[-1:])

    print("follow-ups to a thread the hub started resume that same thread")
    token = e[-1].split("AgentHub claim token: ")[-1].strip()
    TN, N = "01a00000-0000-7000-8000-0000dddd0004", "codex@11/dddd0004"
    q = urllib.parse.urlencode({"as": N, "event": "UserPromptSubmit", "session": TN, "adopt": token})
    rq = urllib.request.Request(f"{BASE}/hook/poll?{q}")
    rq.add_header("Authorization", f"Bearer {TOK['*@11']}")
    got = urllib.request.urlopen(rq, timeout=10).read().decode()
    check("the new thread adopts the request", f"#{res['posted']} " in got, got)
    hook_event(N, "Stop", session=TN)
    hook_event(N, "SessionEnd", session=TN)
    check("it stays live after SessionEnd (a codex exec thread ends every turn)", N in live(), sorted(live()))
    base_e = len(exec_calls())
    say11("Follow-up for the new thread", ["codex@11"], reply_to=res["posted"])
    check("codex exec resume into the same thread",
          wait_for(lambda: any(c[:1] == ["exec"] and "resume" in c and TN in c for c in exec_calls()[base_e:]), 8),
          exec_calls()[base_e:])
    hook_event(N, "UserPromptSubmit", session=TN, consume=True)
    hook_event(N, "Stop", session=TN)
    check("a Codex session that took a request and stopped without answering is reported",
          wait_for(lambda: any(f"#{res['posted']}" in m["body"] and "without answering" in m["body"]
                               for m in notes11()), 20), notes11()[-1:])

    print("\nSessionEnd")
    C = "codex@11/cccc0003"
    hook_event(C, "Stop", session="01a00000-0000-7000-8000-0000cccc0003")
    check("a session that reports in is live", C in live(), sorted(live()))
    hook_event(C, "SessionEnd", session="01a00000-0000-7000-8000-0000cccc0003")
    check("SessionEnd ends a thread the hub did not start", C not in live(), sorted(live()))

    print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILURE(S): " + ", ".join(FAILS)))
finally:
    for p in (procs + [bridge, hub]):
        if p:
            p.terminate()
            try:
                p.wait(timeout=10)
            except Exception:  # noqa: BLE001
                p.kill()
    shutil.rmtree(tmp, ignore_errors=True)

sys.exit(1 if FAILS else 0)
