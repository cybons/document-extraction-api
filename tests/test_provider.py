import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anthropic
import httpx2
import pytest
from conftest import invoice
from test_worker import enqueue, setup_store

from document_extraction_api.errors import JobError
from document_extraction_api.provider import ClaudeProvider
from document_extraction_api.tasks import Invoice, review


def response(text=None, stop="end_turn"):
    return SimpleNamespace(
        usage=SimpleNamespace(input_tokens=100, output_tokens=50),
        stop_reason=stop,
        content=[SimpleNamespace(type="text", text=text or invoice().model_dump_json())],
    )


def upstream(status):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    res = httpx2.Response(status, request=request)
    return anthropic.APIStatusError("private upstream detail", response=res, body=None)


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_retry_then_success_records_all_attempts(settings, monkeypatch, status):
    store = setup_store(settings)
    row = enqueue(store)
    snapshot = json.loads(row["snapshot"])
    snapshot["price"] = {"input": "3", "output": "15"}
    row["snapshot"] = json.dumps(snapshot)
    provider = ClaudeProvider(settings, store)
    mock = AsyncMock(side_effect=[upstream(status), response()])
    monkeypatch.setattr(provider.client.messages, "create", mock)
    monkeypatch.setattr("document_extraction_api.provider.asyncio.sleep", AsyncMock())
    try:
        result = await provider.extract(row, [])
        assert result.total == "110"
        assert mock.await_count == 2
        assert mock.call_args.kwargs["output_config"]["format"]["type"] == "json_schema"
        with store.db() as db:
            attempts = db.execute("SELECT * FROM attempts ORDER BY attempt").fetchall()
        assert len(attempts) == 2
        assert attempts[0]["cost"] is None
        assert attempts[1]["cost"] == "0.00105"
    finally:
        await provider.close()


async def test_permanent_error_no_retry(settings, monkeypatch):
    store = setup_store(settings)
    row = enqueue(store)
    provider = ClaudeProvider(settings, store)
    mock = AsyncMock(side_effect=upstream(401))
    monkeypatch.setattr(provider.client.messages, "create", mock)
    try:
        with pytest.raises(JobError, match="upstream_401"):
            await provider.extract(row, [])
        assert mock.await_count == 1
    finally:
        await provider.close()


async def test_retry_limit(settings, monkeypatch):
    store = setup_store(settings)
    row = enqueue(store)
    provider = ClaudeProvider(settings, store)
    mock = AsyncMock(side_effect=upstream(429))
    monkeypatch.setattr(provider.client.messages, "create", mock)
    monkeypatch.setattr("document_extraction_api.provider.asyncio.sleep", AsyncMock())
    try:
        with pytest.raises(JobError, match="upstream_429"):
            await provider.extract(row, [])
        assert mock.await_count == settings.max_attempts
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "output,code",
    [
        (response("{}"), "invalid_extraction"),
        (response(stop="max_tokens"), "incomplete_or_refused_output"),
        (response(stop="refusal"), "incomplete_or_refused_output"),
    ],
)
async def test_bad_outputs_record_usage(settings, monkeypatch, output, code):
    store = setup_store(settings)
    row = enqueue(store)
    provider = ClaudeProvider(settings, store)
    monkeypatch.setattr(provider.client.messages, "create", AsyncMock(return_value=output))
    try:
        with pytest.raises(JobError, match=code):
            await provider.extract(row, [])
        with store.db() as db:
            assert db.execute("SELECT input_tokens FROM attempts").fetchone()[0] == 100
    finally:
        await provider.close()


def test_nulls_and_reconciliation():
    doc = invoice()
    doc.issuer = None
    doc.total = "111"
    reasons = review(doc, "invoice.extract")
    assert "missing:issuer" in reasons
    assert "totals_mismatch" in reasons
    assert doc.model_dump()["issuer"] is None


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "1e100", "not-money", "1.123456789"])
def test_invalid_amounts_rejected(amount):
    value = invoice().model_dump()
    value["total"] = amount
    with pytest.raises(ValueError):
        Invoice.model_validate(value)


async def test_real_sdk_serializes_request_and_parses_response(settings):
    store = setup_store(settings)
    row = enqueue(store)
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "test-model",
                "content": [{"type": "text", "text": invoice().model_dump_json()}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        )

    provider = ClaudeProvider(settings, store)
    await provider.close()
    provider.client = anthropic.AsyncAnthropic(
        api_key="test-only",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    try:
        assert (await provider.extract(row, [])).total == "110"
        assert seen[0]["output_config"]["format"]["schema"]["additionalProperties"] is False
    finally:
        await provider.close()
