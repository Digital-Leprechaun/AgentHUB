#!/usr/bin/env python3
"""Stops, task ids on messages, planning turns, and the schema migration.

Starts its own hub on a scratch port with scratch tokens, so it never touches live
data.

  python3 test_stop.py
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from runner import WsClient  # noqa: E402

PORT = int(os.environ.get("TEST_PORT", "8798"))
BASE = f"http://127.0.0.1:{PORT}"
TOK = {"*@desk": "t-desk", "*@11": "t-11", "human@hub": "t-human"}
FAILS = []
_id = [0]


def tok_for(addr):
    if "@" not in (addr or ""):
        return ""
    fam = addr.split("/")[0]
    return TOK.get(fam) or TOK.get("*@" + fam.split("@")[1], "")


def call(tool, args, token=None):
    _id[0] += 1
    body = json.dumps({"jsonrpc": "2.0", "id": _id[0], "method": "tools/call",
                       "params": {"name": tool, "arguments": args}}).encode()
    req = urllib.request.Request(f"{BASE}/mcp", data=body, headers={"Content-Type": "application/json"})
    t = tok_for(args.get("as", "")) if token is None else token
    if t:
        req.add_header("Authorization", f"Bearer {t}")
    with urllib.request.urlopen(req, timeout=20) as r:
        res = json.loads(r.read())["result"]
    text = res["content"][0]["text"]
    return {"_error": text} if res.get("isError") else json.loads(text)


def get(path, token=""):
    req = urllib.request.Request(f"{BASE}{path}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read().decode()
    except urllib.error.HTTPError as e:
        return e.read().decode()


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def start(env):
    p = subprocess.Popen([sys.executable, "server.py"], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            urllib.request.urlopen(f"{BASE}/health", timeout=1).read()
            return p
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    p.kill()
    sys.exit("hub did not start")


tmp = tempfile.mkdtemp(prefix="agenthub-stop-")
db = os.path.join(tmp, "t.db")
tokens = os.path.join(tmp, "tokens.json")
json.dump(TOK, open(tokens, "w"))

# A database shaped like the previous build: messages has no task_id column.
old = sqlite3.connect(db)
old.executescript("""
CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, frm TEXT NOT NULL,
  family TEXT NOT NULL, vendor TEXT NOT NULL, host TEXT NOT NULL, topic TEXT NOT NULL DEFAULT '',
  recips TEXT NOT NULL DEFAULT '[]', reply_to INTEGER, verified INTEGER NOT NULL DEFAULT 0,
  body TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0);
