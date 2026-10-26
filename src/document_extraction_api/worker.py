import asyncio
import json
import logging
import shutil
import sys
import time

from .errors import JobError
from .tasks import review

log = logging.getLogger("document_extraction")


async def preprocess(settings, job, folder):
    output = folder / "pages"
    output.mkdir(exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "document_extraction_api.render",
        str(folder / "source"),
        str(output),
        str(settings.max_pages),
        str(json.loads(job["snapshot"])["rotation"]),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(settings.preprocess_seconds):
            code = await proc.wait()
        if code != 0:
            raise JobError("invalid_document")
        images = sorted(output.glob("*.jpg"))
        if not images:
            raise JobError("invalid_document")
        return images
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()


class Worker:
    def __init__(self, settings, store, provider, renderer=preprocess):
        self.settings = settings
        self.store = store
        self.provider = provider
        self.renderer = renderer
        self.healthy = True
        self.maintenance_healthy = True
        self.last_maintenance = 0.0

    async def process(self, job):
        status, result, reasons, error = "failed", None, [], None
        try:
            async with asyncio.timeout(self.settings.job_seconds):
                images = await self.renderer(
                    self.settings, job, self.store.root / "files" / job["id"]
                )
                doc = await self.provider.extract(job, images)
                reasons = review(doc, job["task"])
                result = doc.model_dump(mode="json")
                status = "needs_review" if reasons else "succeeded"
        except TimeoutError:
            error = "job_timeout"
        except JobError as exc:
            error = exc.code
        except asyncio.CancelledError:
            raise
        except Exception:
            # No traceback: external libraries may embed document content or credentials.
            error = "internal_error"
        # Persistence failures must not resubmit the document to the provider.
        while True:
            try:
                await asyncio.to_thread(
                    self.store.finish, job["id"], status, result, reasons, error
                )
                self.healthy = True
                break
            except Exception:
                self.healthy = False
                log.error("event=result_persist_failed job_id=%s", job["id"])
                await asyncio.sleep(2)
        log.info("event=job_finished job_id=%s status=%s code=%s", job["id"], status, error)
        try:
            await asyncio.to_thread(shutil.rmtree, self.store.root / "files" / job["id"])
        except FileNotFoundError:
            pass
        except OSError:
            log.error("event=file_cleanup_failed job_id=%s", job["id"])
            self.maintenance_healthy = False

    def maintenance(self):
        self.store.cleanup(self.settings.result_days, self.settings.history_days)
        active = self.store.active_ids()
        for folder in (self.store.root / "files").iterdir():
            # Only the worker and request handler create UUID directories here.
            # A grace period protects uploads not yet committed to SQLite.
            if (
                folder.is_dir()
                and folder.name not in active
                and time.time() - folder.stat().st_mtime > 120
            ):
                shutil.rmtree(folder)

    async def run(self):
        while True:
            try:
                if time.monotonic() - self.last_maintenance > 60:
                    try:
                        await asyncio.to_thread(self.maintenance)
                        self.maintenance_healthy = True
                    except Exception:
                        self.maintenance_healthy = False
                        log.error("event=maintenance_failed")
                    self.last_maintenance = time.monotonic()
                job = await asyncio.to_thread(self.store.claim)
                self.healthy = True
                if job is None:
                    await asyncio.sleep(0.25)
                    continue
                await self.process(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.healthy = False
                log.error("event=worker_iteration_failed")
                await asyncio.sleep(2)
