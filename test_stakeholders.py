#!/usr/bin/env python3
"""Stakeholders: the master file, a project's stakeholders.md, what the hook tells a
session and reports to the hub, the company recorded on tasks, and the board's API.

Starts its own hub on a scratch port with a scratch documents folder, and runs the
hook with a scratch home folder, so it never touches live data or this machine's
~/.agenthub.

  python3 test_stakeholders.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "hooks"))
from testutil import remove_tree  # noqa: E402

PORT = int(os.environ.get("TEST_PORT", "8798"))
BASE = f"http://127.0.0.1:{PORT}"
TOK = {"*@desk": "t-desk", "human@hub": "t-human"}
FAILS = []

MASTER = """# STAKEHOLDERS

Who the agents work for.

- **color**: its task color on the board.
- **hub**: its company label on the hub.

---

## Acme

Rockets and anvils.

- color: `#f5a26b`
- hub: `acme`
- laws: none yet
- services: none yet

## Widgets.Co

- color: `#b49cff`
- hub: `widgets`
- laws: [Stakeholders/Widgets.Co/LAWS.md](Stakeholders/Widgets.Co/LAWS.md)
- services: [Stakeholders/Widgets.Co/SERVICES.md](Stakeholders/Widgets.Co/SERVICES.md)

## Gizmo Works

