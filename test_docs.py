#!/usr/bin/env python3
"""The house documents behind the Rules page: who may change them, stale saves,
history, line endings, and the notice a save posts.

Starts its own hub on a scratch port with a scratch documents folder, so it never
touches live data.

  python3 test_docs.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from testutil import remove_tree  # noqa: E402

PORT = int(os.environ.get("TEST_PORT", "8797"))
BASE = f"http://127.0.0.1:{PORT}"
TOK = {"*@desk": "t-desk", "human@hub": "t-human"}
FAILS = []


def req(method, path, body=None, token=""):
    """(status, json) for one request."""
    r = urllib.request.Request(f"{BASE}{path}", method=method,
                               data=json.dumps(body).encode() if body is not None else None,
                               headers={"Content-Type": "application/json"})
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def doc(name):
    return req("GET", "/api/doc?name=" + urllib.parse.quote(name))[1]["doc"]


def save(name, text, base, who="human@hub", token="t-human", note=""):
    return req("POST", "/api/doc", {"as": who, "name": name, "text": text, "base_sha": base,
                                    "note": note}, token)


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


tmp = tempfile.mkdtemp(prefix="agenthub-docs-")
docs = os.path.join(tmp, "docs")
os.makedirs(docs)
with open(os.path.join(docs, "RULES.md"), "wb") as fh:
    fh.write(b"# Rules\n\n1. Be kind.\n")
with open(os.path.join(docs, "CRLF.md"), "wb") as fh:
    fh.write(b"# Windows\r\n\r\nline\r\n")
with open(os.path.join(docs, "PRIVATE.md"), "wb") as fh:
    fh.write(b"not listed\n")
with open(os.path.join(tmp, "tokens.json"), "w") as fh:
    json.dump(TOK, fh)

env = dict(os.environ, HUB_PORT=str(PORT), HUB_DB=os.path.join(tmp, "t.db"),
           HUB_TOKENS_FILE=os.path.join(tmp, "tokens.json"), HUB_HUMANS="human@hub",
           HUB_DOCS_DIR=docs, HUB_DOCS="RULES.md,CRLF.md,MISSING.md",
           HUB_WEB_DIR=os.path.join(os.path.dirname(os.path.abspath(__file__)), "web"))
proc = start(env)
try:
    print(f"house document tests against {BASE}\n")

    print("listing")
    _, r = req("GET", "/api/docs?as=human@hub", token="t-human")
    check("only the configured files that exist, in order",
          [d["name"] for d in r["docs"]] == ["RULES.md", "CRLF.md"], r)
    check("a human with its token may edit", r["can_edit"] is True, r)
    _, r = req("GET", "/api/docs?as=claude@desk", token="t-desk")
    check("an agent may not", r["can_edit"] is False, r)
    code, r = req("GET", "/api/doc?name=PRIVATE.md")
    check("an unlisted file cannot be read", code == 400, r)
    code, r = req("GET", "/api/doc?name=..%2Ftokens.json")
    check("no path escapes the folder", code == 400, r)

    print("\nwho may save")
    d = doc("RULES.md")
    new = d["text"] + "2. Be brief.\n"
    code, r = save("RULES.md", new, d["sha"], who="claude@desk", token="t-desk")
    check("an agent is refused (403)", code == 403, r)
    code, r = save("RULES.md", new, d["sha"], token="wrong")
    check("a human with the wrong token is refused (403)", code == 403, r)
    code, r = save("RULES.md", "   ", d["sha"])
    check("an empty document is refused", code == 400, r)

    print("\nsaving")
    code, r = save("RULES.md", new, d["sha"], note="add rule 2")
    check("a human saves", code == 200 and r.get("revision"), r)
    with open(os.path.join(docs, "RULES.md"), "rb") as fh:
        check("the file on disk is the new text", fh.read() == new.encode(), "")
    check("no temporary file is left behind", sorted(os.listdir(docs)) == ["CRLF.md", "PRIVATE.md", "RULES.md"],
          os.listdir(docs))
    _, m = req("GET", "/api/messages?limit=5")
    last = m["messages"][-1]
    check("the save posts a notice from the human", last["from"] == "human@hub" and "RULES.md" in last["body"]
          and "add rule 2" in last["body"], last)
    code, r2 = save("RULES.md", new, r["doc"]["sha"])
    check("saving the same text changes nothing", code == 200 and r2.get("unchanged"), r2)

    print("\nstale saves")
    code, r = save("RULES.md", new + "3. stale\n", d["sha"])
    check("a save from an old version is refused (409)", code == 409, r)
    check("and carries the current version back", r.get("current", {}).get("text") == new, r)
    with open(os.path.join(docs, "RULES.md"), "ab") as fh:
        fh.write(b"3. Edited on the share.\n")
    outside = doc("RULES.md")
    code, r = save("RULES.md", new + "4. Mine.\n", outside["sha"], note="replace outside edit")
    check("a save from the current version replaces an outside edit", code == 200, r)

    print("\nhistory")
    _, h = req("GET", "/api/doc/history?name=RULES.md")
    revs = h["revisions"]
    check("original, first save, outside edit, second save",
          [v["author"] for v in revs] == ["human@hub", "(file)", "human@hub", "(file)"], revs)
    _, v = req("GET", f"/api/doc/revision?id={revs[1]['id']}")
    check("the outside edit is kept", "Edited on the share" in v["revision"]["text"], v)
    check("notes are kept", revs[0]["note"] == "replace outside edit", revs[0])

    print("\nline endings")
    c = doc("CRLF.md")
    code, r = save("CRLF.md", "# Windows\n\nline\nmore", c["sha"])
    with open(os.path.join(docs, "CRLF.md"), "rb") as fh:
        raw = fh.read()
    check("a CRLF file stays CRLF, with a final newline", raw == b"# Windows\r\n\r\nline\r\nmore\r\n", raw)
finally:
    proc.kill()
    proc.wait()
    remove_tree(tmp)

print(f"\n{'ALL PASSED' if not FAILS else f'{len(FAILS)} FAILED: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
