"""docs/failure-paths.md の各項目を1つずつ確かめる。テスト名の番号は一覧の順番。"""

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from conftest import (
    KEY_A,
    FakeExtractor,
    claude_message,
    client,
    fastapi_app,
    make_app,
    png_bytes,
    run_one,
    submit,
)
from PIL import Image

from document_extraction_api import db, render_worker
from document_extraction_api import worker as worker_module
from document_extraction_api.__main__ import InstanceLock, serve
from document_extraction_api.claude import ClaudeUnavailable
from document_extraction_api.render import render_pages, run_with_timeout
from document_extraction_api.worker import RESPONSE_FILE, job_dir

GOOD = {"issued": "2026-10-27", "total": "100", "lines": [{"name": "A", "quantity": "1"}]}

pytestmark = pytest.mark.anyio


# 1. 帳票1件の例外やタイムアウトの後も、後続のジョブが処理される。
async def test_01_next_jobs_run_after_exception_and_timeout(settings):
    async def never_answers():
        await asyncio.sleep(3600)

    extractor = FakeExtractor(
        RuntimeError("boom"),
        never_answers,
        ClaudeUnavailable("claude_timeout"),
        claude_message(GOOD),
    )
    app = make_app(settings, extractor, job_timeout_seconds=2)
    worker = fastapi_app(app).state.worker
    async with client(app) as http:
        ids = []
        for color in range(4):
            response = await submit(http, content=png_bytes((color, 0, 0)))
            ids.append(response.json()["job_id"])

        task = asyncio.create_task(worker.run())
        try:
            async with asyncio.timeout(30):
                while True:
                    statuses = [
                        (await http.get(f"/api/v1/jobs/{i}", headers={"X-API-Key": KEY_A})).json()
                        for i in ids
                    ]
                    if all(s["status"] in ("succeeded", "failed") for s in statuses):
                        break
                    await asyncio.sleep(0.1)
        finally:
            task.cancel()
    assert [s["error"]["code"] if s["error"] else s["status"] for s in statuses] == [
        "claude_error",
        "job_timeout",
        "claude_timeout",
        "succeeded",
    ]


async def test_01b_repeated_error_before_sending_ends_as_unreadable_file(settings, monkeypatch):
    extractor = FakeExtractor()
    app = make_app(settings, extractor, max_attempts=2)
    store = fastapi_app(app).state.store
    async with client(app) as http:
        job_id = (await submit(http)).json()["job_id"]

    def broken_render(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(worker_module, "render_pages", broken_render)
    await run_one(app)
    assert store.get(job_id).status == db.QUEUED  # 1回目はやり直す
    await run_one(app)
    job = store.get(job_id)
    assert job.status == db.FAILED
    assert job.error == {"code": "unreadable_file", "attempts": 2}
    assert extractor.calls == []


async def test_01c_job_that_cannot_be_finalized_does_not_stop_others(settings, monkeypatch):
    app = make_app(settings, FakeExtractor(claude_message(GOOD), claude_message(GOOD)))
    store = fastapi_app(app).state.store
    worker = fastapi_app(app).state.worker
    async with client(app) as http:
        first = (await submit(http, content=png_bytes((1, 0, 0)))).json()["job_id"]
        second = (await submit(http, content=png_bytes((2, 0, 0)))).json()["job_id"]

    original = worker._finalize

    def finalize(job, raw, process=None):
        if job.id == first:
            raise OSError("disk I/O error")
        return original(job, raw, process)

    monkeypatch.setattr(worker, "_finalize", finalize)
    task = asyncio.create_task(worker.run())
    try:
        async with asyncio.timeout(15):
            while store.get(second).status != db.SUCCEEDED:
                await asyncio.sleep(0.1)
    finally:
        task.cancel()
    assert store.get(first).status == db.PROCESSING  # 応答は保存済み。次の立て直しで確定する

    monkeypatch.setattr(worker, "_finalize", original)
    worker.recover()
    assert store.get(first).status == db.SUCCEEDED


# 2. 再起動・停止・DB障害で、ジョブが消えない、重複しない、止まったままにならない。
#    まだClaudeへ送っていないジョブを、再起動で失敗扱いにしない。
async def test_02a_restart_requeues_job_not_yet_sent(settings):
    app = make_app(settings, FakeExtractor())
    store = fastapi_app(app).state.store
    async with client(app) as http:
        job_id = (await submit(http)).json()["job_id"]
    store.claim_next()  # 処理中のまま停止した状態

    restarted = make_app(settings, FakeExtractor(claude_message(GOOD)))
    fastapi_app(restarted).state.worker.recover()
    assert store.get(job_id).status == db.QUEUED
    await run_one(restarted)
    assert store.get(job_id).status == db.SUCCEEDED


async def test_02b_restart_resends_sent_job_until_attempt_limit(settings):
    app = make_app(settings, FakeExtractor(), max_attempts=2)
    store = fastapi_app(app).state.store
    worker = fastapi_app(app).state.worker
    async with client(app) as http:
        job_id = (await submit(http)).json()["job_id"]

    store.claim_next()
    store.mark_sent(job_id)  # Claude へ送った後、応答を受け取る前に停止
    worker.recover()
    assert store.get(job_id).status == db.QUEUED

    store.claim_next()
    store.mark_sent(job_id)  # 2回目も中断。上限に達したので失敗にする
    worker.recover()
    job = store.get(job_id)
    assert job.status == db.FAILED
    assert job.error["code"] == "claude_no_response"


async def test_02c_same_file_is_not_registered_twice(settings):
    app = make_app(settings, FakeExtractor(claude_message(GOOD)))
    store = fastapi_app(app).state.store
    async with client(app) as http:
        first = (await submit(http)).json()
        second = (await submit(http, filename="other-name.png")).json()
    assert first["job_id"] == second["job_id"]
    assert store.claim_next().id == first["job_id"]
    assert store.claim_next() is None


async def test_02d_worker_survives_database_errors(settings, monkeypatch):
    app = make_app(settings, FakeExtractor(claude_message(GOOD)))
    worker = fastapi_app(app).state.worker
    store = fastapi_app(app).state.store
    async with client(app) as http:
        job_id = (await submit(http)).json()["job_id"]

    original = store.claim_next
    calls = {"n": 0}

    def flaky_claim():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("database is locked")
        return original()

    monkeypatch.setattr(store, "claim_next", flaky_claim)
    task = asyncio.create_task(worker.run())
    try:
        async with asyncio.timeout(15):
            while store.get(job_id).status != db.SUCCEEDED:
                await asyncio.sleep(0.1)
    finally:
        task.cancel()


async def test_02e_job_keeps_the_process_it_was_accepted_with(settings, processes_dir):
    app = make_app(settings, FakeExtractor())
    store = fastapi_app(app).state.store
    async with client(app) as http:
        job_id = (await submit(http)).json()["job_id"]
    version = store.get(job_id).process_version

    # 受け付けた後に処理を消して再起動しても、受け付けたときの版で読む
    shutil.rmtree(processes_dir / "site_a" / "typed")
    extractor = FakeExtractor(claude_message(GOOD))
    restarted = make_app(settings, extractor)
    await run_one(restarted)
    job = store.get(job_id)
    assert job.status == db.SUCCEEDED
    assert job.process_version == version
    assert extractor.calls[0]["prompt"] == "全部読む"


# 3. 認証のない要求に、アップロードの受付枠やメモリを使わせない（healthなどのパスも含む）。
@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/api/v1/documents/extract"), ("GET", "/api/v1/health"), ("GET", "/api/v1/jobs/x")],
)
@pytest.mark.parametrize("key", [None, "wrong-key"])
async def test_03_unauthenticated_requests_never_read_the_body(settings, method, path, key):
    app = make_app(settings, FakeExtractor())
    received = []
    sent = []

    async def receive():
        received.append(True)
        return {"type": "http.request", "body": b"x" * 1000, "more_body": False}

    async def send(message):
        sent.append(message)

    headers = [(b"content-length", b"1000")]
    if key:
        headers.append((b"x-api-key", key.encode()))
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers,
        "query_string": b"",
        "root_path": "",
    }
    await app(scope, receive, send)
    assert received == []
    assert sent[0]["status"] == 401
    assert app.slots._value == settings.upload_slots


async def test_03b_oversized_upload_is_rejected_before_reading(settings):
    app = make_app(settings, FakeExtractor())
    received = []

    async def receive():
        received.append(True)
        return {"type": "http.request", "body": b"", "more_body": False}

    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/documents/extract",
        "headers": [(b"x-api-key", KEY_A.encode()), (b"content-length", b"999999999")],
        "query_string": b"",
        "root_path": "",
    }
    await app(scope, receive, send)
    assert received == []
    assert sent[0]["status"] == 413