- color: not a color
"""


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def req(method, path, body=None, token=""):
    r = urllib.request.Request(f"{BASE}{path}", method=method,
                               data=json.dumps(body).encode() if body is not None else None,
                               headers={"Content-Type": "application/json"})
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def call(tool, args, token):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}}
    _, r = req("POST", "/mcp", body, token)
    text = r["result"]["content"][0]["text"]
    return {"_error": text} if r["result"].get("isError") else json.loads(text)


tmp = tempfile.mkdtemp(prefix="agenthub-stake-")
home = os.path.join(tmp, "home")
docs = os.path.join(tmp, "docs")
master = os.path.join(docs, "STAKEHOLDERS.md")
write(master, MASTER)
acme = os.path.join(tmp, "acme")                # binds Acme, then Widgets.Co
write(os.path.join(acme, "stakeholders.md"), "# Stakeholders\n\n- acme\n- Widgets.Co\n- `Acme`\n")
inner = os.path.join(acme, "inner")             # a folder below it: has no file of its own
os.makedirs(inner)
mixed = os.path.join(tmp, "mixed")              # first listed is unknown
write(os.path.join(mixed, "stakeholders.md"), "- Nobody Inc\n- Widgets.Co\n")
widgets = os.path.join(tmp, "widgets")
write(os.path.join(widgets, "stakeholders.md"), "- Widgets.Co\n")
empty = os.path.join(tmp, "empty")
write(os.path.join(empty, "stakeholders.md"), "# Stakeholders\n")
os.makedirs(home)


def hook(event, session, cwd, family="claude@desk", url=BASE, extra=None, **env_extra):
    """Run the real hook as the harness would; returns (stdout JSON or {})."""
    env = dict(os.environ, AGENTHUB_URL=url, AGENTHUB_TOKEN=TOK["*@desk"] if url else "",
               AGENTHUB_EVERY="0", TMPDIR=tmp, TEMP=tmp, TMP=tmp, HOME=home, USERPROFILE=home,
               AGENTHUB_STAKEHOLDERS=master)
    env.pop("CODEX_THREAD_ID", None)
    env.pop("AGENTHUB_SITE", None)
    env.update(env_extra)
    payload = dict({"hook_event_name": event, "session_id": session, "cwd": cwd}, **(extra or {}))
    r = subprocess.run([sys.executable, os.path.join(HERE, "hooks", "agenthub_hook.py"), "--as", family],
                       input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=30)
    out = r.stdout.strip()
    return json.loads(out) if out else {}


def context(res):
    return (res.get("hookSpecificOutput") or {}).get("additionalContext") or ""


proc = None
try:
    print("the master file")
    import agenthub_hook as H  # noqa: E402
    import server as S  # noqa: E402
    hs = {s["name"]: s for s in H.parse_master(MASTER)}
    check("every ## section is a stakeholder, the preamble is not", list(hs) == ["Acme", "Widgets.Co", "Gizmo Works"],
          list(hs))
    check("color and hub label", (hs["Acme"]["color"], hs["Acme"]["hub"]) == ("#f5a26b", "acme"), hs["Acme"])
    check("laws and services are the link targets",
          hs["Widgets.Co"]["laws"] == ["Stakeholders/Widgets.Co/LAWS.md"], hs["Widgets.Co"])
    check("or the text when there is no link", hs["Acme"]["laws"] == "none yet", hs["Acme"])
    check("no hub: the label is the name", hs["Gizmo Works"]["hub"] == "gizmoworks", hs["Gizmo Works"])
    check("a bad color is dropped", hs["Gizmo Works"]["color"] == "", hs["Gizmo Works"])
    ss = {s["name"]: s for s in S.parse_stakeholders(MASTER)}
    check("the hub parses the same names and labels",
          [(s["name"], s["hub"]) for s in ss.values()] == [(s["name"], s["hub"]) for s in hs.values()], ss)
    check("and gives a bad color the open grey", ss["Gizmo Works"]["color"] == S.OPEN_COLOR, ss)
    check("a missing master file means no stakeholders", S.Stakeholders(os.path.join(tmp, "nope.md")).all() == [])
    check("a stakeholder name or label maps to its label",
          (S.Stakeholders(master).label("widgets.co"), S.Stakeholders(master).label("WIDGETS"),
           S.Stakeholders(master).label("Other Co")) == ("widgets", "widgets", "otherco"))

    print("\na project's stakeholders.md")
    check("names in order, once each, headings ignored", H.read_project(acme) == ["acme", "Widgets.Co"],
          H.read_project(acme))
    check("no file: None (an open session)", H.read_project(tmp) is None)
    check("parent folders are never searched", H.read_project(inner) is None)

    print("\nfinding the master file")
    os.environ.pop("AGENTHUB_STAKEHOLDERS", None)
    os.environ["AGENTHUB_SITE"] = site = os.path.join(tmp, "site.json")
    write(site, json.dumps({"shared_folder": {"lab": "/mnt/agents", "*": docs}}))
    check("site.json shared_folder, per host", H.master_path("claude@lab") == os.path.normpath(
        "/mnt/agents/STAKEHOLDERS.md"), H.master_path("claude@lab"))
    check("and its * entry", H.master_path("claude@desk") == os.path.normpath(master), H.master_path("claude@desk"))
    write(site, json.dumps({"shared_folder": docs, "stakeholders": os.path.join(tmp, "x", "S.md")}))
    check("site.json stakeholders wins over shared_folder",
          H.master_path("claude@desk") == os.path.normpath(os.path.join(tmp, "x", "S.md")))
    os.environ["AGENTHUB_STAKEHOLDERS"] = docs
    check("AGENTHUB_STAKEHOLDERS (a folder) wins over both", H.master_path("claude@desk") == os.path.normpath(master))
    os.environ.pop("AGENTHUB_SITE")
    os.environ.pop("AGENTHUB_STAKEHOLDERS")

    # Start the hub: its documents folder holds the master file.
    with open(os.path.join(tmp, "tokens.json"), "w") as fh:
        json.dump(TOK, fh)
    env = dict(os.environ, HUB_PORT=str(PORT), HUB_DB=os.path.join(tmp, "t.db"),
               HUB_TOKENS_FILE=os.path.join(tmp, "tokens.json"), HUB_HUMANS="human@hub",
               HUB_DOCS_DIR=docs, HUB_WEB_DIR=os.path.join(HERE, "web"))
    env.pop("HUB_STAKEHOLDERS", None)
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "server.py")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            urllib.request.urlopen(f"{BASE}/health", timeout=1).read()
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    else:
        sys.exit("hub did not start")

    print(f"\nthe hook at session start (hub {BASE})")
    SA = "aaaaaaaa-1111-4111-8111-11110000acee"
    A = "claude@desk/0000acee"
    got = context(hook("SessionStart", SA, acme))
    check("tells the session its address", A in got, got)
    check("names its stakeholders", "stakeholders: Acme (laws: none yet" in got and "Widgets.Co (laws: " in got, got)
    check("with the laws and services files beside the master file",
          os.path.normpath(os.path.join(docs, "Stakeholders/Widgets.Co/LAWS.md")) in got, got)
    check("and which one its tasks are filed for", "filed for Acme (hub company acme)" in got, got)
    check("no warning when every name is known", "WARNING" not in got, got)
    who = {w["address"]: w for w in call("hub_who", {}, TOK["human@hub"])["agents"]}
    check("the hub records the first stakeholder as the session's company",
          who.get(A, {}).get("company") == "acme", who.get(A))
    hook("PostToolUse", SA, widgets, extra={"tool_name": "Bash"})
    who = {w["address"]: w for w in call("hub_who", {}, TOK["human@hub"])["agents"]}
    check("a later event in another folder keeps the session's company", who.get(A, {}).get("company") == "acme")
    got = context(hook("SessionStart", SA, widgets, extra={"source": "compact"}))
    check("after compaction the session is told again, from the folder it opened on",
          "filed for Acme" in got, got)

    SO = "bbbbbbbb-2222-4222-8222-22220000beef"
    got = context(hook("SessionStart", SO, inner))
    check("a folder without stakeholders.md is open, even below a bound one", "This session is open" in got, got)
    SM = "cccccccc-3333-4333-8333-33330000cafe"
    got = context(hook("SessionStart", SM, mixed))
    check("an unknown stakeholder is a warning to tell the human",
          "WARNING" in got and "Nobody Inc" in got and "Tell the human before doing any work" in got, got)
    check("the known ones are still listed", "Widgets.Co (laws:" in got, got)
    check("the first one listed is unknown, so the tasks are not filed for the second", "filed for" not in got, got)
    who = {w["address"]: w for w in call("hub_who", {}, TOK["human@hub"])["agents"]}
    check("and the session has no company", not who.get("claude@desk/0000cafe", {}).get("company"),
          who.get("claude@desk/0000cafe"))
    got = context(hook("SessionStart", "dddddddd-4444-4444-8444-44440000d00d", empty))
    check("a stakeholders.md that lists nobody is a warning", "lists no stakeholders" in got, got)
    got = context(hook("SessionStart", "eeeeeeee-5555-4555-8555-55550000e00e", acme, url=""))
    check("without the hub the session is still told its stakeholders", "filed for Acme" in got, got)
    os.rename(master, master + ".away")
    got = context(hook("SessionStart", "ffffffff-6666-4666-8666-66660000f00f", acme))
    check("an unreadable master file falls back to the copy last read",
          "filed for Acme" in got and "copy last read" in got, got)
    got = context(hook("SessionStart", "ffffffff-6666-4666-8666-66660000f00f", acme,
                       AGENTHUB_STAKEHOLDERS=os.path.join(tmp, "elsewhere.md")))
    check("with no copy of that file either, the human is told",
          "cannot be checked" in got and "Tell the human" in got, got)
    got = context(hook("SessionStart", "ffffffff-6666-4666-8666-66660000f00f", acme, AGENTHUB_STAKEHOLDERS=""))
    check("as when no master file is configured", "no master file configured" in got, got)
    os.rename(master + ".away", master)

    print("\ntasks carry their stakeholder")
    t = call("hub_task_create", {"as": A, "title": "acme work"}, TOK["*@desk"])["task"]
    check("a primary task takes the creating session's company", t.get("company") == "acme", t)
    s = call("hub_task_create", {"as": "human@hub", "title": "sub", "parent_id": t["id"]}, TOK["human@hub"])["task"]
    check("a subtask takes its primary task's", s.get("company") == "acme", s)
    o = call("hub_task_create", {"as": "claude@desk/0000beef", "title": "open work"}, TOK["*@desk"])["task"]
    check("an open session's task is open", o.get("company") is None, o)
    e = call("hub_task_create", {"as": A, "title": "for widgets", "company": "Widgets.Co"}, TOK["*@desk"])["task"]
    check("an explicit stakeholder name is recorded as its label", e.get("company") == "widgets", e)
    u = call("hub_task_update", {"as": A, "id": t["id"], "company": "widgets"}, TOK["*@desk"])["task"]
    check("hub_task_update changes it", u.get("company") == "widgets", u)
    u = call("hub_task_update", {"as": A, "id": t["id"], "company": ""}, TOK["*@desk"])["task"]
    check("and an empty one makes the task open", u.get("company") is None, u)

    print("\nthe board's API")
    _, r = req("GET", "/api/stakeholders")
    check("/api/stakeholders lists name, label and color",
          [(x["name"], x["hub"], x["color"]) for x in r["stakeholders"]][:2]
          == [("Acme", "acme", "#f5a26b"), ("Widgets.Co", "widgets", "#b49cff")], r)
    check("with the open color", r.get("open_color") == "#6b7483", r)
    _, r = req("GET", "/api/tasks")
    check("/api/tasks carries them too, and each task's company",
          r.get("stakeholders") and any(x.get("company") == "widgets" for x in r["tasks"]), r.get("stakeholders"))
    time.sleep(1.1)  # a new mtime, even on a coarse clock
    write(master, MASTER.replace("#f5a26b", "#123456"))
    _, r = req("GET", "/api/stakeholders")
    check("a changed master file is read again", r["stakeholders"][0]["color"] == "#123456", r)
    os.rename(master, master + ".away")
    _, r = req("GET", "/api/stakeholders")
    check("a missing one means no stakeholders, not an error", r.get("stakeholders") == [], r)
    os.rename(master + ".away", master)
finally:
    if proc:
        proc.kill()
        proc.wait()
    remove_tree(tmp)

print(f"\n{'ALL PASSED' if not FAILS else f'{len(FAILS)} FAILED: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
