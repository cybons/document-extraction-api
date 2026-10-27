"""閲覧画面と閲覧用の API。"""

import base64
import hashlib
import json
import re

import pytest
from conftest import (
    KEY_A,
    KEY_B,
    FakeExtractor,
    claude_message,
    client,
    make_app,
    png_bytes,
    run_one,
    submit,
)

from document_extraction_api.api import _file_name

pytestmark = pytest.mark.anyio

A = {"X-API-Key": KEY_A}
B = {"X-API-Key": KEY_B}
TYPED = {"issued": "2026-10-27", "total": "100", "lines": []}
TITLE = {"title": "表題"}

VIEW_PATHS = ("/detail", "/file", "/response", "/process")


@pytest.fixture
async def jobs(settings):
    """拠点 A に3件（成功・要確認・失敗）、拠点 B に1件のジョブを作る。"""
    extractor = FakeExtractor(
        claude_message(TITLE),
        claude_message({**TYPED, "total": None}),
        claude_message(None, text="JSON ではない"),
        claude_message(TYPED),
    )
    app = make_app(settings, extractor)
    async with client(app) as http:

        async def accept(**kwargs):
            job_id = (await submit(http, **kwargs)).json()["job_id"]
            await run_one(app)
            return job_id

        ids = {
            "title": await accept(task_id="title.extract", filename="C:\\受信\\表題.png"),
            "review": await accept(content=png_bytes((1, 0, 0)), filename="要確認.png"),
            "failed": await accept(content=png_bytes((2, 0, 0))),
            "b": await accept(key=KEY_B, filename="子会社.png"),
        }
        yield http, ids


async def test_ui_page_is_returned_without_key_and_has_no_data(jobs):
    http, ids = jobs
    response = await http.get("/ui")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    page = response.text
    for secret in (*ids.values(), "site_a", "site_b", "表題.png", "子会社.png", KEY_A):
        assert secret not in page
    # キーを入れても中身は同じ（データはキーで API から取る）
    assert (await http.get("/ui", headers=A)).text == page


async def test_ui_page_inline_code_matches_content_security_policy(settings):
    app = make_app(settings, FakeExtractor())
    async with client(app) as http:
        response = await http.get("/ui")
    policy = response.headers["content-security-policy"]
    assert "default-src 'none'" in policy
    assert "frame-ancestors 'none'" in policy
    for tag in ("script", "style"):
        source = re.search(rf"<{tag}>(.*?)</{tag}>", response.text, re.DOTALL).group(1)
        digest = base64.b64encode(hashlib.sha256(source.encode()).digest()).decode()
        assert f"'sha256-{digest}'" in policy
    # 属性に書いたスクリプト・スタイルは CSP で止まるので使わない
    assert not re.search(r"\s(style|on\w+)=", response.text)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/ui"),
        ("GET", "/ui/"),
        ("GET", "/ui.html"),
        ("GET", "/api/v1/jobs"),
        ("GET", "/api/v1/summary"),
        *(("GET", "/api/v1/jobs/x" + suffix) for suffix in VIEW_PATHS),
    ],
)
async def test_everything_but_the_ui_page_needs_a_key(settings, method, path):
    app = make_app(settings, FakeExtractor())
    async with client(app) as http:
        response = await http.request(method, path)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_list_shows_only_the_sites_own_jobs(jobs):
    http, ids = jobs
    listed_a = (await http.get("/api/v1/jobs", headers=A)).json()
    listed_b = (await http.get("/api/v1/jobs", headers=B)).json()
    assert [job["job_id"] for job in listed_a["jobs"]] == [
        ids["failed"],
        ids["review"],
        ids["title"],
    ]
    assert [job["job_id"] for job in listed_b["jobs"]] == [ids["b"]]
    assert listed_a["next_before"] is None
    by_id = {job["job_id"]: job for job in listed_a["jobs"]}
    assert by_id[ids["title"]]["file_name"] == "表題.png"
    assert by_id[ids["title"]]["task_id"] == "title.extract"
    assert by_id[ids["title"]]["status"] == "succeeded"
    assert by_id[ids["title"]]["cost_usd"] == 0.008
    assert by_id[ids["review"]]["issue_count"] == 1
    assert by_id[ids["failed"]]["status"] == "failed"
    assert by_id[ids["failed"]]["error_code"] == "invalid_json"
    assert by_id[ids["failed"]]["file_name"] == "doc.png"


