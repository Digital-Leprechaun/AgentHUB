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
# 'elevated': a subtask whose Worker was moved into the desktop app and now runs it as
# a primary task of its own; for its old Orchestrator it counts as done.
TASK_STATUSES = ("planning", "pending", "active", "blocked", "done", "elevated")
CLOSED_STATUSES = ("done", "elevated")
SWEEP_SECONDS = 3600
ACTIVE_SECONDS = 90          # seen this recently => "active" on the board
WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
KEEPALIVE_SECONDS = 30
MAX_BODY = 32000
MAX_WAIT = 60
PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "agenthub"
SERVER_VERSION = "3.0.0"

# A family-addressed request (claude@desk, anyone@desk) that no bridge claims within
# this long is escalated to the humans and the sender.
REQUEST_ESCALATE_SECONDS = int(os.environ.get("HUB_REQUEST_ESCALATE_SECONDS", "60"))
REQUEST_SWEEP_SECONDS = float(os.environ.get("HUB_REQUEST_SWEEP_SECONDS", "10"))
SESSION_KEEP_DAYS = 30       # sessions older than this drop out of the bridge's view
HUB_SENDER = "agenthub@hub"  # the hub's own voice, for escalations
# Besides the humans, one agent family may flush the queue (HUB_FLUSH_FAMILY, e.g. the
# agent that maintains the hub); unset means humans only.
FLUSH_FAMILY = os.environ.get("HUB_FLUSH_FAMILY", "").lower()

ADDR_RE = re.compile(r"^([a-z0-9][a-z0-9._-]{0,31})@([a-z0-9][a-z0-9._-]{0,31})(?:/([a-z0-9][a-z0-9._/-]{0,63}))?$", re.I)
TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,63}$", re.I)
SID_RE = re.compile(r"^[0-9a-f]{8}$")
LABEL_RE = re.compile(r"^[a-z][a-z0-9._-]{0,31}$")


def sid_of(session_id: str | None) -> str:
    """The 8-hex session part of an address, from a vendor session id.

    The LAST 8 hex digits: Codex thread ids are UUIDv7, whose leading digits are a
    timestamp and would collide for sessions started within a minute of each other."""
    h = re.sub(r"[^0-9a-f]", "", (session_id or "").lower())
    return h[-8:] if len(h) >= 8 else ""


def is_delivery_notice(frm: str) -> bool:
    """Mail from a wake bridge or from the hub itself: a report about delivering mail."""
    f = (frm or "").lower()
    return f == HUB_SENDER or (f.count("/") == 1 and f.endswith("/bridge"))


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


def norm_company(c: str | None) -> str:
    """A company label: lower case letters, digits, '.', '_' and '-' ('' for none)."""
    return re.sub(r"[^a-z0-9._-]", "", (c or "").strip().lower())[:64]


def norm_title(t: str | None) -> str:
    """A session title: one line, at most 120 characters ('' for none)."""
    return " ".join((t or "").split())[:120]


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

-- A session resumed under a new session id (newer Claude CLIs give a resumed
-- conversation a fresh id): mail for the old address follows it to the new one.
-- Mail handed from one session to another (a respawn, or a resume under a new id):
-- the old session is never notified of it again, even if it comes back.
CREATE TABLE IF NOT EXISTS transfers(
  msg_id   INTEGER NOT NULL,
  frm_addr TEXT    NOT NULL,
  PRIMARY KEY(msg_id, frm_addr)
);

CREATE TABLE IF NOT EXISTS successors(
  old TEXT PRIMARY KEY,
  new TEXT NOT NULL,
  ts  TEXT NOT NULL
);