INSERT INTO messages(ts,frm,family,vendor,host,body) VALUES('2026-09-10T00:00:00Z','claude@desk','claude@desk','claude','desk','from the old build');
""")
old.commit()
old.close()

env = dict(os.environ, HUB_PORT=str(PORT), HUB_DB=db, HUB_TOKENS_FILE=tokens,
           HUB_WEB_DIR=os.path.join(os.path.dirname(os.path.abspath(__file__)), "web"))
proc = start(env)
try:
    print(f"stop / task id / planning tests against {BASE}\n")

    print("migration")
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(messages)")}
    check("old database gained messages.task_id", "task_id" in cols, cols)
    pk = call("hub_peek", {"limit": 5})
    check("old message still readable", any(m["body"] == "from the old build" for m in pk["messages"]), pk)

    print("\ntool surface")
    _id[0] += 1
    req = urllib.request.Request(f"{BASE}/mcp", data=json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
        headers={"Content-Type": "application/json"})
    names = {t["name"] for t in json.loads(urllib.request.urlopen(req).read())["result"]["tools"]}
    check("hub_stop / hub_resume / hub_stops exist", {"hub_stop", "hub_resume", "hub_stops"} <= names, names)

    for a in ("claude@desk", "codex@desk", "claude@desk/reviewer", "claude@11", "codex@11"):
        call("hub_hello", {"as": a})

    print("\nStops: STOP NOW is human-only")
    r = call("hub_stop", {"as": "claude@desk", "reason": "I think we should all stop"})
    check("an agent cannot issue STOP NOW", "_error" in r and "human" in r["_error"], r)
    r = call("hub_stop", {"as": "human@hub", "reason": ""})
    check("a STOP needs a reason, even from a human", "_error" in r, r)
    r = call("hub_stop", {"as": "human@hub", "reason": "wrong build going out"}, token="bad")
    check("a human address with a bad token is refused", "_error" in r, r)
    s = call("hub_stop", {"as": "human@hub", "reason": "wrong build going out"})
    sid = s.get("stop", {}).get("id")
    check("A human issues STOP NOW", s.get("stop", {}).get("scope") == "all", s)

    print("\nevery agent is told, on every tool call")
    for a in ("codex@11", "claude@desk/reviewer"):
        r = call("hub_inbox", {"as": a})
        check(f"{a} sees a STOP field first",
              list(r)[0] == "STOP" and r["STOP"]["stops"][0]["id"] == sid, list(r))
    hook = get("/hook/poll?as=claude%40desk", TOK["*@desk"])
    check("the hook poll leads with the STOP banner", hook.startswith("[AgentHub] *** STOP IN FORCE"), hook[:80])
    only = get("/hook/poll?as=claude%40desk&stop_only=1", TOK["*@desk"])
    check("stop_only mode returns the banner without the inbox",
          "STOP IN FORCE" in only and "new message" not in only, only)
    check("the stop is recorded in the log",
          any("STOP NOW" in m["body"] for m in call("hub_peek", {"limit": 10})["messages"]))
    check("/api/stops lists it", f'"id": {sid}' in get("/api/stops"))

    print("\nonly a human lifts STOP NOW")
    r = call("hub_resume", {"as": "claude@desk", "id": sid})
    check("an agent cannot lift STOP NOW", "_error" in r, r)
    r = call("hub_resume", {"as": "human@hub", "id": sid})
    check("A human lifts it", r.get("stop", {}).get("active") is False, r)
    r = call("hub_inbox", {"as": "codex@11"})
    check("agents stop seeing the STOP field", "STOP" not in r, list(r))
    check("hook is quiet about stops again",
          "STOP IN FORCE" not in get("/hook/poll?as=claude%40desk&stop_only=1", TOK["*@desk"]))

    print("\nStops: an agent stops a visitor in its domain")
    r = call("hub_stop", {"as": "claude@desk", "reason": "self", "target": "claude@desk"})
    check("an agent cannot stop itself", "_error" in r, r)
    s = call("hub_stop", {"as": "claude@desk", "target": "codex@11",
                          "domain": "unreal-renderer", "reason": "editing renderer files without asking"})
    tsid = s.get("stop", {}).get("id")
    check("targeted stop created", s.get("stop", {}).get("scope") == "agent", s)
    check("hub flags that domain ownership is not verified", "note" in s, s)
    check("the target sees it", "STOP" in call("hub_inbox", {"as": "codex@11"}))
    check("the target's subagents see it", "STOP" in call("hub_inbox", {"as": "codex@11/tests"}))
    check("other agents do NOT see it", "STOP" not in call("hub_inbox", {"as": "claude@11"}))
    r = call("hub_resume", {"as": "claude@11", "id": tsid})
    check("a non-issuer agent cannot lift it", "_error" in r, r)
    r = call("hub_resume", {"as": "claude@desk/reviewer", "id": tsid})
    check("the issuer's family can lift it", r.get("stop", {}).get("active") is False, r)

    print("\ntask ids on messages")
    t = call("hub_task_create", {"as": "claude@desk", "title": "Plan the hub migration", "status": "planning"})["task"]
    check("a task can start in planning", t["status"] == "planning", t)
    r = call("hub_say", {"as": "claude@desk", "body": "hello", "task_id": 99999})
    check("a message cannot point at a task that does not exist", "_error" in r, r)
    m = call("hub_say", {"as": "claude@desk", "body": "Proposal: copy data/ first.",
                         "to": ["codex@11"], "task_id": t["id"]})
    check("hub_say accepts task_id", m.get("task_id") == t["id"], m)
    got = call("hub_peek", {"task_id": t["id"]})
    check("hub_peek filters by task", got["count"] == 1 and got["messages"][0]["task_id"] == t["id"], got)
    check("/api/messages filters by task", '"task_id": %d' % t["id"] in get(f"/api/messages?task_id={t['id']}"))

    print("\nplanning turns")
    for i, who in enumerate(["codex@11", "claude@desk"]):
        r = call("hub_say", {"as": who, "body": f"planning reply {i}", "task_id": t["id"]})
    check("3 exchanges: no warning yet", "PLANNING_LIMIT" not in r and r.get("planning_turns") == 3, r)
    jr = call("hub_say", {"as": "human@hub", "body": "keep going", "task_id": t["id"]})
    check("Human messages do not count as agent turns", "planning_turns" not in jr, jr)
    r = call("hub_say", {"as": "codex@11", "body": "one more idea", "task_id": t["id"]})
    check("4th agent exchange carries a PLANNING_LIMIT warning", "PLANNING_LIMIT" in r, r)
    listed = [x for x in call("hub_tasks", {})["tasks"] if x["id"] == t["id"]][0]
    check("hub_tasks reports planning_turns and message count",
          listed.get("planning_turns") == 4 and listed.get("messages") == 5, listed)
    call("hub_task_update", {"as": "claude@desk", "id": t["id"], "status": "active"})
    r = call("hub_say", {"as": "codex@11", "body": "executing step 1", "task_id": t["id"]})
    check("execution is not capped (no warning once active)", "PLANNING_LIMIT" not in r and "planning_turns" not in r, r)

    print("\nwake socket receives stop and resume events")
    ws = WsClient(f"ws://127.0.0.1:{PORT}/wake?as=codex%40desk", TOK["*@desk"])
    ws.connect()
    ws.sock.settimeout(10)
    ready = json.loads(ws.recv()[1])
    check("ready frame lists no stops", ready.get("type") == "ready" and ready.get("stops") == [], ready)
    s = call("hub_stop", {"as": "human@hub", "reason": "socket test"})
    kinds = []
    deadline = time.time() + 8
    while time.time() < deadline and "stop" not in kinds:
        f = ws.recv()
        if f and f[0] == 1:
            kinds.append(json.loads(f[1]).get("type"))
    check("runner socket gets a stop event", "stop" in kinds, kinds)
    call("hub_resume", {"as": "human@hub", "id": s["stop"]["id"]})
    deadline = time.time() + 8
    while time.time() < deadline and "resume" not in kinds:
        f = ws.recv()
        if f and f[0] == 1:
            kinds.append(json.loads(f[1]).get("type"))
    check("runner socket gets a resume event", "resume" in kinds, kinds)
    ws.close()

    print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILURE(S): " + ", ".join(FAILS)))
finally:
    proc.terminate()
    proc.wait(timeout=10)
    shutil.rmtree(tmp, ignore_errors=True)

sys.exit(1 if FAILS else 0)
