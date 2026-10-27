"""Claude API への接続。"""

import base64
from dataclasses import dataclass
from typing import Protocol

import anthropic

from .settings import Settings, Site


class ClaudeUnavailable(Exception):
    """Claude から使える応答が得られなかった（接続・タイムアウト・APIエラー）。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ClaudeResponse:
    raw: dict
    """Claude の応答そのもの（課金済みの結果として保存する）。"""


class Extractor(Protocol):
    async def extract(
        self, site: Site, model: str, prompt: str, schema: dict, pages: list[bytes]
    ) -> ClaudeResponse: ...


class AnthropicExtractor:
    def __init__(self, settings: Settings, http_client=None):
        self._settings = settings
        self._http_client = http_client
        self._clients: dict[str, anthropic.AsyncAnthropic] = {}

    def _client(self, site: Site) -> anthropic.AsyncAnthropic:
        if site.name not in self._clients:
            self._clients[site.name] = anthropic.AsyncAnthropic(
                api_key=site.claude_api_key,
                timeout=self._settings.claude_timeout_seconds,
                max_retries=self._settings.claude_max_retries,
                http_client=self._http_client,
            )
        return self._clients[site.name]

    async def extract(
        self, site: Site, model: str, prompt: str, schema: dict, pages: list[bytes]
    ) -> ClaudeResponse:
        content: list[dict] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg" if page.startswith(b"\xff\xd8") else "image/png",
                    "data": base64.standard_b64encode(page).decode("ascii"),
                },
            }
            for page in pages
        ]
        content.append({"type": "text", "text": prompt})
        try:
            async with self._client(site).messages.stream(
                model=model,
                max_tokens=self._settings.max_tokens,
                output_config={
                    "effort": self._settings.effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
                messages=[{"role": "user", "content": content}],
            ) as stream:
                message = await stream.get_final_message()
        except anthropic.APITimeoutError as error:
            raise ClaudeUnavailable("claude_timeout") from error
        except anthropic.APIConnectionError as error:
            raise ClaudeUnavailable("claude_connection_error") from error
        except anthropic.APIStatusError as error:
            raise ClaudeUnavailable(f"claude_http_{error.status_code}") from error
        except anthropic.AnthropicError as error:
            # 認証情報が見つからない、応答を解釈できない、など
            raise ClaudeUnavailable("claude_error") from error
        return ClaudeResponse(raw=message.to_dict())
