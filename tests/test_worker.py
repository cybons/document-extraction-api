import asyncio
import json
import time
import uuid

import pytest
from conftest import FakeProvider, invoice

from document_extraction_api.errors import JobError
from document_extraction_api.store import Store
from document_extraction_api.tasks import TASKS
from document_extraction_api.worker import Worker, preprocess


def setup_store(settings):
    store = Store(settings.data_dir)
    (store.root / "files").mkdir(exist_ok=True)
    store.initialize()
    return store


def enqueue(store, key="one", source=b"source"):
    job_id = str(uuid.uuid4())
    folder = store.root / "files" / job_id
    folder.mkdir()
    (folder / "source").write_bytes(source)
    task = TASKS["invoice.extract"]
    row, _ = store.enqueue(
        job_id,
        "pad",
        key,
        key,
        "invoice.extract",
        {
            "rotation": 0,
            "version": task.version,
            "model": "test-model",
            "prompt": task.prompt,
            "schema": task.schema.model_json_schema(),
            "price": {"input": None, "output": None},
        },
        100,
    )
    return row


async def no_render(settings, job, folder):
    return []


@pytest.mark.parametrize("failure", [RuntimeError("secret-document-text"), JobError("test_error")])
async def test_exception_isolated(settings, failure, caplog):
    store = setup_store(settings)
    one, two = enqueue(store), enqueue(store, "two")

    class Provider(FakeProvider):
        async def extract(self, job, images):
            self.calls += 1
            if self.calls == 1:
                raise failure
            return invoice()

    worker = Worker(settings, store, Provider(settings, store), no_render)
    await worker.process(store.claim())
    await worker.process(store.claim())
    assert store.get(one["id"], "pad")["status"] == "failed"
    assert store.get(two["id"], "pad")["status"] == "succeeded"
    assert "secret-document-text" not in caplog.text


async def test_timeout_then_next_job(settings):
    store = setup_store(settings)
    one, two = enqueue(store), enqueue(store, "two")
    settings.job_seconds = 0.05

    class Provider(FakeProvider):
        async def extract(self, job, images):
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(1)
            return invoice()

    worker = Worker(settings, store, Provider(settings, store), no_render)
    await worker.process(store.claim())
    await worker.process(store.claim())
    assert store.get(one["id"], "pad")["error"] == "job_timeout"
    assert store.get(two["id"], "pad")["status"] == "succeeded"


async def test_result_persistence_retry_does_not_call_provider_again(settings, monkeypatch):
    store = setup_store(settings)
    row = enqueue(store)
    provider = FakeProvider(settings, store)
    real_finish = store.finish
    calls = 0

    def finish(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("temporary disk error")
        real_finish(*args)

    monkeypatch.setattr(store, "finish", finish)
    worker = Worker(settings, store, provider, no_render)
    await worker.process(store.claim())
    assert provider.calls == 1
    assert calls == 2
    assert store.get(row["id"], "pad")["status"] == "succeeded"


def test_restart_marks_running_failed_preserves_queue(settings):
    store = setup_store(settings)
    one, two = enqueue(store), enqueue(store, "two")
    store.claim()
    store.initialize()
    assert store.get(one["id"], "pad")["error"] == "interrupted"
    assert store.claim()["id"] == two["id"]


def test_queue_limit(settings):
    store = setup_store(settings)
    enqueue(store)
    with pytest.raises(JobError, match="queue_full"):
        store.enqueue("two", "pad", "two", "two", "invoice.extract", {}, 1)


def test_retention_and_orphan_cleanup(settings):
    store = setup_store(settings)
    row = enqueue(store)
    store.claim()
    store.finish(row["id"], "succeeded", {"secret": "text"})
    with store.db() as db:
        db.execute("UPDATE jobs SET updated=?", (time.time() - 91 * 86400,))
    store.cleanup(7, 90)
    assert store.get(row["id"], "pad") is None


async def test_real_render_rotation(settings, png):
    from PIL import Image

    store = setup_store(settings)
    row = enqueue(store, source=png)
    snapshot = json.loads(row["snapshot"])
    snapshot["rotation"] = 90
    row["snapshot"] = json.dumps(snapshot)
    images = await preprocess(settings, row, store.root / "files" / row["id"])
    with Image.open(images[0]) as img:
        assert img.size == (50, 100)


async def test_subprocess_timeout_kills_child(settings, monkeypatch):
    store = setup_store(settings)
    row = enqueue(store)
    settings.preprocess_seconds = 0.01

    class Process:
        returncode = None
        killed = False

        async def wait(self):
            if not self.killed:
                await asyncio.sleep(10)
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9

    process = Process()

    async def start(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    with pytest.raises(TimeoutError):
        await preprocess(settings, row, store.root / "files" / row["id"])
    assert process.killed


async def test_pdf_render_and_page_limit(settings, png):
    import io

    from PIL import Image

    with Image.open(io.BytesIO(png)) as image:
        output = io.BytesIO()
        image.save(output, "PDF", save_all=True, append_images=[image.copy()])
    store = setup_store(settings)
    row = enqueue(store, source=output.getvalue())
    folder = store.root / "files" / row["id"]
    images = await preprocess(settings, row, folder)
    assert len(images) == 2
    settings.max_pages = 1
    with pytest.raises(JobError, match="invalid_document"):
        await preprocess(settings, row, folder)


async def test_run_loop_remains_serial_and_continues(settings):
    store = setup_store(settings)
    rows = [enqueue(store, str(i)) for i in range(4)]

    class Provider(FakeProvider):
        active = 0
        peak = 0

        async def extract(self, job, images):
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls += 1
            try:
                await asyncio.sleep(0.02)
                if self.calls == 2:
                    raise RuntimeError("one job only")
                return invoice()
            finally:
                self.active -= 1

    provider = Provider(settings, store)
    worker = Worker(settings, store, provider, no_render)
    loop = asyncio.create_task(worker.run())
    try:
        async with asyncio.timeout(5):
            # Poll the persisted state, which is the contract under test.
            while store.get(rows[-1]["id"], "pad")["status"] in {"queued", "running"}:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        assert provider.peak == 1
        assert [store.get(r["id"], "pad")["status"] for r in rows] == [
            "succeeded",
            "failed",
            "succeeded",
            "succeeded",
        ]
    finally:
        loop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop
