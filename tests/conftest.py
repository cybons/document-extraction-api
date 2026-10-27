import io
import json
import textwrap
from dataclasses import replace
from pathlib import Path

import httpx2
import pytest
from PIL import Image

from document_extraction_api.api import create_app
from document_extraction_api.claude import ClaudeResponse
from document_extraction_api.processes import load_processes
from document_extraction_api.settings import Price, Settings, Site, hash_client_key

KEY_A = "client-key-site-a"
KEY_B = "client-key-site-b"

TITLE_DEFINITION = '''
"""テスト専用：金額も明細も持たない処理。"""
from document_extraction_api.fields import Record, Text

TASK_ID = "title.extract"


class Output(Record):
    title: Text
'''

TYPED_DEFINITION = '''
"""テスト専用：数量・日付・入れ子の明細を持つ処理。"""
from document_extraction_api.fields import Date, Issue, Number, Record, Text

TASK_ID = "typed.extract"


class Line(Record):
    name: Text
    quantity: Number


class Output(Record):
    issued: Date
    total: Number
    lines: list[Line]


def check(data):
    return [Issue("total", "合計がない")] if data.total is None else []
'''


@pytest.fixture
def anyio_backend():
    return "asyncio"


def write_process(root: Path, site: str, name: str, definition: str, prompt: str = "全部読む"):
    directory = root / site / name
    directory.mkdir(parents=True)
    (directory / "definition.py").write_text(textwrap.dedent(definition), encoding="utf-8")
    (directory / "prompt.md").write_text(prompt, encoding="utf-8")


@pytest.fixture
def processes_dir(tmp_path) -> Path:
    root = tmp_path / "processes"
    write_process(root, "site_a", "title", TITLE_DEFINITION)
    write_process(root, "site_a", "typed", TYPED_DEFINITION)
    write_process(root, "site_b", "typed", TYPED_DEFINITION)
    return root


@pytest.fixture
def settings(tmp_path, processes_dir) -> Settings:
    return Settings(
        sites={
            "site_a": Site("site_a", "claude-key-a", frozenset({hash_client_key(KEY_A)})),
            "site_b": Site("site_b", "claude-key-b", frozenset({hash_client_key(KEY_B)})),
        },
        data_dir=tmp_path / "data",
        processes_dir=processes_dir,
        model="claude-test",
        max_upload_bytes=200_000,
        render_timeout_seconds=30,
        prices={"claude-test": Price(4.0, 20.0)},
    )


def claude_message(output, *, stop_reason="end_turn", model="claude-test", text=None) -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [
            {
                "type": "text",
                "text": text if text is not None else json.dumps(output, ensure_ascii=False),
            }
        ],
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 1000, "output_tokens": 200},
    }


class FakeExtractor:
    """Claude の代わり。responses を順に返す（例外なら投げる、コルーチン関数なら待つ）。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def extract(self, site, model, prompt, schema, pages):
        self.calls.append(
            {"site": site.name, "model": model, "prompt": prompt, "schema": schema, "pages": pages}
        )
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            response = await response()
        return ClaudeResponse(raw=response)


def png_bytes(color=(0, 0, 0), mode="RGB", size=(40, 20)) -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def pdf_bytes(pages: int = 1, rotation: int = 0) -> bytes:
    import pypdfium2

    pdf = pypdfium2.PdfDocument.new()
    for _ in range(pages):
        page = pdf.new_page(200, 100)
        if rotation:
            page.set_rotation(rotation)
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def make_app(settings, extractor, **overrides):
    if overrides:
        settings = replace(settings, **overrides)
    registry = load_processes(settings.processes_dir, list(settings.sites))
    app = create_app(settings, registry, extractor, start_worker=False)
    return app


def fastapi_app(app):
    """AuthMiddleware の内側の FastAPI アプリ。"""
    return app.app


def client(app) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test")


async def submit(http, key=KEY_A, task_id="typed.extract", content=None, filename="doc.png"):
    content = content if content is not None else png_bytes()
    return await http.post(
        "/api/v1/documents/extract",
        headers={"X-API-Key": key},
        files={"file": (filename, content)},
        data={"task_id": task_id},
    )


async def run_one(app) -> None:
    """待ちのジョブを1件処理する。"""
    worker = fastapi_app(app).state.worker
    store = fastapi_app(app).state.store
    job = store.claim_next()
    assert job is not None
    await worker.process(job)
