"""応答を保存した後は、Claude がどんな値を返しても、ジョブが終わり、結果を HTTP で返せること。

性質テストで任意の JSON を与え、Codex のレビューで見つかった反例は固定のテストにする。
"""

import json
import uuid

import pytest
from conftest import (
    KEY_A,
    FakeExtractor,
    claude_message,
    client,
    fastapi_app,
    make_app,
    run_one,
    submit,
    write_process,
)
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from hypothesis import HealthCheck, given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from test_failure_paths import _settings_file

from document_extraction_api import db
from document_extraction_api import worker as worker_module
from document_extraction_api.api import _job_response
from document_extraction_api.processes import load_processes
from document_extraction_api.settings import Price, SettingsError, load_settings
from document_extraction_api.worker import RESPONSE_FILE, job_dir

pytestmark = pytest.mark.anyio

SURROGATE = "\ud800"

_chars = st.one_of(st.characters(), st.integers(0xD800, 0xDFFF).map(chr))
_text = st.text(_chars, max_size=8)
_leaves = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(-(10**30), 10**30),
    st.floats(allow_nan=True, allow_infinity=True),
    _text,
)
json_trees = st.recursive(
    _leaves,
    lambda children: st.one_of(
        st.lists(children, max_size=4), st.dictionaries(_text, children, max_size=4)
    ),
    max_leaves=25,
)
# 処理の定義に合いやすい形も混ぜる（検証を通る経路と、値を null にする経路を通すため）
typed_outputs = st.fixed_dictionaries(
    {"issued": json_trees, "total": json_trees, "lines": st.lists(json_trees, max_size=3)}
)
claude_texts = st.one_of(
    json_trees.map(json.dumps),
    typed_outputs.map(json.dumps),
    st.fixed_dictionaries({"title": json_trees}).map(json.dumps),
    _text,
)
metadata_values = st.one_of(st.none(), _text, st.integers(-5, 10**6), st.floats())


def _returned(job: db.Job) -> bytes:
    """GET /api/v1/jobs/{id} と同じ変換で、結果を返せること。"""
    return JSONResponse(jsonable_encoder(_job_response(job))).body


@pytest.fixture
def finalize_env(settings):
    app = make_app(settings, FakeExtractor())
    state = fastapi_app(app).state
    registry = load_processes(settings.processes_dir, list(settings.sites))
    return state.store, state.worker, registry


@hypothesis_settings(
    max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    text=claude_texts,
    task=st.sampled_from(["typed.extract", "title.extract", None]),
    model=metadata_values,
    input_tokens=metadata_values,
)
def test_any_response_finishes_the_job_and_can_be_returned(
    finalize_env, text, task, model, input_tokens
):
    store, worker, registry = finalize_env
    job, _ = store.create_or_find(
        uuid.uuid4().hex,
        "site_a",
        task or "typed.extract",
        "doc.png",
        uuid.uuid4().hex,
        "v",
        "m",
        lambda: None,
    )
    message = claude_message(None, text=text)
    message["model"] = model
    message["usage"]["input_tokens"] = input_tokens
    # task が None のときは処理を渡さない。写しもないので、検証できない経路を通る
    process = registry.get("site_a", task) if task else None
    worker._finalize(job, message, process)

    finished = store.get(job.id)
    assert finished.status in (db.SUCCEEDED, db.NEEDS_REVIEW, db.FAILED)
    assert finished.finished_at is not None
    _returned(finished)


async def _run(settings, message, task_id="title.extract", **overrides):
    extractor = FakeExtractor(message)
    app = make_app(settings, extractor, **overrides)
    async with client(app) as http:
        job_id = (await submit(http, task_id=task_id)).json()["job_id"]
        await run_one(app)
        response = await http.get(f"/api/v1/jobs/{job_id}", headers={"X-API-Key": KEY_A})
    assert response.status_code == 200
    return response.json(), extractor, job_id


async def test_unencodable_text_in_data_is_replaced(settings):
    result, _, _ = await _run(settings, claude_message(None, text='{"title": "\\ud800"}'))
    assert result["status"] == "needs_review"
    assert result["data"] == {"title": None}
    assert result["issues"][-1]["message"] == "記録できない値を null にした: data.title"