-- A request to move a terminal session into the Claude desktop app on its own
-- machine. That machine's wake bridge carries it out (an agent session may not open
-- one itself) and reports back. state: open | done | failed.
CREATE TABLE IF NOT EXISTS escalations(
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  session TEXT NOT NULL,
  frm     TEXT NOT NULL,
  ts      TEXT NOT NULL,
  state   TEXT NOT NULL DEFAULT 'open',
  note    TEXT NOT NULL DEFAULT ''
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
  status    TEXT NOT NULL DEFAULT 'pending',  -- planning|pending|active|blocked|done|elevated
  parent_id INTEGER,
  worker    TEXT,
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

-- A message to a family (claude@desk) or to anyone@host that routing could not
-- narrow to one session is a request: one session, usually a new one started by the
-- host's wake bridge, claims it. state: open | unclaimed (no bridge could take it) |
-- claimed | assigned (no spawner; given to the most recently active session) |
-- legacy (delivered the pre-session way) | failed.
CREATE TABLE IF NOT EXISTS requests(
  msg_id     INTEGER NOT NULL,
  target     TEXT    NOT NULL,
  state      TEXT    NOT NULL,
  created    TEXT    NOT NULL,
  claimed_by TEXT,
  claimed_at TEXT,
  escalated  INTEGER NOT NULL DEFAULT 0,
  note       TEXT    NOT NULL DEFAULT '',
  PRIMARY KEY(msg_id, target)
);
CREATE INDEX IF NOT EXISTS ix_req_state ON requests(state, target);
CREATE INDEX IF NOT EXISTS ix_req_claim ON requests(claimed_by, msg_id);
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
        # kind='session' marks a vendor@host/<sid> address registered by its hook; label,
        # cwd, state (starting|live|ended) and spawned_for belong to sessions.
        # worker_of: the subtask a Worker session was given (see Store.role_of).
        for col in ("session_id", "last_event", "last_event_at", "kind", "label", "cwd", "state",
                    "spawned_for", "company", "title", "worker_of"):
            if col not in acols:
                self.db.execute(f"ALTER TABLE agents ADD COLUMN {col} TEXT")
                log(f"migrated: agents.{col} added")
        rcols = {r[1] for r in self.db.execute("PRAGMA table_info(requests)")}
        if "delivered" not in rcols:
            # A request is delivered to the session that holds it whatever that session's
            # read cursor says (it may have been adopted after later mail was read).
            # Requests that exist already have been delivered under the old rules.
            self.db.execute("ALTER TABLE requests ADD COLUMN delivered INTEGER NOT NULL DEFAULT 0")
            self.db.execute("UPDATE requests SET delivered=1")
            log("migrated: requests.delivered added")
        tcols = {r[1] for r in self.db.execute("PRAGMA table_info(tasks)")}
        if "worker" not in tcols:
            # Who is running a subtask (a Worker session, a hub-using subagent, or a
            # family until a session takes it); the owner stays its Orchestrator.
            self.db.execute("ALTER TABLE tasks ADD COLUMN worker TEXT")
            log("migrated: tasks.worker added")
        if "orig_to" not in cols:
            # What the sender wrote, when routing rewrote `recips` (labels, narrowing).
            self.db.execute("ALTER TABLE messages ADD COLUMN orig_to TEXT")
            log("migrated: messages.orig_to added")
        if "workdir" not in cols:
            # Where a session started for this message should work (a folder on the
            # target machine); the bridge checks it against its allowed spawn folders.
            self.db.execute("ALTER TABLE messages ADD COLUMN workdir TEXT")
            log("migrated: messages.workdir added")
        if "company" not in cols:
            # The company (for example acme or widgets) the message is about. The
            # sender's machine derives it from the sending session's folder; the
            # receiving machine maps it to its own folder for a new session.
            self.db.execute("ALTER TABLE messages ADD COLUMN company TEXT")
            log("migrated: messages.company added")
        if "title" not in cols:
            # The sending session's title (what the user named it in the Claude app), so a
            # session started for the message can be named after the conversation.
            self.db.execute("ALTER TABLE messages ADD COLUMN title TEXT")
            log("migrated: messages.title added")
        self.spawners: dict[str, int] = {}  # family -> open spawn-capable bridge sockets
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

    # -- sessions -----------------------------------------------------------
    def register_session(self, address: str, session_id: str, cwd: str = "",
                         state: str = "live", spawned_for: int | None = None,
                         company: str = "", title: str = "") -> None:
        """Record vendor@host/<sid> as a session. Its hook calls this on every event."""
        a = Addr.parse(address)
        ts = now_iso()
        with self.lock:
            self.db.execute(
                "INSERT INTO agents(address,family,vendor,host,role,first_seen,last_seen,kind,"
                "session_id,cwd,state,spawned_for,company,title)"
                " VALUES(?,?,?,?,?,?,?,'session',?,?,?,?,?,?)"
                " ON CONFLICT(address) DO UPDATE SET kind='session', last_seen=excluded.last_seen,"
                " session_id=excluded.session_id, cwd=COALESCE(NULLIF(excluded.cwd,''), agents.cwd),"
                " state=excluded.state, spawned_for=COALESCE(excluded.spawned_for, agents.spawned_for),"
                " company=COALESCE(NULLIF(excluded.company,''), agents.company),"
                " title=COALESCE(NULLIF(excluded.title,''), agents.title)",
                (str(a).lower(), a.family, a.vendor, a.host, a.role.lower(), ts, ts,
                 session_id, (cwd or "")[:260], state, spawned_for, norm_company(company),
                 norm_title(title)),
            )
            self.db.commit()

    def session_base(self, address: str) -> str | None:
        """The registered session (vendor@host/<sid>) that `address` is or belongs to."""
        fam, _, role = (address or "").lower().partition("/")
        if not role:
            return None
        base = f"{fam}/{role.split('/')[0]}"
        r = self.one("SELECT 1 FROM agents WHERE address=? AND kind='session'", (base,))
        return base if r else None

    def company_of(self, address: str) -> str:
        """The company of the session that `address` is or belongs to, as its hook
        reported it ('' when unknown)."""
        base = self.session_base(address)
        r = self.one("SELECT company FROM agents WHERE address=?", (base,)) if base else None
        return (r["company"] or "") if r else ""

    def title_of(self, address: str) -> str:
        """The title of the session that `address` is or belongs to ('' when unknown)."""
        base = self.session_base(address)
        r = self.one("SELECT title FROM agents WHERE address=?", (base,)) if base else None
        return (r["title"] or "") if r else ""

    def kind_of(self, address: str) -> str:
        """human | anyone | bridge | session | legacy (a family, or a pre-session role)."""
        a = (address or "").lower()
        fam, _, role = a.partition("/")
        if fam in HUMANS:
            return "human"
        if fam.split("@")[0] == "anyone":
            return "anyone"
        if role == "bridge":
            return "bridge"
        if self.session_base(a):
            return "session"
        return "legacy"

    def ended(self, address: str) -> bool:
        r = self.one("SELECT state FROM agents WHERE address=?", ((address or "").lower(),))
        return bool(r) and r["state"] == "ended"

    def has_sessions(self, family: str) -> bool:
        return bool(self.one("SELECT 1 FROM agents WHERE family=? AND kind='session' LIMIT 1",
                             (family.lower(),)))

    def live_sessions(self, family: str) -> list[dict]:
        """Sessions of a family seen in the last day, most recently active first."""
        cut = datetime.fromtimestamp(time.time() - 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = self.q("SELECT * FROM agents WHERE family=? AND kind='session'"
                      " AND COALESCE(state,'live') != 'ended' AND last_seen >= ?"
                      " ORDER BY COALESCE(last_event_at, last_seen) DESC", (family.lower(), cut))
        return [dict(r) for r in rows]

    def sessions(self, family: str) -> list[dict]:
        cut = datetime.fromtimestamp(time.time() - SESSION_KEEP_DAYS * 86400,
                                     timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = self.q("SELECT * FROM agents WHERE family=? AND kind='session' AND last_seen >= ?"
                      " ORDER BY last_seen DESC", (family.lower(), cut))
        return [dict(r) for r in rows]

    def set_label(self, address: str, label: str) -> None:
        """Give a session a readable alias (claude@desk/parser). One live holder per family."""
        label = (label or "").strip().lower()
        if not LABEL_RE.match(label) or SID_RE.match(label) or label in ("bridge",):
            raise HubError(f"bad label {label!r}: a letter, then letters, digits, . _ - (max 32); "
                           "not 8 hex digits and not 'bridge'")
        base = self.session_base(address)
        if not base or base != address.lower():
            raise HubError("only a session address (vendor@host/<session>) can take a label; "
                           "your hook tells you yours at session start")
        fam = base.split("/")[0]
        with self.lock:
            self.db.execute("UPDATE agents SET label=NULL WHERE family=? AND label=?", (fam, label))
            self.db.execute("UPDATE agents SET label=? WHERE address=?", (label, base))
            self.db.commit()

    def by_label(self, family: str, label: str) -> str | None:
        r = self.one("SELECT address FROM agents WHERE family=? AND kind='session' AND label=?",
                     (family.lower(), label.lower()))
        return r["address"] if r else None

    def spawner_add(self, family: str, delta: int) -> None:
        with self.lock:
            n = self.spawners.get(family, 0) + delta
            if n > 0:
                self.spawners[family] = n
            else:
                self.spawners.pop(family, None)

    def can_spawn(self, family: str) -> bool:
        with self.lock:
            return family in self.spawners

    def spawner_on(self, host: str) -> bool:
        with self.lock:
            return any(f.split("@")[1] == host for f in self.spawners)

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
            if r["kind"] == "session" and (r["state"] == "ended" or age > 86400):
                continue  # finished sessions pile up; the board shows only current ones
            state ="connected" if r["ws_open"] else ("active" if age < ACTIVE_SECONDS else "away")
            out.append(
                {
                    "address": r["address"], "family": r["family"], "vendor": r["vendor"],
                    "host": r["host"], "role": r["role"], "parent": r["parent"],
                    "last_seen": r["last_seen"], "state": state,
                    "last_event": r["last_event"], "last_event_at": r["last_event_at"],
                    "kind": r["kind"] or "", "label": r["label"], "cwd": r["cwd"],
                    "company": r["company"] or "", "title": r["title"] or "",
                    "session_state": r["state"], "spawned_for": r["spawned_for"],
                    "unread": self.unread_count(r["address"]),
                }
            )
        return out

    # -- messages -----------------------------------------------------------
    def post(self, frm: Addr, body: str, recips: list[str], topic: str,
             reply_to: int | None, verified: bool, task_id: int | None = None,
             orig_to: list[str] | None = None, requests: list[dict] | None = None,
             workdir: str | None = None, company: str | None = None,
             title: str | None = None) -> dict:
        """`requests` (from Hub.route) are recorded in the same transaction as the
        message, so no reader can ever see the message without its routing."""
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
                "INSERT INTO messages(ts,frm,family,vendor,host,topic,recips,reply_to,verified,body,"
                "task_id,orig_to,workdir,company,title) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, str(frm), frm.family, frm.vendor, frm.host, topic,
                 json.dumps(recips), reply_to, 1 if verified else 0, body, task_id,
                 json.dumps(orig_to) if orig_to is not None else None, workdir or None,
                 norm_company(company) or None, norm_title(title) or None),
            )
            mid = cur.lastrowid
            for rq in requests or []:
                self.db.execute(
                    "INSERT INTO requests(msg_id,target,state,created,claimed_by,claimed_at,note)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (mid, rq["target"], rq["state"], ts, rq.get("claimed_by"),
                     ts if rq.get("claimed_by") else None, rq.get("note", "")),
                )
            self.db.commit()
        self._bump()
        return {"id": mid, "ts": ts, "from": str(frm), "topic": topic, "to": recips,
                "reply_to": reply_to, "verified": verified, "task_id": task_id,
                "company": norm_company(company), "body": body}

    @staticmethod
    def _row_to_msg(r: sqlite3.Row) -> dict:
        return {
            "id": r["id"], "ts": r["ts"], "from": r["frm"], "topic": r["topic"],
            "to": json.loads(r["recips"]), "reply_to": r["reply_to"],
            "verified": bool(r["verified"]), "archived": bool(r["archived"]),
            "task_id": r["task_id"], "company": (r["company"] or "") if "company" in r.keys() else "",
            "body": r["body"],
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

        A SESSION (vendor@host/<sid>) is notified only of what is aimed at it: mail to
        its own address, requests it claimed, its own topic subscriptions and
        @-mentions of its address. Never family mail and never broadcasts -- several
        sessions of one agent each answering the same message is the failure this
        exists to prevent. Broadcasts reach sessions as a count in hub_hello.

        Everything else (a bare family, a pre-session role, a human) keeps the old
        rules, except that family mail that became a request (spawned or handed to a
        session) is no longer delivered to the family as well.

        An unaddressed message broadcasts only on the DEFAULT topic. On a named topic
        it notifies subscribers only. Anyone can still read any topic with peek();
        this governs notification alone.
        """
        kind = self.kind_of(address)
        subs = "topic IN (SELECT topic FROM subs WHERE address = :me)"
        if kind == "session":
            return ("((recips LIKE :exact OR " + subs + " OR body LIKE :mention"
                    " OR id IN (SELECT msg_id FROM requests WHERE claimed_by = :me AND state IN ('claimed','assigned')"
                    " AND delivered = 0))"
                    " AND id NOT IN (SELECT msg_id FROM transfers WHERE frm_addr = :me))")
        if kind in ("anyone", "bridge"):
            return "(recips LIKE :exact)"
        parts = ["(recips = '[]' AND topic = '')"]           # broadcast on the default topic
        if address.lower() != address.split("/")[0].lower():
            parts.append("recips LIKE :exact")                # addressed to me (a role)
        parts.append("(recips LIKE :fam AND id NOT IN"        # addressed to my family
                     " (SELECT msg_id FROM requests WHERE target = :family AND state != 'legacy'))")
        parts += [subs, "body LIKE :mention"]
        return "(" + " OR ".join(parts) + ")"

    def is_for(self, msg: dict, address: str) -> bool:
        """Python twin of targeted_at(): does this message notify `address`?

        One extra case: a bridge (vendor@host/bridge) is kicked by any mail for its
        family, its sessions or anyone@its-host, since it acts for all of them."""
        if (msg.get("from") or "").lower() == address.lower():
            return False
        a = address.lower()
        family = a.split("/")[0]
        recips = [r.lower() for r in (msg.get("to") or [])]
        topic = msg.get("topic") or ""
        mentioned = f"@{a}" in (msg.get("body") or "").lower()
        kind = self.kind_of(a)
        if kind == "bridge":
            anyone = f"anyone@{family.split('@')[1]}"
            return any(r == anyone or r == family or
                       (r.startswith(family + "/") and not r.endswith("/bridge")) for r in recips)
        if kind == "anyone":
            return False
        subscribed = bool(topic) and topic in self.subs_of(address)
        if kind == "session":
            if self.one("SELECT 1 FROM transfers WHERE msg_id=? AND frm_addr=?", (msg["id"], a)):
                return False  # handed to another session
            if a in recips or subscribed or mentioned:
                return True
            return bool(self.one("SELECT 1 FROM requests WHERE msg_id=? AND claimed_by=? AND state IN ('claimed','assigned')"
                                 " AND delivered = 0", (msg["id"], a)))
        if not recips and topic == "":
            return True
        if a != family and a in recips:
            return True
        if family in recips and not self.one(
                "SELECT 1 FROM requests WHERE msg_id=? AND target=? AND state != 'legacy'",
                (msg["id"], family)):
            return True
        return subscribed or mentioned

    def _target_args(self, a: Addr) -> dict:
        me = str(a).lower()
        return {
            "me": me,
            "exact": f'%"{me}"%',
            "fam": f'%"{a.family}"%',
            "family": a.family,
            "mention": f"%@{me}%",
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

    def broadcasts_since(self, hours: int = 24) -> int:
        cut = datetime.fromtimestamp(time.time() - hours * 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return self.one("SELECT COUNT(*) AS n FROM messages WHERE recips='[]' AND topic=''"
                        " AND archived=0 AND ts >= ?", (cut,))["n"]

    def inbox(self, a: Addr, mark: bool = True, limit: int = 100) -> list[dict]:
        address = str(a)
        row = self.one("SELECT last_read FROM cursors WHERE address=?", (address,))
        last = row["last_read"] if row else 0
        args = self._target_args(a)
        args["last"] = last
        args["lim"] = limit
        held = ("id IN (SELECT msg_id FROM requests WHERE claimed_by = :me"
                " AND state IN ('claimed','assigned') AND delivered = 0)")
        sql = (f"SELECT * FROM messages WHERE frm != :me AND archived=0 "
               f"AND ((id > :last AND {self.targeted_at(address)}) OR {held}) ORDER BY id LIMIT :lim")
        with self.lock:
            rows = self.db.execute(sql, args).fetchall()
        msgs = [self._row_to_msg(r) for r in rows]
        if mark and msgs:
            self.set_cursor(address, msgs[-1]["id"])
            with self.lock:
                self.db.executemany("UPDATE requests SET delivered=1 WHERE claimed_by=? AND msg_id=?",
                                    [(address.lower(), m["id"]) for m in msgs])
                self.db.commit()
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
                    topic: str, parent_id: int | None, worker: str | None = None) -> dict:
        title = (title or "").strip()
        if not title:
            raise HubError("task needs a title")
        if parent_id is not None:
            parent = self.task(int(parent_id))
            if parent.get("parent_id") is not None:
                raise HubError(f"task {parent_id} is itself a subtask: tasks go two levels deep. Add a "
                               f"sibling under task {parent['parent_id']} instead")
        ts = now_iso()
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO tasks(title,body,owner,family,vendor,host,topic,status,parent_id,worker,created,updated)"
                " VALUES(?,?,?,?,?,?,?,'pending',?,?,?,?)",
                (title, body or "", str(owner), owner.family, owner.vendor, owner.host,
                 topic, parent_id, (worker or "").strip().lower() or None, ts, ts),
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
        allowed = {"title", "body", "owner", "status", "topic", "worker"}
        sets, args, notes = [], [], []
        cur = self.task(tid)
        for k, v in fields.items():
            if v is None or k not in allowed:
                continue
            if k == "status" and v not in TASK_STATUSES:
                raise HubError("status must be one of " + ", ".join(TASK_STATUSES))
            if k == "status" and v == "elevated" and cur.get("parent_id") is None:
                raise HubError("only a subtask can be marked elevated")
            if k == "worker":
                v = (v or "").strip().lower() or None
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
        if fields.get("status") in CLOSED_STATUSES:
            sets.append("done_at=?")
            args.append(now_iso())
        args.append(tid)
        with self.lock:
            self.db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", tuple(args))
            if fields.get("status") == "elevated":
                # The Worker now runs this work as an Orchestrator of its own.
                self.db.execute("UPDATE agents SET worker_of=NULL WHERE worker_of=?", (str(tid),))
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
            sql += " AND status NOT IN ('done','elevated')"
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

    # -- requests -----------------------------------------------------------
    def requests_open(self, family: str) -> list[dict]:
        """Open requests a bridge for `family` may claim: to the family, or anyone@host."""
        host = family.split("@")[1]
        rows = self.q(
            "SELECT r.*, m.frm, m.ts, m.topic, m.task_id, m.workdir, m.company, m.title, m.reply_to,"
            " t.parent_id AS task_parent, t.owner AS task_owner, t.title AS task_title"
            " FROM requests r"
            " JOIN messages m ON m.id = r.msg_id"
            " LEFT JOIN tasks t ON t.id = m.task_id"
            " WHERE r.state IN ('open','unclaimed') AND (r.target = ? OR r.target = ?)"
            " ORDER BY r.msg_id", (family.lower(), f"anyone@{host}"))
        return [dict(r) for r in rows]

    def claim(self, bridge: Addr, msg_id: int, target: str, session_id: str, cwd: str = "") -> str:
        """Atomically claim a request for a new session of the bridge's family.
        Exactly one claim wins; the session is registered in state 'starting'."""
        sid = sid_of(session_id)
        if not sid:
            raise HubError("claim needs the new session's id (at least 8 hex digits)")
        target = (target or "").lower()
        t = Addr.parse(target)
        if t.vendor == "anyone":
            if t.host != bridge.host:
                raise HubError(f"a bridge on {bridge.host} cannot claim {target}")
        elif t.family != bridge.family or t.role:
            raise HubError(f"a bridge for {bridge.family} cannot claim {target}")
        session = f"{bridge.family}/{sid}"
        with self.lock:
            cur = self.db.execute(
                "UPDATE requests SET state='claimed', claimed_by=?, claimed_at=?, delivered=0"
                " WHERE msg_id=? AND target=? AND state IN ('open','unclaimed')",
                (session, now_iso(), int(msg_id), target))
            self.db.commit()
            if cur.rowcount != 1:
                raise HubError(f"request #{msg_id} for {target} is not open (already claimed?)")
        self.register_session(session, session_id, cwd, state="starting", spawned_for=int(msg_id))
        self._bump()
        return session

    def adopt(self, token: str, me: str) -> int:
        """A started session takes over the requests its bridge claimed for it.

        The bridge claims under a placeholder address made from a random token and
        puts the token in the new session's prompt; the session's hook hands it back
        here from its first prompt. (`claude --bg` does not honour --session-id, so
        the session's real address is only known once its hooks run.) Returns how many
        requests moved."""
        me = me.lower()
        fam = me.split("/")[0]
        placeholder = f"{fam}/{sid_of(token)}" if sid_of(token) else ""
        if not placeholder or placeholder == me:
            return 0
        with self.lock:
            row = self.db.execute("SELECT spawned_for FROM agents WHERE address=? AND kind='session'"
                                  " AND state='starting'", (placeholder,)).fetchone()
            if not row:
                return 0
            cur = self.db.execute("UPDATE requests SET claimed_by=?, delivered=0 WHERE claimed_by=?"
                                  " AND state='claimed'", (me, placeholder))
            self.db.execute("UPDATE agents SET spawned_for=? WHERE address=?", (row["spawned_for"], me))
            self.db.execute("UPDATE agents SET state='ended' WHERE address=?", (placeholder,))
            self.db.commit()
            n = cur.rowcount
        for r in self.q("SELECT m.task_id FROM requests q JOIN messages m ON m.id=q.msg_id"
                        " WHERE q.claimed_by=? AND m.task_id IS NOT NULL", (me,)):
            self.make_worker(me, r["task_id"])
        log(f"{me} adopted {n} request(s) claimed for {placeholder}")
        self._bump()
        return n

    def request_fail(self, bridge: Addr, msg_id: int, note: str, target: str = "") -> None:
        """A bridge could not start the session it claimed for: mark both failed/ended.
        With `target`, an open request it will not take (too old) is closed as well, if
        the target is its family or its host's anyone@."""
        target = (target or "").lower()
        if target in (bridge.family, f"anyone@{bridge.host}"):
            with self.lock:
                self.db.execute("UPDATE requests SET state='failed', note=? WHERE msg_id=? AND target=?"
                                " AND state IN ('open','unclaimed')", ((note or "")[:500], int(msg_id), target))
                self.db.commit()
        with self.lock:
            rows = self.db.execute(
                "SELECT claimed_by FROM requests WHERE msg_id=? AND state='claimed'"
                " AND claimed_by LIKE ?", (int(msg_id), bridge.family + "/%")).fetchall()
            self.db.execute(
                "UPDATE requests SET state='failed', note=? WHERE msg_id=? AND state='claimed'"
                " AND claimed_by LIKE ?", ((note or "")[:500], int(msg_id), bridge.family + "/%"))
            for r in rows:
                self.db.execute("UPDATE agents SET state='ended' WHERE address=?", (r["claimed_by"],))
            self.db.commit()

    def session_dead(self, bridge: Addr, address: str, note: str = "") -> list[dict]:
        """A bridge found one of its family's sessions gone (archived, deleted): end it,
        and hand each request it was given to the family's next live session. Returns
        one entry per request: {msg_id, target, frm, to}, `to` None when nobody is left
        (that request is failed, for the caller to report)."""
        address = (address or "").lower()
        if address.split("/")[0] != bridge.family or self.kind_of(address) != "session":
            raise HubError(f"a bridge for {bridge.family} cannot end {address}")
        with self.lock:
            self.db.execute("UPDATE agents SET state='ended' WHERE address=?", (address,))
            rows = self.db.execute(
                "SELECT r.msg_id, r.target, m.frm FROM requests r JOIN messages m ON m.id = r.msg_id"
                " WHERE r.claimed_by=? AND r.state='assigned'", (address,)).fetchall()
            self.db.commit()
        live = [s["address"] for s in self.live_sessions(bridge.family)]
        out = []
        with self.lock:
            for r in rows:
                sender = self.session_base(r["frm"]) or r["frm"].lower()
                to = next((a for a in live if a != sender), None)
                if to:
                    self.db.execute("UPDATE requests SET claimed_by=?, claimed_at=?, note=?"
                                    " WHERE msg_id=? AND target=?",
                                    (to, now_iso(), f"reassigned from {address}: {note}"[:500],
                                     r["msg_id"], r["target"]))
                else:
                    self.db.execute("UPDATE requests SET state='failed', note=? WHERE msg_id=? AND target=?",
                                    (f"{address} is gone: {note}"[:500], r["msg_id"], r["target"]))
                out.append({"msg_id": r["msg_id"], "target": r["target"], "frm": r["frm"], "to": to})
            self.db.commit()
        self._bump()
        return out

    def session_respawn(self, bridge: Addr, address: str, msg_ids: list[int], note: str = "") -> list[int]:
        """A bridge found one of its family's sessions unable to take its mail (gone,
        hung, or open in an app with nothing to deliver into): end it, and reopen each
        of `msg_ids` -- unread mail aimed at it -- as a request to the family, so the
        bridge starts a new terminal session for it. Mail never moves into another
        live session. Returns the message ids reopened."""
        address = (address or "").lower()
        if address.split("/")[0] != bridge.family or self.kind_of(address) != "session":
            raise HubError(f"a bridge for {bridge.family} cannot respawn {address}")
        family, ts = bridge.family, now_iso()
        why = f"respawn: {address} {note}".strip()[:500]
        out = []
        with self.lock:
            self.db.execute("UPDATE agents SET state='ended' WHERE address=?", (address,))
            for mid in msg_ids:
                mid = int(mid)
                if not self.db.execute("SELECT 1 FROM messages WHERE id=?", (mid,)).fetchone():
                    continue
                # Already reopened once, and taken (or being taken) by another session:
                # never start a second one for the same message.
                if self.db.execute("SELECT 1 FROM requests WHERE msg_id=? AND target=? AND"
                                   " (state='open' OR (claimed_by IS NOT NULL AND claimed_by != ?))",
                                   (mid, family, address)).fetchone():
                    continue
                self.db.execute("INSERT OR IGNORE INTO transfers(msg_id,frm_addr) VALUES(?,?)", (mid, address))
                cur = self.db.execute(
                    "UPDATE requests SET state='open', claimed_by=NULL, claimed_at=NULL, escalated=0,"
                    " delivered=0, created=?, note=? WHERE msg_id=? AND target=?", (ts, why, mid, family))
                if cur.rowcount == 0:
                    self.db.execute(
                        "INSERT INTO requests(msg_id,target,state,created,note) VALUES(?,?,'open',?,?)",
                        (mid, family, ts, why))
                # Requests it had taken under another target (anyone@host) are released too.
                self.db.execute("UPDATE requests SET state='failed', note=? WHERE msg_id=? AND claimed_by=?"
                                " AND target != ?", (why, mid, address, family))
                out.append(mid)
            self.db.commit()
        log(f"{address} respawned for message(s) {', '.join('#' + str(m) for m in out) or 'none'}: {note}")
        self._bump()
        return out

    def succeed(self, old: str, me: str) -> int:
        """Session `me` is the conversation `old` resumed under a new session id: hand it
        `old`'s unread mail and taken requests, end `old`, and route later follow-ups
        for `old` to `me`. Returns how many messages moved."""
        old, me = (old or "").lower(), (me or "").lower()
        if old == me or old.split("/")[0] != me.split("/")[0] or self.kind_of(old) != "session":
            return 0
        ts = now_iso()
        with self.lock:
            row = self.db.execute("SELECT last_read FROM cursors WHERE address=?", (old,)).fetchone()
            last = row[0] if row else 0
            mids = [r[0] for r in self.db.execute(
                "SELECT id FROM messages WHERE id > ? AND recips LIKE ?", (last, f'%"{old}"%'))]
            for mid in mids:
                self.db.execute(
                    "INSERT INTO requests(msg_id,target,state,created,claimed_by,claimed_at,note,delivered)"
                    " VALUES(?,?,'assigned',?,?,?,?,0) ON CONFLICT(msg_id,target) DO UPDATE SET"
                    " state='assigned', claimed_by=excluded.claimed_by, claimed_at=excluded.claimed_at,"
                    " delivered=0", (mid, old, ts, me, ts, f"resumed from {old}"))
                self.db.execute("INSERT OR IGNORE INTO transfers(msg_id,frm_addr) VALUES(?,?)", (mid, old))
            for (mid,) in self.db.execute("SELECT msg_id FROM requests WHERE claimed_by=? AND state IN"
                                          " ('claimed','assigned')", (old,)).fetchall():
                self.db.execute("INSERT OR IGNORE INTO transfers(msg_id,frm_addr) VALUES(?,?)", (mid, old))
            # Requests it already held move as they are: one it has read stays delivered,
            # so the resumed session is not handed old, answered work as new mail.
            self.db.execute("UPDATE requests SET claimed_by=? WHERE claimed_by=? AND state IN"
                            " ('claimed','assigned')", (me, old))
            wo = self.db.execute("SELECT worker_of FROM agents WHERE address=?", (old,)).fetchone()
            if wo and wo[0]:
                self.db.execute("UPDATE agents SET worker_of=? WHERE address=?", (wo[0], me))
            sp = self.db.execute("SELECT spawned_for FROM agents WHERE address=?", (old,)).fetchone()
            if sp and sp[0]:
                self.db.execute("UPDATE agents SET spawned_for=COALESCE(spawned_for, ?) WHERE address=?", (sp[0], me))
            top = self.db.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
            self.db.execute("INSERT INTO cursors(address,last_read) VALUES(?,?) ON CONFLICT(address)"
                            " DO UPDATE SET last_read=MAX(last_read, excluded.last_read)", (old, top))
            self.db.execute("UPDATE agents SET state='ended' WHERE address=?", (old,))
            self.db.execute("INSERT INTO successors(old,new,ts) VALUES(?,?,?) ON CONFLICT(old)"
                            " DO UPDATE SET new=excluded.new, ts=excluded.ts", (old, me, ts))
            self.db.commit()
        log(f"{me} resumed {old}; moved message(s) {', '.join('#' + str(m) for m in mids) or 'none'}")
        self._bump()
        return len(mids)

    def follow(self, address: str) -> str:
        """`address` (a session, or a subagent of one), moved on to the session its
        conversation was resumed as, if it was."""
        a = (address or "").lower()
        base = self.session_base(a)
        if not base:
            return a
        new = self.successor(base)
        return a if new == base else new + a[len(base):]

    def successor(self, address: str) -> str:
        """The live session a resumed conversation now runs as (itself if none)."""
        a = (address or "").lower()
        for _ in range(8):
            r = self.one("SELECT new FROM successors WHERE old=?", (a,))
            if not r:
                break
            a = r["new"]
        return a

    def escalation_add(self, session: str, frm: str) -> int:
        with self.lock:
            cur = self.db.execute("INSERT INTO escalations(session,frm,ts) VALUES(?,?,?)",
                                  (session.lower(), frm.lower(), now_iso()))
            self.db.commit()
        self._bump()
        return cur.lastrowid

    def escalations_open(self, family: str) -> list[dict]:
        return [dict(r) for r in self.q("SELECT * FROM escalations WHERE state='open' AND session LIKE ?",
                                        (family.lower() + "/%",))]

    def escalation_done(self, bridge: Addr, eid: int, ok: bool, note: str) -> dict:
        row = self.one("SELECT * FROM escalations WHERE id=?", (int(eid),))
        if not row or row["session"].split("/")[0] != bridge.family:
            raise HubError(f"a bridge for {bridge.family} cannot settle escalation #{eid}")
        with self.lock:
            self.db.execute("UPDATE escalations SET state=?, note=? WHERE id=?",
                            ("done" if ok else "failed", (note or "")[:500], int(eid)))
            self.db.commit()
        return dict(row)

    def flush(self, keep: set[str] | None = None) -> dict:
        """Clear the queue after a change to the hub or the bridges, so no bridge acts on
        backlog: every known address is marked as having read everything posted so far,
        every request still waiting or being worked is closed as 'flushed', and every
        session not in `keep` (those with a watch connected) is ended, so the board shows
        only sessions that are really there. Nothing is deleted: the messages
        stay readable with hub_peek, and an ended session that comes back is live again."""
        with self.lock:
            top = self.db.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
            addrs = {r[0] for r in self.db.execute("SELECT address FROM agents")}
            addrs |= {r[0] for r in self.db.execute("SELECT family FROM agents")}
            addrs |= {r[0] for r in self.db.execute("SELECT address FROM cursors")}
            moved = 0
            for a in addrs:
                cur = self.db.execute(
                    "INSERT INTO cursors(address,last_read) VALUES(?,?)"
                    " ON CONFLICT(address) DO UPDATE SET last_read=MAX(last_read, excluded.last_read)"
                    " WHERE last_read < excluded.last_read", (a, top))
                moved += cur.rowcount
            reqs = self.db.execute(
                "UPDATE requests SET state='flushed', note=? WHERE msg_id <= ?"
                " AND state IN ('open','unclaimed','claimed','assigned')",
                (f"flushed at #{top}", top)).rowcount
            self.db.commit()
        self._bump()
        ended = 0
        with self.lock:
            for (a,) in self.db.execute("SELECT address FROM agents WHERE kind='session'"
                                        " AND COALESCE(state,'live') != 'ended'").fetchall():
                if a not in (keep or set()):
                    self.db.execute("UPDATE agents SET state='ended', worker_of=NULL WHERE address=?", (a,))
                    ended += 1
            self.db.commit()
        self._bump()
        return {"through": top, "addresses_cleared": moved, "requests_closed": reqs, "sessions_ended": ended}

    # -- Orchestrators and Workers --------------------------------------------
    def role_of(self, address: str) -> tuple[str, int | None]:
        """('human' | 'system' | 'worker' | 'orchestrator', the Worker's subtask or None).

        A Worker works one subtask for an Orchestrator: a session given a subtask
        (agents.worker_of), or a hub-using subagent (vendor@host/<sid>/<role>). Every
        other agent session is an Orchestrator. Elevation is the only way a Worker
        session becomes an Orchestrator."""
        a = (address or "").lower()
        fam = a.split("/")[0]
        if fam in HUMANS:
            return "human", None
        if a == HUB_SENDER or (a.count("/") == 1 and a.endswith("/bridge")):
            return "system", None  # exactly vendor@host/bridge: never a session's child
        if a.count("/") >= 2:
            r = self.one("SELECT id FROM tasks WHERE worker=? AND status NOT IN ('done','elevated')"
                         " ORDER BY id DESC LIMIT 1", (a,))
            return "worker", (r["id"] if r else None)
        r = self.one("SELECT worker_of FROM agents WHERE address=?", (a,))
        if r and r["worker_of"]:
            return "worker", int(r["worker_of"])
        return "orchestrator", None

    def owns_task(self, address: str, task: dict) -> bool:
        """Is `address` the Orchestrator of this task (its owner, or the owner's family
        for tasks filed before sessions had addresses)?"""
        a = (address or "").lower()
        o = (task.get("owner") or "").lower()
        return o == a or ("/" not in o and o == a.split("/")[0]) or self.successor(o) == a

    def worker_may_tag(self, address: str, tid: int) -> bool:
        """May this Worker tag a message with task `tid`? Its own subtask, a subtask
        mail was sent to it under (a hand-off), or for a hub-using subagent any task of
        its parent session's tree."""
        a = (address or "").lower()
        try:
            t = self.task(int(tid))
        except HubError:
            return False
        role, mine = self.role_of(a)
        if mine == t["id"] or (t.get("worker") or "") == a:
            return True
        if a.count("/") >= 2:
            parent = "/".join(a.split("/")[:2])
            top = self.task(t["parent_id"]) if t.get("parent_id") else t
            return self.owns_task(parent, top)
        return bool(self.one(
            "SELECT 1 FROM messages WHERE task_id=? AND (recips LIKE ? OR id IN"
            " (SELECT msg_id FROM requests WHERE claimed_by=?)) LIMIT 1", (t["id"], f'%"{a}"%', a)))

    def make_worker(self, address: str, tid: int | None) -> None:
        """`address` took mail for subtask `tid`: it is that subtask's Worker. A session
        that already orchestrates open work of its own is left as it is."""
        if tid is None:
            return
        a = (address or "").lower()
        try:
            t = self.task(int(tid))
        except HubError:
            return
        if t.get("parent_id") is None or t["status"] in CLOSED_STATUSES:
            return
        if self.one("SELECT 1 FROM tasks WHERE owner=? AND status NOT IN ('done','elevated') LIMIT 1", (a,)):
            return
        with self.lock:
            self.db.execute("UPDATE agents SET worker_of=? WHERE address=?", (str(t["id"]), a))
            w = (t.get("worker") or "")
            if not w or "/" not in w or self.ended(w):
                self.db.execute("UPDATE tasks SET worker=? WHERE id=?", (a, t["id"]))
            self.db.commit()
        self._bump()

    def owed_reply(self, address: str) -> dict | None:
        """The newest mail of the last day that `address` (a session) was asked to act on
        and has not answered: a request it took, or a HANDOFF / QUESTION sent to it, from
        an agent, with no later message from this session back to that agent's family.
        {id, from} or None. The wake bridge reports a session that stops without answering."""
        a = (address or "").lower()
        cut = datetime.fromtimestamp(time.time() - 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = self.one(
            "SELECT id, frm FROM messages WHERE ts >= ? AND frm != ? AND frm NOT LIKE '%/bridge'"
            " AND (id IN (SELECT msg_id FROM requests WHERE claimed_by = ? AND state IN ('claimed','assigned'))"
            "      OR (recips LIKE ? AND (body LIKE 'HANDOFF%' OR body LIKE 'QUESTION%')))"
            " ORDER BY id DESC LIMIT 1", (cut, HUB_SENDER, a, f'%"{a}"%'))
        if not row:
            return None
        fam = row["frm"].lower().split("/")[0]
        mine = a.split("/")[0]
        # Answered by this session, or by the same conversation resumed under a newer
        # session id (any session of this family that replies to it, or reports on the
        # same task to the sender).
        answered = self.one(
            "SELECT 1 FROM messages WHERE id > ? AND ("
            " (frm = ? AND (recips LIKE ? OR reply_to = ?))"
            " OR (frm LIKE ? AND reply_to = ?)"
            " OR (frm LIKE ? AND recips LIKE ? AND task_id IS NOT NULL"
            "     AND task_id = (SELECT task_id FROM messages WHERE id = ?)))",
            (row["id"], a, f'%"{fam}%', row["id"], f"{mine}/%", row["id"],
             f"{mine}/%", f'%"{fam}%', row["id"]))
        return None if answered else {"id": row["id"], "from": row["frm"]}

    def requests_overdue(self) -> list[dict]:
        """Requests nobody took: 'unclaimed' at once, 'open' after the escalate window.
        Each is returned once (escalated is set)."""
        cut = datetime.fromtimestamp(time.time() - REQUEST_ESCALATE_SECONDS,
                                     timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.lock:
            rows = self.db.execute(
                "SELECT r.*, m.frm FROM requests r JOIN messages m ON m.id = r.msg_id"
                " WHERE r.escalated = 0 AND (r.state = 'unclaimed'"
                "  OR (r.state = 'open' AND r.created <= ?))", (cut,)).fetchall()
            for r in rows:
                self.db.execute("UPDATE requests SET escalated=1 WHERE msg_id=? AND target=?",
                                (r["msg_id"], r["target"]))
            self.db.commit()
        return [dict(r) for r in rows]

    def request_of(self, msg_id: int) -> list[dict]:
        return [dict(r) for r in self.q("SELECT * FROM requests WHERE msg_id=?", (int(msg_id),))]

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
        if is_delivery_notice(m.get("from") or ""):
            return (f"[AgentHub] message #{m['id']} for {address} from {m['from']}: {body} -- a problem "
                    "with mail this session sent. Read it with hub_inbox and tell the user.")
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
    "description": "Your address. For an agent session this is your SESSION address, "
                   "vendor@host/<session>, which the AgentHub hook gives you at session start "
                   "(e.g. claude@desk/3f2a91c0); a subagent appends a role "
                   "(claude@desk/3f2a91c0/reviewer). Required on every call.",
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
            "label": {"type": "string", "description": "Optional readable alias for your session, "
                      "e.g. parser: mail to claude@desk/parser then reaches you. One session holds a "
                      "label at a time; the newest claim takes it."},
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
                   "description": "Addresses to notify. A session address (claude@desk/3f2a91c0) "
                                  "or label (claude@desk/parser) reaches that one session. A family "
                                  "(claude@desk) reaches the session this conversation belongs to "
                                  "when reply_to or task_id points at one; otherwise the machine "
                                  "starts a NEW session for it. anyone@desk starts a new session "
                                  "with that machine's default agent. Empty means broadcast."},
            "topic": {"type": "string", "description": "Topic to post under. Omit for the default topic."},
            "reply_to": {"type": "integer", "description": "Message id this replies to. With no "
                         "`to`, the reply goes to that message's sender."},
            "workdir": {"type": "string", "description": "When this mail starts a NEW session (a "
                        "family or anyone@host), the folder on that machine to start it in, "
                        "e.g. D:/Work or /srv/projects. It must be one of that machine's allowed "
                        "spawn folders or inside one; otherwise the machine's default folder is "
                        "used and the session is told. A reply keeps its conversation's folder. "
                        "Rarely needed: `company` normally picks the folder."},
            "company": {"type": "string", "description": "The company this mail is about "
                        "(e.g. acme, widgets). Omit it: the hub fills it in from your "
                        "session's folder, or from the conversation you reply to. Set it only when "
                        "you ask about another company's work. A machine that starts a NEW session "
                        "for this mail starts it in its own folder for that company."},
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
        "description": "Create a task (Orchestrators only). Hub work always has a task: file a "
                       "primary task for the work, then a subtask (parent_id) for each Worker you ask "
                       "-- another machine, or a subagent of yours that uses the hub -- naming it in "
                       "`worker`. Workers never create or update tasks: they report to you, and you "
                       "update the board.",
        "inputSchema": {"type": "object", "required": ["as", "title"], "properties": {
            "as": AS_PROP,
            "title": {"type": "string"},
            "body": {"type": "string"},
            "owner": {"type": "string", "description": "Owning agent. Defaults to you."},
            "topic": {"type": "string"},
            "parent_id": {"type": "integer", "description": "Primary task this is a subtask of (two "
                          "levels only). Only that task's Orchestrator adds subtasks."},
            "worker": {"type": "string", "description": "Who runs this subtask: a session address, "
                       "a subagent address (you/<role>), or a machine's family (claude@11) when the "
                       "hub will start a session for it."},
            "status": {"type": "string", "enum": list(TASK_STATUSES)},
        }},
    },
    {
        "name": "hub_task_update",
        "description": "Change a task (its Orchestrator, or a human). Set status to done to move "
                       "it to the Completed column; mark a subtask `elevated` when its Worker was moved "
                       "into the desktop app and now runs it as its own primary task. Change `worker` "
                       "when another session takes a subtask over.",
        "inputSchema": {"type": "object", "required": ["as", "id"], "properties": {
            "as": AS_PROP,
            "id": {"type": "integer"},
            "status": {"type": "string", "enum": list(TASK_STATUSES)},
            "owner": {"type": "string"},
            "worker": {"type": "string"},
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
        "name": "hub_escalate",
        "description": "Move a terminal session into the Claude desktop app on its own machine, "
                       "so the user can continue that conversation there. The machine's wake bridge "
                       "stops the background copy and opens the same conversation in the app, then "
                       "reports back to you. Use it when the user asks for a session to be elevated.",
        "inputSchema": {"type": "object", "required": ["as", "session"], "properties": {
            "as": AS_PROP,
            "session": {"type": "string", "description": "The session to move, e.g. claude@25/3e735983 "
                                                         "(a label works too)."},
        }},
    },
    {
        "name": "hub_flush",
        "description": "Clear the whole queue after a change to the hub or the wake bridges, so "
                       "nothing acts on old mail: every address is marked as having read everything "
                       "so far, and waiting requests are closed. Nothing is deleted. Humans and the "
                       "AgentHub holder only.",
        "inputSchema": {"type": "object", "required": ["as", "reason"], "properties": {
            "as": AS_PROP,
            "reason": {"type": "string", "description": "Why, for the record."},
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

    # -- routing ------------------------------------------------------------
    def route(self, me: Addr, to: list[str], reply_to, task_id) -> tuple[list[str], list[dict], list[str]]:
        """Resolve a message's recipients. Returns (recips, requests, notes).

        - A label (claude@desk/parser) resolves to the session holding it.
        - A bare family is NARROWED to one of its sessions when the conversation
          already belongs to it: `reply_to` a message that session sent, or `task_id`
          of a task that session owns. Replies therefore route themselves.
        - A family that cannot be narrowed, or anyone@host, becomes a request: the
          host's bridge starts a new session for it. With no spawn-capable bridge for
          the family it goes to the family's most recently active session instead,
          and with no sessions at all it is delivered the pre-session way.
        """
        st = self.store
        me_base = st.session_base(str(me)) or str(me).lower()
        parent = None
        if reply_to:
            r = st.one("SELECT frm FROM messages WHERE id=?", (int(reply_to),))
            parent = r["frm"] if r else None
        owner = None
        if task_id is not None:
            try:
                owner = st.task(int(task_id))["owner"]
            except HubError:
                owner = None  # post() reports the missing task
        out, reqs, notes = [], [], []
        for raw in to:
            a = Addr.parse(raw)
            r = str(a).lower()
            kind = st.kind_of(r)
            if kind == "anyone":
                if a.role:
                    raise HubError(f"{raw}: anyone@host takes no /session part")
                state = "open" if st.spawner_on(a.host) else "unclaimed"
                reqs.append({"target": r, "state": state})
                notes.append(f"{r}: " + ("a bridge on that host will start a new session" if state == "open"
                                         else f"no bridge on {a.host} can start a session; the humans are told"))
            elif kind == "session":
                f = st.follow(r)
                if f != r:
                    notes.append(f"{raw} -> {f} (its conversation was resumed as {f.split('/')[0]}/"
                                 f"{f.split('/')[1]})")
                    r = f
            elif kind in ("human", "bridge"):
                pass
            elif a.role:
                seg, _, rest = a.role.lower().partition("/")
                hit = st.by_label(a.family, seg)
                if hit:
                    r = st.follow(hit + (f"/{rest}" if rest else ""))
                    notes.append(f"{raw} -> {r} (label)")
            else:
                base = None
                claimed = None
                if reply_to:
                    # A follow-up to a request goes to the session that took it.
                    row = st.one("SELECT claimed_by FROM requests WHERE msg_id=? AND claimed_by LIKE ?",
                                 (int(reply_to), r + "/%"))
                    claimed = row["claimed_by"] if row else None
                for cand in (parent, claimed, owner):
                    b = st.session_base(cand) if cand else None
                    b = st.successor(b) if b else None
                    # An ended session cannot take a follow-up: it becomes a request, and
                    # the new session reads the conversation so far from the hub.
                    if b and b.split("/")[0] == r and b != me_base and not st.ended(b):
                        base = b
                        break
                if base:
                    notes.append(f"{raw} -> {base} (the session this conversation belongs to)")
                    r = base
                elif st.can_spawn(r):
                    reqs.append({"target": r, "state": "open"})
                    notes.append(f"{r}: its bridge will start a new session for this")
                else:
                    live = [s for s in st.live_sessions(r) if s["address"] != me_base]
                    if live:
                        reqs.append({"target": r, "state": "assigned", "claimed_by": live[0]["address"],
                                     "note": "no spawn-capable bridge; most recently active session"})
                        notes.append(f"{r}: no bridge can start a session, so it went to "
                                     f"{live[0]['address']}, its most recently active session")
                    elif st.has_sessions(r):
                        # Its sessions have all ended. Queuing into an old one would run the
                        # request whenever someone next opens it, so tell the humans now.
                        why = f"{r} has no live session and no bridge that can start one"
                        reqs.append({"target": r, "state": "unclaimed", "note": why})
                        notes.append(f"{r}: {why}; the humans and you are told")
                    else:
                        reqs.append({"target": r, "state": "legacy"})
            if r not in out:
                out.append(r)
        return out, reqs, notes

    def sweep_requests(self) -> None:
        """Tell the humans and the sender about requests nobody took."""
        for rq in self.store.requests_overdue():
            why = (rq.get("note") or "no bridge on that host can start a session" if rq["state"] == "unclaimed"
                   else f"no bridge claimed it within {REQUEST_ESCALATE_SECONDS} s")
            to = sorted(set(HUMANS) | ({rq["frm"]} if "@" in rq["frm"] else set()))
            msg = self.store.post(Addr.parse(HUB_SENDER),
                                  f"NOTE message #{rq['msg_id']} for {rq['target']} has no taker: {why}. "
                                  "It stays open; a bridge that comes back will still take it.",
                                  to, "agenthub", rq["msg_id"], True)
            self._notify(msg)
            log(f"request #{rq['msg_id']} for {rq['target']} escalated: {why}")

    def report_dead_session(self, session: str, note: str, moved: list[dict]) -> None:
        """After a bridge ended a dead session: log the hand-overs, and tell the humans
        and the sender about each request no live session was left to take."""
        for mv in moved:
            if mv["to"]:
                log(f"request #{mv['msg_id']} for {mv['target']}: {session} is gone, handed to {mv['to']}")
                continue
            to = sorted(set(HUMANS) | ({mv["frm"]} if "@" in mv["frm"] else set()))
            msg = self.store.post(Addr.parse(HUB_SENDER),
                                  f"NOTE message #{mv['msg_id']} for {mv['target']} was not delivered: the "
                                  f"session it went to ({session}) is gone ({note}) and {mv['target']} has "
                                  "no other live session. Open one there and send it again.",
                                  to, "agenthub", mv["msg_id"], True)
            self._notify(msg)
            log(f"request #{mv['msg_id']} for {mv['target']} failed: {session} is gone, no live session left")

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
        if who and "/" not in str(who) and self.store.kind_of(str(who)) == "legacy" \
                and self.store.has_sessions(str(who).lower()):
            result["ADDRESS"] = (f"{who} is the family address, shared by every session on that "
                                 "machine. Use your own session address (vendor@host/<session>), "
                                 "given by the AgentHub hook at session start, as `as` on every call.")
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
        if a.vendor == "anyone":
            raise HubError("anyone@host is a destination, not an identity")
        self.authed(a, token)
        self.store.touch(a, args.get("parent"))
        return a

    # -- tools --------------------------------------------------------------
    def t_hub_hello(self, args: dict, token: str | None) -> dict:
        me = self._me(args, token)
        address = str(me).lower()
        if args.get("label"):
            self.store.set_label(address, args["label"])
        kind = self.store.kind_of(address)
        out = {
            "you": str(me), "family": me.family, "kind": kind,
            "unread": self.store.unread_count(str(me)),
            "topics_subscribed": self.store.subs_of(str(me)),
            "agents": self.store.who(),
            "retention_days": RETAIN_DAYS,
            "note": "Addressing controls notification, not visibility: use hub_peek to read "
                    "anything, including conversations you were not addressed in.",
        }
        if kind == "session":
            ag = self.store.agent(address) or {}
            out["label"] = ag.get("label")
            out["broadcasts_24h"] = self.store.broadcasts_since(24)
            out["note"] += (" You are one session: you are notified only of mail to your own "
                            "address, requests you took, your own topic subscriptions and "
                            "@-mentions of you. Broadcasts are not pushed to sessions -- "
                            "broadcasts_24h counts them; read them with hub_peek.")
        return out

    def t_hub_say(self, args: dict, token: str | None) -> dict:
        me = self._me(args, token)
        to = args.get("to") or []
        if isinstance(to, str):
            to = [to]
        for r in to:
            Addr.parse(r)  # validate
        if not to and args.get("reply_to"):
            # A reply with no `to` answers the message's sender. It never broadcasts:
            # a broadcast reply reaches every watch on the family, not the asker.
            r = self.store.one("SELECT frm FROM messages WHERE id=?", (int(args["reply_to"]),))
            if r and r["frm"].lower() != str(me).lower():
                to = [r["frm"]]
        topic = norm_topic(args.get("topic"))
        verified = self.tokens.enforced and self.tokens.verify(me, token)
        args["task_id"] = self.task_for(me, args)
        recips, reqs, notes = self.route(me, to, args.get("reply_to"), args.get("task_id"))
        workdir = str(args.get("workdir") or "").strip()[:260] or None
        if not workdir and args.get("reply_to"):
            # A follow-up keeps the folder its conversation started in.
            r = self.store.one("SELECT workdir FROM messages WHERE id=?", (int(args["reply_to"]),))
            workdir = r["workdir"] if r else None
        # Company: as given; else the conversation's; else the sending session's, which
        # its hook derived from the session's folder on the sender's machine.
        company = norm_company(args.get("company"))
        if not company and args.get("reply_to"):
            r = self.store.one("SELECT company FROM messages WHERE id=?", (int(args["reply_to"]),))
            company = norm_company(r["company"]) if r else ""
        if not company:
            company = self.store.company_of(str(me))
        msg = self.store.post(me, args.get("body", ""), recips, topic,
                              args.get("reply_to"), verified, args.get("task_id"),
                              orig_to=to if recips != [t.lower() for t in to] else None,
                              requests=reqs, workdir=workdir, company=company,
                              title=self.store.title_of(str(me)))
        self._notify(msg)
        for r in recips:
            if self.store.kind_of(r) == "session" and r != str(me).lower():
                self.store.make_worker(r, msg["task_id"])
        out = {"posted": msg["id"], "ts": msg["ts"], "topic": topic,
               "task_id": msg["task_id"], "notified": recips or "everyone"}
        if notes:
            out["routing"] = notes
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

    def task_for(self, me: Addr, args: dict) -> int | None:
        """The task a message belongs to. Every message from an agent has one: the
        `task_id` given, else the task of the message it replies to. A Worker may use
        only its own subtask (or one handed to it)."""
        role, mine = self.store.role_of(str(me))
        tid = args.get("task_id")
        if tid is None and args.get("reply_to"):
            r = self.store.one("SELECT task_id FROM messages WHERE id=?", (int(args["reply_to"]),))
            tid = r["task_id"] if r else None
        if tid is None and role == "worker" and mine is not None:
            tid = mine
        if role in ("human", "system"):
            return int(tid) if tid is not None else None
        if tid is None:
            raise HubError(
                "every hub message needs a task. Orchestrator: create one with hub_task_create "
                "(and a subtask for each Worker you ask), then pass its task_id. Worker: pass your "
                "subtask's task_id. A reply_to a message that has a task inherits it.")
        t = self.store.task(int(tid))  # raises if there is no such task
        if role == "worker" and not self.store.worker_may_tag(str(me), t["id"]):
            raise HubError(f"you are a Worker on task {mine}; task {t['id']} is not yours to post "
                           "under. Tag your messages with your own subtask, or ask your Orchestrator.")
        return t["id"]

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
        role, mine = self.store.role_of(str(me))
        if role == "worker":
            raise HubError(f"you are a Worker on task {mine}: only its Orchestrator manages tasks. "
                           "Ask your Orchestrator for a new task or a sibling Worker.")
        owner = Addr.parse(args["owner"]) if args.get("owner") else me
        if args.get("parent_id") is not None and role != "human":
            parent = self.store.task(int(args["parent_id"]))
            if not self.store.owns_task(str(me), parent):
                raise HubError(f"task {parent['id']} belongs to {parent['owner']}: only its "
                               "Orchestrator adds subtasks to it")
            owner = me  # a subtask's owner is its Orchestrator; `worker` says who runs it
        t = self.store.task_create(me, args.get("title", ""), args.get("body", ""),
                                   owner, norm_topic(args.get("topic")),
                                   args.get("parent_id"), args.get("worker"))
        if args.get("status") and args["status"] != "pending":
            t = self.store.task_update(me, t["id"], status=args["status"])
        self.watchers.push("task", t)
        return {"task": t}

    def t_hub_task_update(self, args: dict, token: str | None) -> dict:
        me = self._me(args, token)
        cur = self.store.task(int(args["id"]))
        role, mine = self.store.role_of(str(me))
        if role == "worker":
            raise HubError(f"you are a Worker on task {mine}: tell your Orchestrator "
                           f"({cur['owner']}) and it updates the task")
        if role != "human" and not self.store.owns_task(str(me), cur):
            raise HubError(f"task {cur['id']} belongs to {cur['owner']}: only its Orchestrator "
                           "(or a human) updates it")
        t = self.store.task_update(
            me, int(args["id"]), status=args.get("status"), owner=args.get("owner"),
            title=args.get("title"), body=args.get("body"), worker=args.get("worker"),
            topic=norm_topic(args["topic"]) if args.get("topic") is not None else None,
        )
        self.watchers.push("task", t)
        out = {"task": t}
        if args.get("status") in CLOSED_STATUSES and t.get("parent_id") is None:
            left = [x["id"] for x in self.store.q(
                "SELECT id FROM tasks WHERE parent_id=? AND status NOT IN ('done','elevated')", (t["id"],))]
            if left:
                out["WARNING"] = (f"task {t['id']} is closed but its subtask(s) "
                                  f"{', '.join('#' + str(x) for x in left)} are still open: close them too")
        return out

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

    def t_hub_escalate(self, args: dict, token: str | None) -> dict:
        me = self._me(args, token)
        raw = (args.get("session") or "").strip().lower()
        a = Addr.parse(raw)
        target = raw
        if a.role and self.store.kind_of(raw) != "session":
            hit = self.store.by_label(a.family, a.role.split("/")[0])
            target = hit or raw
        target = self.store.successor(target)
        if self.store.kind_of(target) != "session" or target.split("@")[0] not in ("claude", "codex"):
            raise HubError(f"{raw} is not a known Claude or Codex session")
        eid = self.store.escalation_add(target, str(me))
        verified = self.tokens.enforced and self.tokens.verify(me, token)
        msg = self.store.post(me, f"ESCALATE #{eid}: {target} is to be moved into the Claude desktop app "
                                  f"on its machine, at the request of {me}.", sorted(HUMANS), "agenthub",
                              None, verified)
        self._notify(msg)
        return {"escalation": eid, "session": target,
                "note": f"the bridge on {target.split('@')[1].split('/')[0]} will move it and report back to you"}

    def t_hub_flush(self, args: dict, token: str | None) -> dict:
        me = self._me(args, token)
        if not (self.is_human(me, token) or me.family == FLUSH_FAMILY):
            raise HubError(f"only a human or the AgentHub holder ({FLUSH_FAMILY}) may flush the queue")
        reason = (args.get("reason") or "").strip()
        if not reason:
            raise HubError("say why the queue is being flushed")
        out = self.store.flush(keep={a.lower() for a in self.wakers.addresses()})
        verified = self.tokens.enforced and self.tokens.verify(me, token)
        msg = self.store.post(me, f"FLUSH: every message through #{out['through']} is marked read, "
                                  f"{out['requests_closed']} waiting request(s) are closed and "
                                  f"{out['sessions_ended']} idle session(s) without a watch are ended. Nothing was "
                                  f"deleted; resend anything still needed. Reason: {reason}",
                              [], "agenthub", None, verified)
        self._notify(msg)
        log(f"queue flushed by {me} through #{out['through']}: {out}")
        return out

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
                    "hub_hello first with your session address (vendor@host/<session>, given "
                    "by the AgentHub hook at session start). Every tool takes `as`. Answer only "
                    "mail delivered to your own session. Addressing controls notification, not visibility -- "
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
        # A bridge that can start sessions says so; family mail then becomes a request
        # for it instead of going to an existing session.
        spawner = me.role == "bridge" and args.get("spawn") in ("1", "true")
        WAKERS.add(address, conn)
        STORE.touch(me)
        STORE.set_ws(address, True)
        if spawner:
            STORE.spawner_add(me.family, 1)
        log(f"wake socket open: {address} ({conn.fmt}{', spawner' if spawner else ''})")
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
            if spawner:
                STORE.spawner_add(me.family, -1)
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
        session = args.get("session", "")
        event = args.get("event", "")
        if session and me.role and me.role.lower() == sid_of(session):
            # The hook addresses itself as vendor@host/<sid of its session>: register it.
            # A Codex thread the hub started runs one `codex exec` per turn, so it ends
            # after every turn: for it SessionEnd means idle (the bridge resumes it).
            ag = STORE.agent(str(me).lower()) or {}
            idle_end = me.vendor == "codex" and bool(ag.get("spawned_for"))
            STORE.register_session(str(me), session, args.get("cwd", ""),
                                   state="ended" if event == "SessionEnd" and not idle_end else "live",
                                   company=args.get("company", ""), title=args.get("title", ""))
        STORE.note_event(str(me).lower() if me.role else str(me), event, session)
        if args.get("adopt") and me.role:
            STORE.adopt(args["adopt"], str(me))  # before the inbox read below, so it is delivered now
        if args.get("succeed") and me.role:
            STORE.succeed(args["succeed"], str(me))  # likewise
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
            if any(is_delivery_notice(m["from"]) for m in msgs):
                # The humans do not read the hub: the session that sent the mail is the
                # one place a failed delivery can reach them.
                lines.append("A wake bridge or the hub reports a problem with mail sent from this "
                             "session (not delivered, not picked up, or not answered). Tell the user in your "
                             "reply to them: which message, to whom, what went wrong, and what he needs to do.")
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
        out = {
            "agent": STORE.agent(address),
            "unread": [{"id": m["id"], "from": m["from"], "to": m["to"], "topic": m["topic"],
                        "ts": m["ts"]} for m in unread],
            "stops": STORE.stops_for(address),
            "now": now_iso(),
        }
        if not me.role:
            # For the bridge: every session of the family with its unread mail ("direct"
            # = addressed to it or a request it took, the mail that justifies a wake),
            # and the requests it may claim.
            sessions = []
            for s in STORE.sessions(me.family):
                sa = Addr.parse(s["address"])
                mail = STORE.inbox(sa, mark=False, limit=100)
                claimed = {r["msg_id"] for r in STORE.q(
                    "SELECT msg_id FROM requests WHERE claimed_by=? AND state IN ('claimed','assigned')", (s["address"],))}
                s["unread"] = [{"id": m["id"], "from": m["from"], "to": m["to"], "topic": m["topic"],
                                "ts": m["ts"], "direct": s["address"] in [t.lower() for t in m["to"]]
                                or m["id"] in claimed} for m in mail]
                s["stops"] = bool(STORE.stops_for(s["address"]))
                s["owes_reply"] = STORE.owed_reply(s["address"]) if s.get("state") != "starting" else None
                sessions.append(s)
            out["sessions"] = sessions
            out["requests"] = STORE.requests_open(me.family)
            out["escalations"] = STORE.escalations_open(me.family)
        self.send_json(out)

    def bridge_call(self, body: dict) -> Addr:
        me = Addr.parse(body.get("as", ""))
        HUB.authed(me, self.token())
        if me.role != "bridge":
            raise HubError("only a bridge (vendor@host/bridge) may claim requests")
        return me

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
            elif path == "/api/claim":
                me = self.bridge_call(body)
                session = STORE.claim(me, int(body["msg_id"]), body.get("target", ""),
                                      body.get("session", ""), body.get("cwd", ""))
                log(f"request #{body['msg_id']} for {body.get('target')} claimed by {session}")
                self.send_json({"session": session})
            elif path == "/api/request/fail":
                me = self.bridge_call(body)
                STORE.request_fail(me, int(body["msg_id"]), body.get("note", ""), body.get("target", ""))
                self.send_json({"ok": True})
            elif path == "/api/escalation/done":
                me = self.bridge_call(body)
                STORE.escalation_done(me, int(body["id"]), bool(body.get("ok")), body.get("note", ""))
                self.send_json({"ok": True})
            elif path == "/api/session/respawn":
                me = self.bridge_call(body)
                reopened = STORE.session_respawn(me, body.get("session", ""), body.get("msg_ids") or [],
                                                 body.get("note", ""))
                self.send_json({"reopened": reopened})
            elif path == "/api/session/dead":
                me = self.bridge_call(body)
                moved = STORE.session_dead(me, body.get("session", ""), body.get("note", ""))
                HUB.report_dead_session(body.get("session", ""), body.get("note", ""), moved)
                self.send_json({"moved": moved})
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


def request_sweeper() -> None:
    while True:
        time.sleep(REQUEST_SWEEP_SECONDS)
        try:
            HUB.sweep_requests()
        except Exception as e:  # noqa: BLE001
            log(f"request sweep failed: {e!r}")


def main() -> None:
    global STORE, TOKENS, HUB
    STORE = Store(DB_PATH)
    TOKENS = Tokens(TOKENS_FILE)
    HUB = Hub(STORE, TOKENS, WAKERS, WATCHERS)
    STORE.sweep()
    threading.Thread(target=sweeper, daemon=True).start()
    threading.Thread(target=request_sweeper, daemon=True).start()
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
