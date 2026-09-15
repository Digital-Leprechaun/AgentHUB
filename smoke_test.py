#!/usr/bin/env python3
"""End-to-end smoke test: drives AgentHub over the real MCP HTTP endpoint.

Usage: python3 smoke_test.py [base_url] [token]
"""
import json
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8788").rstrip("/")
TOKEN = sys.argv[2] if len(sys.argv) > 2 else ""
FAILS = []
_id = [0]

# When a tokens file is present, auth is enforced per family, so each call has to
# carry the right one. Falls back to open mode when there is no file.
try:
    import json as _j
    with open("data/tokens.json", encoding="utf-8") as _fh:
        FAMILY_TOKENS = {k.lower(): v for k, v in _j.load(_fh).items()}
except (FileNotFoundError, ValueError):
    FAMILY_TOKENS = {}


def token_for(addr: str) -> str:
    fam = (addr or "").split("/")[0].lower()
    host = fam.split("@")[-1]
    return FAMILY_TOKENS.get(fam) or FAMILY_TOKENS.get(f"*@{host}") or TOKEN


def rpc(method, params=None, token=None):
    _id[0] += 1
    body = json.dumps({"jsonrpc": "2.0", "id": _id[0], "method": method,
                       "params": params or {}}).encode()
    req = urllib.request.Request(f"{BASE}/mcp", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream"})
    tok = TOKEN if token is None else token
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(req, timeout=70) as r:
        return json.loads(r.read() or b"{}")


def call(tool, args, token=None):
    if token is None and args.get("as"):
        token = token_for(args["as"])
    r = rpc("tools/call", {"name": tool, "arguments": args}, token)
    res = r.get("result") or {}
    text = (res.get("content") or [{}])[0].get("text", "")
    if res.get("isError"):
        return {"_error": text}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text}


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


print(f"AgentHub smoke test against {BASE}\n")

print("handshake")
r = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "smoke", "version": "1"}})
check("initialize returns a protocolVersion", bool(r.get("result", {}).get("protocolVersion")), r)
check("serverInfo names agenthub", r.get("result", {}).get("serverInfo", {}).get("name") == "agenthub", r)
tools = rpc("tools/list").get("result", {}).get("tools", [])
names = {t["name"] for t in tools}
check(f"tools/list returns {len(tools)} tools", len(tools) == 14, sorted(names))
check("every tool has an inputSchema", all("inputSchema" in t for t in tools))

print("\nidentity + presence")
h = call("hub_hello", {"as": "claude@desk"})
check("claude@desk says hello", h.get("you") == "claude@desk", h)
call("hub_hello", {"as": "codex@11"})
call("hub_hello", {"as": "grok@desk"})
sub = call("hub_hello", {"as": "claude@desk/reviewer", "parent": "claude@desk"})
check("subagent gets its own identity", sub.get("you") == "claude@desk/reviewer", sub)
check("subagent shares the family", sub.get("family") == "claude@desk", sub)
who = call("hub_who", {})
check("who lists 4 agents", len(who.get("agents", [])) == 4, who)

print("\nbad input is rejected")
check("bad address rejected", "_error" in call("hub_hello", {"as": "not-an-address"}))
check("empty body rejected", "_error" in call("hub_say", {"as": "claude@desk", "body": "  "}))
check("bad topic rejected", "_error" in call("hub_say", {"as": "claude@desk", "body": "x", "topic": "no spaces!"}))
check("unknown tool rejected", "_error" in call("hub_nope", {}))

if FAMILY_TOKENS:
    print("\nauth (tokens.json present)")
    check("wrong token rejected",
          "_error" in call("hub_say", {"as": "claude@desk", "body": "x"}, token="wrong"))
    check("missing token rejected",
          "_error" in call("hub_say", {"as": "claude@desk", "body": "x"}, token=""))
    other = next((v for k, v in FAMILY_TOKENS.items()
                  if v != token_for("claude@desk")), "not-a-token")
    check("another machine's token cannot act as claude@desk",
          "_error" in call("hub_say", {"as": "claude@desk", "body": "x"}, token=other))
    check("the machine token covers a subagent",
          "_error" not in call("hub_hello", {"as": "claude@desk/reviewer"},
                               token=token_for("claude@desk")))
    v = call("hub_say", {"as": "claude@desk", "body": "signed message"})
    got = call("hub_peek", {"limit": 1})
    check("authenticated sender is marked verified",
          got.get("messages", [{}])[-1].get("verified") is True, got)
else:
    print("\nauth: skipped (no data/tokens.json -- hub is in open mode)")

print("\nmessaging")
m1 = call("hub_say", {"as": "claude@desk", "body": "Anyone touched the cook script?",
                      "to": ["codex@11"]})
check("direct message posts", isinstance(m1.get("posted"), int), m1)
m2 = call("hub_say", {"as": "grok@desk", "body": "Broadcast: build box is free."})
check("broadcast posts", isinstance(m2.get("posted"), int), m2)
call("hub_say", {"as": "claude@desk", "body": "The shader map is clean now.", "topic": "game"})

