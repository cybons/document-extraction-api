"""閲覧画面と閲覧用の API。"""

import base64
import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import closing
from dataclasses import replace

import pytest
from conftest import (
    KEY_A,
    KEY_B,
    FakeExtractor,
    claude_message,
    client,
    fastapi_app,
    make_app,
    png_bytes,
    run_one,
    submit,
    write_process,
)

from document_extraction_api.api import _file_name
from document_extraction_api.processes import ProcessError, describe_path, load_processes
from document_extraction_api.settings import SettingsError, parse_settings
from document_extraction_api.worker import PROCESS_DIR, job_dir

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


NAMED_DEFINITION = '''
"""テスト専用：画面に出す名前を書いた処理。"""
from pydantic import Field

from document_extraction_api.fields import Date, Number, Record, Text

TASK_ID = "named.extract"
NAME = "名前付きの帳票"


class Line(Record):
    name: Text = Field(title="品名")
    quantity: Number = Field(title="数量")


class Output(Record):
    issued: Date = Field(title="発行日")
    total: Number
    lines: list[Line] = Field(title="明細")
'''


@pytest.fixture
def named(settings, processes_dir):
    write_process(processes_dir, "site_a", "named", NAMED_DEFINITION)
    sites = dict(settings.sites)
    sites["site_a"] = replace(sites["site_a"], display_name="親会社")
    return replace(settings, sites=sites)


async def test_names_from_settings_and_process_definition(named):
    output = {
        "issued": "令和8年13月1日",
        "total": "1O",
        "lines": [{"name": "部品", "quantity": "x"}],
    }
    app = make_app(named, FakeExtractor(claude_message(output), claude_message(TITLE)))
    async with client(app) as http:
        job_id = (await submit(http, task_id="named.extract")).json()["job_id"]
        await run_one(app)
        await submit(http, task_id="title.extract", content=png_bytes((5, 5, 5)))
        await run_one(app)
        summary = (await http.get("/api/v1/summary", headers=A)).json()
        listed = (await http.get("/api/v1/jobs", headers=A)).json()["jobs"]
        detail = (await http.get(f"/api/v1/jobs/{job_id}/detail", headers=A)).json()
        other = (await http.get("/api/v1/summary", headers=B)).json()
    assert summary["site_name"] == "親会社"
    assert other["site_name"] == "site_b"  # 名前を書いていなければ設定のキー名
    names = {task["task_id"]: task["task_name"] for task in summary["tasks"]}
    assert names == {"named.extract": "名前付きの帳票", "title.extract": None}
    item = next(job for job in listed if job["job_id"] == job_id)
    assert item["task_name"] == "名前付きの帳票"
    fields = {issue["field"]: issue["message"] for issue in item["issues"]}
    assert fields["発行日"] == "日付として読めない"
    assert fields["total"] == "数値として読めない"  # 名前のない項目はキーのまま
    assert fields["明細 1行目の数量"] == "数値として読めない"
    assert detail["task_name"] == "名前付きの帳票"
    assert detail["labels"] == {
        "issued": "発行日",
        "lines": "明細",
        "lines[].name": "品名",
        "lines[].quantity": "数量",
    }
    assert len(detail["issue_fields"]) == len(detail["issues"])
    assert "明細 1行目の数量" in detail["issue_fields"]


async def test_names_are_those_of_the_process_the_job_was_accepted_with(named, processes_dir):
    app = make_app(
        named, FakeExtractor(claude_message({"issued": None, "total": None, "lines": []}))
    )
    async with client(app) as http:
        job_id = (await submit(http, task_id="named.extract")).json()["job_id"]
        await run_one(app)
    # 受け付けた後に処理の名前を変えても、そのジョブには受け付けたときの名前を出す
    renamed = NAMED_DEFINITION.replace("名前付きの帳票", "変えた名前").replace("発行日", "日付")
    (processes_dir / "site_a" / "named" / "definition.py").write_text(renamed, encoding="utf-8")
    app = make_app(named, FakeExtractor())
    async with client(app) as http:
        detail = (await http.get(f"/api/v1/jobs/{job_id}/detail", headers=A)).json()
    assert detail["task_name"] == "名前付きの帳票"
    assert detail["labels"]["issued"] == "発行日"


async def test_detail_still_works_when_the_process_copy_cannot_be_loaded(jobs, settings):
    http, ids = jobs
    snapshot = job_dir(settings, ids["review"]) / PROCESS_DIR
    for path in snapshot.rglob("definition.py"):
        path.write_text("raise RuntimeError('壊れた')", encoding="utf-8")
    response = await http.get(f"/api/v1/jobs/{ids['review']}/detail", headers=A)
    listed = await http.get("/api/v1/jobs", headers=A)
    assert response.status_code == 200
    assert response.json()["task_name"] is None
    assert response.json()["labels"] == {}
    assert listed.status_code == 200


async def test_list_filters_by_several_statuses(jobs):
    http, ids = jobs
    response = await http.get(
        "/api/v1/jobs", headers=A, params=[("status", "failed"), ("status", "needs_review")]
    )
    assert [job["job_id"] for job in response.json()["jobs"]] == [ids["failed"], ids["review"]]


async def test_summary_by_task(jobs):
    http, ids = jobs
    summary = (
        await http.get("/api/v1/summary", headers=A, params={"task_id": "typed.extract"})
    ).json()
    assert summary["jobs"] == 2
    assert [task["task_id"] for task in summary["tasks"]] == ["typed.extract"]
    assert summary["tasks"][0]["status_counts"]["failed"] == 1
    assert summary["tasks"][0]["status_counts"]["needs_review"] == 1


