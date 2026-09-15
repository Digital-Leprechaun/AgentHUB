#!/usr/bin/env python3
"""AgentHub: an MCP communication hub for coding agents on a local network.

The successor to agent-relay 0.1. Standard library only. One port serves three
audiences:

  MCP     POST /mcp                  Streamable HTTP MCP endpoint. Claude Code,
                                     Codex CLI and Grok Build all connect here.
  Humans  GET  /                     live chat + log
          GET  /board                Kanban of agents and tasks
  Hooks   GET  /hook/poll?as=X       plain text for a PostToolUse hook to inject
          WS   /wake?as=X            wake channel for an idle agent's runner

Identity is per CALL, not per connection: every MCP tool takes an `as` argument
naming the exact agent, e.g. "claude@desk" or "claude@desk/reviewer". This is
deliberate -- Claude Code subagents run in-process and share the parent's MCP
connection, so connection-scoped identity would collapse every subagent into its
parent and make the board wrong.

Auth is a bearer token per FAMILY (vendor@host). A family token covers all of
that family's subagents. The server records whether the sender's token verified,
so readers can trust the `from` field.

Append-only is OPTIONAL and off by default (it was mandatory in 0.1). Set
HUB_APPEND_ONLY=1 to install triggers that abort any DELETE on messages and any
UPDATE other than the retention flag; the server drops those triggers again when
the flag is off, so it can be toggled on a live database either way.

Retention: messages older than HUB_RETAIN_DAYS (default 14) are flagged
archived=1. They drop out of the default views and search but are never removed
and remain searchable with include_archived.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import struct
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("HUB_PORT", "8787"))
DB_PATH = os.environ.get("HUB_DB", os.path.join("data", "hub.db"))
TOKENS_FILE = os.environ.get("HUB_TOKENS_FILE", os.path.join("data", "tokens.json"))
WEB_DIR = os.environ.get("HUB_WEB_DIR", "web")
RETAIN_DAYS = int(os.environ.get("HUB_RETAIN_DAYS", "14"))
APPEND_ONLY = os.environ.get("HUB_APPEND_ONLY", "0") in ("1", "true", "yes")
# Families that are people, not agents. Only a human may issue a global STOP NOW.
HUMANS = {h.strip().lower() for h in os.environ.get("HUB_HUMANS", "human@hub").split(",") if h.strip()}
PLANNING_TURNS = int(os.environ.get("HUB_PLANNING_TURNS", "3"))
TASK_STATUSES = ("planning", "pending", "active", "blocked", "done")
SWEEP_SECONDS = 3600
ACTIVE_SECONDS = 90          # seen this recently => "active" on the board
WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
KEEPALIVE_SECONDS = 30
MAX_BODY = 32000
MAX_WAIT = 60
PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "agenthub"
SERVER_VERSION = "2.0.0"

ADDR_RE = re.compile(r"^([a-z0-9][a-z0-9._-]{0,31})@([a-z0-9][a-z0-9._-]{0,31})(?:/([a-z0-9][a-z0-9._/-]{0,63}))?$", re.I)
TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,63}$", re.I)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"{now_iso()} {msg}", flush=True)


class HubError(Exception):
    """A user-facing error; the message is safe to return to the agent."""


# ----------------------------------------------------------------------------
# identity
# ----------------------------------------------------------------------------
class Addr:
    """vendor@host[/role] -- e.g. claude@desk, claude@desk/reviewer."""

    __slots__ = ("vendor", "host", "role")

    def __init__(self, vendor: str, host: str, role: str = ""):
        self.vendor, self.host, self.role = vendor.lower(), host.lower(), role

    @classmethod
    def parse(cls, s: str) -> "Addr":
        m = ADDR_RE.match((s or "").strip())
        if not m:
            raise HubError(
                f"bad address {s!r}; expected vendor@host or vendor@host/role, "
                "e.g. claude@desk or claude@desk/reviewer"
            )
        return cls(m.group(1), m.group(2), m.group(3) or "")

    @property
    def family(self) -> str:
        return f"{self.vendor}@{self.host}"

    def __str__(self) -> str:
        return self.family + (f"/{self.role}" if self.role else "")


def norm_topic(t: str | None) -> str:
    """'' is the default topic and is always valid."""
    t = (t or "").strip()
    if not t:
        return ""
    if not TOPIC_RE.match(t):
        raise HubError(f"bad topic {t!r}; letters, digits, . _ - / only")
    return t.lower()


# ----------------------------------------------------------------------------
# storage
# ----------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS messages(
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       TEXT    NOT NULL,
  frm      TEXT    NOT NULL,
  family   TEXT    NOT NULL,
  vendor   TEXT    NOT NULL,
  host     TEXT    NOT NULL,
  topic    TEXT    NOT NULL DEFAULT '',
  recips   TEXT    NOT NULL DEFAULT '[]',   -- JSON array; [] means broadcast
  reply_to INTEGER,
  verified INTEGER NOT NULL DEFAULT 0,
  body     TEXT    NOT NULL,
  archived INTEGER NOT NULL DEFAULT 0,
  task_id  INTEGER                          -- the task this message is about, if any
);
CREATE INDEX IF NOT EXISTS ix_msg_topic   ON messages(topic, id);
CREATE INDEX IF NOT EXISTS ix_msg_family  ON messages(family, id);
CREATE INDEX IF NOT EXISTS ix_msg_arch    ON messages(archived, id);

CREATE TABLE IF NOT EXISTS agents(
  address    TEXT PRIMARY KEY,
  family     TEXT NOT NULL,
  vendor     TEXT NOT NULL,
  host       TEXT NOT NULL,
  role       TEXT NOT NULL DEFAULT '',
  parent     TEXT,
  first_seen TEXT NOT NULL,
  last_seen  TEXT NOT NULL,
  ws_open    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cursors(
  address   TEXT PRIMARY KEY,
  last_read INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS subs(
  address TEXT NOT NULL,
  topic   TEXT NOT NULL,
  PRIMARY KEY(address, topic)
);

-- Tasks are a mutable projection; task_events below is the history.
CREATE TABLE IF NOT EXISTS tasks(
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  title     TEXT NOT NULL,
  body      TEXT NOT NULL DEFAULT '',
  owner     TEXT NOT NULL,
  family    TEXT NOT NULL,
  vendor    TEXT NOT NULL,
  host      TEXT NOT NULL,
  topic     TEXT NOT NULL DEFAULT '',
  status    TEXT NOT NULL DEFAULT 'pending',  -- planning|pending|active|blocked|done
  parent_id INTEGER,
  created   TEXT NOT NULL,
  updated   TEXT NOT NULL,
  done_at   TEXT
);
CREATE INDEX IF NOT EXISTS ix_task_family ON tasks(family, status);

CREATE TABLE IF NOT EXISTS task_events(
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      TEXT NOT NULL,
  task_id INTEGER NOT NULL,
  actor   TEXT NOT NULL,
  kind    TEXT NOT NULL,
  detail  TEXT NOT NULL DEFAULT ''
);

-- Stops. scope='all' is a human STOP NOW; scope='agent' is an agent stopping another
-- agent that is working in its domain. A stop stays in force until lifted.
CREATE TABLE IF NOT EXISTS stops(
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        TEXT NOT NULL,
  issuer    TEXT NOT NULL,
  scope     TEXT NOT NULL,
  target    TEXT,
  domain    TEXT NOT NULL DEFAULT '',
  reason    TEXT NOT NULL,
  lifted_ts TEXT,
  lifted_by TEXT
);
"""