async def test_other_sites_jobs_are_not_found_in_any_view(jobs):
    http, ids = jobs
    for suffix in ("", *VIEW_PATHS):
        response = await http.get(f"/api/v1/jobs/{ids['title']}{suffix}", headers=B)
        assert response.status_code == 404, suffix
        assert response.json()["error"]["code"] == "job_not_found"
        assert (await http.get(f"/api/v1/jobs/{ids['b']}{suffix}", headers=B)).status_code == (200)


async def test_summary_counts_only_the_sites_own_jobs(jobs):
    http, ids = jobs
    summary_a = (await http.get("/api/v1/summary", headers=A)).json()
    summary_b = (await http.get("/api/v1/summary", headers=B)).json()
    assert summary_a["site"] == "site_a"
    assert summary_a["jobs"] == 3
    assert summary_a["status_counts"] == {
        "queued": 0,
        "processing": 0,
        "succeeded": 1,
        "needs_review": 1,
        "failed": 1,
    }
    # 応答が JSON にならなかったジョブも課金されているので、費用に入る
    assert summary_a["input_tokens"] == 3000
    assert summary_a["output_tokens"] == 600
    assert summary_a["cost_usd"] == pytest.approx(0.024)
    assert summary_a["unpriced_jobs"] == 0
    assert [task["task_id"] for task in summary_a["tasks"]] == ["title.extract", "typed.extract"]
    assert summary_a["tasks"][1]["jobs"] == 2
    assert summary_b["site"] == "site_b"
    assert summary_b["jobs"] == 1
    assert summary_b["status_counts"]["succeeded"] == 1


async def test_summary_counts_jobs_without_a_price(settings):
    extractor = FakeExtractor(claude_message(TYPED, model="claude-unpriced"))
    app = make_app(settings, extractor)
    async with client(app) as http:
        await submit(http)
        await run_one(app)
        summary = (await http.get("/api/v1/summary", headers=A)).json()
    assert summary["cost_usd"] == 0
    assert summary["unpriced_jobs"] == 1


async def test_list_filters_and_pages(jobs):
    http, ids = jobs

    async def listed(**params):
        response = await http.get("/api/v1/jobs", headers=A, params=params)
        assert response.status_code == 200
        return [job["job_id"] for job in response.json()["jobs"]]

    assert await listed(status="failed") == [ids["failed"]]
    assert await listed(task_id="title.extract") == [ids["title"]]
    assert await listed(task_id="typed.extract", status="needs_review") == [ids["review"]]
    assert await listed(**{"from": "2000-01-01T00:00:00+09:00"}) == await listed()
    assert await listed(**{"to": "2000-01-01T00:00:00Z"}) == []
    assert await listed(**{"from": "2999-01-01T00:00:00+09:00"}) == []

    first = (await http.get("/api/v1/jobs", headers=A, params={"limit": 2})).json()
    assert [job["job_id"] for job in first["jobs"]] == [ids["failed"], ids["review"]]
    assert first["next_before"] == ids["review"]
    assert await listed(limit=2, before=first["next_before"]) == [ids["title"]]


async def test_summary_period(jobs):
    http, ids = jobs
    future = await http.get(
        "/api/v1/summary", headers=A, params={"from": "2999-01-01T00:00:00+09:00"}
    )
    past = await http.get("/api/v1/summary", headers=A, params={"to": "2999-01-01T00:00:00Z"})
    assert future.json()["jobs"] == 0
    assert future.json()["cost_usd"] == 0
    assert future.json()["tasks"] == []
    assert past.json()["jobs"] == 3


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/api/v1/jobs", {"status": "done"}),
        ("/api/v1/jobs", {"limit": 0}),
        ("/api/v1/jobs", {"limit": 201}),
        ("/api/v1/jobs", {"limit": "x"}),
        ("/api/v1/jobs", {"from": "2026-10-01T00:00:00"}),
        ("/api/v1/jobs", {"to": "10月"}),
        ("/api/v1/summary", {"from": "2026-10-01"}),
    ],
)
async def test_invalid_parameters(settings, path, params):
    app = make_app(settings, FakeExtractor())
    async with client(app) as http:
        response = await http.get(path, headers=A, params=params)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