async def test_summary_days_are_cut_at_the_given_offset(jobs, settings):
    http, ids = jobs
    # 日本時間で 10/27 0:30（UTC では 10/26 15:30）と 10/27 23:30（UTC では 10/27 14:30）
    with closing(sqlite3.connect(settings.data_dir / "jobs.sqlite3")) as conn, conn:
        conn.execute(
            "UPDATE jobs SET created_at=? WHERE id=?",
            ("2026-10-26T15:30:00.000000+00:00", ids["title"]),
        )
        conn.execute(
            "UPDATE jobs SET created_at=? WHERE id IN (?, ?)",
            ("2026-10-27T14:30:00.000000+00:00", ids["review"], ids["failed"]),
        )
    tokyo = (await http.get("/api/v1/summary", headers=A, params={"utc_offset": "+09:00"})).json()
    utc = (await http.get("/api/v1/summary", headers=A)).json()
    assert [(day["date"], day["jobs"]) for day in tokyo["days"]] == [("2026-10-27", 3)]
    assert [(day["date"], day["jobs"]) for day in utc["days"]] == [
        ("2026-10-26", 1),
        ("2026-10-27", 2),
    ]
    day = tokyo["days"][0]
    assert day["status_counts"] == {
        "queued": 0,
        "processing": 0,
        "succeeded": 1,
        "needs_review": 1,
        "failed": 1,
    }
    assert day["cost_usd"] == pytest.approx(0.024)


@pytest.mark.parametrize("offset", ["9", "+9:00", "+09:60", "+15:00", "JST", "+09:00:00"])
async def test_invalid_utc_offset(settings, offset):
    app = make_app(settings, FakeExtractor())
    async with client(app) as http:
        response = await http.get("/api/v1/summary", headers=A, params={"utc_offset": offset})
    assert response.status_code == 400


@pytest.mark.parametrize(
    ("path", "described"),
    [
        ("total_amount", "合計金額"),
        ("items[1].quantity", "明細 2行目の数量"),
        ("items[0].unknown", "明細 1行目のunknown"),
        ("other", "other"),
        ("tags[2]", "tags 3行目"),
        ("", ""),
    ],
)
def test_describe_path(path, described):
    labels = {"total_amount": "合計金額", "items": "明細", "items[].quantity": "数量"}
    assert describe_path(path, labels) == described


def test_invalid_names_are_rejected(tmp_path):
    write_process(tmp_path, "s", "bad", NAMED_DEFINITION.replace('"名前付きの帳票"', "1"))
    with pytest.raises(ProcessError):
        load_processes(tmp_path, ["s"])
    raw = {
        "data_dir": "d",
        "processes_dir": "p",
        "sites": {"s": {"name": "", "claude_api_key": "k", "client_key_sha256": ["a"]}},
    }
    with pytest.raises(SettingsError):
        parse_settings(raw, tmp_path)


async def test_summary_adds_token_counts_beyond_the_sqlite_integer_range(settings):
    big = claude_message(TYPED)
    big["usage"]["input_tokens"] = 2**62
    app = make_app(settings, FakeExtractor(big, big))
    async with client(app) as http:
        for colour in ((1, 0, 0), (2, 0, 0)):
            await submit(http, content=png_bytes(colour))
            await run_one(app)
        response = await http.get("/api/v1/summary", headers=A)
    assert response.status_code == 200
    summary = response.json()
    assert summary["input_tokens"] == 2**63
    assert summary["tasks"][0]["input_tokens"] == 2**63


async def test_summary_cost_that_adds_up_to_infinity_is_not_given(jobs, settings):
    http, _ = jobs
    with closing(sqlite3.connect(settings.data_dir / "jobs.sqlite3")) as conn:
        conn.execute("UPDATE jobs SET cost_usd=1.7e308 WHERE site='site_a'")
        conn.commit()
    response = await http.get("/api/v1/summary", headers=A, params={"utc_offset": "+00:00"})
    assert response.status_code == 200
    summary = response.json()
    assert summary["cost_usd"] is None
    assert summary["tasks"][1]["cost_usd"] is None


async def test_summary_reads_one_moment_of_the_database(settings, monkeypatch):
    """集計の途中で別のジョブが受け付けられても、件数と内訳が食い違わない。"""
    app = make_app(settings, FakeExtractor(claude_message(TYPED)))
    store = fastapi_app(app).state.store
    connect = type(store).connect

    class Interrupted:
        def __init__(self, conn):
            self.conn = conn
            self.done = False

        def execute(self, sql, *args):
            result = self.conn.execute(sql, *args)
            if not self.done and "AS jobs" in sql:
                self.done = True
                monkeypatch.setattr(type(store), "connect", connect)
                store.create_or_find(
                    uuid.uuid4().hex,
                    "site_a",
                    "title.extract",
                    "x.png",
                    uuid.uuid4().hex,
                    "v",
                    "m",
                    lambda: None,
                )
            return result

        def __getattr__(self, name):
            return getattr(self.conn, name)

    async with client(app) as http:
        await submit(http)
        await run_one(app)
        monkeypatch.setattr(type(store), "connect", lambda self: Interrupted(connect(self)))
        summary = (await http.get("/api/v1/summary", headers=A)).json()
        assert (await http.get("/api/v1/summary", headers=A)).json()["jobs"] == 2
    assert summary["jobs"] == sum(summary["status_counts"].values()) == 1
    assert sum(task["jobs"] for task in summary["tasks"]) == 1
    assert sum(day["jobs"] for day in summary["days"]) == 1