async def test_unencodable_raw_value_in_issues_is_replaced(settings):
    text = '{"issued": null, "total": "\\ud800", "lines": []}'
    result, _, _ = await _run(settings, claude_message(None, text=text), "typed.extract")
    assert result["status"] == "needs_review"
    assert result["issues"][0] == {"path": "total", "raw": None, "message": "数値として読めない"}
    assert result["issues"][-1]["message"] == "記録できない値を null にした: issues[0].raw"


async def test_unencodable_message_from_check_is_replaced(settings, processes_dir):
    write_process(
        processes_dir,
        "site_a",
        "surrogate",
        """
        from document_extraction_api.fields import Issue, Record, Text

        TASK_ID = "surrogate.extract"


        class Output(Record):
            title: Text


        def check(data):
            return [Issue("title", "\\ud800")]
        """,
    )
    result, _, _ = await _run(settings, claude_message({"title": "A"}), "surrogate.extract")
    assert result["status"] == "needs_review"
    assert result["issues"][0]["message"] is None
    assert result["issues"][-1]["message"].endswith("issues[0].message")


async def test_unencodable_text_decoded_by_the_sdk_is_saved_and_not_sent_again(settings):
    message = claude_message(None, text='{"title": "' + SURROGATE + '"}')
    result, extractor, job_id = await _run(settings, message)
    assert result["status"] == "needs_review"
    assert len(extractor.calls) == 1
    assert (job_dir(settings, job_id) / RESPONSE_FILE).exists()


async def test_unencodable_model_name_is_replaced(settings):
    result, _, _ = await _run(settings, claude_message({"title": "A"}, model=SURROGATE))
    assert result["status"] == "needs_review"
    assert result["versions"]["model"] is None


async def test_number_beyond_the_digit_limit_fails_as_invalid_json(settings):
    text = '{"title": ' + "1" * 5000 + "}"
    result, _, _ = await _run(settings, claude_message(None, text=text))
    assert result["status"] == "failed"
    assert result["error"] == {"code": "invalid_json"}


async def test_saved_response_beyond_the_digit_limit_fails_on_recovery(settings):
    app = make_app(settings, FakeExtractor())
    store = fastapi_app(app).state.store
    async with client(app) as http:
        job_id = (await submit(http)).json()["job_id"]
    store.claim_next()
    store.mark_sent(job_id)
    (job_dir(settings, job_id) / RESPONSE_FILE).write_text(
        '{"usage": {"input_tokens": ' + "1" * 5000 + "}}", encoding="utf-8"
    )
    fastapi_app(app).state.worker.recover()
    job = store.get(job_id)
    assert job.status == db.FAILED
    assert job.error == {"code": "invalid_json"}


@pytest.mark.parametrize(
    ("task_id", "text"),
    [
        ("typed.extract", '{"issued": null, "total": 1e400, "lines": []}'),
        ("title.extract", '{"unexpected": 1e400}'),
        ("title.extract", '{"title": ' + "[" * 1100 + "]" * 1100 + "}"),
        ("title.extract", '{"title": ' + "[" * 40 + "]" * 40 + "}"),
    ],
)
async def test_values_that_cannot_be_returned_are_replaced(settings, task_id, text):
    result, _, _ = await _run(settings, claude_message(None, text=text), task_id)
    assert result["status"] == "needs_review"
    assert result["issues"][-1]["message"].startswith("記録できない値を null にした")


async def test_cost_that_overflows_is_not_recorded(settings):
    prices = {"claude-test": Price(1e308, 1e308)}
    result, _, _ = await _run(settings, claude_message({"title": "A"}), prices=prices)
    assert result["status"] == "succeeded"
    assert result["usage"]["cost_usd"] is None


@pytest.mark.parametrize("value", ["inf", "nan", "-1"])
def test_price_must_be_a_finite_non_negative_number(tmp_path, value):
    path = _settings_file(tmp_path, 0)
    path.write_text(
        path.read_text(encoding="utf-8")
        + f"[prices.claude-test]\ninput_per_mtok = {value}\noutput_per_mtok = 1\n",
        encoding="utf-8",
    )
    with pytest.raises(SettingsError):
        load_settings(path)


async def test_anything_unexpected_while_preparing_the_record_still_finishes(settings, monkeypatch):
    def broken(status, values):
        raise RuntimeError("bug")

    monkeypatch.setattr(worker_module, "_storable_result", broken)
    result, _, _ = await _run(settings, claude_message({"title": "A"}))
    assert result["status"] == "needs_review"
    assert result["data"] is None
    assert result["issues"] == [dict(worker_module._UNRECORDABLE_ISSUE)]