# Installed only when HUB_APPEND_ONLY=1, and dropped again when it is off, so the
# setting can be toggled on a live database in either direction.
APPEND_ONLY_ON = """
CREATE TRIGGER IF NOT EXISTS messages_no_delete
BEFORE DELETE ON messages
BEGIN SELECT RAISE(ABORT, 'messages are append-only: delete is forbidden'); END;
DROP TRIGGER IF EXISTS messages_no_update;
CREATE TRIGGER messages_no_update
BEFORE UPDATE OF id, ts, frm, family, vendor, host, topic, recips, reply_to, verified, body, task_id
ON messages
BEGIN SELECT RAISE(ABORT, 'messages are append-only: edit is forbidden'); END;
CREATE TRIGGER IF NOT EXISTS task_events_no_delete
BEFORE DELETE ON task_events
BEGIN SELECT RAISE(ABORT, 'task_events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS task_events_no_update
BEFORE UPDATE ON task_events
BEGIN SELECT RAISE(ABORT, 'task_events are append-only'); END;
"""

APPEND_ONLY_OFF = """
DROP TRIGGER IF EXISTS messages_no_delete;
DROP TRIGGER IF EXISTS messages_no_update;
DROP TRIGGER IF EXISTS task_events_no_delete;
DROP TRIGGER IF EXISTS task_events_no_update;
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  body, content='messages', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, body) VALUES (new.id, new.body);
END;
"""


