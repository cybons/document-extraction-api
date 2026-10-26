import asyncio
import hashlib
import hmac
import json
import logging
import re
import shutil
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from filelock import FileLock
from starlette.datastructures import UploadFile

from .config import Settings
from .errors import JobError
from .provider import ClaudeProvider
from .schemas import JobResponse
from .store import Store
from .tasks import TASKS
from .worker import Worker


def error_response(code, status):
    return JSONResponse({"error": {"code": code}}, status_code=status)


class Gate:
    """Authenticate and bound the raw multipart body before Starlette spools any file."""

    def __init__(self, app, settings):
        self.app, self.settings = app, settings
        self.slots = asyncio.Semaphore(2)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        if scope["path"] not in {"/health/live", "/health/ready"}:
            auth = headers.get(b"authorization", b"").decode("latin1")
            owner = next(
                (
                    name
                    for name, c in self.settings.clients.items()
                    if hmac.compare_digest(
                        auth.encode(), ("Bearer " + c.key.get_secret_value()).encode()
                    )
                ),
                None,
            )
            if owner is None:
                return await error_response("unauthorized", 401)(scope, receive, send)
            scope.setdefault("state", {})["owner"] = owner
        if scope["method"] == "POST":
            if self.slots.locked():
                return await error_response("upload_busy", 429)(scope, receive, send)
            async with self.slots:
                body = bytearray()
                try:
                    async with asyncio.timeout(self.settings.upload_seconds):
                        while True:
                            message = await receive()
                            if message["type"] == "http.disconnect":
                                return
                            body.extend(message.get("body", b""))
                            if len(body) > self.settings.max_file_bytes + 64 * 1024:
                                return await error_response("request_too_large", 413)(
                                    scope, receive, send
                                )
                            if not message.get("more_body", False):
                                break
                except TimeoutError:
                    return await error_response("upload_timeout", 408)(scope, receive, send)

                delivered = False

                async def buffered():
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {"type": "http.request", "body": bytes(body), "more_body": False}
                    return await receive()

                return await self.app(scope, buffered, send)
        return await self.app(scope, receive, send)


def public_job(row):
    snapshot = json.loads(row["snapshot"])
    return {
        "job_id": row["id"],
        "status": row["status"],
        "task_id": row["task"],
        "result_url": f"/api/v1/documents/jobs/{row['id']}",
        "created_at": datetime.fromtimestamp(row["created"], UTC).isoformat(),
        "updated_at": datetime.fromtimestamp(row["updated"], UTC).isoformat(),
        "data": json.loads(row["result"]) if row["result"] else None,
        "review_reasons": json.loads(row["reasons"]) if row["reasons"] else [],
        "error": {"code": row["error"]} if row["error"] else None,
        "result_expired": bool(row["expired"]),
        "versions": {"model": snapshot["model"], "task": snapshot["version"]},
    }


