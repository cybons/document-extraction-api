"""HTTP API。"""

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import shutil
import unicodedata
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, BinaryIO

import anyio
from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.datastructures import UploadFile as StarletteUploadFile

from . import db
from .auth import UPLOAD_PATH, AuthMiddleware
from .claude import Extractor
from .processes import Process, ProcessRegistry, describe_path, load_snapshot, write_snapshot
from .settings import Settings
from .worker import INPUT_FILE, PROCESS_DIR, RESPONSE_FILE, Worker, job_dir

logger = logging.getLogger(__name__)

_SIGNATURES = (b"%PDF-", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"II*\x00", b"MM\x00*")
_MEDIA_TYPES = {
    b"%PDF-": "application/pdf",
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"II*\x00": "image/tiff",
    b"MM\x00*": "image/tiff",
}
_STATUSES = (db.QUEUED, db.PROCESSING, db.SUCCEEDED, db.NEEDS_REVIEW, db.FAILED)
MAX_FILE_NAME = 255
_UTC_OFFSET = re.compile(r"([+-])(\d{2}):(\d{2})")
MAX_LIST_LIMIT = 200
# 閲覧用の応答。ジョブのファイルを直接開かれても、中身を動かさない
_VIEW_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "Cache-Control": "no-store",
}


class _UnsupportedFile(Exception):
    pass


class _FileTooLarge(Exception):
    pass


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _is_supported(head: bytes) -> bool:
    return _media_type(head) is not None


def _media_type(head: bytes) -> str | None:
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    for signature in _SIGNATURES:
        if head.startswith(signature):
            return _MEDIA_TYPES[signature]
    return None


def _file_name(raw: str | None) -> str | None:
    """送られたファイル名を、記録して返せる形にそろえる。フォルダの部分は落とす。"""
    if not raw:
        return None
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    # UTF-8 にできない文字と制御文字は ? にする
    name = name.encode("utf-8", "replace").decode("utf-8")
    name = "".join("?" if unicodedata.category(c) == "Cc" else c for c in name)
    return name[:MAX_FILE_NAME] or None


def _instant(value: str | None) -> str | None:
    """時差の付いた ISO 8601 の時刻を、DB と同じ UTC の文字列にする。読めなければ ValueError。"""
    if value is None:
        return None
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError("時差がない")
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _offset_minutes(value: str) -> int:
    """+09:00 のような時差を分にする。読めなければ ValueError。"""
    match = _UTC_OFFSET.fullmatch(value)
    if match is None:
        raise ValueError(value)
    sign, hours, minutes = match.groups()
    total = int(hours) * 60 + int(minutes)
    if total > 14 * 60 or int(minutes) >= 60:
        raise ValueError(value)
    return total if sign == "+" else -total


