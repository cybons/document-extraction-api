"""処理の定義（definition.py）が使う項目の型。

Claude には、どの項目も「帳票に書かれている文字」か null で書かせる。数値や日付への変換は
ここで行い、変換できない値は検証の誤りとして扱う（その項目を null にし、元の文字を残す）。
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, PlainSerializer, WithJsonSchema

_NULLABLE_STRING = WithJsonSchema({"anyOf": [{"type": "string"}, {"type": "null"}]})


class Record(BaseModel):
    """出力の形を書くときの基底クラス。"""

    model_config = ConfigDict(extra="ignore")


@dataclass(frozen=True)
class Issue:
    """処理の検証（check）が返す指摘。1件でもあればジョブは要確認になる。"""

    path: str
    message: str


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip()


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str) and not _normalize(value):
        return None
    return value


def _parse_text(value: Any) -> Any:
    return _blank_to_none(value)


def _parse_number(value: Any) -> Any:
    value = _blank_to_none(value)
    if value is None or isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError("数値として読めない")
    if isinstance(value, int | float):
        return Decimal(str(value))
    if not isinstance(value, str):
        raise ValueError("数値として読めない")
    text = re.sub(r"[,\s]", "", _normalize(value))
    if not re.fullmatch(r"[+-]?(\d+(\.\d*)?|\.\d+)", text):
        raise ValueError("数値として読めない")
    try:
        return Decimal(text)
    except InvalidOperation as error:
        raise ValueError("数値として読めない") from error


_ERAS = {"令和": 2018, "R": 2018, "平成": 1988, "H": 1988}
_DATE_PATTERNS = (
    re.compile(r"(?P<y>\d{4})[-/.](?P<m>\d{1,2})[-/.](?P<d>\d{1,2})"),
    re.compile(r"(?P<y>\d{4})年(?P<m>\d{1,2})月(?P<d>\d{1,2})日"),
    re.compile(
        r"(?P<era>令和|平成|R|H)(?P<y>\d{1,2}|元)[年./-](?P<m>\d{1,2})[月./-](?P<d>\d{1,2})日?"
    ),
)


def _parse_date(value: Any) -> Any:
    value = _blank_to_none(value)
    if value is None or isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError("日付として読めない")
    text = re.sub(r"\s", "", _normalize(value))
    for pattern in _DATE_PATTERNS:
        match = pattern.fullmatch(text)
        if not match:
            continue
        parts = match.groupdict()
        year = 1 if parts["y"] == "元" else int(parts["y"])
        if era := parts.get("era"):
            year += _ERAS[era]
        try:
            return date(year, int(parts["m"]), int(parts["d"]))
        except ValueError as error:
            raise ValueError("日付として読めない") from error
    raise ValueError("日付として読めない")


Text = Annotated[str | None, BeforeValidator(_parse_text), _NULLABLE_STRING]
"""文字のまま扱う項目。"""

Number = Annotated[
    Decimal | None,
    BeforeValidator(_parse_number),
    PlainSerializer(lambda v: None if v is None else str(v), return_type=str | None),
    _NULLABLE_STRING,
]
"""数量・金額など。桁区切りのカンマと全角数字は受け付ける。結果では文字列の数値になる。"""

Date = Annotated[date | None, BeforeValidator(_parse_date), _NULLABLE_STRING]
"""日付。西暦（2026-10-27、2026/10/27、2026年10月27日）と令和・平成を受け付ける。"""
