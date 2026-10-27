"""認証と受付枠。

FastAPI の依存関係はアップロードの本文を読んだ後に動くため、ASGI のミドルウェアで先に判定する。
認証のない要求には、本文を読まずに 401 を返す（health などのパスも同じ）。
"""

import asyncio
import json

from .settings import Site, hash_client_key

UPLOAD_PATH = "/api/v1/documents/extract"


class _BodyTooLarge(Exception):
    pass


class AuthMiddleware:
    def __init__(self, app, sites: dict[str, Site], upload_slots: int, max_body_bytes: int):
        self.app = app
        self.site_by_hash = {
            h: site.name for site in sites.values() for h in site.client_key_hashes
        }
        self.slots = asyncio.Semaphore(upload_slots)
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        key = _header(scope, b"x-api-key")
        site = self.site_by_hash.get(hash_client_key(key)) if key else None
        if site is None:
            await _respond(send, 401, "unauthorized", "APIキーがないか、正しくない")
            return
        scope.setdefault("state", {})["site"] = site

        if scope["path"] != UPLOAD_PATH:
            await self.app(scope, receive, send)
            return

        length = _header(scope, b"content-length")
        if length is not None and (not length.isdigit() or int(length) > self.max_body_bytes):
            await _respond(send, 413, "file_too_large", "ファイルが大きすぎる")
            return
        if self.slots.locked():
            await _respond(send, 503, "busy", "受付が混み合っている。少し待って送り直す")
            return
        async with self.slots:
            await self._limited(scope, receive, send)

    async def _limited(self, scope, receive, send):
        received = 0
        too_large = False

        async def limited_receive():
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    too_large = True
                    raise _BodyTooLarge()
            return message

        async def guarded_send(message):
            # 上限を超えた後にアプリが返そうとする応答（500 など）は捨て、413 に置き換える
            if not too_large:
                await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not too_large:
                raise
        if too_large:
            await _respond(send, 413, "file_too_large", "ファイルが大きすぎる")


def _header(scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")
    return None


async def _respond(send, status: int, code: str, message: str) -> None:
    body = json.dumps({"error": {"code": code, "message": message}}, ensure_ascii=False).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"connection", b"close")],
        }
    )
    await send({"type": "http.response.body", "body": body})