inbox = call("hub_inbox", {"as": "codex@11"})
bodies = [m["body"] for m in inbox.get("messages", [])]
check("addressee receives the direct message",
      any("cook script" in b for b in bodies), bodies)
check("addressee also receives the broadcast",
      any("build box is free" in b for b in bodies), bodies)
check("addressee does NOT get the unrelated topic message",
      not any("shader map" in b for b in bodies), bodies)

again = call("hub_inbox", {"as": "codex@11"})
check("inbox marks read (second call is empty)", again.get("count") == 0, again)

call("hub_say", {"as": "claude@desk", "body": "Explicitly addressed on a topic.",
                 "topic": "game", "to": ["codex@11"]})
ex = call("hub_inbox", {"as": "codex@11"})
check("explicit `to` still notifies across an unsubscribed topic",
      any("Explicitly addressed" in m["body"] for m in ex.get("messages", [])), ex)

print("\nlisten-in (addressing gates notification, not visibility)")
peek = call("hub_peek", {"topic": "game"})
check("uninvolved agent can read the game topic",
      any("shader map" in m["body"] for m in peek.get("messages", [])), peek)
allpeek = call("hub_peek", {"limit": 50})
check("peek sees every message", len(allpeek.get("messages", [])) >= 3, allpeek)

print("\ntopic subscription")
call("hub_subscribe", {"as": "grok@desk", "topic": "game"})
call("hub_inbox", {"as": "grok@desk"})  # drain
call("hub_say", {"as": "claude@desk", "body": "Cook finished, 0 errors.", "topic": "game"})
gi = call("hub_inbox", {"as": "grok@desk"})
check("subscriber receives topic traffic unaddressed",
      any("Cook finished" in m["body"] for m in gi.get("messages", [])), gi)

print("\nfamily addressing reaches subagents")
call("hub_inbox", {"as": "claude@desk/reviewer"})  # drain
call("hub_say", {"as": "codex@11", "body": "Family ping.", "to": ["claude@desk"]})
ri = call("hub_inbox", {"as": "claude@desk/reviewer"})
check("subagent sees mail addressed to its family",
      any("Family ping" in m["body"] for m in ri.get("messages", [])), ri)

print("\nsearch")
s = call("hub_search", {"query": "shader"})
check("search finds the shader message",
      any("shader" in m["body"].lower() for m in s.get("messages", [])), s)
check("empty query rejected", "_error" in call("hub_search", {"query": ""}))

print("\ntopics")
t = call("hub_topics", {})
check("game topic is listed", any(x["topic"] == "game" for x in t.get("topics", [])), t)
check("default topic is listed", any(x["topic"] == "" for x in t.get("topics", [])), t)

print("\ntasks / board")
t1 = call("hub_task_create", {"as": "claude@desk", "title": "Port EnhancedInput binds", "topic": "game"})
tid = t1.get("task", {}).get("id")
check("task created as pending", t1.get("task", {}).get("status") == "pending", t1)
check("task owner defaults to creator", t1.get("task", {}).get("owner") == "claude@desk", t1)
t2 = call("hub_task_create", {"as": "claude@desk", "title": "Review the diff",
                              "owner": "claude@desk/reviewer", "parent_id": tid})
check("subagent task owned by subagent",
      t2.get("task", {}).get("owner") == "claude@desk/reviewer", t2)
check("subagent task rolls up to the family column",
      t2.get("task", {}).get("family") == "claude@desk", t2)
call("hub_task_update", {"as": "claude@desk", "id": tid, "status": "active"})
done = call("hub_task_update", {"as": "claude@desk", "id": tid, "status": "done"})
check("task can be completed", done.get("task", {}).get("status") == "done", done)
check("completion is timestamped", bool(done.get("task", {}).get("done_at")), done)
check("bad status rejected",
      "_error" in call("hub_task_update", {"as": "claude@desk", "id": tid, "status": "sideways"}))
lst = call("hub_tasks", {"owner": "claude@desk"})
check("family task list includes the subagent task", lst.get("count", 0) >= 2, lst)

print("\nHTTP surface")


def get(path, token=""):
    req = urllib.request.Request(f"{BASE}{path}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


check("/health", get("/health")[1].strip() == "ok")
check("/api/who serves json", '"agents"' in get("/api/who")[1])
check("/api/tasks serves json", '"tasks"' in get("/api/tasks")[1])
check("/ serves the chat page", "AgentHub" in get("/")[1])
check("/board serves the board", "board" in get("/board")[1].lower())
code, text = get("/hook/poll?as=grok%40desk", token_for("grok@desk"))
check("/hook/poll returns plain text for a hook", code == 200)
if FAMILY_TOKENS:
    code, text = get("/hook/poll?as=grok%40desk")
    check("/hook/poll refuses an unauthenticated agent", "not authorised" in text.lower(), text)

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILURE(S): " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
