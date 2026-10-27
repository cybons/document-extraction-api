import shutil
from pathlib import Path

import pytest
from conftest import (
    KEY_A,
    KEY_B,
    FakeExtractor,
    claude_message,
    client,
    fastapi_app,
    make_app,
    pdf_bytes,
    png_bytes,
    run_one,
    submit,
    write_process,
)

from document_extraction_api import db
from document_extraction_api import worker as worker_module
from document_extraction_api.claude import ClaudeUnavailable
from document_extraction_api.processes import load_processes
from document_extraction_api.worker import PROCESS_DIR, RESPONSE_FILE, job_dir

pytestmark = pytest.mark.anyio


async def _run(settings, output=None, *, task_id="typed.extract", message=None):
    extractor = FakeExtractor(message if message is not None else claude_message(output))
    app = make_app(settings, extractor)
    async with client(app) as http:
        job_id = (await submit(http, task_id=task_id)).json()["job_id"]
        await run_one(app)
        return (await http.get(f"/api/v1/jobs/{job_id}", headers={"X-API-Key": KEY_A})).json()


async def test_values_are_parsed(settings):
    result = await _run(
        settings,
        {
            "issued": "令和8年10月27日",
            "total": "１,２００",
            "lines": [{"name": "部品", "quantity": "10"}],
        },
    )
    assert result["status"] == "succeeded"
    assert result["data"] == {
        "issued": {"value": "2026-10-27", "raw": "令和8年10月27日"},
        "total": "1200",
        "lines": [{"name": "部品", "quantity": "10"}],
    }
    assert result["issues"] == []
    assert result["versions"]["model"] == "claude-test"
    assert result["usage"] == {"input_tokens": 1000, "output_tokens": 200, "cost_usd": 0.008}


async def test_invalid_values_become_null_and_need_review(settings):
    result = await _run(
        settings,
        {
            "issued": "2026/13/40",
            "total": "100",
            "lines": [
                {"name": "A", "quantity": "5"},
                {"name": "B", "quantity": "1O"},
            ],
        },
    )
    assert result["status"] == "needs_review"
    assert result["data"]["issued"] == {"value": None, "raw": "2026/13/40"}
    assert result["data"]["lines"][1] == {"name": "B", "quantity": None}
    assert result["data"]["lines"][0]["quantity"] == "5"
    assert result["issues"] == [
        {"path": "lines[1].quantity", "raw": "1O", "message": "数値として読めない"},
        {"path": "issued", "raw": "2026/13/40", "message": "日付として読めない"},
    ]


async def test_unreadable_date_in_nested_record_keeps_raw(settings, processes_dir):
    write_process(
        processes_dir,
        "site_a",
        "dates",
        """
        from document_extraction_api.fields import Date, Record

        TASK_ID = "dates.extract"


        class Line(Record):
            shipped: Date


        class Output(Record):
            lines: list[Line]
        """,
    )
    output = {"lines": [{"shipped": "2026-10-27"}, {"shipped": "来週"}]}
    result = await _run(settings, output, task_id="dates.extract")
    assert result["status"] == "needs_review"
    assert result["data"]["lines"] == [
        {"shipped": {"value": "2026-10-27", "raw": "2026-10-27"}},
        {"shipped": {"value": None, "raw": "来週"}},
    ]
    assert result["issues"] == [
        {"path": "lines[1].shipped", "raw": "来週", "message": "日付として読めない"}
    ]


async def test_check_of_the_process_marks_review(settings):
    result = await _run(settings, {"issued": None, "total": None, "lines": []})
    assert result["status"] == "needs_review"
    assert result["issues"] == [{"path": "total", "raw": None, "message": "合計がない"}]


async def test_check_that_raises_marks_review_instead_of_failing(settings, processes_dir):
    write_process(
        processes_dir,
        "site_a",
        "broken",
        """
        from document_extraction_api.fields import Record, Text

        TASK_ID = "broken.extract"


        class Output(Record):
            title: Text


        def check(data):
            return 1 / 0
        """,
    )
    result = await _run(settings, {"title": "A"}, task_id="broken.extract")
    assert result["status"] == "needs_review"
    assert result["data"] == {"title": "A"}
    assert result["issues"] == [
        {"path": "", "raw": None, "message": "check が例外で止まった: ZeroDivisionError"}
    ]


