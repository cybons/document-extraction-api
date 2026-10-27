"""ジョブを1件ずつ処理するワーカー。

- Claude へ送る前に sent_at を記録し、受け取った応答は DB より先にディスクへ保存する。
- 起動時と手が空いたときに、処理中のまま残ったジョブを立て直す。
  応答が保存済みなら Claude を呼ばずに結果を確定し、まだ送っていなければ待ちに戻す。
- 1件の例外やタイムアウトで止まらず、次のジョブへ進む。
- 失敗にするのは、ファイルが読めない、Claude が応答しない、応答が JSON にならない、の3つだけ。
  想定外の誤りも、起きた段階でこのどれかに入れる。Claude へ送る前の誤りは max_attempts 回まで
  やり直し、続けば「ファイルが読めない」にする。応答を保存した後は失敗にしない。
"""

import asyncio
import contextlib
import json
import logging
import math
import os
import shutil
from pathlib import Path
from typing import Any

from . import db
from .claude import ClaudeUnavailable, Extractor
from .processes import InvalidOutput, Process, load_snapshot, validate_output
from .render import RenderError, render_pages
from .settings import Settings
from .storable import replaced_issue, storable

logger = logging.getLogger(__name__)

RESPONSE_FILE = "response.json"
INPUT_FILE = "input"
PROCESS_DIR = "process"
MAX_IMAGE_BASE64_BYTES = 10_000_000
"""Claude API の画像1枚の上限（base64 で 10MB）。設定では緩められない。"""
MAX_REQUEST_BYTES = 32_000_000
"""Claude API の要求全体の上限（32MB）。画像・プロンプト・出力の形を合わせて測る。
設定では緩められない。"""
REQUEST_OVERHEAD_BYTES = 4096
"""要求のうち、画像・プロンプト・出力の形以外（モデル名や JSON の括弧など）に見込む大きさ。"""
IMAGE_OVERHEAD_BYTES = 256
"""画像1枚ごとに、base64 の本体以外に見込む大きさ。"""


def job_dir(settings: Settings, job_id: str) -> Path:
    return settings.data_dir / "jobs" / job_id