def create_app(
    settings: Settings,
    registry: ProcessRegistry,
    extractor: Extractor,
    *,
    start_worker: bool = True,
):
    store = db.JobStore(settings.data_dir / "jobs.sqlite3")
    store.initialize()
    worker = Worker(settings, store, extractor)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(worker.run()) if start_worker else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.worker = worker

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        if request.url.path == UPLOAD_PATH:
            return _error(400, "invalid_request", "file と task_id を送る")
        return _error(400, "invalid_request", "パラメータが正しくない")

    @app.get("/api/v1/health")
    async def health():
        return {"status": "ok"}

    @app.post(UPLOAD_PATH)
    async def extract(
        request: Request,
        file: Annotated[UploadFile, File()],
        task_id: Annotated[str, Form()],
    ):
        site = request.state.site
        form = await request.form()  # FastAPI が読んだ結果を使う（本文は読み直さない）
        if sum(isinstance(value, StarletteUploadFile) for _, value in form.multi_items()) != 1:
            return _error(400, "invalid_request", "ファイルは1つだけ送る")
        process = registry.get(site, task_id)
        if process is None:
            return _error(404, "task_not_found", "この拠点では使えないタスク")
        try:
            # 要求がキャンセルされても、ファイルの保存と登録は最後まで行う
            with anyio.CancelScope(shield=True):
                job, created = await anyio.to_thread.run_sync(
                    _accept, settings, store, site, process, _file_name(file.filename), file.file
                )
        except _FileTooLarge:
            return _error(413, "file_too_large", "ファイルが大きすぎる")
        except _UnsupportedFile:
            return _error(415, "unsupported_file", "PDF・PNG・JPEG・TIFF・WebP のみ受け付ける")
        if created:
            logger.info("ジョブ受付: %s %s %s", job.id, site, task_id)
            worker.wake.set()
        return JSONResponse({"job_id": job.id, "status": job.status}, status_code=202)

    names: dict[str, tuple[str | None, dict[str, str]]] = {}

    def process_names(job: db.Job) -> tuple[str | None, dict[str, str]]:
        """受け付けたときの処理に書いた帳票の名前と項目の名前。読めなければ (None, {})。"""
        if job.process_version not in names:
            try:
                process = load_snapshot(job.site, job_dir(settings, job.id) / PROCESS_DIR)
            except Exception:
                # 名前は表示のためだけのもの。読めなくても閲覧は止めない（覚えずに次に読み直す）
                return None, {}
            names[job.process_version] = (process.display_name, process.labels or {})
        return names[job.process_version]

    def task_name(site: str, task_id: str) -> str | None:
        process = registry.get(site, task_id)
        return process.display_name if process is not None else None

    async def own_job(request: Request, job_id: str) -> db.Job | None:
        """要求のキーの拠点のジョブだけを返す。ほかの拠点のジョブは、ないものとして扱う。"""
        job = await anyio.to_thread.run_sync(store.get, job_id)
        return job if job is not None and job.site == request.state.site else None

    def job_not_found() -> JSONResponse:
        return _error(404, "job_not_found", "ジョブがない")

    @app.get("/api/v1/jobs/{job_id}")
    async def get_job(request: Request, job_id: str):
        job = await own_job(request, job_id)
        if job is None:
            return job_not_found()
        return _job_response(job)

    # ここから閲覧用。拠点は必ずキーから決め、パラメータでは受け付けない

    @app.get("/api/v1/jobs")
    async def list_jobs(
        request: Request,
        start: Annotated[str | None, Query(alias="from")] = None,
        end: Annotated[str | None, Query(alias="to")] = None,
        status: Annotated[list[str] | None, Query()] = None,
        task_id: str | None = None,
        before: str | None = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIST_LIMIT)] = 50,
    ):
        try:
            start, end = _instant(start), _instant(end)
        except ValueError:
            return _error(400, "invalid_request", "from・to は時差の付いた ISO 8601 の時刻にする")
        if status and any(value not in _STATUSES for value in status):
            return _error(400, "invalid_request", "status が正しくない")

        def listed() -> list[tuple[db.Job, tuple[str | None, dict[str, str]]]]:
            jobs = store.list_jobs(
                request.state.site,
                start=start,
                end=end,
                statuses=status,
                task_id=task_id,
                before=before,
                limit=limit,
            )
            return [(job, process_names(job)) for job in jobs]

        jobs = await anyio.to_thread.run_sync(listed)
        return {
            "jobs": [_list_item(job, *info) for job, info in jobs],
            "next_before": jobs[-1][0].id if len(jobs) == limit else None,
        }

    @app.get("/api/v1/summary")
    async def summary(
        request: Request,
        start: Annotated[str | None, Query(alias="from")] = None,
        end: Annotated[str | None, Query(alias="to")] = None,
        task_id: str | None = None,
        utc_offset: str = "+00:00",
    ):
        try:
            start, end = _instant(start), _instant(end)
        except ValueError:
            return _error(400, "invalid_request", "from・to は時差の付いた ISO 8601 の時刻にする")
        try:
            offset = _offset_minutes(utc_offset)
        except ValueError:
            return _error(400, "invalid_request", "utc_offset は +09:00 のように書く")
        site = request.state.site
        result = await anyio.to_thread.run_sync(
            lambda: store.summary(
                site, start=start, end=end, task_id=task_id, utc_offset_minutes=offset
            )
        )
        for task in result["tasks"]:
            task["task_name"] = task_name(site, task["task_id"])
        return {"site": site, "site_name": settings.sites[site].display_name or site, **result}

    @app.get("/api/v1/jobs/{job_id}/detail")
    async def job_detail(request: Request, job_id: str):
        job = await own_job(request, job_id)
        if job is None:
            return job_not_found()
        display_name, labels = await anyio.to_thread.run_sync(process_names, job)
        return _job_response(job) | {
            "task_name": display_name,
            "labels": labels,
            "issue_fields": [_issue_field(issue, labels) for issue in job.issues],
            "file_name": job.file_name,
            "file_sha256": job.file_sha256,
            "requested_model": job.requested_model,
            "attempts": job.attempts,
            "created_at": job.created_at,
            "sent_at": job.sent_at,
            "finished_at": job.finished_at,
            "updated_at": job.updated_at,
        }

    @app.get("/api/v1/jobs/{job_id}/file")
    async def job_file(request: Request, job_id: str):
        job = await own_job(request, job_id)
        if job is None:
            return job_not_found()
        path = job_dir(settings, job.id) / INPUT_FILE
        media_type = await anyio.to_thread.run_sync(_stored_media_type, path)
        if media_type is None:
            return _error(404, "file_not_found", "ファイルがない")
        return FileResponse(path, media_type=media_type, headers=_VIEW_HEADERS)

    @app.get("/api/v1/jobs/{job_id}/response")
    async def job_raw_response(request: Request, job_id: str):
        job = await own_job(request, job_id)
        if job is None:
            return job_not_found()
        # 保存した応答は ASCII の JSON なので、読み直さずにそのまま返す
        path = job_dir(settings, job.id) / RESPONSE_FILE
        body = await anyio.to_thread.run_sync(_read_bytes, path)
        if body is None:
            return _error(404, "response_not_found", "Claude の応答がない")
        return Response(body, media_type="application/json", headers=_VIEW_HEADERS)

    @app.get("/api/v1/jobs/{job_id}/process")
    async def job_process(request: Request, job_id: str):
        job = await own_job(request, job_id)
        if job is None:
            return job_not_found()
        snapshot = await anyio.to_thread.run_sync(
            _read_snapshot, job_dir(settings, job.id) / PROCESS_DIR
        )
        if snapshot is None:
            return _error(404, "process_not_found", "処理の写しがない")
        return snapshot

    return AuthMiddleware(
        app,
        settings.sites,
        upload_slots=settings.upload_slots,
        max_body_bytes=settings.max_upload_bytes + 64 * 1024,
    )


