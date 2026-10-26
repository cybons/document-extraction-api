import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .errors import JobError


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "jobs.sqlite3"

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self):
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA secure_delete=ON")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, idem TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, task TEXT NOT NULL, status TEXT NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL, snapshot TEXT NOT NULL,
                    result TEXT, reasons TEXT, error TEXT, expired INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(owner, idem)
                );
                CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, created);
                CREATE TABLE IF NOT EXISTS attempts (
                    job_id TEXT NOT NULL, attempt INTEGER NOT NULL, model TEXT NOT NULL,
                    created REAL NOT NULL, input_tokens INTEGER, output_tokens INTEGER,
                    cost TEXT, price TEXT NOT NULL, outcome TEXT NOT NULL,
                    PRIMARY KEY(job_id, attempt)
                );
            """)
            db.execute(
                """UPDATE jobs SET status='failed', error='interrupted', updated=?
                          WHERE status='running'""",
                (time.time(),),
            )

    def enqueue(self, job_id, owner, idem, fingerprint, task, snapshot, max_pending):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM jobs WHERE owner=? AND idem=?", (owner, idem)
            ).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise JobError("idempotency_conflict")
                return dict(row), False
            count = db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')")
            if count.fetchone()[0] >= max_pending:
                raise JobError("queue_full")
            now = time.time()
            db.execute(
                """INSERT INTO jobs
                (id,owner,idem,fingerprint,task,status,created,updated,snapshot)
                VALUES (?,?,?,?,?,'queued',?,?,?)""",
                (job_id, owner, idem, fingerprint, task, now, now, json.dumps(snapshot)),
            )
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()), True

    def claim(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE jobs SET status='running',updated=? WHERE id=?", (time.time(), row["id"])
            )
            return dict(row)

    def finish(self, job_id, status, result=None, reasons=None, error=None):
        with self.db() as db:
            db.execute(
                """UPDATE jobs SET status=?,result=?,reasons=?,error=?,updated=?
                          WHERE id=? AND status='running'""",
                (
                    status,
                    json.dumps(result) if result is not None else None,
                    json.dumps(reasons or []),
                    error,
                    time.time(),
                    job_id,
                ),
            )

    def attempt(self, job_id, number, model, usage, cost, price, outcome):
        with self.db() as db:
            db.execute(
                """INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id, attempt) DO UPDATE SET
                input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens,
                cost=excluded.cost, outcome=excluded.outcome""",
                (
                    job_id,
                    number,
                    model,
                    time.time(),
                    usage[0] if usage else None,
                    usage[1] if usage else None,
                    cost,
                    json.dumps(price),
                    outcome,
                ),
            )

    def get(self, job_id, owner):
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE id=? AND owner=?", (job_id, owner)
            ).fetchone()
            return dict(row) if row else None

    def active_ids(self):
        with self.db() as db:
            return {
                r[0] for r in db.execute("SELECT id FROM jobs WHERE status IN ('queued','running')")
            }

    def cleanup(self, result_days, history_days):
        now = time.time()
        with self.db() as db:
            db.execute("PRAGMA secure_delete=ON")
            db.execute(
                """UPDATE jobs SET result=NULL,reasons=NULL,expired=1
                WHERE status NOT IN ('queued','running') AND updated < ?""",
                (now - result_days * 86400,),
            )
            db.execute(
                """DELETE FROM attempts WHERE job_id IN
                (SELECT id FROM jobs WHERE status NOT IN ('queued','running') AND updated < ?)""",
                (now - history_days * 86400,),
            )
            db.execute(
                "DELETE FROM jobs WHERE status NOT IN ('queued','running') AND updated < ?",
                (now - history_days * 86400,),
            )
        with self.db() as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def ping(self):
        with self.db() as db:
            db.execute("SELECT 1").fetchone()