async def test_detail_shows_everything_recorded(jobs):
    http, ids = jobs
    detail = (await http.get(f"/api/v1/jobs/{ids['review']}/detail", headers=A)).json()
    plain = (await http.get(f"/api/v1/jobs/{ids['review']}", headers=A)).json()
    # 呼び出し側に返す結果の形は変えず、閲覧用に記録を足す
    assert {key: detail[key] for key in plain} == plain
    assert detail["status"] == "needs_review"
    assert detail["issues"][0]["path"] == "total"
    assert detail["file_name"] == "要確認.png"
    assert detail["file_sha256"] == hashlib.sha256(png_bytes((1, 0, 0))).hexdigest()
    assert detail["requested_model"] == "claude-test"
    assert detail["attempts"] == 1
    for stamp in ("created_at", "sent_at", "finished_at", "updated_at"):
        assert detail[stamp]
    assert detail["created_at"] <= detail["sent_at"] <= detail["finished_at"]


async def test_original_file_response_and_process_snapshot(jobs):
    http, ids = jobs
    base = f"/api/v1/jobs/{ids['title']}"
    file = await http.get(f"{base}/file", headers=A)
    response = await http.get(f"{base}/response", headers=A)
    process = await http.get(f"{base}/process", headers=A)
    assert file.status_code == 200
    assert file.content == png_bytes()
    assert file.headers["content-type"] == "image/png"
    assert file.headers["x-content-type-options"] == "nosniff"
    assert response.status_code == 200
    assert response.json() == claude_message(TITLE)
    assert process.status_code == 200
    snapshot = process.json()
    assert snapshot["name"] == "title"
    assert set(snapshot["files"]) == {"definition.py", "prompt.md"}
    assert 'TASK_ID = "title.extract"' in snapshot["files"]["definition.py"]
    assert snapshot["files"]["prompt.md"] == "全部読む"


async def test_response_is_not_found_before_claude_answers(settings):
    app = make_app(settings, FakeExtractor())
    async with client(app) as http:
        job_id = (await submit(http)).json()["job_id"]
        response = await http.get(f"/api/v1/jobs/{job_id}/response", headers=A)
        listed = (await http.get("/api/v1/jobs", headers=A)).json()["jobs"]
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "response_not_found"
    assert listed[0]["status"] == "queued"
    assert listed[0]["cost_usd"] is None


async def test_resubmitting_the_same_file_keeps_the_first_name(settings):
    app = make_app(settings, FakeExtractor())
    async with client(app) as http:
        first = (await submit(http, filename="最初.png")).json()["job_id"]
        second = (await submit(http, filename="二回目.png")).json()["job_id"]
        detail = (await http.get(f"/api/v1/jobs/{first}/detail", headers=A)).json()
    assert first == second
    assert detail["file_name"] == "最初.png"


@pytest.mark.parametrize(
    ("sent", "stored"),
    [
        ("請求書.pdf", "請求書.pdf"),
        ("C:\\Users\\a\\請求書.pdf", "請求書.pdf"),
        ("../../etc/passwd", "passwd"),
        ("a\x00b\nc.pdf", "a?b?c.pdf"),
        ("\ud800.pdf", "?.pdf"),
        ("x" * 1000, "x" * 255),
        ("", None),
        (None, None),
        ("dir/", None),
    ],
)
def test_file_name_is_made_storable(sent, stored):
    assert _file_name(sent) == stored
    if stored is not None:
        stored.encode("utf-8")


async def test_unusual_file_names_are_accepted_and_shown(settings):
    app = make_app(settings, FakeExtractor())
    async with client(app) as http:
        name = "<img src=x onerror=alert(1)>" + "長" * 300 + ".png"
        response = await submit(http, filename=name)
        assert response.status_code == 202
        detail = await http.get(f"/api/v1/jobs/{response.json()['job_id']}/detail", headers=A)
    stored = detail.json()["file_name"]
    assert stored.startswith("<img src=x onerror=alert(1)>長")
    assert len(stored) == 255
    json.dumps(detail.json(), ensure_ascii=False).encode("utf-8")