@pytest.mark.parametrize(
    "check",
    [
        "def check(data):\n    return None\n",
        "def check(data):\n    yield Issue('title', 'x')\n    raise RuntimeError('bug')\n",
        "def check(data):\n    return [object()]\n",
        "def check(data):\n    return [Issue('title', ValueError('x'))]\n",
        "def check(data):\n    return [Issue(1, 'x')]\n",
    ],
)
async def test_any_error_in_check_marks_review_and_finishes_the_job(settings, processes_dir, check):
    definition = (
        "from document_extraction_api.fields import Issue, Record, Text\n"
        "TASK_ID = 'broken.extract'\n"
        "class Output(Record):\n"
        "    title: Text\n"
    ) + check
    write_process(processes_dir, "site_a", "broken", definition)
    extractor = FakeExtractor(claude_message({"title": "A"}))
    app = make_app(settings, extractor)
    store = fastapi_app(app).state.store
    async with client(app) as http:
        job_id = (await submit(http, task_id="broken.extract")).json()["job_id"]
    await run_one(app)
    fastapi_app(app).state.worker.recover()
    job = store.get(job_id)
    assert job.status == db.NEEDS_REVIEW
    assert job.data == {"title": "A"}
    assert job.issues[-1]["message"].startswith("check ")
    assert len(extractor.calls) == 1
    assert store.processing() == []


async def test_unexpected_error_in_validation_marks_review(settings, monkeypatch):
    def broken_validate(process, output):
        raise RuntimeError("bug")

    monkeypatch.setattr(worker_module, "validate_output", broken_validate)
    result = await _run(settings, {"title": "A"}, task_id="title.extract")
    assert result["status"] == "needs_review"
    assert result["data"] == {"title": "A"}
    assert result["issues"] == [
        {"path": "", "raw": None, "message": "検証が例外で止まった: RuntimeError"}
    ]


async def test_saved_response_is_kept_when_the_process_cannot_be_loaded(settings, monkeypatch):
    app = make_app(settings, FakeExtractor(claude_message({"title": "A"})))
    store = fastapi_app(app).state.store
    worker = fastapi_app(app).state.worker
    async with client(app) as http:
        job_id = (await submit(http, task_id="title.extract")).json()["job_id"]

    def broken_finish(*args, **kwargs):
        raise OSError("disk I/O error")

    monkeypatch.setattr(store, "finish", broken_finish)
    await run_one(app)
    monkeypatch.undo()
    assert (job_dir(settings, job_id) / RESPONSE_FILE).exists()

    shutil.rmtree(job_dir(settings, job_id) / PROCESS_DIR)
    worker.recover()
    job = store.get(job_id)
    assert job.status == db.NEEDS_REVIEW
    assert job.data == {"title": "A"}
    assert job.issues[0]["message"] == "処理の定義を読み込めず、検証していない"


async def test_unreadable_values_returned_as_null_are_kept_null(settings):
    result = await _run(settings, {"issued": None, "total": "1", "lines": []})
    assert result["status"] == "succeeded"
    assert result["data"]["issued"] == {"value": None, "raw": None}


@pytest.mark.parametrize(
    ("message", "code"),
    [
        (claude_message(None, stop_reason="refusal"), "claude_refused"),
        (
            claude_message(None, stop_reason="max_tokens", text='{"issued": "20'),
            "response_truncated",
        ),
        (claude_message(None, text="これはJSONではない"), "invalid_json"),
    ],
)
async def test_unusable_responses_fail_and_keep_the_raw_response(settings, message, code):
    result = await _run(settings, message=message)
    assert result["status"] == "failed"
    assert result["error"]["code"] == code
    assert result["data"] is None
    assert result["usage"]["input_tokens"] == 1000


