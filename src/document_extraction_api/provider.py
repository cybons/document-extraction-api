import asyncio
import base64
import json
import random
from decimal import Decimal

import anthropic
from pydantic import ValidationError

from .errors import JobError
from .tasks import TASKS


class ClaudeProvider:
    def __init__(self, settings, store):
        self.settings = settings
        self.store = store
        self.client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            max_retries=0,
            timeout=settings.request_seconds,
        )

    async def close(self):
        await self.client.close()

    async def extract(self, job, images):
        snapshot = json.loads(job["snapshot"])
        task = TASKS[job["task"]]
        if snapshot["version"] != task.version:
            raise JobError("task_version_unavailable")
        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.b64encode(image.read_bytes()).decode("ascii"),
                },
            }
            for image in images
        ]
        content.append({"type": "text", "text": "この帳票を指定されたスキーマで抽出してください。"})
        price = snapshot["price"]
        for number in range(1, self.settings.max_attempts + 1):
            await asyncio.to_thread(
                self.store.attempt,
                job["id"],
                number,
                snapshot["model"],
                None,
                None,
                price,
                "started_usage_unknown",
            )
            response = None
            outcome = "provider_error"
            retry = False
            delay = min(30, 2**number + random.random())
            try:
                async with asyncio.timeout(self.settings.request_seconds):
                    response = await self.client.messages.create(
                        model=snapshot["model"],
                        max_tokens=self.settings.max_tokens,
                        system=snapshot["prompt"],
                        messages=[{"role": "user", "content": content}],
                        output_config={
                            "format": {"type": "json_schema", "schema": snapshot["schema"]}
                        },
                    )
                outcome = "response_received"
            except anthropic.APIConnectionError, TimeoutError:
                outcome, retry = "connection_or_timeout", True
            except anthropic.APIStatusError as exc:
                retry = exc.status_code == 429 or exc.status_code >= 500
                outcome = "upstream_" + str(exc.status_code)
                try:
                    delay = min(60, max(delay, float(exc.response.headers.get("retry-after", 0))))
                except ValueError:
                    pass
            usage = (
                (response.usage.input_tokens, response.usage.output_tokens) if response else None
            )
            cost = None
            if usage and all(v is not None for v in price.values()):
                cost = str(
                    (
                        Decimal(usage[0]) * Decimal(price["input"])
                        + Decimal(usage[1]) * Decimal(price["output"])
                    )
                    / Decimal(1_000_000)
                )
            await asyncio.to_thread(
                self.store.attempt,
                job["id"],
                number,
                snapshot["model"],
                usage,
                cost,
                price,
                outcome,
            )
            if response is not None:
                if response.stop_reason != "end_turn":
                    raise JobError("incomplete_or_refused_output")
                text = "".join(b.text for b in response.content if b.type == "text")
                try:
                    return task.schema.model_validate_json(text)
                except ValidationError as exc:
                    raise JobError("invalid_extraction") from exc
            if not retry or number == self.settings.max_attempts:
                raise JobError(outcome)
            await asyncio.sleep(delay)
        raise JobError("provider_error")
