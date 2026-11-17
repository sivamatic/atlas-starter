"""Tickets: Atlas proposes, a person approves, exactly one ticket is created (Units 4 and 5).

  propose()  Atlas drafts a ticket. Nothing is written to the ticket system yet.
  approve()  The requester approves with an Idempotency-Key. Double-clicks, network retries and
             a flaky ticket system must still produce exactly one ticket.

Storage is plain SQL that runs on SQLite (tests, dev) and Postgres (production).
The ticket system is SIMULATED (FakeTicketSystem). A real one (ServiceNow, Jira Service
Management, Zendesk) needs the same thing from you: pass an idempotency key, and retry only
errors that are safe to retry.
"""
import json
import sqlite3
import threading
import time
import uuid

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS ticket_proposals (
        id TEXT PRIMARY KEY, requester TEXT NOT NULL, summary TEXT NOT NULL, details TEXT NOT NULL,
        status TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL,
        idempotency_key TEXT UNIQUE, approved_by TEXT, ticket_id TEXT)""",
    """CREATE TABLE IF NOT EXISTS ticket_audit (
        seq BIGINT NOT NULL, proposal_id TEXT NOT NULL, at DOUBLE PRECISION NOT NULL, actor TEXT NOT NULL,
        action TEXT NOT NULL, detail TEXT NOT NULL)""",
]


class NotFound(Exception): pass
class Forbidden(Exception): pass
class Conflict(Exception): pass
class TicketSystemError(Exception):
    """A transient failure from the ticket system (timeout, 5xx). Safe to retry with the same key."""
class TicketSystemDown(Exception):
    """Retries ran out. The approval is recorded; approving again with the same key resumes."""


class DB:
    """A tiny wrapper so the same SQL ("?" placeholders) runs on SQLite and on Postgres."""

    def __init__(self, url: str = ""):
        self.lock = threading.Lock()
        if url.startswith("postgres"):
            import psycopg
            self.conn, self.pg = psycopg.connect(url, autocommit=True), True
        else:
            path = url[len("sqlite:///"):] if url.startswith("sqlite:///") else ":memory:"
            self.conn, self.pg = sqlite3.connect(path, check_same_thread=False, isolation_level=None), False
        for stmt in SCHEMA:
            self.run(stmt)

    def run(self, sql: str, args=()):
        """Execute one statement. Returns (rows, rowcount)."""
        with self.lock:
            cur = self.conn.execute(sql.replace("?", "%s") if self.pg else sql, args)
            rows = cur.fetchall() if cur.description else []
            return rows, cur.rowcount


class FakeTicketSystem:
    """SIMULATED external ticket system. Idempotent by key, like the real ones should be.
    fail_next = n makes the next n calls raise TicketSystemError (a flaky network)."""

    def __init__(self, fail_next: int = 0):
        self.tickets, self.by_key, self.calls, self.fail_next = {}, {}, 0, fail_next

    def create(self, key: str, payload: dict) -> str:
        self.calls += 1
        if self.fail_next > 0:
            self.fail_next -= 1
            raise TicketSystemError("ticket system timed out")
        if key not in self.by_key:
            tid = "TCK-%04d" % (1000 + len(self.tickets) + 1)
            self.tickets[tid] = payload
            self.by_key[key] = tid
        return self.by_key[key]


_last = [0]
_seq_lock = threading.Lock()


def next_seq() -> int:
    """Strictly increasing within this process, so the audit trail keeps its order even when the clock doesn't move."""
    with _seq_lock:
        _last[0] = max(time.time_ns(), _last[0] + 1)
        return _last[0]


COLS = "id, requester, summary, details, status, created_at, idempotency_key, approved_by, ticket_id"


class TicketService:
    def __init__(self, db: DB, system, ttl_s: int = 900, clock=time.time, attempts: int = 3):
        self.db, self.system, self.ttl_s, self.clock, self.attempts = db, system, ttl_s, clock, attempts

    def _audit(self, pid, actor, action, detail=""):
        self.db.run("INSERT INTO ticket_audit (seq, proposal_id, at, actor, action, detail) VALUES (?, ?, ?, ?, ?, ?)",
                    (next_seq(), pid, self.clock(), actor, action, detail))

    def get(self, pid: str) -> dict | None:
        rows, _ = self.db.run("SELECT " + COLS + " FROM ticket_proposals WHERE id = ?", (pid,))
        return dict(zip(COLS.split(", "), rows[0])) if rows else None

    def audit(self, pid: str) -> list[dict]:
        rows, _ = self.db.run("SELECT actor, action, detail FROM ticket_audit WHERE proposal_id = ? ORDER BY seq", (pid,))
        return [{"actor": a, "action": b, "detail": c} for a, b, c in rows]

    def propose(self, user, summary: str, details: str) -> dict:
        pid = "prop_" + uuid.uuid4().hex[:12]
        self.db.run("INSERT INTO ticket_proposals (id, requester, summary, details, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
                    (pid, user.id, summary, details, self.clock()))
        self._audit(pid, "atlas", "proposed", json.dumps({"for": user.id, "summary": summary}))
        return self.get(pid)

    def approve(self, pid: str, user, key: str) -> dict:
        """Approve proposal pid as user, with an idempotency key. Return
        {"proposal": <row as dict>, "ticket_id": str, "replayed": bool}.

        Rules, in order:
          1. Unknown proposal: raise NotFound.  Someone other than the requester: raise Forbidden.
          2. Same key as an earlier approval of this proposal:
               - ticket already created: return it with replayed True (no new ticket);
               - ticket not created yet (the system was down): go to step 5 and try again.
          3. Not pending (approved with another key, or expired): raise Conflict.
             Older than ttl_s: mark it 'expired', audit it, raise Conflict.
          4. Claim it atomically: UPDATE ... SET status='approved', idempotency_key=?, approved_by=?
             WHERE id=? AND status='pending'. If no row changed, someone else won: raise Conflict.
             Audit "approved".
          5. Create the ticket: self.system.create(key, payload), retrying TicketSystemError up to
             self.attempts tries in total. If all fail: audit "create_failed", raise TicketSystemDown.
          6. Save ticket_id and status 'created', audit "created", return with replayed False.
        """
        # TODO(week5): follow the rules in the docstring, in order.
        raise NotImplementedError("TODO(week5): TicketService.approve")
