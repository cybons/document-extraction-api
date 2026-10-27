"""DB に記録し、HTTP で返せる値にそろえる。

Claude の応答から作った値は、どんな形でも例外を出さずにここを通す。記録で出る例外を DB の障害
だけにするため（値が原因の例外は、立て直しでやり直しても同じところで止まる）。
"""

import math
from typing import Any

MAX_DEPTH = 32
"""入れ子の深さの上限。帳票の形は数段で足りる。FastAPI の変換（再帰）が止まらない深さに抑える。"""
MAX_REPORTED_PATHS = 5
"""置き換えた箇所のうち、指摘に書く数。"""


def storable(value: Any) -> tuple[Any, list[str]]:
    """value を、記録して返せる値にそろえる。

    UTF-8 にできない文字列、有限でない数、深すぎる入れ子、JSON にない型は null に置き換える。
    文字列でないか UTF-8 にできないキーの項目は落とす。戻り値は、そろえた値と置き換えた箇所。
    """
    replaced: list[str] = []
    return _walk(value, "", 0, replaced), replaced


def _walk(node: Any, path: str, depth: int, replaced: list[str]) -> Any:
    if node is None or isinstance(node, bool):
        return node
    if isinstance(node, int):
        return node if _fits_json(node) else _replace(path, replaced)
    if isinstance(node, float):
        return node if math.isfinite(node) else _replace(path, replaced)
    if isinstance(node, str):
        return node if _is_utf8(node) else _replace(path, replaced)
    if isinstance(node, dict | list | tuple):
        if depth >= MAX_DEPTH:
            return _replace(path, replaced)
        if isinstance(node, dict):
            result = {}
            for key, child in node.items():
                if not (isinstance(key, str) and _is_utf8(key)):
                    _replace(path or "$", replaced)
                    continue
                result[key] = _walk(child, f"{path}.{key}" if path else key, depth + 1, replaced)
            return result
        return [_walk(child, f"{path}[{i}]", depth + 1, replaced) for i, child in enumerate(node)]
    return _replace(path, replaced)


def _replace(path: str, replaced: list[str]) -> None:
    replaced.append(path)
    return None


def _is_utf8(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _fits_json(number: int) -> bool:
    try:
        str(number)
    except ValueError:  # 桁数が int と str の変換の上限を超える
        return False
    return True


def replaced_issue(replaced: list[str]) -> dict:
    """置き換えた箇所をまとめた指摘。箇所の名前も、記録できる文字だけで書く。"""
    shown = [_safe_path(path) for path in replaced[:MAX_REPORTED_PATHS]]
    rest = len(replaced) - len(shown)
    message = "記録できない値を null にした: " + ", ".join(shown)
    if rest:
        message += f" ほか {rest} 件"
    return {"path": "", "raw": None, "message": message}


def _safe_path(path: str) -> str:
    path = path or "$"
    return path if _is_utf8(path) else path.encode("utf-8", "backslashreplace").decode("utf-8")