def _accept(
    settings: Settings,
    store: db.JobStore,
    site: str,
    process: Process,
    file_name: str | None,
    source: BinaryIO,
) -> tuple[db.Job, bool]:
    temp_dir = settings.data_dir / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp = temp_dir / uuid.uuid4().hex
    try:
        digest = hashlib.sha256()
        source.seek(0)
        head = source.read(16)
        if not _is_supported(head):
            raise _UnsupportedFile()
        with temp.open("wb") as out:
            chunk = head
            size = 0
            while chunk:
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise _FileTooLarge()
                digest.update(chunk)
                out.write(chunk)
                chunk = source.read(1024 * 1024)
            out.flush()
            os.fsync(out.fileno())

        job_id = str(uuid.uuid7())
        directory = job_dir(settings, job_id)

        def place_file() -> None:
            directory.mkdir(parents=True)
            write_snapshot(process, directory / PROCESS_DIR)
            os.replace(temp, directory / INPUT_FILE)

        try:
            return store.create_or_find(
                job_id,
                site,
                process.task_id,
                file_name,
                digest.hexdigest(),
                process.version,
                settings.model,
                place_file,
            )
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
    finally:
        Path(temp).unlink(missing_ok=True)


def _job_response(job: db.Job) -> dict:
    finished = job.status in (db.SUCCEEDED, db.NEEDS_REVIEW, db.FAILED)
    return {
        "job_id": job.id,
        "task_id": job.task_id,
        "site": job.site,
        "status": job.status,
        "data": job.data,
        "issues": job.issues,
        "error": job.error,
        "versions": {"process": job.process_version, "model": job.model},
        "usage": (
            {
                "input_tokens": job.input_tokens,
                "output_tokens": job.output_tokens,
                "cost_usd": job.cost_usd,
            }
            if finished and job.input_tokens is not None
            else None
        ),
    }


def _issue_field(issue: object, labels: dict[str, str]) -> str | None:
    path = issue.get("path") if isinstance(issue, dict) else None
    return describe_path(path, labels) if isinstance(path, str) else None


def _list_item(job: db.Job, task_name: str | None, labels: dict[str, str]) -> dict:
    return {
        "job_id": job.id,
        "task_id": job.task_id,
        "task_name": task_name,
        "status": job.status,
        "file_name": job.file_name,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
        "cost_usd": job.cost_usd,
        "error_code": job.error.get("code") if isinstance(job.error, dict) else None,
        "issue_count": len(job.issues),
        "issues": [
            {
                "field": _issue_field(issue, labels),
                "message": issue.get("message") if isinstance(issue, dict) else None,
            }
            for issue in job.issues
        ],
    }


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _stored_media_type(path: Path) -> str | None:
    try:
        with path.open("rb") as f:
            return _media_type(f.read(16)) or "application/octet-stream"
    except FileNotFoundError:
        return None


def _read_snapshot(directory: Path) -> dict | None:
    """受け付けたときの処理の写し。ファイルの中身は文字として返す。"""
    try:
        folders = sorted(path for path in directory.iterdir() if path.is_dir())
    except FileNotFoundError:
        return None
    if not folders:
        return None
    folder = folders[0]
    files = {
        path.name: path.read_bytes().decode("utf-8", "replace")
        for path in sorted(folder.iterdir())
        if path.is_file()
    }
    return {"name": folder.name, "files": files}
