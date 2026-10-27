"""実際の Anthropic SDK を通し、送る要求の形と応答の扱いを確かめる（通信は偽物）。"""

import json

import httpx2
import pytest
from conftest import png_bytes

from document_extraction_api.claude import AnthropicExtractor, ClaudeUnavailable

pytestmark = pytest.mark.anyio

SCHEMA = {
    "type": "object",
    "properties": {"title": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
    "required": ["title"],
    "additionalProperties": False,
}


def _sse(text: str) -> bytes:
    events = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-served",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1234, "output_tokens": 1},
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 56},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


async def test_request_shape_and_response(settings):
    captured = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["headers"] = request.headers
        captured["body"] = json.loads(request.content)
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=_sse('{"title": "x"}')
        )

    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    extractor = AnthropicExtractor(settings, http_client=http_client)
    page = png_bytes()
    response = await extractor.extract(
        settings.sites["site_a"], "claude-test", "全部読む", SCHEMA, [page, page]
    )

    body = captured["body"]
    assert captured["headers"]["authorization"] == "Bearer claude-key-a"
    assert body["model"] == "claude-test"
    assert body["stream"] is True
    assert body["output_config"] == {
        "effort": "medium",
        "format": {"type": "json_schema", "schema": SCHEMA},
    }
    for forbidden in ("temperature", "top_p", "top_k", "thinking", "tool_choice", "system"):
        assert forbidden not in body
    content = body["messages"][0]["content"]
    assert [block["type"] for block in content] == ["image", "image", "text"]
    assert content[0]["source"]["media_type"] == "image/png"
    assert content[2]["text"] == "全部読む"

    assert response.raw["model"] == "claude-served"
    assert response.raw["stop_reason"] == "end_turn"
    assert response.raw["usage"]["input_tokens"] == 1234
    assert response.raw["usage"]["output_tokens"] == 56
    assert response.raw["content"][0]["text"] == '{"title": "x"}'


async def test_api_errors_become_unavailable(settings):
    def handler(request):
        return httpx2.Response(
            529,
            json={"type": "error", "error": {"type": "overloaded_error", "message": "overloaded"}},
        )

    settings = settings.__class__(**{**settings.__dict__, "claude_max_retries": 0})
    extractor = AnthropicExtractor(
        settings, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    )
    with pytest.raises(ClaudeUnavailable) as raised:
        await extractor.extract(settings.sites["site_a"], "claude-test", "p", SCHEMA, [png_bytes()])
    assert raised.value.code == "claude_http_529"
