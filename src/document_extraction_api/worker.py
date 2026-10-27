"""ジョブを1件ずつ処理するワーカー。

- Claude へ送る前に sent_at を記録し、受け取った応答は DB より先にディスクへ保存する。
- 起動時と手が空いたときに、処理中のまま残ったジョブを立て直す。
  応答が保存済みなら Claude を呼ばずに結果を確定し、まだ送っていなければ待ちに戻す。
- 1件の例外やタイムアウトで止まらず、次のジョブへ進む。
"""

import asyncio
import contextlib
import json
import logging
import os
import shutil
from pathlib import Path

from . import db
from .claude import ClaudeUnavailable, Extractor
from .processes import InvalidOutput, Process, ProcessRegistry, validate_output
from .render import RenderError, render_pages
from .settings import Settings

logger = logging.getLogger(__name__)

RESPONSE_FILE = "response.json"
INPUT_FILE = "input"


def job_dir(settings: Settings, job_id: str) -> Path:
    return settings.data_dir / "jobs" / job_id


class Worker:
    def __init__(
        self,
        settings: Settings,
        store: db.JobStore,
        registry: ProcessRegistry,
        extractor: Extractor,
        idle_seconds: float = 5,
    ):
        self.settings = settings
        self.store = store
        self.registry = registry
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
            response_path = job_dir(self.settings, job.id) / RESPONSE_FILE
            if response_path.exists():
                try:
                    raw = json.loads(response_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    self.store.finish(job.id, db.FAILED, error=_error("invalid_json"))
                    continue
                self._finalize(job, raw)
            elif job.sent_at is None or job.attempts < self.settings.max_attempts:
                self.store.requeue(job.id)
                logger.info("ジョブを待ちに戻した: %s", job.id)
            else:
                self.store.finish(
                    job.id, db.FAILED, error=_error("claude_no_response", job.attempts)
                )
                logger.warning("ジョブを失敗にした（応答を得られないまま中断）: %s", job.id)

    async def process(self, job: db.Job) -> None:
        try:
            await self._process(job)
        except Exception as error:
            logger.error("ジョブの処理中に例外: %s %s", job.id, type(error).__name__)
            if (job_dir(self.settings, job.id) / RESPONSE_FILE).exists():
                # 課金済みの応答は保存してある。結果の確定は recover に任せる
                return
            await self._finish_safely(job, db.FAILED, error=_error("internal_error"))

    async def _process(self, job: db.Job) -> None:
        directory = job_dir(self.settings, job.id)
        if (directory / RESPONSE_FILE).exists():
            raw = json.loads((directory / RESPONSE_FILE).read_text(encoding="utf-8"))
            await asyncio.to_thread(self._finalize, job, raw)
            return

        process = self.registry.get(job.site, job.task_id)
        if process is None:
            await self._finish_safely(job, db.FAILED, error=_error("process_not_found"))
            return
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
        if sum(len(image) for image in images) * 4 / 3 > self.settings.max_request_image_bytes:
            # Claude API の要求の上限（32MB）を超えるので送らない
            await self._finish_safely(job, db.FAILED, error=_error("too_large"))
            return

        await asyncio.to_thread(self.store.mark_sent, job.id)
        site = self.settings.sites[job.site]
        try:
            # 制限時間は Claude の呼び出しだけにかける。応答を受け取った後の保存と確定は打ち切らない
            response = await asyncio.wait_for(
                self.extractor.extract(site, process.prompt, process.schema, images),
                self.settings.job_timeout_seconds,
            )
        except ClaudeUnavailable as error:
            await self._finish_safely(job, db.FAILED, error=_error(error.code))
            return
        except TimeoutError:
            await self._finish_safely(job, db.FAILED, error=_error("job_timeout"))
            return

        await asyncio.to_thread(_write_atomic, directory / RESPONSE_FILE, response.raw)
        await asyncio.to_thread(self._finalize, job, response.raw, process)

    def _finalize(self, job: db.Job, raw: dict, process: Process | None = None) -> None:
        """保存済みの Claude の応答から結果を確定する。"""
        process = process or self.registry.get(job.site, job.task_id)
        usage = raw.get("usage") or {}
        model = raw.get("model")
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        record = {
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": self._cost(model, input_tokens, output_tokens),
            "raw_response": raw,
        }

        stop_reason = raw.get("stop_reason")
        if stop_reason == "refusal":
            self.store.finish(job.id, db.FAILED, error=_error("claude_refused"), **record)
            return
        if stop_reason == "max_tokens":
            self.store.finish(job.id, db.FAILED, error=_error("response_truncated"), **record)
            return
        if process is None:
            self.store.finish(job.id, db.FAILED, error=_error("process_not_found"), **record)
            return
        text = "".join(
            block.get("text", "") for block in raw.get("content", []) if block.get("type") == "text"
        )
        try:
            validated = validate_output(process, json.loads(text))
        except json.JSONDecodeError, InvalidOutput:
            self.store.finish(job.id, db.FAILED, error=_error("invalid_json"), **record)
            return
        status = db.NEEDS_REVIEW if validated.issues else db.SUCCEEDED
        self.store.finish(job.id, status, data=validated.data, issues=validated.issues, **record)
        logger.info("ジョブ完了: %s %s", job.id, status)

    def _cost(self, model: str | None, input_tokens: int | None, output_tokens: int | None):
        price = self.settings.prices.get(model or "")
        if price is None or input_tokens is None or output_tokens is None:
            return None
        return round(
            (input_tokens * price.input_per_mtok + output_tokens * price.output_per_mtok)
            / 1_000_000,
            6,
        )

    async def _finish_safely(self, job: db.Job, status: str, **values) -> None:
        """結果の記録に失敗しても止まらない。残ったジョブは recover が立て直す。"""
        try:
            await asyncio.to_thread(self.store.finish, job.id, status, **values)
            logger.info(
                "ジョブ完了: %s %s %s", job.id, status, (values.get("error") or {}).get("code")
            )
        except Exception as error:
            logger.error("ジョブの記録に失敗: %s %s", job.id, type(error).__name__)


def _error(code: str, attempts: int | None = None) -> dict:
    error = {"code": code}
    if attempts is not None:
        error["attempts"] = attempts
    return error


def _write_atomic(path: Path, value: dict) -> None:
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)