class Store:
    def __init__(self, path: str):
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.cond = threading.Condition()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        # Migrations for databases created by an earlier build.
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(messages)")}
        if "task_id" not in cols:
            self.db.execute("ALTER TABLE messages ADD COLUMN task_id INTEGER")
            log("migrated: messages.task_id added")
        self.db.execute("CREATE INDEX IF NOT EXISTS ix_msg_task ON messages(task_id, id)")
        # Hook events tell the hub which session an agent is running and whether it is
        # busy; the Codex wake bridge needs both to wake the right session at the right time.
        acols = {r[1] for r in self.db.execute("PRAGMA table_info(agents)")}
        for col in ("session_id", "last_event", "last_event_at"):
            if col not in acols:
                self.db.execute(f"ALTER TABLE agents ADD COLUMN {col} TEXT")
                log(f"migrated: agents.{col} added")
        self.db.executescript(APPEND_ONLY_ON if APPEND_ONLY else APPEND_ONLY_OFF)
        log(f"append-only: {'ENFORCED' if APPEND_ONLY else 'off'}")
        self.fts = True
        try:
            self.db.executescript(FTS_SCHEMA)
        except sqlite3.OperationalError as e:
            self.fts = False
            log(f"note: FTS5 unavailable ({e}); search falls back to LIKE")
        self.db.commit()

    # -- helpers ------------------------------------------------------------
    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()):
        r = self.q(sql, args)
        return r[0] if r else None

    def newest_id(self) -> int:
        return self.one("SELECT COALESCE(MAX(id),0) AS n FROM messages")["n"]

    def wait_for_new(self, since_id: int, timeout: float) -> None:
        deadline = time.time() + timeout
        with self.cond:
            while self.newest_id() <= since_id:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return
                self.cond.wait(min(remaining, 5))

    def _bump(self) -> None:
        with self.cond:
            self.cond.notify_all()

    # -- agents -------------------------------------------------------------
    def touch(self, a: Addr, parent: str | None = None) -> None:
        ts = now_iso()
        with self.lock:
            self.db.execute(
                "INSERT INTO agents(address,family,vendor,host,role,parent,first_seen,last_seen)"
                " VALUES(?,?,?,?,?,?,?,?)"
                " ON CONFLICT(address) DO UPDATE SET last_seen=excluded.last_seen,"
                " parent=COALESCE(excluded.parent, agents.parent)",
                (str(a), a.family, a.vendor, a.host, a.role, parent, ts, ts),
            )
            self.db.commit()

    def note_event(self, address: str, event: str, session: str) -> None:
        """Record the latest hook event (and the session it came from) for an agent."""
        event = (event or "").strip()[:40]
        session = (session or "").strip()[:128]
        if not event:
            return
        with self.lock:
            self.db.execute(
                "UPDATE agents SET last_event=?, last_event_at=?,"
                " session_id=COALESCE(NULLIF(?, ''), session_id) WHERE address=?",
                (event, now_iso(), session, address),
            )
            self.db.commit()

    def agent(self, address: str) -> dict | None:
        r = self.one("SELECT * FROM agents WHERE address=?", (address,))
        return dict(r) if r else None

    def set_ws(self, address: str, open_: bool) -> None:
        with self.lock:
            self.db.execute("UPDATE agents SET ws_open=? WHERE address=?", (1 if open_ else 0, address))
            self.db.commit()

    def who(self) -> list[dict]:
        rows = self.q("SELECT * FROM agents ORDER BY family, role")
        out = []
        now = time.time()
        for r in rows:
            try:
                seen = datetime.strptime(r["last_seen"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                seen = 0
            age = now - seen
            state = "connected" if r["ws_open"] else ("active" if age < ACTIVE_SECONDS else "away")
            out.append(
                {
                    "address": r["address"], "family": r["family"], "vendor": r["vendor"],
                    "host": r["host"], "role": r["role"], "parent": r["parent"],
                    "last_seen": r["last_seen"], "state": state,
                    "last_event": r["last_event"], "last_event_at": r["last_event_at"],
                    "unread": self.unread_count(r["address"]),
                }
            )
        return out

    # -- messages -----------------------------------------------------------
    def post(self, frm: Addr, body: str, recips: list[str], topic: str,
             reply_to: int | None, verified: bool, task_id: int | None = None) -> dict:
        body = (body or "").strip()
        if not body:
            raise HubError("empty message")
        if len(body) > MAX_BODY:
            raise HubError(f"message too long ({len(body)} > {MAX_BODY})")
        if task_id is not None:
            self.task(int(task_id))  # raises if there is no such task
            task_id = int(task_id)
        ts = now_iso()
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO messages(ts,frm,family,vendor,host,topic,recips,reply_to,verified,body,task_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (ts, str(frm), frm.family, frm.vendor, frm.host, topic,
                 json.dumps(recips), reply_to, 1 if verified else 0, body, task_id),
            )
            self.db.commit()
            mid = cur.lastrowid
        self._bump()
        return {"id": mid, "ts": ts, "from": str(frm), "topic": topic, "to": recips,
                "reply_to": reply_to, "verified": verified, "task_id": task_id, "body": body}

    @staticmethod
    def _row_to_msg(r: sqlite3.Row) -> dict:
        return {
            "id": r["id"], "ts": r["ts"], "from": r["frm"], "topic": r["topic"],
            "to": json.loads(r["recips"]), "reply_to": r["reply_to"],
            "verified": bool(r["verified"]), "archived": bool(r["archived"]),
            "task_id": r["task_id"], "body": r["body"],
        }

    def planning_turns(self, task_id: int) -> int:
        """Planning turns: agent-to-agent messages about a task while it is still in planning."""
        return self.one(
            "SELECT COUNT(*) AS n FROM messages WHERE task_id=?"
            " AND ts >= COALESCE((SELECT MAX(ts) FROM task_events WHERE task_id=?"
            "   AND detail LIKE '%status->planning%'), '')"
            " AND family NOT IN (SELECT value FROM json_each(?))",
            (task_id, task_id, json.dumps(sorted(HUMANS))),
        )["n"]

    def targeted_at(self, address: str) -> str:
        """SQL fragment: is this message *for* `address`? (notification, not visibility)

        An unaddressed message broadcasts only on the DEFAULT topic. On a named
        topic it notifies subscribers only -- otherwise topics would not segregate
        anything and every agent's inbox would carry every conversation. Anyone can
        still read any topic with peek(); this governs notification alone.
        """
        return (
            "((recips = '[]' AND topic = '')"                  # broadcast on the default topic
            " OR recips LIKE :exact"                           # addressed to me
            " OR recips LIKE :fam"                             # addressed to my family
            " OR topic IN (SELECT topic FROM subs WHERE address = :me)"
            " OR body LIKE :mention)"
        )

    def is_for(self, msg: dict, address: str) -> bool:
        """Python twin of targeted_at(): does this message notify `address`?"""
        if msg.get("from") == address:
            return False
        family = address.split("/")[0]
        recips = msg.get("to") or []
        topic = msg.get("topic") or ""
        if not recips and topic == "":
            return True
        if address in recips or family in recips:
            return True
        if topic and topic in self.subs_of(address):
            return True
        return f"@{address}" in (msg.get("body") or "")

    def _target_args(self, a: Addr) -> dict:
        return {
            "me": str(a),
            "exact": f'%"{str(a)}"%',
            "fam": f'%"{a.family}"%',
            "mention": f"%@{str(a)}%",
        }

    def unread_count(self, address: str) -> int:
        row = self.one("SELECT last_read FROM cursors WHERE address=?", (address,))
        last = row["last_read"] if row else 0
        try:
            a = Addr.parse(address)
        except HubError:
            return 0
        args = self._target_args(a)
        args["last"] = last
        sql = (f"SELECT COUNT(*) AS n FROM messages WHERE id > :last AND frm != :me "
               f"AND archived=0 AND {self.targeted_at(address)}")
        with self.lock:
            return self.db.execute(sql, args).fetchone()["n"]

    def inbox(self, a: Addr, mark: bool = True, limit: int = 100) -> list[dict]:
        address = str(a)
        row = self.one("SELECT last_read FROM cursors WHERE address=?", (address,))
        last = row["last_read"] if row else 0
        args = self._target_args(a)
        args["last"] = last
        args["lim"] = limit
        sql = (f"SELECT * FROM messages WHERE id > :last AND frm != :me AND archived=0 "
               f"AND {self.targeted_at(address)} ORDER BY id LIMIT :lim")
        with self.lock:
            rows = self.db.execute(sql, args).fetchall()
        msgs = [self._row_to_msg(r) for r in rows]
        if mark and msgs:
            self.set_cursor(address, msgs[-1]["id"])
        return msgs

    def set_cursor(self, address: str, mid: int) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO cursors(address,last_read) VALUES(?,?)"
                " ON CONFLICT(address) DO UPDATE SET last_read=MAX(last_read, excluded.last_read)",
                (address, mid),
            )
            self.db.commit()

    def peek(self, topic: str | None = None, frm: str | None = None,
             since: int = 0, limit: int = 50, include_archived: bool = False,
             task_id: int | None = None) -> list[dict]:
        sql = "SELECT * FROM messages WHERE id > ?"
        args: list = [since]
        if not include_archived:
            sql += " AND archived=0"
        if topic is not None:
            sql += " AND topic = ?"
            args.append(norm_topic(topic))
        if task_id is not None:
            sql += " AND task_id = ?"
            args.append(int(task_id))
        if frm:
            sql += " AND (frm = ? OR family = ?)"
            args += [frm, frm]
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(limit, 500)))
        rows = self.q(sql, tuple(args))
        return [self._row_to_msg(r) for r in reversed(rows)]

    def search(self, query: str, limit: int = 50, include_archived: bool = False) -> list[dict]:
        query = (query or "").strip()
        if not query:
            raise HubError("empty search query")
        limit = max(1, min(limit, 200))
        if self.fts:
            sql = ("SELECT m.* FROM messages_fts f JOIN messages m ON m.id = f.rowid"
                   " WHERE messages_fts MATCH ?")
            args: list = [query]
            if not include_archived:
                sql += " AND m.archived=0"
            sql += " ORDER BY m.id DESC LIMIT ?"
            args.append(limit)
            try:
                rows = self.q(sql, tuple(args))
                return [self._row_to_msg(r) for r in rows]
            except sqlite3.OperationalError:
                pass  # malformed MATCH expression -> fall through to LIKE
        sql = "SELECT * FROM messages WHERE body LIKE ?"
        args = [f"%{query}%"]
        if not include_archived:
            sql += " AND archived=0"
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [self._row_to_msg(r) for r in self.q(sql, tuple(args))]

    def topics(self) -> list[dict]:
        rows = self.q(
            "SELECT topic, COUNT(*) AS n, MAX(id) AS last FROM messages"
            " WHERE archived=0 GROUP BY topic ORDER BY last DESC"
        )
        return [{"topic": r["topic"], "messages": r["n"], "last_id": r["last"]} for r in rows]

    def subscribe(self, address: str, topic: str, on: bool) -> None:
        with self.lock:
            if on:
                self.db.execute("INSERT OR IGNORE INTO subs(address,topic) VALUES(?,?)", (address, topic))
            else:
                self.db.execute("DELETE FROM subs WHERE address=? AND topic=?", (address, topic))
            self.db.commit()

    def subs_of(self, address: str) -> list[str]:
        return [r["topic"] for r in self.q("SELECT topic FROM subs WHERE address=?", (address,))]

    # -- tasks --------------------------------------------------------------
    def task_event(self, task_id: int, actor: str, kind: str, detail: str = "") -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO task_events(ts,task_id,actor,kind,detail) VALUES(?,?,?,?,?)",
                (now_iso(), task_id, actor, kind, detail),
            )
            self.db.commit()

    def task_create(self, actor: Addr, title: str, body: str, owner: Addr,
                    topic: str, parent_id: int | None) -> dict:
        title = (title or "").strip()
        if not title:
            raise HubError("task needs a title")
        ts = now_iso()
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO tasks(title,body,owner,family,vendor,host,topic,status,parent_id,created,updated)"
                " VALUES(?,?,?,?,?,?,?,'pending',?,?,?)",
                (title, body or "", str(owner), owner.family, owner.vendor, owner.host,
                 topic, parent_id, ts, ts),
            )
            self.db.commit()
            tid = cur.lastrowid
        self.task_event(tid, str(actor), "created", title)
        self._bump()
        return self.task(tid)

    def task(self, tid: int) -> dict:
        r = self.one("SELECT * FROM tasks WHERE id=?", (tid,))
        if not r:
            raise HubError(f"no task {tid}")
        return dict(r)

    def task_update(self, actor: Addr, tid: int, **fields) -> dict:
        allowed = {"title", "body", "owner", "status", "topic"}
        sets, args, notes = [], [], []
        cur = self.task(tid)
        for k, v in fields.items():
            if v is None or k not in allowed:
                continue
            if k == "status" and v not in TASK_STATUSES:
                raise HubError("status must be one of " + ", ".join(TASK_STATUSES))
            if k == "owner":
                o = Addr.parse(v)
                sets += ["owner=?", "family=?", "vendor=?", "host=?"]
                args += [str(o), o.family, o.vendor, o.host]
                notes.append(f"owner->{o}")
                continue
            sets.append(f"{k}=?")
            args.append(v)
            notes.append(f"{k}->{v}")
        if not sets:
            return cur
        sets.append("updated=?")
        args.append(now_iso())
        if fields.get("status") == "done":
            sets.append("done_at=?")
            args.append(now_iso())
        args.append(tid)
        with self.lock:
            self.db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", tuple(args))
            self.db.commit()
        self.task_event(tid, str(actor), "updated", "; ".join(notes))
        self._bump()
        return self.task(tid)

    def tasks(self, owner: str | None = None, status: str | None = None,
              include_done: bool = True) -> list[dict]:
        sql, args = "SELECT * FROM tasks WHERE 1=1", []
        if owner:
            sql += " AND (owner=? OR family=?)"
            args += [owner, owner]
        if status:
            sql += " AND status=?"
            args.append(status)
        elif not include_done:
            sql += " AND status!='done'"
        sql += " ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'planning' THEN 1"
        sql += " WHEN 'blocked' THEN 2 WHEN 'pending' THEN 3 ELSE 4 END, id DESC"
        out = []
        for r in self.q(sql, tuple(args)):
            t = dict(r)
            t["messages"] = self.one("SELECT COUNT(*) AS n FROM messages WHERE task_id=?",
                                     (t["id"],))["n"]
            if t["status"] == "planning":
                t["planning_turns"] = self.planning_turns(t["id"])
            out.append(t)
        return out

    # -- stops ------------------------------------------------------------
    def stop_create(self, issuer: Addr, reason: str, target: str | None, domain: str) -> dict:
        reason = (reason or "").strip()
        if not reason:
            raise HubError("a STOP must carry a reason")
        scope = "agent" if target else "all"
        ts = now_iso()
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO stops(ts,issuer,scope,target,domain,reason) VALUES(?,?,?,?,?,?)",
                (ts, str(issuer), scope, target, domain or "", reason),
            )
            self.db.commit()
            sid = cur.lastrowid
        self._bump()
        return self.stop(sid)

    def stop(self, sid: int) -> dict:
        r = self.one("SELECT * FROM stops WHERE id=?", (sid,))
        if not r:
            raise HubError(f"no stop {sid}")
        d = dict(r)
        d["active"] = d["lifted_ts"] is None
        return d

    def stop_lift(self, sid: int, by: Addr) -> dict:
        with self.lock:
            self.db.execute(
                "UPDATE stops SET lifted_ts=?, lifted_by=? WHERE id=? AND lifted_ts IS NULL",
                (now_iso(), str(by), sid),
            )
            self.db.commit()
        self._bump()
        return self.stop(sid)

    def stops_active(self) -> list[dict]:
        rows = self.q("SELECT * FROM stops WHERE lifted_ts IS NULL ORDER BY id")
        return [dict(r, active=True) for r in rows]

    @staticmethod
    def stop_applies(stop: dict, address: str) -> bool:
        if stop["scope"] == "all":
            return True
        t = (stop.get("target") or "").lower()
        a = address.lower()
        return a == t or a.startswith(t + "/") or a.split("/")[0] == t

    def stops_for(self, address: str) -> list[dict]:
        return [s for s in self.stops_active() if self.stop_applies(s, address)]

    # -- retention ----------------------------------------------------------
    def sweep(self) -> int:
        cutoff = datetime.now(timezone.utc).timestamp() - RETAIN_DAYS * 86400
        cut_iso = datetime.fromtimestamp(cutoff, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.lock:
            cur = self.db.execute(
                "UPDATE messages SET archived=1 WHERE archived=0 AND ts < ?", (cut_iso,)
            )
            self.db.commit()
            n = cur.rowcount
        if n:
            log(f"retention: archived {n} message(s) older than {cut_iso}")
        return n


# ----------------------------------------------------------------------------
# tokens
# ----------------------------------------------------------------------------
class Tokens:
    """Family tokens: {"claude@desk": "secret", ...}.

    Two granularities, and a host may use either:

      "claude@desk": "..."   one token per vendor+host. Narrower blast radius if a
                              single CLI's config leaks, since each vendor keeps its
                              config in a different file.
      "*@desk":      "..."   one token for the whole machine; every vendor on that
                              host uses it.

    An exact vendor@host entry wins over the host wildcard, so a machine can share
    one token generally and still pin a single agent to its own.

    Be clear-eyed about what this proves: on a single-user box every CLI can read
    every other CLI's config, so the agents on one host are mutually spoofable
    whatever you issue. The boundary a token really draws is around the MACHINE.
    A token covers its family's subagents, because a subagent shares its parent's
    MCP connection and cannot hold a credential of its own.
    """

    def __init__(self, path: str):
        self.path = path
        self.map: dict[str, str] = {}
        self.mtime = 0.0
        self.lock = threading.Lock()
        self.reload()

    def reload(self) -> None:
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            with self.lock:
                self.map, self.mtime = {}, 0.0
            return
        if st.st_mtime == self.mtime:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            with self.lock:
                self.map = {k.lower(): str(v) for k, v in data.items()}
                self.mtime = st.st_mtime
            log(f"tokens: loaded {len(self.map)} famil(ies) from {self.path}")
        except Exception as e:  # noqa: BLE001
            log(f"tokens: failed to read {self.path}: {e}")

    @property
    def enforced(self) -> bool:
        self.reload()
        with self.lock:
            return bool(self.map)

    def verify(self, a: Addr, token: str | None) -> bool:
        """True if `token` proves the caller owns `a`'s family, or `a`'s whole host."""
        self.reload()
        with self.lock:
            if not self.map:
                return True                       # no tokens configured => open mode
            want = self.map.get(a.family) or self.map.get(f"*@{a.host}")
        if not want:
            return False                          # unknown family on a locked-down hub
        return bool(token) and secrets.compare_digest(token, want)


# ----------------------------------------------------------------------------
# websocket (hand-rolled; carried forward from 0.1)
# ----------------------------------------------------------------------------
class WsConn:
    """Minimal RFC6455 server side: text frames out, ping/pong and close in."""

    def __init__(self, rfile, wfile, sock):
        self.rfile, self.wfile, self.sock = rfile, wfile, sock
        self.lock = threading.Lock()
        self.open = True
        self.fmt = "json"

    @staticmethod
    def accept_key(key: str) -> str:
        return base64.b64encode(hashlib.sha1(key.encode() + WS_GUID).digest()).decode()

    def send(self, text: str) -> bool:
        data = text.encode("utf-8")
        n = len(data)
        if n < 126:
            hdr = struct.pack("!BB", 0x81, n)
        elif n < 65536:
            hdr = struct.pack("!BBH", 0x81, 126, n)
        else:
            hdr = struct.pack("!BBQ", 0x81, 127, n)
        with self.lock:
            if not self.open:
                return False
            try:
                self.wfile.write(hdr + data)
                self.wfile.flush()
                return True
            except Exception:  # noqa: BLE001
                self.open = False
                return False

    def ping(self) -> bool:
        with self.lock:
            if not self.open:
                return False
            try:
                self.wfile.write(struct.pack("!BB", 0x89, 0))
                self.wfile.flush()
                return True
            except Exception:  # noqa: BLE001
                self.open = False
                return False

    def close(self) -> None:
        with self.lock:
            self.open = False
        try:
            self.sock.shutdown(2)
        except Exception:  # noqa: BLE001
            pass

    def read_frame(self) -> tuple[int, bytes] | None:
        """Blocking read of one frame. Returns (opcode, payload) or None on close."""
        try:
            h = self.rfile.read(2)
            if len(h) < 2:
                return None
            b1, b2 = h[0], h[1]
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            ln = b2 & 0x7F
            if ln == 126:
                ln = struct.unpack("!H", self.rfile.read(2))[0]
            elif ln == 127:
                ln = struct.unpack("!Q", self.rfile.read(8))[0]
            if ln > 1 << 20:
                return None
            mask = self.rfile.read(4) if masked else b""
            payload = self.rfile.read(ln) if ln else b""
            if masked:
                payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
            return opcode, payload
        except Exception:  # noqa: BLE001
            return None


def render_wake(payload: dict, address: str) -> str | None:
    """One human- and model-readable line per wake event, for format=text sockets.

    These lines become notifications in an agent's session (Claude Code's Monitor
    tool turns each one into an event), so they say what arrived and what to do."""
    kind = payload.get("type")
    if kind == "message":
        m = payload["message"]
        topic = f" #{m['topic']}" if m.get("topic") else ""
        task = f" [task {m['task_id']}]" if m.get("task_id") else ""
        body = " ".join((m.get("body") or "").split())
        if len(body) > 240:
            body = body[:237] + "..."
        return (f"[AgentHub] message #{m['id']} for {address} from {m['from']}{topic}{task}: "
                f"{body} -- read it with hub_inbox and reply with hub_say if it concerns you.")
    if kind == "stop":
        s = payload["stop"]
        what = "STOP NOW" if s["scope"] == "all" else f"STOP on {s['target']}"
        return (f"[AgentHub] {what} (stop #{s['id']}) by {s['issuer']}: {s['reason']} "
                f"-- HALT your current task now.")
    if kind == "resume":
        s = payload["stop"]
        return f"[AgentHub] stop #{s['id']} lifted by {s.get('lifted_by')}."
    if kind == "waiting":
        parts = []
        if payload.get("stops"):
            parts.append(f"{len(payload['stops'])} stop(s) in force -- run hub_stops and halt")
        if payload.get("unread"):
            parts.append(f"{payload['unread']} unread message(s) -- read them with hub_inbox")
        return f"[AgentHub] waiting for {address}: " + "; ".join(parts) + "." if parts else None
    return None


class Wakers:
    """Open wake sockets, keyed by agent address. Tier 3: hub -> runner -> CLI.

    A socket opened with format=text receives one readable line per event instead of
    JSON; that is what an agent's own watch (Claude Code's Monitor tool) consumes."""

    def __init__(self):
        self.lock = threading.Lock()
        self.conns: dict[str, list[WsConn]] = {}

    def add(self, address: str, c: WsConn) -> None:
        with self.lock:
            self.conns.setdefault(address, []).append(c)

    def drop(self, address: str, c: WsConn) -> None:
        with self.lock:
            lst = self.conns.get(address) or []
            if c in lst:
                lst.remove(c)
            if not lst:
                self.conns.pop(address, None)

    def addresses(self) -> list[str]:
        with self.lock:
            return list(self.conns)

    def send_to(self, address: str, payload: dict) -> int:
        """Send to the sockets registered under exactly `address`. Callers decide who
        a message is for (see Hub._notify); expanding families here as well would
        deliver the same event twice to a subagent's socket."""
        with self.lock:
            conns = list(self.conns.get(address) or [])
        sent = 0
        for c in conns:
            text = render_wake(payload, address) if c.fmt == "text" else json.dumps(payload)
            if text is None:
                continue
            if c.send(text):
                sent += 1
        return sent

    def broadcast(self, payload: dict) -> None:
        text = json.dumps(payload)
        with self.lock:
            everyone = [c for cs in self.conns.values() for c in cs]
        for c in everyone:
            c.send(text)


class Watchers:
    """Browser SSE clients for the live monitor and board."""

    def __init__(self):
        self.lock = threading.Lock()
        self.qs: list[list] = []

    def add(self) -> list:
        q: list = []
        with self.lock:
            self.qs.append(q)
        return q

    def drop(self, q: list) -> None:
        with self.lock:
            if q in self.qs:
                self.qs.remove(q)

    def push(self, event: str, data: dict) -> None:
        with self.lock:
            qs = list(self.qs)
        for q in qs:
            q.append((event, data))


# ----------------------------------------------------------------------------
# MCP tool surface
# ----------------------------------------------------------------------------
AS_PROP = {
    "type": "string",
    "description": "Your address: vendor@host, or vendor@host/role for a subagent. "
                   "e.g. claude@desk or claude@desk/reviewer. Required on every call.",
}

TOOLS = [
    {
        "name": "hub_hello",
        "description": "Announce yourself (or a subagent) and heartbeat. Returns who else is "
                       "online, your subscribed topics and your unread count. Call once at the "
                       "start of a session.",
        "inputSchema": {"type": "object", "required": ["as"], "properties": {
            "as": AS_PROP,
            "parent": {"type": "string", "description": "Parent agent address, if you are a subagent."},
        }},
    },
    {
        "name": "hub_say",
        "description": "Post a message. Addressing controls who gets NOTIFIED, never who can "
                       "read: every agent can read everything with hub_peek. With no `to` and "
                       "no `topic` the message reaches everyone; with no `to` but a `topic` it "
                       "notifies that topic's subscribers only; an explicit `to` always notifies.",
        "inputSchema": {"type": "object", "required": ["as", "body"], "properties": {
            "as": AS_PROP,
            "body": {"type": "string", "description": "Message text."},
            "to": {"type": "array", "items": {"type": "string"},
                   "description": "Addresses to notify. A family (claude@desk) reaches its "
                                  "subagents. Empty means broadcast."},
            "topic": {"type": "string", "description": "Topic to post under. Omit for the default topic."},
            "reply_to": {"type": "integer", "description": "Message id this replies to."},
            "task_id": {"type": "integer", "description": "The task this message is about. Tag every "
                        "message that concerns a task so its history stays together and "
                        "planning turns can be counted."},
        }},
    },
    {
        "name": "hub_inbox",
        "description": "Fetch messages addressed to you since you last read, and mark them read. "
                       "Set wait to block up to N seconds for something to arrive.",
        "inputSchema": {"type": "object", "required": ["as"], "properties": {
            "as": AS_PROP,
            "wait": {"type": "integer", "description": "Long-poll seconds, 0 to 60. Default 0."},
            "peek": {"type": "boolean", "description": "Read without marking as read."},
        }},
    },
    {
        "name": "hub_peek",
        "description": "Read the log regardless of addressing. This is how you listen in on a "
                       "conversation you were not addressed in and decide whether to join.",
        "inputSchema": {"type": "object", "properties": {
            "topic": {"type": "string", "description": "Limit to a topic."},
            "from": {"type": "string", "description": "Limit to an agent or family."},
            "since": {"type": "integer", "description": "Only messages after this id."},
            "limit": {"type": "integer", "description": "Max messages, default 50."},
            "include_archived": {"type": "boolean", "description": "Include messages past the retention window."},
            "task_id": {"type": "integer", "description": "Only messages tagged with this task."},
        }},
    },
    {
        "name": "hub_search",
        "description": "Full-text search the message log.",
        "inputSchema": {"type": "object", "required": ["query"], "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
            "include_archived": {"type": "boolean"},
        }},
    },
    {
        "name": "hub_topics",
        "description": "List topics with message counts. The empty topic is the default one.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "hub_subscribe",
        "description": "Subscribe to a topic so its messages land in your inbox without being "
                       "addressed to you. Set on=false to unsubscribe.",
        "inputSchema": {"type": "object", "required": ["as", "topic"], "properties": {
            "as": AS_PROP,
            "topic": {"type": "string"},
            "on": {"type": "boolean", "description": "Default true."},
        }},
    },
    {
        "name": "hub_who",
        "description": "List known agents, their state (connected, active or away) and unread counts.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "hub_task_create",
        "description": "Create a task. It appears on the hub Kanban board under its owner column.",
        "inputSchema": {"type": "object", "required": ["as", "title"], "properties": {
            "as": AS_PROP,
            "title": {"type": "string"},
            "body": {"type": "string"},
            "owner": {"type": "string", "description": "Owning agent. Defaults to you."},
            "topic": {"type": "string"},
            "parent_id": {"type": "integer", "description": "Parent task id, for a slice of work given to a subagent."},
            "status": {"type": "string", "enum": list(TASK_STATUSES)},
        }},
    },
    {
        "name": "hub_task_update",
        "description": "Change a task: status, owner, title or body. Set status to done to move "
                       "it to the Completed column.",
        "inputSchema": {"type": "object", "required": ["as", "id"], "properties": {
            "as": AS_PROP,
            "id": {"type": "integer"},
            "status": {"type": "string", "enum": list(TASK_STATUSES)},
            "owner": {"type": "string"},
            "title": {"type": "string"},
            "body": {"type": "string"},
            "topic": {"type": "string"},
        }},
    },
    {
        "name": "hub_tasks",
        "description": "List tasks, optionally filtered by owner (agent or family) or status.",
        "inputSchema": {"type": "object", "properties": {
            "owner": {"type": "string"},
            "status": {"type": "string", "enum": list(TASK_STATUSES)},
            "include_done": {"type": "boolean", "description": "Default true."},
        }},
    },
    {
        "name": "hub_stop",
        "description": "Without `target` this is STOP NOW: it halts every agent, and only a "
                       "human may issue it. With `target`, an agent stops another agent that is "
                       "working in its own domain; name the domain. A reason is always required. "
                       "When a stop applies to you, every hub tool result carries a STOP field: halt "
                       "your current task and do not resume until it is lifted.",
        "inputSchema": {"type": "object", "required": ["as", "reason"], "properties": {
            "as": AS_PROP,
            "reason": {"type": "string"},
            "target": {"type": "string", "description": "Agent or family to stop. Omit for STOP NOW (humans only)."},
            "domain": {"type": "string", "description": "The domain the target is working in (for a targeted stop)."},
        }},
    },
    {
        "name": "hub_resume",
        "description": "Lift a stop. A STOP NOW can only be lifted by a human; a targeted stop by "
                       "its issuer or a human.",
        "inputSchema": {"type": "object", "required": ["as", "id"], "properties": {
            "as": AS_PROP,
            "id": {"type": "integer", "description": "Stop id, from hub_stops or the STOP field."},
        }},
    },
    {
        "name": "hub_stops",
        "description": "List stops currently in force.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class Hub:
    def __init__(self, store: Store, tokens: Tokens, wakers: Wakers, watchers: Watchers):
        self.store, self.tokens, self.wakers, self.watchers = store, tokens, wakers, watchers

    # -- auth ---------------------------------------------------------------
    def authed(self, a: Addr, token: str | None) -> bool:
        if not self.tokens.enforced:
            return True
        if not self.tokens.verify(a, token):
            raise HubError(
                f"not authorised for {a.family}: send that family token as "
                "Authorization: Bearer <token>"
            )
        return True

    # -- delivery -----------------------------------------------------------
    def _notify(self, msg: dict) -> None:
        """Wake every open socket this message is for, and push it to browser watchers.

        "For" is the same rule the inbox uses (Store.is_for), so an agent is woken for
        exactly the messages hub_inbox will then hand it -- no more, no less."""
        self.watchers.push("message", msg)
        for w in self.wakers.addresses():
            if self.store.is_for(msg, w):
                self.wakers.send_to(w, {"type": "message", "message": msg})

    # -- dispatch -----------------------------------------------------------
    def call(self, name: str, args: dict, token: str | None) -> dict:
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            raise HubError(f"unknown tool {name}")
        result = fn(args, token)
        who = args.get("as")
        if who and name not in ("hub_stop", "hub_resume"):
            stops = self.store.stops_for(str(who))
            if stops:
                # First key, so it is the first thing an agent reads.
                result = {"STOP": self.stop_notice(stops), **result}
        return result

    @staticmethod
    def stop_notice(stops: list[dict]) -> dict:
        return {
            "instruction": "HALT your current task now. Do not resume until the stop is "
                           "lifted. You may still read the hub and reply about the stop.",
            "stops": [{"id": s["id"], "scope": s["scope"], "target": s["target"],
                       "domain": s["domain"], "issuer": s["issuer"], "reason": s["reason"],
                       "since": s["ts"]} for s in stops],
        }

    def is_human(self, a: Addr, token: str | None) -> bool:
        if a.family not in HUMANS:
            return False
        return (not self.tokens.enforced) or self.tokens.verify(a, token)

    def _me(self, args: dict, token: str | None) -> Addr:
        a = Addr.parse(args.get("as", ""))
        self.authed(a, token)
        self.store.touch(a, args.get("parent"))
        return a

    # -- tools --------------------------------------------------------------
    def t_hub_hello(self, args: dict, token: str | None) -> dict:
        me = self._me(args, token)
        return {
            "you": str(me), "family": me.family,
            "unread": self.store.unread_count(str(me)),
            "topics_subscribed": self.store.subs_of(str(me)),
            "agents": self.store.who(),
            "retention_days": RETAIN_DAYS,
            "note": "Addressing controls notification, not visibility: use hub_peek to read "
                    "anything, including conversations you were not addressed in.",
        }

    def t_hub_say(self, args: dict, token: str | None) -> dict:
        me = self._me(args, token)
        to = args.get("to") or []
        if isinstance(to, str):
            to = [to]
        for r in to:
            Addr.parse(r)  # validate
        topic = norm_topic(args.get("topic"))
        verified = self.tokens.enforced and self.tokens.verify(me, token)
        msg = self.store.post(me, args.get("body", ""), to, topic,
                              args.get("reply_to"), verified, args.get("task_id"))
        self._notify(msg)
        out = {"posted": msg["id"], "ts": msg["ts"], "topic": topic,
               "task_id": msg["task_id"], "notified": to or "everyone"}
        if msg["task_id"] is not None and me.family not in HUMANS:
            t = self.store.task(msg["task_id"])
            if t["status"] == "planning":
                n = self.store.planning_turns(t["id"])
                out["planning_turns"] = n
                if n > PLANNING_TURNS:
                    out["PLANNING_LIMIT"] = (f"Task {t['id']} has had {n} planning exchanges (limit "
                                     f"{PLANNING_TURNS}). Consult a human before planning further, "
                                     "unless one told you to dig deep. Execution is not capped.")
        return out

    def t_hub_inbox(self, args: dict, token: str | None) -> dict:
        me = self._me(args, token)
        wait = max(0, min(int(args.get("wait") or 0), MAX_WAIT))
        peek = bool(args.get("peek"))
        msgs = self.store.inbox(me, mark=not peek)
        if not msgs and wait:
            self.store.wait_for_new(self.store.newest_id(), wait)
            msgs = self.store.inbox(me, mark=not peek)
        return {"you": str(me), "count": len(msgs), "messages": msgs}

    def t_hub_peek(self, args: dict, token: str | None) -> dict:
        msgs = self.store.peek(
            topic=args.get("topic"), frm=args.get("from"),
            since=int(args.get("since") or 0), limit=int(args.get("limit") or 50),
            include_archived=bool(args.get("include_archived")),
            task_id=args.get("task_id"),
        )
        return {"count": len(msgs), "messages": msgs}

    def t_hub_search(self, args: dict, token: str | None) -> dict:
        msgs = self.store.search(args.get("query", ""), int(args.get("limit") or 50),
                                 bool(args.get("include_archived")))
        return {"count": len(msgs), "messages": msgs}

    def t_hub_topics(self, args: dict, token: str | None) -> dict:
        return {"topics": self.store.topics()}

    def t_hub_subscribe(self, args: dict, token: str | None) -> dict:
        me = self._me(args, token)
        topic = norm_topic(args.get("topic"))
        on = args.get("on")
        on = True if on is None else bool(on)
        self.store.subscribe(str(me), topic, on)
        return {"you": str(me), "topics_subscribed": self.store.subs_of(str(me))}

    def t_hub_who(self, args: dict, token: str | None) -> dict:
        return {"agents": self.store.who(), "wake_sockets": self.wakers.addresses()}

    def t_hub_task_create(self, args: dict, token: str | None) -> dict:
        me = self._me(args, token)
        owner = Addr.parse(args["owner"]) if args.get("owner") else me
        t = self.store.task_create(me, args.get("title", ""), args.get("body", ""),
                                   owner, norm_topic(args.get("topic")),
                                   args.get("parent_id"))
        if args.get("status") and args["status"] != "pending":
            t = self.store.task_update(me, t["id"], status=args["status"])
        self.watchers.push("task", t)
        return {"task": t}

    def t_hub_task_update(self, args: dict, token: str | None) -> dict:
        me = self._me(args, token)
        t = self.store.task_update(
            me, int(args["id"]), status=args.get("status"), owner=args.get("owner"),
            title=args.get("title"), body=args.get("body"),
            topic=norm_topic(args["topic"]) if args.get("topic") is not None else None,
        )
        self.watchers.push("task", t)
        return {"task": t}

    def t_hub_tasks(self, args: dict, token: str | None) -> dict:
        inc = args.get("include_done")
        ts = self.store.tasks(args.get("owner"), args.get("status"),
                              True if inc is None else bool(inc))
        return {"count": len(ts), "tasks": ts}

    def t_hub_stop(self, args: dict, token: str | None) -> dict:
        me = self._me(args, token)
        target = (args.get("target") or "").strip().lower() or None
        if target is None and not self.is_human(me, token):
            raise HubError("STOP NOW can only be issued by a human. An agent may stop "
                           "another agent working in its domain by naming a `target`.")
        if target is not None:
            Addr.parse(target)
            if target in (str(me).lower(), me.family):
                raise HubError("an agent cannot stop itself")
        verified = self.tokens.enforced and self.tokens.verify(me, token)
        s = self.store.stop_create(me, args.get("reason", ""), target, args.get("domain", ""))
        label = "STOP NOW" if s["scope"] == "all" else f"STOP {target}"
        dom = f" [domain: {s['domain']}]" if s["domain"] else ""
        msg = self.store.post(me, f"{label} (stop #{s['id']}){dom}: {s['reason']}",
                              [] if target is None else [target], "", None, verified)
        self._notify(msg)
        self._push_stop(s, "stop")
        out = {"stop": s}
        if target is not None:
            out["note"] = ("The hub does not check domain ownership yet. A targeted stop is "
                           "legitimate only when the target is working in your domain.")
        return out

    def t_hub_resume(self, args: dict, token: str | None) -> dict:
        me = self._me(args, token)
        s = self.store.stop(int(args["id"]))
        if not s["active"]:
            return {"stop": s, "note": "already lifted"}
        human = self.is_human(me, token)
        if s["scope"] == "all" and not human:
            raise HubError("a STOP NOW can only be lifted by a human")
        if s["scope"] == "agent" and not human and Addr.parse(s["issuer"]).family != me.family:
            raise HubError("a targeted stop can only be lifted by its issuer or a human")
        verified = self.tokens.enforced and self.tokens.verify(me, token)
        s = self.store.stop_lift(s["id"], me)
        msg = self.store.post(me, f"RESUME: stop #{s['id']} lifted",
                              [] if s["scope"] == "all" else [s["target"]], "", None, verified)
        self._notify(msg)
        self._push_stop(s, "resume")
        return {"stop": s}

    def t_hub_stops(self, args: dict, token: str | None) -> dict:
        return {"stops": self.store.stops_active()}

    def _push_stop(self, s: dict, kind: str) -> None:
        self.watchers.push(kind, s)
        for addr in self.wakers.addresses():
            if self.store.stop_applies(s, addr):
                self.wakers.send_to(addr, {"type": kind, "stop": s})


# ----------------------------------------------------------------------------
# HTTP / MCP transport
# ----------------------------------------------------------------------------
STORE: Store
TOKENS: Tokens
WAKERS = Wakers()
WATCHERS = Watchers()
HUB: Hub


def jsonrpc_error(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def jsonrpc_ok(rid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"{SERVER_NAME}/{SERVER_VERSION}"

    def log_message(self, fmt: str, *a) -> None:  # quieter than the default
        pass

    # -- helpers ------------------------------------------------------------
    def token(self) -> str | None:
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return self.headers.get("X-Hub-Token")

    def send_json(self, obj, code: int = 200, extra: dict | None = None) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def send_text(self, text: str, code: int = 200, ctype: str = "text/plain; charset=utf-8") -> None:
        data = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def q(self) -> dict:
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    # -- CORS ---------------------------------------------------------------
    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Hub-Token, Mcp-Session-Id, MCP-Protocol-Version")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- MCP ----------------------------------------------------------------
    def mcp_one(self, req: dict) -> dict | None:
        rid = req.get("id")
        method = req.get("method") or ""
        params = req.get("params") or {}
        if method == "initialize":
            return jsonrpc_ok(rid, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "AgentHub lets coding agents on this network talk to each other. Call "
                    "hub_hello first with your address (vendor@host, e.g. claude@desk). Every "
                    "tool takes `as`. Addressing controls notification, not visibility -- "
                    "hub_peek reads any conversation. Track work with hub_task_create / "
                    "hub_task_update so it shows on the board."
                ),
            })
        if method.startswith("notifications/"):
            return None
        if method == "ping":
            return jsonrpc_ok(rid, {})
        if method == "tools/list":
            return jsonrpc_ok(rid, {"tools": TOOLS})
        if method == "tools/call":
            name = params.get("name") or ""
            args = params.get("arguments") or {}
            try:
                result = HUB.call(name, args, self.token())
                text = json.dumps(result, indent=2, default=str)
                return jsonrpc_ok(rid, {"content": [{"type": "text", "text": text}],
                                        "isError": False})
            except HubError as e:
                return jsonrpc_ok(rid, {"content": [{"type": "text", "text": f"error: {e}"}],
                                        "isError": True})
            except Exception as e:  # noqa: BLE001
                log(f"tool {name} failed: {e!r}")
                return jsonrpc_ok(rid, {"content": [{"type": "text", "text": f"error: {e}"}],
                                        "isError": True})
        if method in ("resources/list", "prompts/list"):
            key = method.split("/")[0]
            return jsonrpc_ok(rid, {key: []})
        return jsonrpc_error(rid, -32601, f"method not found: {method}")

    def handle_mcp(self) -> None:
        raw = self.read_body()
        try:
            req = json.loads(raw or b"{}")
        except json.JSONDecodeError as e:
            self.send_json(jsonrpc_error(None, -32700, f"parse error: {e}"), 400)
            return
        sid = self.headers.get("Mcp-Session-Id") or secrets.token_hex(8)
        if isinstance(req, list):
            out = [r for r in (self.mcp_one(x) for x in req) if r is not None]
            if not out:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_json(out, extra={"Mcp-Session-Id": sid})
            return
        resp = self.mcp_one(req)
        if resp is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.send_header("Mcp-Session-Id", sid)
            self.end_headers()
            return
        self.send_json(resp, extra={"Mcp-Session-Id": sid})

    # -- SSE for the web pages ---------------------------------------------
    def serve_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        q = WATCHERS.add()
        last_ping = time.time()
        try:
            while True:
                if q:
                    event, data = q.pop(0)
                    payload = f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
                    self.wfile.write(payload.encode("utf-8"))
                    self.wfile.flush()
                    continue
                if time.time() - last_ping > 15:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_ping = time.time()
                time.sleep(0.25)
        except Exception:  # noqa: BLE001
            pass
        finally:
            WATCHERS.drop(q)

    # -- wake socket (tier 3) ----------------------------------------------
    def serve_wake(self) -> None:
        args = self.q()
        try:
            me = Addr.parse(args.get("as", ""))
            HUB.authed(me, self.token() or args.get("token"))
        except HubError as e:
            self.send_text(str(e), 403)
            return
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or "websocket" not in (self.headers.get("Upgrade") or "").lower():
            self.send_text("expected a websocket upgrade", 400)
            return
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", WsConn.accept_key(key))
        self.end_headers()
        conn = WsConn(self.rfile, self.wfile, self.connection)
        conn.fmt = "text" if args.get("format") == "text" else "json"
        address = str(me)
        WAKERS.add(address, conn)
        STORE.touch(me)
        STORE.set_ws(address, True)
        log(f"wake socket open: {address} ({conn.fmt})")
        unread = STORE.unread_count(address)
        stops = STORE.stops_for(address)
        if conn.fmt == "text":
            # Quiet on connect unless something is already waiting: every line wakes the agent.
            line = render_wake({"type": "waiting", "unread": unread, "stops": stops}, address)
            if line:
                conn.send(line)
        else:
            conn.send(json.dumps({"type": "ready", "you": address, "unread": unread, "stops": stops}))
            backlog = STORE.inbox(me, mark=False, limit=20)
            if backlog:
                conn.send(json.dumps({"type": "backlog", "messages": backlog}))

        def keepalive() -> None:
            while conn.open:
                time.sleep(KEEPALIVE_SECONDS)
                if not conn.ping():
                    break

        threading.Thread(target=keepalive, daemon=True).start()
        try:
            while True:
                frame = conn.read_frame()
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == 0x8:
                    break
                if opcode == 0x1:
                    STORE.touch(me)  # runner heartbeat
        finally:
            conn.close()
            WAKERS.drop(address, conn)
            STORE.set_ws(address, False)
            log(f"wake socket closed: {address}")

    # -- hook endpoint ------------------------------------------------------
    def serve_hook_poll(self) -> None:
        args = self.q()
        try:
            me = Addr.parse(args.get("as", ""))
            HUB.authed(me, self.token() or args.get("token"))
        except HubError as e:
            self.send_text(f"[agenthub] {e}", 403)
            return
        STORE.touch(me)
        STORE.note_event(str(me), args.get("event", ""), args.get("session", ""))
        lines: list[str] = []
        stops = STORE.stops_for(str(me))
        if stops:
            lines.append("[AgentHub] *** STOP IN FORCE -- HALT your current task now. ***")
            for st in stops:
                what = "STOP NOW" if st["scope"] == "all" else f"stop on {st['target']}"
                lines.append(f"  stop #{st['id']} {what} by {st['issuer']}: {st['reason']}")
            lines.append("Do not resume until it is lifted (hub_stops shows what is in force).")
        if args.get("stop_only") in ("1", "true"):
            self.send_text("\n".join(lines))
            return
        wait = 0 if stops else max(0, min(int(args.get("wait") or 0), MAX_WAIT))
        msgs = STORE.inbox(me, mark=True, limit=20)
        if not msgs and wait:
            STORE.wait_for_new(STORE.newest_id(), wait)
            msgs = STORE.inbox(me, mark=True, limit=20)
        if msgs:
            lines.append(f"[AgentHub] {len(msgs)} new message(s):")
            for m in msgs:
                topic = f" #{m['topic']}" if m["topic"] else ""
                task = f" [task {m['task_id']}]" if m.get("task_id") else ""
                lines.append(f"  #{m['id']} {m['from']}{topic}{task}: {m['body']}")
            lines.append("Reply with hub_say if it concerns you; hub_peek to read more context.")
        self.send_text("\n".join(lines))

    # -- pending (for the Codex wake bridge) -------------------------------------
    def serve_pending(self) -> None:
        """An agent's unread mail, stops and last hook event -- WITHOUT touching it.

        The wake bridge polls this on the agent's behalf; recording that as activity
        would make an idle Codex look awake and defeat the idle check."""
        args = self.q()
        try:
            me = Addr.parse(args.get("as", ""))
            HUB.authed(me, self.token() or args.get("token"))
        except HubError as e:
            self.send_json({"error": str(e)}, 403)
            return
        address = str(me)
        unread = STORE.inbox(me, mark=False, limit=100)
        self.send_json({
            "agent": STORE.agent(address),
            "unread": [{"id": m["id"], "from": m["from"], "to": m["to"], "topic": m["topic"],
                        "ts": m["ts"]} for m in unread],
            "stops": STORE.stops_for(address),
            "now": now_iso(),
        })

    # -- static -------------------------------------------------------------
    def serve_static(self, name: str) -> None:
        safe = os.path.normpath(name).lstrip("/\\").replace("\\", "/")
        if ".." in safe:
            self.send_text("no", 400)
            return
        path = os.path.join(WEB_DIR, safe)
        if not os.path.isfile(path):
            self.send_text("not found", 404)
            return
        ctype = {
            ".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
        }.get(os.path.splitext(path)[1], "application/octet-stream")
        with open(path, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -- routing ------------------------------------------------------------
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        args = self.q()
        try:
            if path == "/health":
                self.send_text("ok")
            elif path == "/mcp":
                # Spec-legal: this server does not offer a server-initiated SSE stream.
                self.send_json(jsonrpc_error(None, -32000, "use POST for MCP"), 405)
            elif path == "/wake":
                self.serve_wake()
            elif path == "/events":
                self.serve_events()
            elif path == "/hook/poll":
                self.serve_hook_poll()
            elif path == "/api/who":
                self.send_json({"agents": STORE.who(), "wake_sockets": WAKERS.addresses()})
            elif path == "/api/topics":
                self.send_json({"topics": STORE.topics()})
            elif path == "/api/messages":
                self.send_json({"messages": STORE.peek(
                    topic=args.get("topic"), frm=args.get("from"),
                    since=int(args.get("since") or 0), limit=int(args.get("limit") or 200),
                    include_archived=args.get("include_archived") in ("1", "true"),
                    task_id=args.get("task_id"))})
            elif path == "/api/search":
                self.send_json({"messages": STORE.search(
                    args.get("q", ""), int(args.get("limit") or 100),
                    args.get("include_archived") in ("1", "true"))})
            elif path == "/api/pending":
                self.serve_pending()
            elif path == "/api/stops":
                self.send_json({"stops": STORE.stops_active(), "humans": sorted(HUMANS)})
            elif path == "/api/tasks":
                self.send_json({"tasks": STORE.tasks(args.get("owner"), args.get("status"))})
            elif path in ("/", "/index.html"):
                self.serve_static("index.html")
            elif path in ("/board", "/board.html"):
                self.serve_static("board.html")
            else:
                self.serve_static(path)
        except HubError as e:
            self.send_json({"error": str(e)}, 400)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            log(f"GET {path} failed: {e!r}")
            try:
                self.send_json({"error": str(e)}, 500)
            except Exception:  # noqa: BLE001
                pass

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/mcp":
                self.handle_mcp()
                return
            body = json.loads(self.read_body() or b"{}")
            if path == "/api/say":
                res = HUB.call("hub_say", body, self.token())
                self.send_json(res)
            elif path == "/api/task":
                res = HUB.call("hub_task_create", body, self.token())
                self.send_json(res)
            elif path == "/api/task/update":
                res = HUB.call("hub_task_update", body, self.token())
                self.send_json(res)
            elif path == "/api/stop":
                self.send_json(HUB.call("hub_stop", body, self.token()))
            elif path == "/api/resume":
                self.send_json(HUB.call("hub_resume", body, self.token()))
            else:
                self.send_json({"error": "not found"}, 404)
        except HubError as e:
            self.send_json({"error": str(e)}, 400)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            log(f"POST {path} failed: {e!r}")
            try:
                self.send_json({"error": str(e)}, 500)
            except Exception:  # noqa: BLE001
                pass

    def do_DELETE(self) -> None:
        if urlparse(self.path).path == "/mcp":
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_json({"error": "not found"}, 404)


def sweeper() -> None:
    while True:
        try:
            STORE.sweep()
        except Exception as e:  # noqa: BLE001
            log(f"retention sweep failed: {e!r}")
        time.sleep(SWEEP_SECONDS)


def main() -> None:
    global STORE, TOKENS, HUB
    STORE = Store(DB_PATH)
    TOKENS = Tokens(TOKENS_FILE)
    HUB = Hub(STORE, TOKENS, WAKERS, WATCHERS)
    STORE.sweep()
    threading.Thread(target=sweeper, daemon=True).start()
    mode = "token-authenticated" if TOKENS.enforced else "OPEN (no tokens.json)"
    log(f"{SERVER_NAME} {SERVER_VERSION} on :{PORT}  db={DB_PATH}  auth={mode}")
    log(f"  MCP    POST http://<host>:{PORT}/mcp")
    log(f"  board  GET  http://<host>:{PORT}/board")
    log(f"  chat   GET  http://<host>:{PORT}/")
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")


if __name__ == "__main__":
    main()
