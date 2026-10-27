"""処理の定義（definition.py）が使う項目の型。

Claude には、どの項目も「帳票に書かれている文字」か null で書かせる。数値や日付への変換は
ここで行う。変換できない数値は検証の誤りとして扱う（その項目を null にし、元の文字を issues に
残す）。日付は、変換した値と元の文字の組（DateValue）で返す。変換できない日付は value を null に
し、元の文字を raw に残して、problem に理由を書く。
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    PlainSerializer,
    PrivateAttr,
    WithJsonSchema,
)

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


class DateValue(BaseModel):
    """日付の項目の値。value は読み取った日付、raw は帳票に書かれていた文字。"""

    value: date | None
    raw: str | None
    _problem: str | None = PrivateAttr(default=None)

    @property
    def problem(self) -> str | None:
        """raw を日付に変換できなかった理由。変換できたか、書かれていなければ None。"""
        return self._problem


def _to_date_value(value: Any) -> Any:
    if isinstance(value, DateValue):
        return value
    value = _blank_to_none(value)
    if value is None:
        return DateValue(value=None, raw=None)
    if not isinstance(value, str):
        raise ValueError("日付として読めない")
    try:
        return DateValue(value=_parse_date(value), raw=value)
    except ValueError as error:
        unreadable = DateValue(value=None, raw=value)
        unreadable._problem = str(error)
        return unreadable


def _parse_date(value: str) -> date:
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

Date = Annotated[DateValue, BeforeValidator(_to_date_value), _NULLABLE_STRING]
"""日付。西暦（2026-10-27、2026/10/27、2026年10月27日）と令和・平成を受け付ける。

結果では {"value": "2026-10-27", "raw": "令和8年10月27日"} の形になる。書かれていない日付は
value も raw も null。変換できない日付は value が null で、要確認になる。
"""
