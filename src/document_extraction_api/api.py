"""HTTP API。"""

import asyncio
import contextlib
import hashlib
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Annotated, BinaryIO

import anyio
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile as StarletteUploadFile

from . import db
from .auth import UPLOAD_PATH, AuthMiddleware
from .claude import Extractor
from .processes import Process, ProcessRegistry, write_snapshot
from .settings import Settings
from .worker import INPUT_FILE, PROCESS_DIR, Worker, job_dir

logger = logging.getLogger(__name__)

_SIGNATURES = (b"%PDF-", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"II*\x00", b"MM\x00*")


class _UnsupportedFile(Exception):
    pass


class _FileTooLarge(Exception):
    pass


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _is_supported(head: bytes) -> bool:
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return True
    return any(head.startswith(signature) for signature in _SIGNATURES)


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
        return _error(400, "invalid_request", "file と task_id を送る")

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
                    _accept, settings, store, site, process, file.file
                )
        except _FileTooLarge:
            return _error(413, "file_too_large", "ファイルが大きすぎる")
        except _UnsupportedFile:
            return _error(415, "unsupported_file", "PDF・PNG・JPEG・TIFF・WebP のみ受け付ける")
        if created:
            logger.info("ジョブ受付: %s %s %s", job.id, site, task_id)
            worker.wake.set()
        return JSONResponse({"job_id": job.id, "status": job.status}, status_code=202)

    @app.get("/api/v1/jobs/{job_id}")
    async def get_job(request: Request, job_id: str):
        job = await anyio.to_thread.run_sync(store.get, job_id)
        if job is None or job.site != request.state.site:
            return _error(404, "job_not_found", "ジョブがない")
        return _job_response(job)

    return AuthMiddleware(
        app,
        settings.sites,
        upload_slots=settings.upload_slots,
        max_body_bytes=settings.max_upload_bytes + 64 * 1024,
    )


def _accept(
    settings: Settings, store: db.JobStore, site: str, process: Process, source: BinaryIO
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
