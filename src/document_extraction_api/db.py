"""ジョブの記録（SQLite）。呼び出しはすべて同期で、非同期側からはスレッドで呼ぶ。"""

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

QUEUED = "queued"
PROCESSING = "processing"
SUCCEEDED = "succeeded"
NEEDS_REVIEW = "needs_review"
FAILED = "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    site TEXT NOT NULL,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL,
    file_name TEXT,
    file_sha256 TEXT NOT NULL,
    process_version TEXT NOT NULL,
    requested_model TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    sent_at TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    raw_response TEXT,
    data TEXT,
    issues TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status, created_at);
CREATE INDEX IF NOT EXISTS jobs_dedup ON jobs (site, task_id, file_sha256);
CREATE INDEX IF NOT EXISTS jobs_site ON jobs (site, created_at);
"""


@dataclass(frozen=True)
class Job:
    id: str
    site: str
    task_id: str
    status: str
    file_name: str | None
    file_sha256: str
    process_version: str
    requested_model: str
    attempts: int
    sent_at: str | None
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    data: Any
    issues: list
    error: dict | None
    created_at: str
    updated_at: str
    finished_at: str | None


def now() -> str:
    # 閲覧の期間の絞り込みで文字列のまま比べるので、桁をそろえる
    return datetime.now(UTC).isoformat(timespec="microseconds")


class JobStore:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as conn:
            conn.executescript(_SCHEMA)

    def create_or_find(
        self,
        job_id: str,
        site: str,
        task_id: str,
        file_name: str | None,
        file_sha256: str,
        process_version: str,
        requested_model: str,
        place_file: Callable[[], None],
    ) -> tuple[Job, bool]:
        """同じ拠点・タスク・ファイル・処理の版・モデルのジョブ（失敗以外）があればそれを返す。

        なければ place_file() でファイルを置いてからジョブを登録する。戻り値の bool は新規かどうか。
        既存のジョブを返すときは、ファイル名は最初に送られたときのまま。
        """
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE site=? AND task_id=? AND file_sha256=?"
                    " AND process_version=? AND requested_model=? AND status!=?"
                    " ORDER BY created_at LIMIT 1",
                    (site, task_id, file_sha256, process_version, requested_model, FAILED),
                ).fetchone()
                if row is not None:
                    conn.execute("COMMIT")
                    return _job(row), False
                place_file()
                stamp = now()
                conn.execute(
                    "INSERT INTO jobs (id, site, task_id, status, file_name, file_sha256,"
                    " process_version, requested_model, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        job_id,
                        site,
                        task_id,
                        QUEUED,
                        file_name,
                        file_sha256,
                        process_version,
                        requested_model,
                        stamp,
                        stamp,
                    ),
                )
                # 読み直しも COMMIT の前に行う。失敗したら登録ごと取り消すので、ファイルを消してよい
                row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return _job(row), True

    def get(self, job_id: str) -> Job | None:
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _job(row) if row else None

    def list_jobs(
        self,
        site: str,
        *,
        start: str | None = None,
        end: str | None = None,
        status: str | None = None,
        task_id: str | None = None,
        before: str | None = None,
        limit: int = 50,
    ) -> list[Job]:
        """拠点のジョブを新しい順に返す。before はページ送りの手がかり（前のページの最後の id）。"""
        where, params = _period(site, start, end)
        if status is not None:
            where.append("status=?")
            params.append(status)
        if task_id is not None:
            where.append("task_id=?")
            params.append(task_id)
        if before is not None:
            where.append("id<?")
            params.append(before)
        with closing(self.connect()) as conn:
            rows = conn.execute(
                f"SELECT * FROM jobs WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [_job(row) for row in rows]

    def summary(self, site: str, *, start: str | None = None, end: str | None = None) -> dict:
        """拠点のジョブの状態ごとの件数、トークンと費用の合計、タスクごとの内訳。"""
        where, params = _period(site, start, end)
        condition = " AND ".join(where)
        # トークンを使ったのに費用が出ていないジョブ（単価の設定がないモデル）も数える
        totals = (
            "COUNT(*) AS jobs, COALESCE(SUM(input_tokens), 0) AS input_tokens,"
            " COALESCE(SUM(output_tokens), 0) AS output_tokens,"
            " COALESCE(SUM(cost_usd), 0) AS cost_usd,"
            " SUM(input_tokens IS NOT NULL AND cost_usd IS NULL) AS unpriced_jobs"
        )
        with closing(self.connect()) as conn:
            total = conn.execute(f"SELECT {totals} FROM jobs WHERE {condition}", params).fetchone()
            statuses = conn.execute(
                f"SELECT status, COUNT(*) AS n FROM jobs WHERE {condition} GROUP BY status", params
            ).fetchall()
            tasks = conn.execute(
                f"SELECT task_id, {totals} FROM jobs WHERE {condition}"
                " GROUP BY task_id ORDER BY task_id",
                params,
            ).fetchall()
        return {
            "status_counts": {
                name: 0 for name in (QUEUED, PROCESSING, SUCCEEDED, NEEDS_REVIEW, FAILED)
            }
            | {row["status"]: row["n"] for row in statuses},
            **_totals(total),
            "tasks": [{"task_id": row["task_id"], **_totals(row)} for row in tasks],
        }

    def claim_next(self) -> Job | None:
        """最も古い待ちのジョブを処理中にして返す。"""
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT id FROM jobs WHERE status=? ORDER BY created_at LIMIT 1", (QUEUED,)
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return None
                conn.execute(
                    "UPDATE jobs SET status=?, attempts=attempts+1, sent_at=NULL, updated_at=?"
                    " WHERE id=?",
                    (PROCESSING, now(), row["id"]),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return self.get(row["id"])

    def processing(self) -> list[Job]:
        with closing(self.connect()) as conn:
            rows = conn.execute("SELECT * FROM jobs WHERE status=?", (PROCESSING,)).fetchall()
        return [_job(row) for row in rows]

    def set_process_version(self, job_id: str, version: str) -> None:
        self._update(job_id, process_version=version)

    def mark_sent(self, job_id: str) -> None:
        self._update(job_id, sent_at=now())

    def requeue(self, job_id: str) -> None:
        self._update(job_id, status=QUEUED, sent_at=None)

    def finish(
        self,
        job_id: str,
        status: str,
        *,
        data: Any = None,
        issues: list | None = None,
        error: dict | None = None,
        model: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_usd: float | None = None,
        raw_response: dict | None = None,
    ) -> None:
        self._update(
            job_id,
            status=status,
            data=None if data is None else json.dumps(data, ensure_ascii=False),
            issues=json.dumps(issues or [], ensure_ascii=False),
            error=None if error is None else json.dumps(error, ensure_ascii=False),
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            raw_response=None
            if raw_response is None
            # 返さない列。UTF-8 にできない文字があっても記録で止まらないように ASCII で書く
            else json.dumps(raw_response),
            finished_at=now(),
        )

    def _update(self, job_id: str, **values: Any) -> None:
        values["updated_at"] = now()
        columns = ", ".join(f"{name}=?" for name in values)
        with closing(self.connect()) as conn:
            conn.execute(f"UPDATE jobs SET {columns} WHERE id=?", (*values.values(), job_id))


def _period(site: str, start: str | None, end: str | None) -> tuple[list[str], list[Any]]:
    where, params = ["site=?"], [site]
    if start is not None:
        where.append("created_at>=?")
        params.append(start)
    if end is not None:
        where.append("created_at<?")
        params.append(end)
    return where, params


def _totals(row: sqlite3.Row) -> dict:
    return {
        "jobs": row["jobs"],
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "cost_usd": row["cost_usd"],
        "unpriced_jobs": row["unpriced_jobs"] or 0,
    }


def _job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        site=row["site"],
        task_id=row["task_id"],
        status=row["status"],
        file_name=row["file_name"],
        file_sha256=row["file_sha256"],
        process_version=row["process_version"],
        requested_model=row["requested_model"],
        attempts=row["attempts"],
        sent_at=row["sent_at"],
        model=row["model"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cost_usd=row["cost_usd"],
        data=json.loads(row["data"]) if row["data"] else None,
        issues=json.loads(row["issues"]) if row["issues"] else [],
        error=json.loads(row["error"]) if row["error"] else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        finished_at=row["finished_at"],
    )
