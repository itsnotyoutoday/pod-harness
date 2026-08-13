"""Job records — the coordination that object storage cannot provide.

## Why this exists

Specs, logs and artifacts live perfectly well on the volume. Ownership does not.

Object storage has no atomic compare-and-swap and no leases, so two processes can both read
`jobs/abc.json`, both see it unclaimed, both write "claimed", and both run it — with no way
to detect that it happened. Today nothing hits that, because `batch_pod.py` bakes the job id
into the pod's start command and the pod never scans for work. But nothing *enforces* that
invariant, and the first worker that polls a directory breaks it silently.

So coordination moves here, where a claim can be a guarded transition:

    coordination — who owns this job, is it claimed, is it alive   ← registry (this file)
    payload      — spec, corpus, logs, artifacts                   ← object storage

## Why no database server

Because the control plane is the only writer. `assign()` is a guarded state transition
performed by one process; there is no distributed claim to arbitrate, so there is nothing
for consensus to do. SQLite's single-writer model is not a limitation here — it is an exact
match for the design.

That stops being true only if genuinely pull-based workers arrive, at which point
`PostgresRegistry` implements the same interface and nothing above changes.

## Job ids

ULID: 26 chars, lexicographically sortable by creation time. Sortability is not cosmetic —
it means `ls` of a job directory is chronological, and log correlation does not need a
join. Server-minted, never client-chosen, which is what removes collisions.

`idempotency_key` is the client's channel instead: unique-indexed, so a retried submit
returns the existing job rather than starting a second pod-hour. Agents retry on timeout;
this makes that safe.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATES = ("queued", "assigned", "running", "done", "failed", "cancelled")
TERMINAL = ("done", "failed", "cancelled")

# Which transitions are legal. Enforced rather than documented, because an illegal
# transition is exactly how a job gets run twice.
ALLOWED: dict[str, tuple[str, ...]] = {
    "queued":    ("assigned", "cancelled", "failed"),
    "assigned":  ("running", "failed", "cancelled"),
    "running":   ("done", "failed", "cancelled"),
    "done":      (),
    "failed":    ("queued",),        # explicit retry re-queues
    "cancelled": (),
}

_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"      # Crockford: no I, L, O, U


def new_job_id() -> str:
    """ULID: 48-bit millisecond timestamp + 80 bits of randomness, Crockford base32."""
    ms = int(time.time() * 1000)
    ts = "".join(_B32[(ms >> (45 - 5 * i)) & 31] for i in range(10))
    rand = "".join(secrets.choice(_B32) for _ in range(16))
    return f"job_{ts}{rand}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TransitionError(RuntimeError):
    """Raised when a state change is not legal from the current state. Carries both states
    so the caller can tell a race from a bug."""


class Registry(ABC):
    @abstractmethod
    def create(self, spec: dict, *, idempotency_key: str | None = None) -> dict: ...
    @abstractmethod
    def get(self, job_id: str) -> dict | None: ...
    @abstractmethod
    def by_idempotency_key(self, key: str) -> dict | None: ...
    @abstractmethod
    def assign(self, job_id: str, runner_id: str, *, cost_hr: float = 0.0) -> dict: ...
    @abstractmethod
    def set_state(self, job_id: str, state: str, *, error: str | None = None) -> dict: ...
    @abstractmethod
    def record_stage(self, job_id: str, stage: str, info: dict) -> None: ...
    @abstractmethod
    def list(self, *, state: str | None = None, limit: int = 100) -> list[dict]: ...
    @abstractmethod
    def due_for_reaping(self, *, now_ts: float | None = None) -> list[dict]: ...


class SqliteRegistry(Registry):
    """One file, transactional, ample at this scale.

    All mutation goes through `_txn`, which takes an IMMEDIATE transaction — so a
    concurrent writer blocks rather than racing, and the guarded transition in `assign` is
    genuinely atomic rather than merely careful.
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.environ.get(
            "PODH_REGISTRY", Path.home() / ".lingua" / "jobs.db"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")     # readers never block the writer
        c.execute("PRAGMA foreign_keys=ON")
        return c

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id           TEXT PRIMARY KEY,
                    idempotency_key  TEXT UNIQUE,
                    state            TEXT NOT NULL,
                    spec             TEXT NOT NULL,
                    code_rev         TEXT,
                    assigned_to      TEXT,
                    provider         TEXT,
                    created_at       TEXT NOT NULL,
                    assigned_at      TEXT,
                    started_at       TEXT,
                    finished_at      TEXT,
                    deadline_ts      INTEGER,
                    cost_hr          REAL DEFAULT 0,
                    last_seq         INTEGER DEFAULT 0,
                    stage_state      TEXT DEFAULT '{}',
                    error            TEXT
                )""")
            c.execute("CREATE INDEX IF NOT EXISTS ix_jobs_state ON jobs(state)")
            c.execute("CREATE INDEX IF NOT EXISTS ix_jobs_deadline ON jobs(deadline_ts)")

    def _txn(self):
        return self._conn()

    @staticmethod
    def _end(c: sqlite3.Connection) -> None:
        """COMMIT only if a transaction is actually open.

        The failure paths BEGIN, find the guard rejected them, and must close the
        transaction before raising TransitionError. Calling COMMIT unconditionally raises
        'cannot commit - no transaction is active' from inside an except-path, which
        replaces the meaningful error with a meaningless one.
        """
        try:
            if c.in_transaction:
                c.execute("COMMIT")
        except sqlite3.OperationalError:
            pass

    @staticmethod
    def _row(r: sqlite3.Row | None) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        d["spec"] = json.loads(d["spec"])
        d["stage_state"] = json.loads(d["stage_state"] or "{}")
        return d

    def create(self, spec: dict, *, idempotency_key: str | None = None) -> dict:
        if idempotency_key:
            existing = self.by_idempotency_key(idempotency_key)
            if existing:
                return existing          # idempotent: never a second pod-hour

        job_id = new_job_id()
        budget = float((spec.get("runner") or {}).get("budget_min") or 0)
        deadline = int(time.time() + budget * 60) if budget else None
        with self._lock, self._txn() as c:
            try:
                c.execute(
                    "INSERT INTO jobs (job_id, idempotency_key, state, spec, provider,"
                    " created_at, deadline_ts) VALUES (?,?,?,?,?,?,?)",
                    (job_id, idempotency_key, "queued", json.dumps(spec),
                     (spec.get("runner") or {}).get("provider"), _now(), deadline))
            except sqlite3.IntegrityError:
                # Lost a race on the unique index — the other writer's job is the answer.
                existing = self.by_idempotency_key(idempotency_key or "")
                if existing:
                    return existing
                raise
        return self.get(job_id)          # type: ignore[return-value]

    def get(self, job_id: str) -> dict | None:
        with self._conn() as c:
            return self._row(c.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    def by_idempotency_key(self, key: str) -> dict | None:
        with self._conn() as c:
            return self._row(c.execute(
                "SELECT * FROM jobs WHERE idempotency_key=?", (key,)).fetchone())

    def assign(self, job_id: str, runner_id: str, *, cost_hr: float = 0.0) -> dict:
        """Guarded claim. The UPDATE's WHERE clause carries the expected state, so a second
        caller updates zero rows and is told so — the check and the write are one statement
        and cannot be interleaved."""
        with self._lock, self._txn() as c:
            c.execute("BEGIN IMMEDIATE")
            cur = c.execute(
                "UPDATE jobs SET state='assigned', assigned_to=?, assigned_at=?, cost_hr=?"
                " WHERE job_id=? AND state='queued'",
                (runner_id, _now(), cost_hr, job_id))
            if cur.rowcount == 0:
                row = self._row(c.execute(
                    "SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())
                self._end(c)
                if row is None:
                    raise TransitionError(f"no such job: {job_id}")
                raise TransitionError(
                    f"job {job_id} is {row['state']!r}, not 'queued' — "
                    f"already assigned to {row.get('assigned_to')!r}")
            self._end(c)
        return self.get(job_id)          # type: ignore[return-value]

    def set_state(self, job_id: str, state: str, *, error: str | None = None) -> dict:
        if state not in STATES:
            raise ValueError(f"unknown state {state!r}; want one of {STATES}")
        with self._lock, self._txn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                self._end(c)
                raise TransitionError(f"no such job: {job_id}")
            cur_state = row["state"]
            if state != cur_state and state not in ALLOWED.get(cur_state, ()):
                self._end(c)
                raise TransitionError(f"{cur_state} -> {state} is not a legal transition")
            sets, vals = ["state=?"], [state]
            if state == "running":
                sets.append("started_at=COALESCE(started_at,?)"); vals.append(_now())
            if state in TERMINAL:
                sets.append("finished_at=?"); vals.append(_now())
            if error is not None:
                sets.append("error=?"); vals.append(error)
            vals.append(job_id)
            c.execute(f"UPDATE jobs SET {','.join(sets)} WHERE job_id=?", vals)
            self._end(c)
        return self.get(job_id)          # type: ignore[return-value]

    def record_stage(self, job_id: str, stage: str, info: dict) -> None:
        """Merge one stage's outcome into stage_state. This is what makes a failed job say
        WHICH stage failed and why verification rejected it — and what resume reads."""
        with self._lock, self._txn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT stage_state FROM jobs WHERE job_id=?",
                            (job_id,)).fetchone()
            if row is None:
                self._end(c)
                return
            st = json.loads(row["stage_state"] or "{}")
            st[stage] = {**st.get(stage, {}), **info}
            c.execute("UPDATE jobs SET stage_state=? WHERE job_id=?",
                      (json.dumps(st), job_id))
            self._end(c)

    def list(self, *, state: str | None = None, limit: int = 100) -> list[dict]:
        q = "SELECT * FROM jobs"
        args: list[Any] = []
        if state:
            q += " WHERE state=?"; args.append(state)
        q += " ORDER BY job_id DESC LIMIT ?"      # ULIDs sort chronologically
        args.append(limit)
        with self._conn() as c:
            return [self._row(r) for r in c.execute(q, args).fetchall()]  # type: ignore

    def due_for_reaping(self, *, now_ts: float | None = None) -> list[dict]:
        """Non-terminal jobs past their deadline. The reaper's work list."""
        now = int(now_ts or time.time())
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM jobs WHERE deadline_ts IS NOT NULL AND deadline_ts < ?"
                " AND state NOT IN ('done','failed','cancelled')", (now,)).fetchall()
        return [self._row(r) for r in rows]      # type: ignore[misc]


class MemoryRegistry(SqliteRegistry):
    """Same implementation against an in-memory database — for tests and one-shot CLI use.

    Subclassing rather than reimplementing so the tests exercise the real transition guards
    and the real SQL, not a simplified stand-in that could diverge.
    """

    def __init__(self):
        self._shared = sqlite3.connect(":memory:", check_same_thread=False,
                                       isolation_level=None)
        self._shared.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self.path = Path(":memory:")
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:      # type: ignore[override]
        return self._shared

    def _init_schema(self) -> None:
        c = self._shared
        c.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, state TEXT NOT NULL,
                spec TEXT NOT NULL, code_rev TEXT, assigned_to TEXT, provider TEXT,
                created_at TEXT NOT NULL, assigned_at TEXT, started_at TEXT,
                finished_at TEXT, deadline_ts INTEGER, cost_hr REAL DEFAULT 0,
                last_seq INTEGER DEFAULT 0, stage_state TEXT DEFAULT '{}', error TEXT)""")


def open_registry(path: str | Path | None = None) -> Registry:
    """The one place a registry implementation is chosen."""
    if str(path) == ":memory:":
        return MemoryRegistry()
    return SqliteRegistry(path)