async def test_json_that_does_not_fit_the_definition_needs_review(settings):
    result = await _run(settings, message=claude_message({"unexpected": 1}))
    assert result["status"] == "needs_review"
    assert result["data"] == {"unexpected": 1}
    assert result["issues"] == [{"path": "", "raw": None, "message": "出力の形が定義と合わない"}]


async def test_claude_unavailable_fails_the_job(settings):
    app = make_app(settings, FakeExtractor(ClaudeUnavailable("claude_http_529")))
    async with client(app) as http:
        job_id = (await submit(http)).json()["job_id"]
        await run_one(app)
        result = (await http.get(f"/api/v1/jobs/{job_id}", headers={"X-API-Key": KEY_A})).json()
    assert result["status"] == "failed"
    assert result["error"] == {"code": "claude_http_529"}
    assert result["usage"] is None


async def test_pdf_pages_are_sent_as_images(settings):
    extractor = FakeExtractor(claude_message({"title": "x"}))
    app = make_app(settings, extractor)
    async with client(app) as http:
        await submit(http, task_id="title.extract", content=pdf_bytes(pages=2), filename="a.pdf")
        await run_one(app)
    assert len(extractor.calls[0]["pages"]) == 2
    assert all(page.startswith(b"\x89PNG") for page in extractor.calls[0]["pages"])
    assert extractor.calls[0]["site"] == "site_a"


async def test_too_many_pages_fails_as_unreadable(settings):
    extractor = FakeExtractor()
    app = make_app(settings, extractor, max_pages=2)
    async with client(app) as http:
        job_id = (await submit(http, content=pdf_bytes(pages=3), filename="a.pdf")).json()["job_id"]
        await run_one(app)
        result = (await http.get(f"/api/v1/jobs/{job_id}", headers={"X-API-Key": KEY_A})).json()
    assert result["error"] == {"code": "too_many_pages"}
    assert extractor.calls == []


async def test_broken_file_fails_as_unreadable(settings):
    app = make_app(settings, FakeExtractor())
    async with client(app) as http:
        job_id = (await submit(http, content=b"%PDF-1.7 broken", filename="a.pdf")).json()["job_id"]
        await run_one(app)
        result = (await http.get(f"/api/v1/jobs/{job_id}", headers={"X-API-Key": KEY_A})).json()
    assert result["error"] == {"code": "unreadable_file"}


async def test_sites_use_their_own_processes_and_cannot_see_each_other(settings):
    app = make_app(settings, FakeExtractor())
    async with client(app) as http:
        own = await submit(http, key=KEY_A)
        assert own.status_code == 202
        hidden = await http.get(
            f"/api/v1/jobs/{own.json()['job_id']}", headers={"X-API-Key": KEY_B}
        )
        assert hidden.status_code == 404
        not_for_b = await submit(http, key=KEY_B, task_id="title.extract")
        assert not_for_b.status_code == 404
        assert not_for_b.json()["error"]["code"] == "task_not_found"


async def test_unsupported_file_and_missing_fields(settings):
    app = make_app(settings, FakeExtractor())
    async with client(app) as http:
        text = await submit(http, content=b"hello", filename="a.txt")
        assert text.status_code == 415
        missing = await http.post(
            "/api/v1/documents/extract", headers={"X-API-Key": KEY_A}, data={"task_id": "x"}
        )
        assert missing.status_code == 400
    assert list((settings.data_dir / "tmp").iterdir()) == []