async def test_03c_streamed_body_over_limit_is_cut_off(settings):
    app = make_app(settings, FakeExtractor())
    async with client(app) as http:

        async def body():
            yield b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.png"\r\n\r\n'
            for _ in range(100):
                yield b"y" * 10_000

        response = await http.post(
            "/api/v1/documents/extract",
            headers={"X-API-Key": KEY_A, "content-type": "multipart/form-data; boundary=b"},
            content=body(),
        )
    assert response.status_code == 413


# 4. Claudeの応答を受け取った後にDBへの記録が失敗しても、課金済みの結果を捨てない。
async def test_04_response_is_kept_when_recording_fails(settings, monkeypatch):
    extractor = FakeExtractor(claude_message(GOOD))
    app = make_app(settings, extractor)
    store = fastapi_app(app).state.store
    worker = fastapi_app(app).state.worker
    async with client(app) as http:
        job_id = (await submit(http)).json()["job_id"]

    original = store.finish

    def broken_finish(*args, **kwargs):
        raise OSError("disk I/O error")

    monkeypatch.setattr(store, "finish", broken_finish)
    await run_one(app)
    assert (job_dir(settings, job_id) / RESPONSE_FILE).exists()
    assert store.get(job_id).status == db.PROCESSING

    monkeypatch.setattr(store, "finish", original)
    worker.recover()
    job = store.get(job_id)
    assert job.status == db.SUCCEEDED
    assert job.data["total"] == "100"
    assert len(extractor.calls) == 1  # Claude を呼び直していない


# 5. 受付処理（ファイル保存とDB登録）でイベントループを止めない。
#    要求がキャンセルされても、登録済みジョブのファイルを消さない。
async def test_05_acceptance_does_not_block_and_survives_cancellation(settings, monkeypatch):
    app = make_app(settings, FakeExtractor())
    store = fastapi_app(app).state.store
    entered = threading.Event()
    release = threading.Event()
    original = store.create_or_find

    def slow_create_or_find(*args, **kwargs):
        entered.set()
        release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "create_or_find", slow_create_or_find)
    async with client(app) as http:
        upload = asyncio.create_task(submit(http))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        health = await asyncio.wait_for(
            http.get("/api/v1/health", headers={"X-API-Key": KEY_A}), timeout=2
        )
        assert health.status_code == 200  # 受付中でも他の要求に答える

        upload.cancel()
        await asyncio.sleep(0.05)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await upload
        for _ in range(100):
            job = store.claim_next()
            if job:
                break
            await asyncio.sleep(0.05)
    assert job is not None
    assert (job_dir(settings, job.id) / "input").exists()


# 6. 変換の子プロセスは、時間を超えたら確実に終了させる（孫プロセスも含む）。
#    子プロセス自身にも制限時間を持たせる。
@pytest.mark.skipif(os.name == "nt", reason="POSIX のプロセスグループで確かめる")
def test_06a_timeout_kills_child_and_grandchild(tmp_path):
    pid_file = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys, time;"
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
        f"open({str(pid_file)!r}, 'w').write(str(p.pid));"
        "time.sleep(60)"
    )
    started = time.monotonic()
    assert run_with_timeout([sys.executable, "-c", script], timeout=2) is None
    assert time.monotonic() - started < 10
    grandchild = int(pid_file.read_text())
    for _ in range(50):
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(grandchild, signal.SIGKILL)
        pytest.fail("孫プロセスが残った")


@pytest.mark.skipif(os.name == "nt", reason="名前付きパイプで変換を止める")
def test_06b_render_worker_stops_itself(tmp_path):
    fifo = tmp_path / "stuck"
    os.mkfifo(fifo)  # 書き手がいないので、開いた時点で止まる
    started = time.monotonic()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "document_extraction_api.render_worker",
            str(fifo),
            str(tmp_path / "out"),
            "10",
            "100",
            "1000",
            "1",
        ],
        timeout=20,
    )
    assert result.returncode == render_worker.EXIT_TIMEOUT
    assert time.monotonic() - started < 10


