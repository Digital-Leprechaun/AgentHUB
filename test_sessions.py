#!/usr/bin/env python3
"""Session addressing tests (docs/session-addressing.md), against a scratch hub.

Every session has its own address (vendor@host/<sid>), mail to it reaches only that
session, replies and task mail narrow a family address to the session that owns the
conversation, the hook's guard stops a session acting as anyone else, and mail to a
family or anyone@host starts a NEW session through the wake bridge (a fake `claude`).

Never touches live data.

  python3 test_sessions.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("TEST_PORT", "8791"))
BASE = f"http://127.0.0.1:{PORT}"
TOK = {"*@desk": "t-desk", "*@11": "t-11", "human@hub": "t-human"}
FAILS = []

S1 = "11111111-1111-4111-8111-1111aaaa0001"   # session ids as the harness reports them
S2 = "22222222-2222-4222-8222-2222aaaa0002"
A = "claude@desk/aaaa0001"                     # ... and the addresses they give
B = "claude@desk/aaaa0002"


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


def post(path, body, token):
    req = urllib.request.Request(f"{BASE}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def hook(addr_family, event, session, payload_extra=None, companies=""):
    """Run the real agenthub_hook.py as the harness would; returns its stdout JSON or {}."""
    env = dict(os.environ, AGENTHUB_URL=BASE, AGENTHUB_TOKEN=tok_for(addr_family),
               AGENTHUB_EVERY="0", TMPDIR=tmp, TEMP=tmp, TMP=tmp,
               AGENTHUB_COMPANIES=companies or os.path.join(tmp, "no-companies.json"))
    env.pop("CODEX_THREAD_ID", None)
    payload = {"hook_event_name": event, "session_id": session, "cwd": tmp}
    payload.update(payload_extra or {})
    r = subprocess.run([sys.executable, os.path.join(HERE, "hooks", "agenthub_hook.py"),
                        "--as", addr_family], input=json.dumps(payload), capture_output=True,
                       text=True, env=env, timeout=20)
    out = r.stdout.strip()
    return json.loads(out) if out else {}


def hook_text(res):
    h = res.get("hookSpecificOutput") or {}
    return h.get("additionalContext") or res.get("reason") or ""


def inbox(addr):
    return [m["body"] for m in call("hub_inbox", {"as": addr})["messages"]]


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def wait_for(pred, secs):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.2)
    return pred()


class Watch:
    def __init__(self, addr):
        env = dict(os.environ, AGENTHUB_URL=BASE, AGENTHUB_TOKEN=tok_for(addr))
        self.p = subprocess.Popen([sys.executable, os.path.join(HERE, "hooks", "agenthub_watch.py"),
                                   "--as", addr], env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
        self.lines = []
        threading.Thread(target=lambda: [self.lines.append(l) for l in self.p.stdout], daemon=True).start()

    def stop(self):
        self.p.terminate()
        self.p.wait(timeout=5)


tmp = tempfile.mkdtemp(prefix="agenthub-sessions-")
home = os.path.join(tmp, "home")
os.makedirs(home)
tokens = os.path.join(tmp, "tokens.json")
json.dump(TOK, open(tokens, "w"))

# A fake `claude`: records every call; `claude agents --json` lists AGENTS_FILE (else none).
CLAUDE_CALLS = os.path.join(tmp, "claude_calls.jsonl")
AGENTS_FILE = os.path.join(tmp, "agents.json")
fake_py = os.path.join(tmp, "fakeclaude.py")
with open(fake_py, "w", encoding="utf-8") as fh:
    fh.write(
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "if args[:1] == ['agents']:\n"
        f"    print(open({AGENTS_FILE!r}).read() if os.path.exists({AGENTS_FILE!r}) else '[]'); sys.exit(0)\n"
        f"open({CLAUDE_CALLS!r}, 'a', encoding='utf-8').write(json.dumps({{'args': args, 'cwd': os.getcwd()}}) + '\\n')\n"
        "print('backgrounded - fake')\n")
if os.name == "nt":
    fake_claude = os.path.join(tmp, "claude.cmd")
    with open(fake_claude, "w") as fh:
        fh.write(f'@"{sys.executable}" "{fake_py}" %*\n')
else:
    fake_claude = os.path.join(tmp, "claude")
    with open(fake_claude, "w") as fh:
        fh.write(f'#!/bin/sh\nexec "{sys.executable}" "{fake_py}" "$@"\n')
    os.chmod(fake_claude, 0o755)

spawn_dir = os.path.join(tmp, "work")
os.makedirs(spawn_dir)
spawn_dir2 = os.path.join(tmp, "work2")
os.makedirs(os.path.join(spawn_dir2, "proj"))
site = os.path.join(tmp, "site.json")
json.dump({"humans": ["human@hub"], "spawn": {"cwd": {"*": [spawn_dir, spawn_dir2]}, "max": 10,
                                               "remote_control": {"desk": True, "*": False}}},
          open(site, "w"))
# Each machine maps companies to its own folders: the sender (on "11") and the
# receiving bridge (on "desk") use different files and different folders.
acme_desk = os.path.join(tmp, "acme-desk")
os.makedirs(os.path.join(acme_desk, "sub"))
acme_11 = os.path.join(tmp, "acme-11")
os.makedirs(os.path.join(acme_11, "deep"))
companies_desk = os.path.join(tmp, "companies-desk.json")
json.dump({"acme": [acme_desk], "ghost": [os.path.join(tmp, "missing")]}, open(companies_desk, "w"))
companies_11 = os.path.join(tmp, "companies-11.json")
json.dump({"Acme": acme_11}, open(companies_11, "w"))


def claude_calls():
    try:
        with open(CLAUDE_CALLS, encoding="utf-8") as fh:
            calls = [json.loads(l) for l in fh if l.strip()]
        return [c for c in calls if c["args"][:1] != ["auth"]]  # the bridge's sign-in pre-check
    except FileNotFoundError:
        return []


hub = subprocess.Popen([sys.executable, os.path.join(HERE, "server.py")],
                       env=dict(os.environ, HUB_PORT=str(PORT), HUB_DB=os.path.join(tmp, "t.db"),
                                HUB_TOKENS_FILE=tokens, HUB_WEB_DIR=os.path.join(HERE, "web"),
                                HUB_HUMANS="human@hub", HUB_REQUEST_ESCALATE_SECONDS="3",
                                HUB_REQUEST_SWEEP_SECONDS="0.5"),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
procs = []
try:
    for _ in range(50):
        try:
            urllib.request.urlopen(f"{BASE}/health", timeout=1).read()
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    else:
        sys.exit("hub did not start")
    print(f"session addressing tests against {BASE}\n")
    for a in ("codex@11", "human@hub", "claude@desk"):
        call("hub_hello", {"as": a})

    # ------------------------------------------------------------------ hook
    print("the hook gives each session its own address")
    r1 = hook("claude@desk", "SessionStart", S1)
    check("SessionStart tells the session its address", A in hook_text(r1), r1)
    hook("claude@desk", "SessionStart", S2)
    who = {x["address"]: x for x in call("hub_who", {})["agents"]}
    check("both sessions are registered as sessions",
          who.get(A, {}).get("kind") == "session" and who.get(B, {}).get("kind") == "session",
          {k: v.get("kind") for k, v in who.items()})
    check("with the cwd the hook reported", who.get(A, {}).get("cwd") == tmp, who.get(A))
    check("hub_hello knows a session", call("hub_hello", {"as": A}).get("kind") == "session")
    fam = call("hub_hello", {"as": "claude@desk"})
    check("the family address is warned to use its session address", "ADDRESS" in fam, fam.keys())

    print("\nthe guard")

    def pre(tool, inp, session=S1):
        return hook("claude@desk", "PreToolUse", session, {"tool_name": tool, "tool_input": inp})

    def denied(res):
        return (res.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"

    d = pre("mcp__agenthub__hub_say", {"as": "claude@desk", "body": "x"})
    check("a hub call as the family is denied", denied(d), d)
    check("and the reason names the right address",
          A in (d.get("hookSpecificOutput") or {}).get("permissionDecisionReason", ""), d)
    check("a hub call as another session is denied", denied(pre("mcp__agenthub__hub_inbox", {"as": B})))
    check("a hub call as itself is allowed", pre("mcp__agenthub__hub_say", {"as": A, "body": "x"}) == {})
    check("so is its own subagent", pre("mcp__agenthub__hub_hello", {"as": A + "/reviewer"}) == {})
    check("tools without `as` are left alone", pre("mcp__agenthub__hub_peek", {}) == {})
    w = 'python "C:/x/.agenthub/agenthub_watch.py" --as claude@desk --once 2>/dev/null'
    check("a watch started as the family is denied", denied(pre("Bash", {"command": w})))
    check("a watch started as itself is allowed",
          pre("Bash", {"command": w.replace("claude@desk", A)}) == {})
    check("other shell commands are left alone", pre("Bash", {"command": "ls -la"}) == {})

    print("\na hub-using subagent is a Worker: it talks as session/<role>, never manages tasks")
    SUB = {"agent_id": "ae661ddd0c51082b4", "agent_type": "general-purpose"}

    def pre_sub(tool, inp):
        return hook("claude@desk", "PreToolUse", S1, {"tool_name": tool, "tool_input": inp, **SUB})

    d = pre_sub("mcp__agenthub__hub_say", {"as": A, "body": "x"})
    check("a subagent cannot post as its session", denied(d), d)
    check("and is told to use its own address",
          A + "/" in (d.get("hookSpecificOutput") or {}).get("permissionDecisionReason", ""), d)
    check("it may post as its own Worker address",
          pre_sub("mcp__agenthub__hub_say", {"as": A + "/scout", "body": "x", "task_id": 1}) == {})
    check("a subagent cannot take the session's mail", denied(pre_sub("mcp__agenthub__hub_inbox", {"as": A})))
    d = pre_sub("mcp__agenthub__hub_task_update", {"as": A + "/scout", "id": 1, "status": "done"})
    check("a subagent cannot change the board", denied(d), d)
    check("and is told its Orchestrator does that",
          "Orchestrator" in (d.get("hookSpecificOutput") or {}).get("permissionDecisionReason", ""), d)
    check("a subagent cannot start a watch", denied(pre_sub("Bash", {"command": w.replace("claude@desk", A)})))
    check("a subagent may read the hub", pre_sub("mcp__agenthub__hub_peek", {"limit": 5}) == {})
    check("a subagent's other commands are left alone", pre_sub("Bash", {"command": "echo hi"}) == {})
    call("hub_say", {"as": "codex@11", "body": "mail for the parent", "to": [A]})
    r = hook("claude@desk", "PostToolUse", S1, {"tool_name": "Bash", **SUB})
    check("mail is not injected into a subagent", "mail for the parent" not in hook_text(r), r)
    r = hook("claude@desk", "PostToolUse", S1, {"tool_name": "Bash"})
    check("it stays for the parent's next event", "mail for the parent" in hook_text(r), r)
    s_ = call("hub_stop", {"as": "human@hub", "target": "claude@desk", "reason": "subagent test"})["stop"]
    r = hook("claude@desk", "PostToolUse", S1, {"tool_name": "Bash", **SUB})
    check("a STOP still reaches a subagent", "STOP" in hook_text(r), r)
    call("hub_resume", {"as": "human@hub", "id": s_["id"]})

    # ------------------------------------------------------------ delivery
    print("\nmail to a session reaches only that session")
    wa, wb, wf = Watch(A), Watch(B), Watch("claude@desk")
    procs += [wa.p, wb.p, wf.p]
    time.sleep(1.5)
    res = call("hub_say", {"as": "codex@11", "body": "for A only", "to": [A]})
    time.sleep(1.2)
    check("A's watch wakes", any("for A only" in l for l in wa.lines), wa.lines)
    check("B's watch does not", not any("for A only" in l for l in wb.lines), wb.lines)
    check("the family watch does not", not any("for A only" in l for l in wf.lines), wf.lines)
    check("A's inbox has it", "for A only" in inbox(A))
    check("B's inbox does not", "for A only" not in inbox(B))

    print("\nbroadcasts are counted, not pushed, to sessions")
    call("hub_say", {"as": "human@hub", "body": "everyone: a broadcast"})
    time.sleep(1.2)
    check("a session's inbox skips broadcasts", "everyone: a broadcast" not in inbox(A))
    check("a session's watch skips broadcasts", not any("a broadcast" in l for l in wa.lines), wa.lines)
    check("hub_hello counts them", call("hub_hello", {"as": A}).get("broadcasts_24h", 0) >= 1)
    check("the legacy family address still gets them", "everyone: a broadcast" in inbox("claude@desk"))

    print("\nfamily mail with no bridge goes to ONE session")
    hook("claude@desk", "UserPromptSubmit", S2)  # B is now the most recently active
    res = call("hub_say", {"as": "codex@11", "body": "family, no bridge", "to": ["claude@desk"]})
    check("the routing note says which session", B in json.dumps(res.get("routing")), res)
    check("B has it", "family, no bridge" in inbox(B))
    check("A does not", "family, no bridge" not in inbox(A))
    check("the family address does not", "family, no bridge" not in inbox("claude@desk"))
    time.sleep(1.2)
    check("only B's watch woke", any("family, no bridge" in l for l in wb.lines)
          and not any("family, no bridge" in l for l in wa.lines + wf.lines))

    print("\nreplies and task mail narrow a family address to their session")
    mid = call("hub_say", {"as": A, "body": "question from A", "to": ["codex@11"]})["posted"]
    res = call("hub_say", {"as": "codex@11", "body": "answer to A", "to": ["claude@desk"], "reply_to": mid})
    check("reply_to routes to the sender's session", res.get("notified") == [A], res)
    check("A has the reply", "answer to A" in inbox(A))
    check("B does not", "answer to A" not in inbox(B))
    res = call("hub_say", {"as": "codex@11", "body": "reply with no to", "reply_to": mid})
    check("a reply with no `to` goes to the sender, not everyone", res.get("notified") == [A], res)
    check("B does not get it", "reply with no to" not in inbox(B))
    t = call("hub_task_create", {"as": A, "title": "A's task", "status": "active"})["task"]
    check("a session owns the tasks it creates", t["owner"] == A and t["family"] == "claude@desk", t)
    res = call("hub_say", {"as": "codex@11", "body": "about A's task", "to": ["claude@desk"],
                           "task_id": t["id"]})
    check("task_id routes to the owning session", res.get("notified") == [A], res)
    check("B does not get task mail", "about A's task" not in inbox(B))

    print("\nlabels")
    call("hub_hello", {"as": A, "label": "parser"})
    res = call("hub_say", {"as": "codex@11", "body": "to the parser label", "to": ["claude@desk/parser"]})
    check("a label resolves to its session", res.get("notified") == [A], res)
    check("A has it", "to the parser label" in inbox(A))
    call("hub_hello", {"as": B, "label": "parser"})
    res = call("hub_say", {"as": "codex@11", "body": "label moved", "to": ["claude@desk/parser"]})
    check("the newest holder takes the label", res.get("notified") == [B], res)
    check("only a session can take a label",
          "_error" in call("hub_hello", {"as": "claude@desk", "label": "x1"}))
    check("anyone@host cannot be an identity", "_error" in call("hub_hello", {"as": "anyone@desk"}))

    print("\nSTOP on the family stops every session")
    s = call("hub_stop", {"as": "human@hub", "target": "claude@desk", "reason": "test"})["stop"]
    check("session A sees it", "STOP" in call("hub_inbox", {"as": A}))
    check("session B sees it", "STOP" in call("hub_inbox", {"as": B}))
    call("hub_resume", {"as": "human@hub", "id": s["id"]})
    check("and it lifts", "STOP" not in call("hub_inbox", {"as": A}))
    for w in (wa, wb, wf):
        w.stop()

    # ------------------------------------------------------------ requests
    print("\nno taker: the hub escalates")
    res = call("hub_say", {"as": "claude@11", "body": "anyone there on 11?", "to": ["anyone@11"]})
    check("anyone@ with no bridge is reported at once", "no bridge" in json.dumps(res.get("routing")), res)

    def notes():
        return [m for m in call("hub_peek", {"limit": 200})["messages"] if m["from"] == "agenthub@hub"]

    check("the hub tells the humans and the sender", wait_for(lambda: len(notes()) >= 1, 6), notes())
    n = notes()[-1] if notes() else {}
    check("addressed to both", set(n.get("to", [])) == {"human@hub", "claude@11"}, n)

    print("\na family request starts a NEW session through the bridge")
    benv = dict(os.environ, AGENTHUB_URL=BASE, AGENTHUB_TOKEN=TOK["*@desk"], HOME=home,
                USERPROFILE=home, AGENTHUB_SITE=site, AGENTHUB_COMPANIES=companies_desk)
    bridge = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "hooks", "agenthub_wake_bridge.py"), "--as", "claude@desk",
         "--claude", fake_claude, "--grace", "3", "--debounce", "0.3", "--poll", "0.5", "--busy-stale", "6",
         "--log", os.path.join(tmp, "claude_bridge.log")],
        env=benv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(bridge)
    check("the bridge registers as a spawner",
          wait_for(lambda: "claude@desk/bridge" in call("hub_who", {})["wake_sockets"], 8))
    # Park both sessions: idle, and nothing unread, so the only calls are spawns.
    for sess, addr in ((S1, A), (S2, B)):
        inbox(addr)
        hook("claude@desk", "Stop", sess)
    before = len(claude_calls())
    res = call("hub_say", {"as": "codex@11", "body": "someone take this job", "to": ["claude@desk"]})
    job = res["posted"]
    check("the routing note says a new session starts", "new session" in json.dumps(res.get("routing")), res)
    check("the bridge starts claude", wait_for(lambda: len(claude_calls()) == before + 1, 8), claude_calls())
    c = claude_calls()[-1] if claude_calls() else {"args": []}
    args = c["args"]
    check("in the background with a fresh session id",
          args[:2] == ["--bg", "--session-id"] and len(args) > 2 and len(args[2]) == 36, args)
    check("with Remote Control on, named like the session",
          "--remote-control" in args and args[args.index("--remote-control") + 1].startswith("#"), args)
    check("in auto mode", "--permission-mode" in args and args[args.index("--permission-mode") + 1] == "auto",
          args)
    check("in the configured folder", os.path.normcase(c.get("cwd", "")) == os.path.normcase(spawn_dir), c)
    check("the prompt carries no message content", "someone take this job" not in args[-1], args[-1:])
    new_sid = args[2] if len(args) > 2 else ""
    check("the prompt carries the claim token", f"AgentHub claim token: {new_sid}" in args[-1], args[-1:])
    check("A and B never see it", "someone take this job" not in inbox(A) + inbox(B))
    # `claude --bg` ignores --session-id: the real session has an id of its own.
    S3 = "33333333-3333-4333-8333-3333aaaa0003"
    new_addr = "claude@desk/aaaa0003"
    first = hook("claude@desk", "SessionStart", S3)
    check("the started session learns its own (real) address", new_addr in hook_text(first), first)
    prompt = hook("claude@desk", "UserPromptSubmit", S3, {"prompt": args[-1]})
    check("its first prompt adopts the request and delivers it",
          "someone take this job" in hook_text(prompt), prompt)
    who = {x["address"]: x for x in call("hub_who", {})["agents"]}
    check("it is registered as started for it", str(who.get(new_addr, {}).get("spawned_for")) == str(job),
          who.get(new_addr))
    placeholder = "claude@desk/" + new_sid.replace("-", "")[-8:]
    check("the placeholder is retired", placeholder not in who, sorted(who))
    res = call("hub_say", {"as": "codex@11", "body": "follow-up", "to": ["claude@desk"], "reply_to": job})
    check("a follow-up to the request reaches the session that took it",
          res.get("notified") == [new_addr], res)
    ans = call("hub_say", {"as": new_addr, "body": "done", "to": ["codex@11"], "reply_to": job})["posted"]
    res = call("hub_say", {"as": "codex@11", "body": "thanks", "to": ["claude@desk"], "reply_to": ans})
    check("replying to the new session's answer reaches that session", res.get("notified") == [new_addr], res)
    stranger = hook("claude@desk", "UserPromptSubmit", S2, {"prompt": args[-1]})
    check("a token that was already used moves nothing",
          "follow-up" not in hook_text(stranger) and "thanks" not in hook_text(stranger), stranger)
    time.sleep(1.5)
    spawns = [c for c in claude_calls() if "--session-id" in c["args"]]
    check("and starts no further session (at most it resumes that one)", len(spawns) == 1, spawns)

    print("\nanyone@host, and a started session that never picks up its request")
    before = len(claude_calls())
    res = call("hub_say", {"as": "human@hub", "body": "anyone on desk?", "to": ["anyone@desk"]})
    check("starts a session with the host's default agent",
          wait_for(lambda: len(claude_calls()) == before + 1, 8), claude_calls())
    lost = claude_calls()[-1]["args"][2] if claude_calls() else ""

    def bridge_notes():
        return [m for m in call("hub_peek", {"limit": 300})["messages"] if m["from"] == "claude@desk/bridge"]

    check("one that never adopts it is reported after 2 x grace",
          wait_for(lambda: any("never picked it up" in m["body"] for m in bridge_notes()), 15),
          bridge_notes()[-1:])
    check("and is not resumed (it never existed)",
          not any("--resume" in c["args"] and lost in c["args"] for c in claude_calls()), claude_calls()[-2:])

    print("\nthe sender picks the folder, within the host's allowed spawn folders")

    def spawn_with(workdir, body):
        before = len(claude_calls())
        call("hub_say", {"as": "codex@11", "body": body, "to": ["claude@desk"], "workdir": workdir})
        ok = wait_for(lambda: len(claude_calls()) == before + 1, 8)
        return claude_calls()[-1] if ok else {"args": [""], "cwd": ""}

    same = lambda a, b: os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    c = spawn_with(spawn_dir2, "work in the second folder")
    check("an allowed folder is used as asked", same(c["cwd"], spawn_dir2), c.get("cwd"))
    c = spawn_with(os.path.join(spawn_dir2, "proj"), "work in a project under it")
    check("a folder inside an allowed one starts in the allowed one", same(c["cwd"], spawn_dir2), c.get("cwd"))
    check("and tells the session to work in the project",
          os.path.join(spawn_dir2, "proj") in c["args"][-1], c["args"][-1:])
    c = spawn_with(tmp, "work somewhere not allowed")
    check("a folder outside every allowed one falls back to the default", same(c["cwd"], spawn_dir), c.get("cwd"))
    check("and the session is told why", "not one of" in c["args"][-1], c["args"][-1:])

    print("\nthe company picks the folder: the sender's machine names it, the receiver's maps it")
    SC = "66666666-6666-4666-8666-66660000c0de"
    transcript = os.path.join(tmp, "sender.jsonl")
    with open(transcript, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "message": "hi"}) + "\n")
        fh.write(json.dumps({"type": "custom-title", "customTitle": "Old name"}) + "\n")
        fh.write(json.dumps({"type": "custom-title", "customTitle": "[HUB] Leak triage"}) + "\n")
    hook("claude@11", "SessionStart", SC, {"cwd": os.path.join(acme_11, "deep"), "transcript_path": transcript},
         companies=companies_11)
    sender = "claude@11/0000c0de"
    me_row = [w for w in call("hub_who", {})["agents"] if w["address"] == sender]
    check("the hook registers the session's company from its folder",
          me_row and me_row[0].get("company") == "acme", me_row)

    def spawn_by(args, secs=8):
        # Match the start by its request number: a respawn left over from an earlier
        # section may start a session of its own in between.
        res = call("hub_say", dict({"to": ["claude@desk"]}, **args))
        tag = f"AgentHub request #{res.get('posted')} "
        hit = lambda: [c for c in claude_calls() if c["args"] and tag in c["args"][-1]]
        ok = wait_for(lambda: bool(hit()), secs)
        return res, (hit()[-1] if ok else {"args": [""], "cwd": ""})

    res, c = spawn_by({"as": sender, "body": "acme work, no folder named"})
    msg = [m for m in call("hub_peek", {"limit": 400})["messages"] if m["id"] == res.get("posted")]
    check("the hub stamps the sender's company on the mail", msg and msg[0].get("company") == "acme", msg)
    check("the receiver starts it in its own folder for that company", same(c["cwd"], acme_desk), c.get("cwd"))
    check("and tells the session the company", "company acme" in c["args"][-1], c["args"][-1:])
    check("the new session is named after the sender's session title, without [HUB]",
          "--name" in c["args"] and c["args"][c["args"].index("--name") + 1] == "Leak triage", c["args"])
    # It goes to the session started for the first message, which (being fake) never
    # starts: once that is clear, a new session takes it, in the same folder.
    res2, c = spawn_by({"as": "codex@11", "body": "follow-up from a non-session", "reply_to": res["posted"]},
                       secs=30)
    check("a reply keeps its conversation's company", same(c["cwd"], acme_desk), c.get("cwd"))
    _, c = spawn_by({"as": sender, "body": "not acme, the explicit company wins", "company": "nosuch"})
    check("a company this machine does not know falls back to the default", same(c["cwd"], spawn_dir), c.get("cwd"))
    check("and the session is told why", "no folder for company 'nosuch'" in c["args"][-1], c["args"][-1:])
    _, c = spawn_by({"as": sender, "body": "ghost", "company": "ghost"})
    check("a company whose folder is missing falls back too", same(c["cwd"], spawn_dir), c.get("cwd"))
    _, c = spawn_by({"as": sender, "body": "workdir wins", "workdir": spawn_dir2})
    check("an explicit workdir still overrides the company", same(c["cwd"], spawn_dir2), c.get("cwd"))
    _, c = spawn_by({"as": sender, "body": "inside a company folder", "workdir": os.path.join(acme_desk, "sub")})
    check("a workdir inside a company folder is allowed", same(c["cwd"], acme_desk), c.get("cwd"))
    check("and the session is told the subfolder", os.path.join(acme_desk, "sub") in c["args"][-1], c["args"][-1:])
    _, c = spawn_by({"as": "codex@11", "body": "no session, no company"})
    check("mail with no company starts in the default", same(c["cwd"], spawn_dir), c.get("cwd"))

    print("\na request is claimed exactly once")
    res = call("hub_say", {"as": "codex@11", "body": "race", "to": ["anyone@11"]})
    mid = res["posted"]
    code1, _ = post("/api/claim", {"as": "claude@11/bridge", "msg_id": mid, "target": "anyone@11",
                                   "session": "33333333-3333-4333-8333-3333aaaa0003"}, TOK["*@11"])
    code2, body2 = post("/api/claim", {"as": "claude@11/bridge", "msg_id": mid, "target": "anyone@11",
                                       "session": "44444444-4444-4444-8444-4444aaaa0004"}, TOK["*@11"])
    check("the first claim wins", code1 == 200, code1)
    check("the second is refused", code2 == 400 and "not open" in body2.get("error", ""), (code2, body2))
    code3, _ = post("/api/claim", {"as": "claude@desk/bridge", "msg_id": mid, "target": "anyone@11",
                                   "session": "55555555-5555-4555-8555-5555aaaa0005"}, TOK["*@desk"])
    check("a bridge cannot claim for another host", code3 == 400, code3)

    print("\nmail for one session: busy waits, idle is delivered to, anything else gets a new session")

    def agents(*rows):
        json.dump(list(rows), open(AGENTS_FILE, "w"))

    def calls_since(n):
        return [c["args"] for c in claude_calls()[n:]]

    # Settle what earlier sections left: S3 has read its mail and is idle in an app.
    inbox(new_addr)
    hook("claude@desk", "Stop", S3)
    inbox(A)
    agents({"sessionId": S1, "kind": "interactive", "status": "busy"})
    hook("claude@desk", "PreToolUse", S1, {"tool_name": "Bash", "tool_input": {}})
    n = len(claude_calls())
    call("hub_say", {"as": "codex@11", "body": "while A is busy", "to": [A]})
    time.sleep(3)
    touched = [c for c in calls_since(n) if S1 in c or c[:2] == ["stop", "aaaa0001"]]
    check("a busy session is left alone: its hooks deliver", touched == [], touched)
    inbox(A)
    hook("claude@desk", "Stop", S1)

    print("\nan idle terminal session with no watch is stopped and resumed in place")
    agents({"id": "aaaa0001", "sessionId": S1, "kind": "background", "status": "idle"})
    n = len(claude_calls())
    call("hub_say", {"as": "codex@11", "body": "A is idle in the background", "to": [A]})
    check("the bridge stops it, then resumes it",
          wait_for(lambda: any(c[:3] == ["--bg", "--resume", S1] for c in calls_since(n)), 8), calls_since(n))
    seq = calls_since(n)
    check("stop comes first (resuming a running session would copy it)",
          seq and seq[0][:2] == ["stop", "aaaa0001"], seq)
    c = next((c for c in seq if c[:2] == ["--bg", "--resume"]), [])
    check("the wake names A's address and carries no content",
          len(c) > 3 and A in c[3] and "background" not in c[3], c)
    check("no new session is started for A",
          not any("--session-id" in c and A in c[-1] for c in seq), seq)
    hook("claude@desk", "UserPromptSubmit", S1, {"prompt": c[3] if len(c) > 3 else ""})
    inbox(A)
    hook("claude@desk", "Stop", S1)

    print("\nan idle session open in an app with no watch gets a new terminal session")
    agents({"sessionId": S1, "kind": "interactive", "status": "idle"})
    n = len(claude_calls())
    mid = call("hub_say", {"as": "codex@11", "body": "A is open in the app", "to": [A]})["posted"]
    check("a new session is started for it",
          wait_for(lambda: any("--session-id" in c for c in calls_since(n)), 8), calls_since(n))
    seq = calls_since(n)
    check("the app session is never stopped or resumed",
          not any(c[:1] == ["stop"] or "--resume" in c for c in seq), seq)
    c = next((c for c in seq if "--session-id" in c), [""])
    check("the new session is told it continues A's conversation",
          "continues a conversation" in c[-1] and f"#{mid}" in c[-1], c[-1:])
    who = {x["address"]: x for x in call("hub_who", {})["agents"]}
    check("A is ended on the hub (hub_who lists live sessions only)",
          who.get(A, {}).get("session_state", "ended") == "ended", who.get(A))
    res = call("hub_say", {"as": "codex@11", "body": "a reply to A", "to": ["claude@desk"], "reply_to":
                           call("hub_say", {"as": A, "body": "from A", "to": ["codex@11"]})["posted"]})
    check("a follow-up to an ended session becomes a request for a new one",
          "new session" in json.dumps(res.get("routing")), res)
    hook("claude@desk", "Stop", S1)  # A comes back...
    check("a respawned session never gets the handed-on mail again",
          "A is open in the app" not in inbox(A))

    print("\na hub-started session that is gone is resumed, keeping its context")
    agents()
    n = len(claude_calls())
    call("hub_say", {"as": "codex@11", "body": "back to you, S3", "to": [new_addr]})
    check("the bridge resumes it by its own session id",
          wait_for(lambda: any(c[:3] == ["--bg", "--resume", S3] for c in calls_since(n)), 8), calls_since(n))
    check("without stopping anything (it is not running)",
          not any(c[:1] == ["stop"] for c in calls_since(n)), calls_since(n))

    print("\nthe sender hears about every problem, and is told to tell the user")
    hook("claude@11", "UserPromptSubmit", SC, {"prompt": "x"}, companies=companies_11)  # read old mail
    agents({"sessionId": S2, "kind": "interactive", "status": "idle"})
    inbox(B)
    hook("claude@desk", "Stop", S2)
    m1 = call("hub_say", {"as": sender, "body": "HANDOFF [9] B is open in the app with no watch", "to": [B]})["posted"]

    def notes_to_sender():
        return [m for m in call("hub_peek", {"limit": 400})["messages"]
                if m["from"] == "claude@desk/bridge" and sender in m["to"]]
    check("a respawn is reported to the sender",
          wait_for(lambda: any(f"#{m1}" in m["body"] and "new terminal session" in m["body"]
                               for m in notes_to_sender()), 10), notes_to_sender()[-1:])
    got = hook_text(hook("claude@11", "UserPromptSubmit", SC, {"prompt": "y"}, companies=companies_11))
    check("the sender's hook delivers it with an instruction to tell the user", "Tell the user" in got, got)

    print("\na session that takes a HANDOFF and stops without answering is reported")
    S4 = "44444444-4444-4444-8444-4444aaaa0004"
    D = "claude@desk/aaaa0004"
    hook("claude@desk", "SessionStart", S4)
    agents({"sessionId": S4, "kind": "background", "status": "idle", "id": "44444444"})
    m2 = call("hub_say", {"as": sender, "body": "HANDOFF [9] please do the thing", "to": [D]})["posted"]
    hook("claude@desk", "UserPromptSubmit", S4, {"prompt": "go"})  # it reads the handoff ...
    hook("claude@desk", "Stop", S4)                                # ... and stops, no answer
    check("the sender is told it went idle without answering",
          wait_for(lambda: any(f"#{m2}" in m["body"] and "without answering" in m["body"]
                               for m in notes_to_sender()), 20), notes_to_sender()[-1:])
    m3 = call("hub_say", {"as": sender, "body": "QUESTION [9] and this?", "to": [D]})["posted"]
    hook("claude@desk", "UserPromptSubmit", S4, {"prompt": "go"})
    call("hub_say", {"as": D, "body": "RESULT [9] done", "to": [sender], "reply_to": m3})
    hook("claude@desk", "Stop", S4)
    time.sleep(9)
    check("an answered one is not reported",
          not any(f"#{m3}" in m["body"] for m in notes_to_sender()), notes_to_sender()[-1:])

    print("\na delivery notice never starts a session, and a message is never reopened twice")
    agents()  # D (S4) is no longer running
    n = len(claude_calls())
    note = call("hub_say", {"as": "claude@desk/bridge", "body": "Wake bridge: something failed", "to": [D]})["posted"]
    time.sleep(4)
    check("a bridge notice to a gone session starts nothing",
          not any("--session-id" in c or S4 in c for c in calls_since(n)), calls_since(n))
    m5 = call("hub_say", {"as": sender, "body": "HANDOFF [9] for gone D", "to": [D]})["posted"]
    check("real mail to it still gets a new session",
          wait_for(lambda: any("--session-id" in c and f"#{m5}" in c[-1] for c in calls_since(n)), 10),
          calls_since(n))
    code, body = post("/api/session/respawn", {"as": "claude@desk/bridge", "session": D, "msg_ids": [m5],
                                               "note": "again"}, TOK["*@desk"])
    check("reopening a message that is already reopened does nothing",
          code == 200 and body.get("reopened") == [], (code, body))

    print("\na conversation resumed under a new session id keeps its mail and its follow-ups")
    S6 = "66666666-6666-4666-8666-6666aaaa0006"
    E = "claude@desk/aaaa0006"
    hook("claude@desk", "SessionStart", S6)
    hook("claude@desk", "Stop", S6)
    agents({"id": "66666666", "sessionId": S6, "kind": "background", "status": "idle"})
    n = len(claude_calls())
    m8 = call("hub_say", {"as": sender, "body": "QUESTION [9] for E", "to": [E]})["posted"]
    check("E is stopped and resumed",
          wait_for(lambda: any(c[:3] == ["--bg", "--resume", S6] for c in calls_since(n)), 8), calls_since(n))
    c = next((c for c in calls_since(n) if c[:3] == ["--bg", "--resume", S6]), [""] * 4)
    check("the wake names no address to call as, only the one it resumes from",
          f"AgentHub resumed from: {E}" in c[3] and "hub_inbox as" not in c[3], c[3:])
    agents()
    S7 = "77777777-7777-4777-8777-7777aaaa0007"     # the CLI gave the resumed conversation a new id
    F = "claude@desk/aaaa0007"
    hook("claude@desk", "SessionStart", S7)
    got = hook_text(hook("claude@desk", "UserPromptSubmit", S7, {"prompt": c[3]}))
    check("its first prompt hands it E's waiting mail", "QUESTION [9] for E" in got, got)
    ans = call("hub_say", {"as": F, "body": "ANSWER [9] here", "to": [sender], "reply_to": m8})["posted"]
    res = call("hub_say", {"as": sender, "body": "follow-up for E", "to": ["claude@desk"], "reply_to": m8})
    check("a follow-up to E's conversation reaches the resumed session", res.get("notified") == [F], res)
    res = call("hub_say", {"as": sender, "body": "direct to old E", "to": [E], "task_id": _test_task()})
    check("direct mail to the old address follows it too", res.get("notified") == [F], res)
    import importlib.util
    spec = importlib.util.spec_from_file_location("hk", os.path.join(HERE, "hooks", "agenthub_hook.py"))
    hk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hk)
    check("the resume marker accepts dotted and hyphenated hosts",
          bool(hk.RESUMED_FROM.search("AgentHub resumed from: claude@build-1.lab_x/0123abcd")))
    time.sleep(4)
    check("and nobody is told E ended without answering",
          not any(f"#{m8}" in m["body"] and "without answering" in m["body"] for m in notes_to_sender()),
          notes_to_sender()[-1:])

    print("\nhub_escalate: the bridge moves a terminal session into the desktop app")
    S8 = "88888888-8888-4888-8888-8888aaaa0008"
    G = "claude@desk/aaaa0008"
    hook("claude@desk", "SessionStart", S8)
    hook("claude@desk", "Stop", S8)
    agents({"id": "88888888", "sessionId": S8, "kind": "background", "status": "idle"})
    n = len(claude_calls())
    res = call("hub_escalate", {"as": sender, "session": G})
    check("the hub takes the request", res.get("session") == G and res.get("escalation"), res)
    check("the bridge stops the background copy, then opens it in the app",
          wait_for(lambda: ["--desktop", "--resume", S8] in [c[:3] for c in calls_since(n)], 8), calls_since(n))
    seq = calls_since(n)
    check("stop comes first", seq and seq[0][:2] == ["stop", "88888888"], seq)
    check("the requester is told it worked",
          wait_for(lambda: any("RESULT escalation" in m["body"] for m in notes_to_sender()), 8),
          notes_to_sender()[-1:])
    time.sleep(2)
    check("it is done once", sum(c[:2] == ["--desktop", "--resume"] for c in calls_since(n)) == 1, calls_since(n))
    agents()
    bad = call("hub_escalate", {"as": sender, "session": "claude@desk/nosuch01"})
    check("an unknown session is refused", "_error" in bad, bad)

    print("\nOrchestrators and Workers")
    O = sender                                    # an Orchestrator session on 11
    agents()
    nomsg = call("hub_say", {"as": O, "body": "no task", "to": ["claude@desk"], "_notask": True})
    check("hub mail without a task is refused", "_error" in nomsg and "needs a task" in nomsg["_error"], nomsg)
    P = call("hub_task_create", {"as": O, "title": "Acquire network info", "status": "active"})["task"]["id"]
    S1 = call("hub_task_create", {"as": O, "title": "desk: report its GPU", "parent_id": P,
                                  "worker": "claude@desk"})["task"]
    check("an Orchestrator adds a subtask, owned by itself, with a worker",
          S1["parent_id"] == P and S1["owner"] == O and S1["worker"] == "claude@desk", S1)
    ST2 = call("hub_task_create", {"as": O, "title": "local scout", "parent_id": P,
                                  "worker": O + "/scout"})["task"]["id"]
    deep = call("hub_task_create", {"as": O, "title": "too deep", "parent_id": S1["id"]})
    check("a subtask cannot have subtasks", "_error" in deep and "two levels" in deep["_error"], deep)
    other = call("hub_task_create", {"as": "codex@11", "title": "not mine", "parent_id": P})
    check("only the task's Orchestrator adds subtasks", "_error" in other, other)

    n = len(claude_calls())
    req = call("hub_say", {"as": O, "body": "QUESTION what GPU?", "to": ["claude@desk"], "task_id": S1["id"]})
    check("the request starts a session", wait_for(
        lambda: any(f"AgentHub request #{req['posted']} " in c[-1] for c in calls_since(n)), 10), calls_since(n))
    sp = next((c for c in calls_since(n) if f"AgentHub request #{req['posted']} " in c[-1]), [""])
    check("which is told it is a Worker on the subtask, for its Orchestrator",
          f"Worker on task #{S1['id']}" in sp[-1] and O in sp[-1] and "Do not create or update" in sp[-1], sp[-1:])
    SW = "99999999-9999-4999-8999-9999aaaa0009"
    W = "claude@desk/aaaa0009"
    hook("claude@desk", "SessionStart", SW)
    hook("claude@desk", "UserPromptSubmit", SW, {"prompt": sp[-1]})
    tk = call("hub_tasks", {"owner": O})["tasks"]
    check("taking the request makes it the subtask's worker",
          any(t["id"] == S1["id"] and t["worker"] == W for t in tk), [t for t in tk if t["id"] == S1["id"]])
    r = call("hub_task_create", {"as": W, "title": "my own task"})
    check("a Worker cannot create tasks", "_error" in r and "Worker" in r["_error"], r)
    r = call("hub_task_update", {"as": W, "id": S1["id"], "status": "done"})
    check("nor update its own subtask", "_error" in r and "Orchestrator" in r["_error"], r)
    r = call("hub_say", {"as": W, "body": "RESULT RTX 9000", "to": [O], "_notask": True})
    check("an untagged Worker message goes under its subtask", r.get("task_id") == S1["id"], r)
    r = call("hub_say", {"as": W, "body": "off-task", "to": [O], "task_id": P})
    check("but not under another task", "_error" in r, r)

    r = call("hub_say", {"as": O + "/scout", "body": "RESULT scouted", "to": [O], "task_id": ST2})
    check("a hub-using subagent posts under its subtask", r.get("task_id") == ST2, r)
    r = call("hub_say", {"as": O + "/scout", "body": "x", "to": [O], "task_id": _test_task()})
    check("but not under a task outside its Orchestrator's tree", "_error" in r, r)
    r = call("hub_task_update", {"as": "codex@11", "id": S1["id"], "status": "done"})
    check("another agent cannot update the Orchestrator's task", "_error" in r, r)

    r = call("hub_task_update", {"as": O, "id": P, "status": "done"})
    check("closing a primary task with open subtasks warns", "WARNING" in r, r)
    call("hub_task_update", {"as": O, "id": P, "status": "active"})
    r = call("hub_task_update", {"as": O, "id": S1["id"], "status": "elevated"})
    check("the Orchestrator marks the subtask elevated", r.get("task", {}).get("status") == "elevated", r)
    r = call("hub_task_create", {"as": W, "title": "now mine", "status": "active"})
    check("and the elevated session becomes an Orchestrator", "task" in r, r)
    r = call("hub_task_create", {"as": O + "/bridge", "title": "sneaky"})
    check("<session>/bridge gets no bridge rights (it is a Worker subagent)", "_error" in r, r)
    d = hook("claude@desk", "PreToolUse", S4, {"tool_name": "mcp__agenthub__hub_say",
                                               "tool_input": {"as": D + "/bridge", "body": "x"}})
    check("and the hook will not let a session act as <session>/bridge",
          (d.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny", d)
    r = call("hub_task_update", {"as": O, "id": P, "status": "elevated"})
    check("a primary task cannot be marked elevated", "_error" in r, r)

    print("\nhub_flush clears the queue without deleting anything")
    agents()
    n = len(claude_calls())
    rq = call("hub_say", {"as": sender, "body": "late adoption", "to": ["claude@desk"]})["posted"]
    wait_for(lambda: any(f"AgentHub request #{rq} " in c[-1] for c in calls_since(n)), 10)
    sp = next((c for c in calls_since(n) if f"AgentHub request #{rq} " in c[-1]), [""])
    S9, G9 = "99999999-9999-4999-8999-9999aaaa0019", "claude@desk/aaaa0019"
    hook("claude@desk", "SessionStart", S9)
    call("hub_say", {"as": sender, "body": "later direct mail", "to": [G9]})
    inbox(G9)  # the session reads the later mail first: its cursor moves past the request
    got = hook_text(hook("claude@desk", "UserPromptSubmit", S9, {"prompt": sp[-1]}))
    check("a request adopted after later mail was read is still delivered", "late adoption" in got, got)
    code, body = post("/api/request/fail", {"as": "claude@desk/bridge", "msg_id": rq, "note": "x",
                                            "target": "claude@desk"}, TOK["*@desk"])
    check("a bridge can close a request it will not take", code == 200, (code, body))

    m6 = call("hub_say", {"as": sender, "body": "HANDOFF [9] backlog item", "to": [B]})["posted"]
    m7 = call("hub_say", {"as": sender, "body": "backlog request", "to": ["claude@desk"]})["posted"]
    refused = call("hub_flush", {"as": "codex@11", "reason": "test"})
    check("only a human or the AgentHub holder may flush", "_error" in refused, refused)
    out = call("hub_flush", {"as": "human@hub", "reason": "bridge update"})
    check("it reports what it cleared", out.get("through", 0) >= m7 and out.get("requests_closed", 0) >= 1, out)
    check("waiting mail is no longer waiting", "HANDOFF [9] backlog item" not in inbox(B))
    check("and nothing is deleted",
          {m6, m7} <= {m["id"] for m in call("hub_peek", {"limit": 500})["messages"]})
    who = {x["address"] for x in call("hub_who", {})["agents"]}
    check("idle sessions without a watch are ended", out.get("sessions_ended", 0) >= 1 and B not in who,
          (out, sorted(who)))
    back = hook("claude@desk", "Stop", S2)
    rows = {x["address"]: x for x in call("hub_who", {})["agents"]}
    check("and one that comes back is live again", B in rows, (back, sorted(rows)))

    print("\nthe hook asks a Claude session once to put its hub id in its title, once it uses the hub")
    tr = os.path.join(tmp, "titled.jsonl")
    with open(tr, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "custom-title", "customTitle": "Fix the parser"}) + "\n")
    t0 = hook_text(hook("claude@desk", "UserPromptSubmit", S4, {"prompt": "a", "transcript_path": tr}))
    check("a session that has not used the hub is not asked", "hub id in its title" not in t0, t0)
    hook("claude@desk", "PreToolUse", S4, {"tool_name": "mcp__agenthub__hub_peek", "tool_input": {}})
    t0 = hook_text(hook("claude@desk", "UserPromptSubmit", S4, {"prompt": "a", "transcript_path": tr}))
    check("reading the hub does not count", "hub id in its title" not in t0, t0)
    hook("claude@desk", "PreToolUse", S4, {"tool_name": "mcp__agenthub__hub_say",
                                          "tool_input": {"as": "claude@desk/aaaa0004", "body": "x"}})
    t1 = hook_text(hook("claude@desk", "UserPromptSubmit", S4, {"prompt": "a", "transcript_path": tr}))
    check("it asks for the [id] prefix", '"[aaaa0004] Fix the parser"' in t1, t1)
    t2 = hook_text(hook("claude@desk", "UserPromptSubmit", S4, {"prompt": "b", "transcript_path": tr}))
    check("only once per title", "hub id in its title" not in t2, t2)
    with open(tr, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "custom-title", "customTitle": "[aaaa0004] Fix the parser"}) + "\n")
    t3 = hook_text(hook("claude@desk", "UserPromptSubmit", S4, {"prompt": "c", "transcript_path": tr}))
    check("and not once it has it", "hub id in its title" not in t3, t3)

    print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILURE(S): " + ", ".join(FAILS)))
finally:
    for p in procs + [hub]:
        p.terminate()
        try:
            p.wait(timeout=10)
        except Exception:  # noqa: BLE001
            p.kill()
    shutil.rmtree(tmp, ignore_errors=True)

sys.exit(1 if FAILS else 0)