class Worker:
    def __init__(
        self,
        settings: Settings,
        store: db.JobStore,
        extractor: Extractor,
        idle_seconds: float = 5,
    ):
        self.settings = settings
        self.store = store
        self.extractor = extractor
        self.idle_seconds = idle_seconds
        self.wake = asyncio.Event()

    async def run(self) -> None:
        failures = 0
        while True:
            try:
                await asyncio.to_thread(self.recover)
                job = await asyncio.to_thread(self.store.claim_next)
                failures = 0
            except Exception as error:
                failures += 1
                logger.error("ジョブの取得に失敗: %s", type(error).__name__)
                await asyncio.sleep(min(60, 2**failures))
                continue
            if job is None:
                self.wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.wake.wait(), self.idle_seconds)
                continue
            await self.process(job)

    def recover(self) -> None:
        """処理中のまま残ったジョブを立て直す（このワーカーが処理していない前提で呼ぶ）。"""
        for job in self.store.processing():
            try:
                self._recover_one(job)
            except Exception as error:
                # 1件の誤りで、ほかのジョブの立て直しと処理を止めない。次の機会にやり直す
                logger.error("ジョブの立て直しに失敗: %s %s", job.id, type(error).__name__)

    def _recover_one(self, job: db.Job) -> None:
        response_path = job_dir(self.settings, job.id) / RESPONSE_FILE
        if response_path.exists():
            try:
                raw = json.loads(response_path.read_text(encoding="utf-8"))
            except (ValueError, RecursionError):
                # 壊れた JSON、UTF-8 でない中身、桁数や入れ子の上限を超えた中身
                self.store.finish(job.id, db.FAILED, error=_error("invalid_json"))
                return
            self._finalize(job, raw)
        elif job.attempts < self.settings.max_attempts:
            self.store.requeue(job.id)
            logger.info("ジョブを待ちに戻した: %s", job.id)
        else:
            # 送る前に止まり続けたならファイルが読めない、送った後なら Claude が応答しない
            code = "unreadable_file" if job.sent_at is None else "claude_no_response"
            self.store.finish(job.id, db.FAILED, error=_error(code, job.attempts))
            logger.warning("ジョブを失敗にした（%s 回中断）: %s %s", job.attempts, job.id, code)

    async def process(self, job: db.Job) -> None:
        try:
            await self._process(job)
        except Exception as error:
            logger.error("ジョブの処理中に例外: %s %s", job.id, type(error).__name__)
            await asyncio.to_thread(self._recover_safely, job.id)

    def _recover_safely(self, job_id: str) -> None:
        """例外で止まったジョブを、やり直すか終わらせる。ここで失敗しても recover が拾う。"""
        try:
            job = self.store.get(job_id)
            if job is not None and job.status == db.PROCESSING:
                self._recover_one(job)
        except Exception as error:
            logger.error("ジョブの立て直しに失敗: %s %s", job_id, type(error).__name__)

    async def _process(self, job: db.Job) -> None:
        directory = job_dir(self.settings, job.id)
        if (directory / RESPONSE_FILE).exists():
            raw = json.loads((directory / RESPONSE_FILE).read_text(encoding="utf-8"))
            await asyncio.to_thread(self._finalize, job, raw)
            return

        process = await asyncio.to_thread(load_snapshot, job.site, directory / PROCESS_DIR)
        if process.version != job.process_version:
            await asyncio.to_thread(self.store.set_process_version, job.id, process.version)

        pages_dir = directory / "pages"
        shutil.rmtree(pages_dir, ignore_errors=True)
        try:
            pages = await asyncio.to_thread(
                render_pages, directory / INPUT_FILE, pages_dir, self.settings
            )
        except RenderError as error:
            await self._finish_safely(job, db.FAILED, error=_error(error.code))
            return
        images = [page.read_bytes() for page in pages]
        sizes = [_base64_length(image) for image in images]
        if (
            max(sizes) > MAX_IMAGE_BASE64_BYTES
            or sum(sizes) > self.settings.max_request_image_bytes
            or _request_bytes(process, sizes) > MAX_REQUEST_BYTES
        ):
            # Claude API の画像1枚（10MB）か要求全体（32MB）の上限を超えるので送らない
            await self._finish_safely(job, db.FAILED, error=_error("too_large"))
            return

        await asyncio.to_thread(self.store.mark_sent, job.id)
        site = self.settings.sites[job.site]
        try:
            # 制限時間は Claude の呼び出しだけにかける。応答を受け取った後の保存と確定は打ち切らない
            response = await asyncio.wait_for(
                self.extractor.extract(
                    site, job.requested_model, process.prompt, process.schema, images
                ),
                self.settings.job_timeout_seconds,
            )
        except ClaudeUnavailable as error:
            await self._finish_safely(job, db.FAILED, error=_error(error.code))
            return
        except TimeoutError:
            await self._finish_safely(job, db.FAILED, error=_error("job_timeout"))
            return
        except Exception as error:
            logger.error("Claude の呼び出しで例外: %s %s", job.id, type(error).__name__)
            await self._finish_safely(job, db.FAILED, error=_error("claude_error"))
            return

        await asyncio.to_thread(_write_atomic, directory / RESPONSE_FILE, response.raw)
        await asyncio.to_thread(self._finalize, job, response.raw, process)

    def _finalize(self, job: db.Job, raw: Any, process: Process | None = None) -> None:
        """保存済みの Claude の応答から結果を確定する。

        結果は、記録して返せる値にそろえてから記録する。そろえる途中で何が起きても、決まった中身
        だけで要確認にして終える。ここから外へ出る例外は、記録（DB）の誤りだけにする。
        """
        try:
            status, values = self._result(job, raw, process)
            status, values = _storable_result(status, values)
        except Exception as error:
            logger.error("結果を確定できない: %s %s", job.id, type(error).__name__)
            status, values = db.NEEDS_REVIEW, {"issues": [_UNRECORDABLE_ISSUE]}
        self.store.finish(job.id, status, **values)
        logger.info("ジョブ完了: %s %s %s", job.id, status, (values.get("error") or {}).get("code"))

    def _result(self, job: db.Job, raw: Any, process: Process | None) -> tuple[str, dict]:
        if not isinstance(raw, dict):
            return db.FAILED, {"error": _error("invalid_json")}
        if process is None:
            try:
                process = load_snapshot(job.site, job_dir(self.settings, job.id) / PROCESS_DIR)
            except Exception as error:
                logger.error("処理の写しを読み込めない: %s %s", job.id, type(error).__name__)
        # 応答に付いてくるモデル名と使用量は、型を確かめてから記録する。合わない値は空として
        # 記録し、元の値は raw_response に残る
        model, input_tokens, output_tokens, unreadable = _metadata(raw)
        record = {
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": self._cost(model, input_tokens, output_tokens),
            "raw_response": raw,
        }
        metadata_issues = [
            {"path": "", "raw": None, "message": f"応答の {name} を読めない"} for name in unreadable
        ]

        stop_reason = raw.get("stop_reason")
        if stop_reason == "refusal":
            return db.FAILED, {"error": _error("claude_refused"), **record}
        if stop_reason == "max_tokens":
            return db.FAILED, {"error": _error("response_truncated"), **record}
        content = raw.get("content")
        text = "".join(
            block["text"]
            for block in (content if isinstance(content, list) else [])
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
        try:
            output = json.loads(text)
        except (ValueError, RecursionError):
            # JSON の誤りのほか、桁数や入れ子の上限を超えたときも、応答が JSON にならないとする
            return db.FAILED, {"error": _error("invalid_json"), **record}
        if process is None:
            # 課金済みの結果は捨てない。検証できなかったことを示して要確認にする
            issue = {"path": "", "raw": None, "message": "処理の定義を読み込めず、検証していない"}
            return db.NEEDS_REVIEW, {"data": output, "issues": [issue, *metadata_issues], **record}
        try:
            validated = validate_output(process, output)
        except Exception as error:
            # JSON としては読めたので失敗にしない。検証できなかった応答をそのまま返して要確認にする
            if isinstance(error, InvalidOutput):
                message = "出力の形が定義と合わない"
            else:
                logger.error("検証で例外: %s %s", job.id, type(error).__name__)
                message = f"検証が例外で止まった: {type(error).__name__}"
            issues = [{"path": "", "raw": None, "message": message}, *metadata_issues]
            return db.NEEDS_REVIEW, {"data": output, "issues": issues, **record}
        issues = [*validated.issues, *metadata_issues]
        status = db.NEEDS_REVIEW if issues else db.SUCCEEDED
        return status, {"data": validated.data, "issues": issues, **record}

    def _cost(self, model: str | None, input_tokens: int | None, output_tokens: int | None):
        price = self.settings.prices.get(model or "")
        if price is None or input_tokens is None or output_tokens is None:
            return None
        cost = (
            input_tokens * price.input_per_mtok + output_tokens * price.output_per_mtok
        ) / 1_000_000
        return round(cost, 6) if math.isfinite(cost) else None

    async def _finish_safely(self, job: db.Job, status: str, **values) -> None:
        """結果の記録に失敗しても止まらない。残ったジョブは recover が立て直す。"""
        try:
            await asyncio.to_thread(self.store.finish, job.id, status, **values)
            logger.info(
                "ジョブ完了: %s %s %s", job.id, status, (values.get("error") or {}).get("code")
            )
        except Exception as error:
            logger.error("ジョブの記録に失敗: %s %s", job.id, type(error).__name__)


_UNRECORDABLE_ISSUE = {
    "path": "",
    "raw": None,
    "message": "結果を記録できる形にできず、検証の結果を残していない",
}


def _storable_result(status: str, values: dict) -> tuple[str, dict]:
    """記録する値をそろえる。置き換えた箇所があれば指摘を足し、成功を要確認にする。

    raw_response は返さない列なので、そろえずに ASCII の JSON で記録する（db.finish）。
    """
    result = dict(values)
    replaced: list[str] = []
    for name in ("data", "issues", "error", "model"):
        if name in result:
            result[name], found = storable(result[name])
            replaced += [_join(name, path) for path in found]
    if replaced:
        result["issues"] = [*(result.get("issues") or []), replaced_issue(replaced)]
        if status == db.SUCCEEDED:
            status = db.NEEDS_REVIEW
    if result.get("raw_response") is not None:
        try:
            json.dumps(result["raw_response"])
        except (ValueError, RecursionError):
            result["raw_response"] = None  # 元の応答はディスクに残っている
    # 記録する前に、返すときと同じ厳しさで JSON にできることを確かめる（通らなければ受け皿へ）
    returned = {name: value for name, value in result.items() if name != "raw_response"}
    json.dumps(returned, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return status, result


def _join(name: str, path: str) -> str:
    if not path:
        return name
    return f"{name}{path}" if path.startswith("[") else f"{name}.{path}"


def _error(code: str, attempts: int | None = None) -> dict:
    error = {"code": code}
    if attempts is not None:
        error["attempts"] = attempts
    return error


_MAX_TOKENS_RECORDED = 2**63 - 1
"""SQLite の整数に入る上限。"""


def _metadata(raw: dict) -> tuple[str | None, int | None, int | None, list[str]]:
    """応答からモデル名と入出力のトークン数を取り出す。型が合わない値は None にし、名前を返す。"""
    unreadable = []
    model = raw.get("model")
    if model is not None and not isinstance(model, str):
        model = None
        unreadable.append("model")
    usage = raw.get("usage")
    if usage is not None and not isinstance(usage, dict):
        unreadable.append("usage")
    usage = usage if isinstance(usage, dict) else {}
    tokens = []
    for name in ("input_tokens", "output_tokens"):
        value = usage.get(name)
        if value is not None and not (
            isinstance(value, int)
            and not isinstance(value, bool)
            and 0 <= value <= _MAX_TOKENS_RECORDED
        ):
            value = None
            unreadable.append(f"usage.{name}")
        tokens.append(value)
    return model, tokens[0], tokens[1], unreadable


def _base64_length(data: bytes) -> int:
    return 4 * ((len(data) + 2) // 3)


def _request_bytes(process: Process, sizes: list[int]) -> int:
    """Claude へ送る要求の大きさを、多めに見積もる（日本語は \\uXXXX で数える）。"""
    return (
        sum(sizes)
        + IMAGE_OVERHEAD_BYTES * len(sizes)
        + len(json.dumps(process.prompt))
        + len(json.dumps(process.schema))
        + REQUEST_OVERHEAD_BYTES
    )


def _write_atomic(path: Path, value: dict) -> None:
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as f:
        # ASCII で書く。UTF-8 にできない文字があっても保存で止まらない
        # （保存の前に止まると、送り直して二重に課金する）
        json.dump(value, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)