# 7. 起動の失敗（ポート使用中、多重起動）をログに必ず残す。
#    APIキー・ファイル名・帳票本文はログに出さない。
def _settings_file(tmp_path: Path, port: int) -> Path:
    path = tmp_path / "settings.toml"
    path.write_text(
        f"""
data_dir = "data"
processes_dir = "processes"
log_file = "logs/service.log"
port = {port}
[sites.site_a]
claude_api_key = "sk-test"
client_key_sha256 = ["{"0" * 64}"]
""",
        encoding="utf-8",
    )
    return path


def test_07a_port_in_use_is_logged(tmp_path, processes_dir):
    import socket

    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        assert serve(_settings_file(tmp_path, port)) == 1
    finally:
        blocker.close()
        logging.getLogger().handlers.clear()
    assert "ポート" in (tmp_path / "logs" / "service.log").read_text(encoding="utf-8")


def test_07b_second_instance_is_logged(tmp_path, processes_dir):
    lock = InstanceLock(tmp_path / "data" / "service.lock")
    lock.acquire()
    try:
        assert serve(_settings_file(tmp_path, 0)) == 1
    finally:
        lock.release()
        logging.getLogger().handlers.clear()
    assert "すでに起動" in (tmp_path / "logs" / "service.log").read_text(encoding="utf-8")


async def test_07c_logs_do_not_contain_secrets(settings, caplog):
    caplog.set_level(logging.DEBUG)
    secret_text = "SECRET-DOCUMENT-TEXT"
    output = {"issued": "bad-date", "total": secret_text, "lines": []}
    extractor = FakeExtractor(claude_message(output), RuntimeError(secret_text))
    app = make_app(settings, extractor)
    async with client(app) as http:
        await submit(http, filename="秘密の納品書.png")
        await submit(http, filename="秘密の納品書2.png", content=png_bytes((9, 9, 9)))
        await submit(http, key="not-a-key")
        await run_one(app)
        await run_one(app)
    for secret in (KEY_A, "not-a-key", "claude-key-a", "秘密の納品書", secret_text, "bad-date"):
        assert secret not in caplog.text


# 8. 透過画像は、白い背景に合成してから変換する。
def test_08_transparent_image_is_composited_on_white(tmp_path, settings):
    source = tmp_path / "input"
    source.write_bytes(png_bytes((0, 0, 0, 0), mode="RGBA"))
    pages = render_pages(source, tmp_path / "pages", settings)
    with Image.open(pages[0]) as page:
        assert page.mode == "RGB"
        assert page.getpixel((0, 0)) == (255, 255, 255)


# 9. 金額も明細も持たないテスト専用タスクが、基盤のコードを変えずにHTTP経由で受付から取得まで通る。
async def test_09_task_without_amounts_runs_end_to_end(settings):
    extractor = FakeExtractor(claude_message({"title": "テスト文書"}))
    app = make_app(settings, extractor)
    async with client(app) as http:
        accepted = await submit(http, task_id="title.extract")
        assert accepted.status_code == 202
        job_id = accepted.json()["job_id"]
        await run_one(app)
        result = (await http.get(f"/api/v1/jobs/{job_id}", headers={"X-API-Key": KEY_A})).json()
    assert result["status"] == "succeeded"
    assert result["data"] == {"title": "テスト文書"}
    assert result["versions"]["process"].startswith("site_a/title@")
    assert extractor.calls[0]["schema"]["properties"] == {
        "title": {"anyOf": [{"type": "string"}, {"type": "null"}]}
    }


def test_07d_missing_claude_key_stops_startup(tmp_path, processes_dir):
    path = _settings_file(tmp_path, 0)
    path.write_text(path.read_text(encoding="utf-8").replace('claude_api_key = "sk-test"\n', ""))
    try:
        assert serve(path) == 1
    finally:
        logging.getLogger().handlers.clear()
    assert "claude_api_key" in (tmp_path / "logs" / "service.log").read_text(encoding="utf-8")


async def test_04b_slow_recording_after_response_is_not_cut_off(settings, monkeypatch):
    extractor = FakeExtractor(claude_message(GOOD))
    app = make_app(settings, extractor, job_timeout_seconds=1)
    store = fastapi_app(app).state.store
    async with client(app) as http:
        job_id = (await submit(http)).json()["job_id"]

    original = store.finish

    def slow_finish(*args, **kwargs):
        time.sleep(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "finish", slow_finish)
    await run_one(app)
    job = store.get(job_id)
    assert job.status == db.SUCCEEDED
    assert job.data["total"] == "100"
    assert len(extractor.calls) == 1