@pytest.mark.parametrize("second_field", ["file", "other"])
async def test_more_than_one_file_is_rejected(settings, second_field):
    app = make_app(settings, FakeExtractor())
    store = fastapi_app(app).state.store
    async with client(app) as http:
        response = await http.post(
            "/api/v1/documents/extract",
            headers={"X-API-Key": KEY_A},
            files=[
                ("file", ("a.png", png_bytes((1, 0, 0)))),
                (second_field, ("b.png", png_bytes())),
            ],
            data={"task_id": "typed.extract"},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert store.claim_next() is None
    assert not (settings.data_dir / "jobs").exists() or not any(
        (settings.data_dir / "jobs").iterdir()
    )


async def test_file_over_the_upload_limit_is_rejected(settings):
    app = make_app(settings, FakeExtractor(), max_upload_bytes=100)
    store = fastapi_app(app).state.store
    async with client(app) as http:
        response = await submit(http, content=png_bytes(size=(200, 200)))
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"
    assert store.claim_next() is None
    assert list((settings.data_dir / "tmp").iterdir()) == []


async def test_job_response_while_queued(settings):
    app = make_app(settings, FakeExtractor())
    async with client(app) as http:
        job_id = (await submit(http)).json()["job_id"]
        result = (await http.get(f"/api/v1/jobs/{job_id}", headers={"X-API-Key": KEY_A})).json()
    assert result == {
        "job_id": job_id,
        "task_id": "typed.extract",
        "site": "site_a",
        "status": "queued",
        "data": None,
        "issues": [],
        "error": None,
        "versions": {"process": result["versions"]["process"], "model": None},
        "usage": None,
    }


_UNSUPPORTED_KEYWORDS = {
    "format",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
    "title",
    "default",
}


def _walk_schema(node, path="$"):
    if isinstance(node, list):
        for index, item in enumerate(node):
            _walk_schema(item, f"{path}[{index}]")
        return
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        if key == "properties":
            for name, child in value.items():
                _walk_schema(child, f"{path}.{name}")
            continue
        assert key not in _UNSUPPORTED_KEYWORDS, f"{path} に {key}"
        _walk_schema(value, f"{path}/{key}")
    if node.get("type") == "object":
        assert node.get("additionalProperties") is False, path
        assert set(node.get("required", [])) == set(node.get("properties", {})), path


@pytest.mark.parametrize("root", ["shipped", "tests"])
def test_schemas_sent_to_claude_use_supported_keywords(root, processes_dir):
    directory = Path(__file__).parent.parent / "processes" if root == "shipped" else processes_dir
    sites = [p.name for p in directory.iterdir() if p.is_dir()]
    registry = load_processes(directory, sites)
    assert list(registry)
    for process in registry:
        _walk_schema(process.schema)


def test_heavy_pages_are_sent_as_jpeg(tmp_path, settings):
    from PIL import Image

    from document_extraction_api.render import render_pages

    source = tmp_path / "scan.png"
    Image.effect_noise((2000, 2600), 60).convert("RGB").save(source)
    pages = render_pages(source, tmp_path / "pages", settings)
    assert [p.suffix for p in pages] == [".jpg"]


async def test_pages_over_the_request_limit_are_not_sent(settings):
    extractor = FakeExtractor()
    app = make_app(settings, extractor, max_request_image_bytes=100)
    async with client(app) as http:
        job_id = (await submit(http)).json()["job_id"]
        await run_one(app)
        result = (await http.get(f"/api/v1/jobs/{job_id}", headers={"X-API-Key": KEY_A})).json()
    assert result["error"] == {"code": "too_large"}
    assert extractor.calls == []


async def test_request_over_the_fixed_limit_is_not_sent_even_if_the_setting_allows(
    settings, monkeypatch
):
    monkeypatch.setattr(worker_module, "MAX_REQUEST_BYTES", 100)
    extractor = FakeExtractor()
    app = make_app(settings, extractor, max_request_image_bytes=10**12)
    async with client(app) as http:
        job_id = (await submit(http)).json()["job_id"]
        await run_one(app)
        result = (await http.get(f"/api/v1/jobs/{job_id}", headers={"X-API-Key": KEY_A})).json()
    assert result["error"] == {"code": "too_large"}
    assert extractor.calls == []


async def test_prompt_counts_toward_the_request_limit(settings, processes_dir, monkeypatch):
    monkeypatch.setattr(worker_module, "MAX_REQUEST_BYTES", 100_000)
    definition = "from document_extraction_api.fields import Record, Text\n" \
        "TASK_ID = 'long.extract'\n" \
        "class Output(Record):\n" \
        "    title: Text\n"
    write_process(processes_dir, "site_a", "long", definition, prompt="帳票" * 10_000)
    write_process(processes_dir, "site_a", "short", definition.replace("long", "short"))
    extractor = FakeExtractor(claude_message({"title": "A"}))
    app = make_app(settings, extractor)
    async with client(app) as http:
        long_id = (await submit(http, task_id="long.extract")).json()["job_id"]
        await run_one(app)
        short_id = (await submit(http, task_id="short.extract")).json()["job_id"]
        await run_one(app)
        headers = {"X-API-Key": KEY_A}
        long_job = (await http.get(f"/api/v1/jobs/{long_id}", headers=headers)).json()
        short_job = (await http.get(f"/api/v1/jobs/{short_id}", headers=headers)).json()
    assert long_job["error"] == {"code": "too_large"}
    assert short_job["status"] == "succeeded"
    assert len(extractor.calls) == 1


async def test_page_over_the_single_image_limit_is_not_sent(settings, monkeypatch):
    monkeypatch.setattr(worker_module, "MAX_IMAGE_BASE64_BYTES", 100)
    extractor = FakeExtractor()
    app = make_app(settings, extractor)
    async with client(app) as http:
        job_id = (await submit(http)).json()["job_id"]
        await run_one(app)
        result = (await http.get(f"/api/v1/jobs/{job_id}", headers={"X-API-Key": KEY_A})).json()
    assert result["error"] == {"code": "too_large"}
    assert extractor.calls == []


@pytest.mark.parametrize(
    "message",
    [
        ["応答が", "オブジェクトでない"],
        {"content": "文字", "stop_reason": "end_turn", "usage": "x"},
        {"content": [None, {"type": "text", "text": 1}], "stop_reason": "end_turn"},
    ],
)
async def test_response_of_unexpected_shape_fails_as_invalid_json(settings, message):
    result = await _run(settings, message=message)
    assert result["status"] == "failed"
    assert result["error"] == {"code": "invalid_json"}


def test_date_is_sent_to_claude_as_text(processes_dir):
    registry = load_processes(processes_dir, ["site_a"])
    process = registry.get("site_a", "typed.extract")
    assert process.schema["properties"]["issued"] == {
        "anyOf": [{"type": "string"}, {"type": "null"}]
    }


def _with_metadata(model="claude-test", **usage):
    message = claude_message({"issued": None, "total": "1", "lines": []}, model=model)
    message["usage"] = {"input_tokens": 1000, "output_tokens": 200, **usage}
    return message


BROKEN_METADATA = [
    (_with_metadata(input_tokens="unknown"), "usage.input_tokens"),
    (_with_metadata(model={"name": "claude-test"}), "model"),
    (_with_metadata(model="unpriced", input_tokens=[]), "usage.input_tokens"),
    (_with_metadata(output_tokens=True), "usage.output_tokens"),
    (_with_metadata(output_tokens=2**70), "usage.output_tokens"),
    (_with_metadata(input_tokens=-1), "usage.input_tokens"),
]


@pytest.mark.parametrize(("message", "name"), BROKEN_METADATA)
async def test_unreadable_model_or_usage_finishes_the_job_as_review(settings, message, name):
    result = await _run(settings, message=message)
    assert result["status"] == "needs_review"
    assert result["data"]["total"] == "1"
    assert result["issues"] == [{"path": "", "raw": None, "message": f"応答の {name} を読めない"}]


@pytest.mark.parametrize(("message", "name"), BROKEN_METADATA)
async def test_unreadable_model_or_usage_in_a_saved_response_finishes_on_recovery(
    settings, message, name
):
    app = make_app(settings, FakeExtractor())
    store = fastapi_app(app).state.store
    async with client(app) as http:
        job_id = (await submit(http)).json()["job_id"]
    store.claim_next()
    store.mark_sent(job_id)
    worker_module._write_atomic(job_dir(settings, job_id) / RESPONSE_FILE, message)
    fastapi_app(app).state.worker.recover()
    job = store.get(job_id)
    assert job.status == db.NEEDS_REVIEW
    assert job.issues == [{"path": "", "raw": None, "message": f"応答の {name} を読めない"}]