def create_app(settings=None, provider_factory=ClaudeProvider, renderer=None):
    settings = settings or Settings()
    root = settings.data_dir.resolve()
    store = Store(root)

    @asynccontextmanager
    async def lifespan(app):
        root.mkdir(parents=True, exist_ok=True)
        lock = FileLock(root / "service.lock", timeout=0)
        with lock:
            (root / "files").mkdir(exist_ok=True)
            store.initialize()
            provider = provider_factory(settings, store)
            worker = Worker(
                settings, store, provider, **({"renderer": renderer} if renderer else {})
            )
            app.state.worker = worker
            app.state.loop = asyncio.create_task(worker.run(), name="document-worker")
            try:
                yield
            finally:
                app.state.loop.cancel()
                with suppress(asyncio.CancelledError):
                    await app.state.loop
                await provider.close()

    app = FastAPI(
        title="Document Extraction API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.add_middleware(Gate, settings=settings)
    app.state.store = store

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error_response(str(exc.detail), exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return error_response("invalid_request", 422)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        logging.getLogger("document_extraction").error("event=request_failed")
        return error_response("service_unavailable", 503)

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready():
        worker = app.state.worker
        try:
            await asyncio.to_thread(store.ping)
        except Exception:
            return error_response("not_ready", 503)
        if app.state.loop.done() or not worker.healthy or not worker.maintenance_healthy:
            return error_response("not_ready", 503)
        return {"status": "ready"}

    @app.post(
        "/api/v1/documents/extract",
        status_code=202,
        response_model=JobResponse,
        responses={200: {"model": JobResponse}},
        openapi_extra={
            "parameters": [
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "maxLength": 128},
                }
            ],
            "requestBody": {
                "required": True,
                "content": {
                    "multipart/form-data": {
                        "schema": {
                            "type": "object",
                            "required": ["file", "task_id"],
                            "properties": {
                                "file": {"type": "string", "format": "binary"},
                                "task_id": {"type": "string", "enum": list(TASKS)},
                                "rotation": {
                                    "type": "integer",
                                    "enum": [0, 90, 180, 270],
                                    "default": 0,
                                },
                            },
                        }
                    }
                },
            },
        },
    )
    async def extract(request: Request):
        owner = request.state.owner
        idem = request.headers.get("Idempotency-Key", "")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", idem):
            raise HTTPException(422, "invalid_idempotency_key")
        try:
            async with request.form(max_files=1, max_fields=2, max_part_size=8192) as form:
                if set(form) - {"file", "task_id", "rotation"}:
                    raise HTTPException(422, "unexpected_field")
                if any(len(form.getlist(k)) != 1 for k in form):
                    raise HTTPException(422, "duplicate_field")
                task_id = form.get("task_id")
                rotation = form.get("rotation", "0")
                upload = form.get("file")
                if not isinstance(task_id, str) or task_id not in TASKS:
                    raise HTTPException(422, "unknown_task")
                if task_id not in settings.clients[owner].tasks:
                    raise HTTPException(403, "task_forbidden")
                if rotation not in {"0", "90", "180", "270"}:
                    raise HTTPException(422, "invalid_rotation")
                if not isinstance(upload, UploadFile):
                    raise HTTPException(422, "file_required")
                content = await upload.read(settings.max_file_bytes + 1)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(400, "invalid_multipart") from exc
        if not content or len(content) > settings.max_file_bytes:
            raise HTTPException(413, "invalid_file_size")
        if not content.startswith((b"%PDF-", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
            raise HTTPException(415, "unsupported_file")
        fingerprint = hashlib.sha256(
            task_id.encode() + b"\0" + rotation.encode() + b"\0" + content
        ).hexdigest()
        job_id = str(uuid.uuid4())
        folder = root / "files" / job_id
        task = TASKS[task_id]
        snapshot = {
            "model": settings.model,
            "version": task.version,
            "prompt": task.prompt,
            "schema": task.schema.model_json_schema(),
            "rotation": int(rotation),
            "price": {
                "input": str(settings.input_usd_per_million)
                if settings.input_usd_per_million is not None
                else None,
                "output": str(settings.output_usd_per_million)
                if settings.output_usd_per_million is not None
                else None,
            },
        }
        # Bounded local writes; file creation and DB commit form one uncancelled section.
        folder.mkdir()
        created = False
        try:
            (folder / "source").write_bytes(content)
            row, created = store.enqueue(
                job_id, owner, idem, fingerprint, task_id, snapshot, settings.max_pending
            )
            return JSONResponse(public_job(row), status_code=202 if created else 200)
        except JobError as exc:
            raise HTTPException(
                409 if exc.code == "idempotency_conflict" else 429, exc.code
            ) from exc
        finally:
            if not created:
                shutil.rmtree(folder, ignore_errors=True)

    @app.get(
        "/api/v1/documents/jobs/{job_id}",
        response_model=JobResponse,
        responses={410: {"model": JobResponse}},
    )
    async def get_job(job_id: uuid.UUID, request: Request):
        row = await asyncio.to_thread(store.get, str(job_id), request.state.owner)
        if not row:
            raise HTTPException(404, "job_not_found")
        if row["expired"]:
            return JSONResponse(public_job(row), status_code=410)
        return public_job(row)

    def openapi():
        if app.openapi_schema is None:
            schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
            schema.setdefault("components", {})["securitySchemes"] = {
                "ClientKey": {"type": "http", "scheme": "bearer"}
            }
            for path, operations in schema["paths"].items():
                if path.startswith("/api/"):
                    for operation in operations.values():
                        operation["security"] = [{"ClientKey": []}]
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi
    return app
