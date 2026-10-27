"""閲覧画面（見るだけ）。

HTML 1枚だけを、キーなしで返す。データは含めず、開いた後にキーを入れてもらい、そのキーで
JSON の API を呼ぶ。中身は固定なので、起動時に1回だけ読み込む。
"""

import base64
import hashlib
import re
from importlib import resources

UI_PATH = "/ui"


def _hash(source: str) -> str:
    digest = hashlib.sha256(source.encode()).digest()
    return f"'sha256-{base64.b64encode(digest).decode()}'"


def _inline(html: str, tag: str) -> str:
    match = re.search(rf"<{tag}>(.*?)</{tag}>", html, re.DOTALL)
    assert match is not None, tag
    return match.group(1)


_HTML = resources.files(__package__).joinpath("ui.html").read_text(encoding="utf-8")
BODY = _HTML.encode()
HEADERS = [
    (b"content-type", b"text/html; charset=utf-8"),
    (b"cache-control", b"no-store"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (
        b"content-security-policy",
        (
            "default-src 'none'; "
            f"script-src {_hash(_inline(_HTML, 'script'))}; "
            f"style-src {_hash(_inline(_HTML, 'style'))}; "
            "connect-src 'self'; img-src blob:; frame-src blob:; object-src blob:; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        ).encode(),
    ),
]
