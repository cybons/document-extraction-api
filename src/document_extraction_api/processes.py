"""拠点ごとの処理（processes/<拠点>/<タスク>/）を読み込み、Claude の出力を検証する。

各フォルダには prompt.md（Claude への指示文）と definition.py を置く。definition.py には
TASK_ID、出力の形 Output（fields.Record の派生）、任意で check(data) -> list[Issue] を書く。

ジョブを受け付けたときに、処理の中身をジョブのフォルダへ写す（write_snapshot）。ワーカーは
その写しで処理するので、受け付けた後に処理を直したり消したりしても、そのジョブは受け付けた
ときの版で読む。
"""

import copy
import hashlib
import types
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from . import fields

_FIELDS_SOURCE = Path(fields.__file__)
PROMPT_FILE = "prompt.md"
DEFINITION_FILE = "definition.py"


class ProcessError(Exception):
    pass


@dataclass(frozen=True)
class Process:
    site: str
    task_id: str
    name: str
    prompt: str
    output: type[BaseModel]
    check: Callable[[Any], list[fields.Issue]] | None
    schema: dict
    digest: str
    sources: dict[str, bytes]
    """読み込んだときの prompt.md と definition.py の中身。"""

    @property
    def version(self) -> str:
        return f"{self.site}/{self.name}@{self.digest}"


@dataclass(frozen=True)
class Validated:
    data: dict | None
    issues: list[dict]


class InvalidOutput(Exception):
    """出力の構造が形に合わない（JSON として使えない）。"""


class ProcessRegistry:
    def __init__(self, processes: list[Process]):
        self._by_key: dict[tuple[str, str], Process] = {}
        for process in processes:
            key = (process.site, process.task_id)
            if key in self._by_key:
                raise ProcessError(f"拠点 {process.site} にタスク {process.task_id} が2つある")
            self._by_key[key] = process

    def get(self, site: str, task_id: str) -> Process | None:
        return self._by_key.get((site, task_id))

    def __iter__(self):
        return iter(self._by_key.values())


def load_processes(root: Path, sites: list[str]) -> ProcessRegistry:
    processes = []
    for site in sites:
        site_dir = root / site
        if not site_dir.is_dir():
            continue
        for task_dir in sorted(p for p in site_dir.iterdir() if p.is_dir()):
            if (task_dir / DEFINITION_FILE).exists():
                processes.append(load_process(site, task_dir))
    return ProcessRegistry(processes)


def load_process(site: str, task_dir: Path) -> Process:
    prompt_path = task_dir / PROMPT_FILE
    definition_path = task_dir / DEFINITION_FILE
    if not prompt_path.exists():
        raise ProcessError(f"{task_dir} に {PROMPT_FILE} がない")
    sources = {PROMPT_FILE: prompt_path.read_bytes(), DEFINITION_FILE: definition_path.read_bytes()}
    # 読んだ中身そのものを実行する（版の計算と写しに使う中身と、実行する中身をそろえる）
    module = types.ModuleType(f"_processes.{site}.{task_dir.name}")
    module.__file__ = str(definition_path)
    exec(compile(sources[DEFINITION_FILE], str(definition_path), "exec"), module.__dict__)

    task_id = getattr(module, "TASK_ID", None)
    output = getattr(module, "Output", None)
    check = getattr(module, "check", None)
    if not isinstance(task_id, str) or not task_id:
        raise ProcessError(f"{definition_path} に TASK_ID がない")
    if not (isinstance(output, type) and issubclass(output, BaseModel)):
        raise ProcessError(f"{definition_path} に Output（fields.Record の派生）がない")

    digest = hashlib.sha256()
    for source in (sources[PROMPT_FILE], sources[DEFINITION_FILE], _FIELDS_SOURCE.read_bytes()):
        digest.update(source)
    return Process(
        site=site,
        task_id=task_id,
        name=task_dir.name,
        prompt=sources[PROMPT_FILE].decode("utf-8"),
        output=output,
        check=check,
        schema=claude_schema(output),
        digest=digest.hexdigest()[:12],
        sources=sources,
    )


def write_snapshot(process: Process, directory: Path) -> None:
    """処理の中身をジョブのフォルダへ写す。"""
    target = directory / process.name
    target.mkdir(parents=True)
    for name, source in process.sources.items():
        (target / name).write_bytes(source)


def load_snapshot(site: str, directory: Path) -> Process:
    """write_snapshot で写した処理を読み込む。"""
    folders = [path for path in directory.iterdir() if path.is_dir()]
    if len(folders) != 1:
        raise ProcessError(f"{directory} に処理の写しが1つだけあるはず")
    return load_process(site, folders[0])


def claude_schema(output: type[BaseModel]) -> dict:
    """Claude の構造化出力に渡す JSON Schema を作る。

    すべてのオブジェクトで、全項目を required にし、additionalProperties を false にする。
    構造化出力が対応しない title・default などの注記は落とす。
    """

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(item) for item in node]
        if not isinstance(node, dict):
            return node
        result = {k: walk(v) for k, v in node.items() if k not in ("title", "default")}
        if result.get("type") == "object" and "properties" in node:
            result["properties"] = {k: walk(v) for k, v in node["properties"].items()}
            result["required"] = list(node["properties"])
            result["additionalProperties"] = False
        return result

    return walk(output.model_json_schema())


def validate_output(process: Process, raw: Any) -> Validated:
    """Claude の出力を定義で検証する。

    値が形に合わない項目は null にし、元の値を issues に残す。日付は変換できなくても元の文字を
    data に残し、issues にも入れる。そのうえで check の指摘を足す。
    構造が合わないときは InvalidOutput を投げる。
    """
    data = copy.deepcopy(raw)
    issues: list[dict] = []
    try:
        model = process.output.model_validate(data)
    except ValidationError as error:
        for err in error.errors(include_url=False):
            loc = err["loc"]
            try:
                original = _get(raw, loc)
                _set(data, loc, None)
            except (KeyError, IndexError, TypeError) as missing:
                raise InvalidOutput() from missing
            issues.append({"path": _format_path(loc), "raw": original, "message": _message(err)})
        try:
            model = process.output.model_validate(data)
        except ValidationError as again:
            raise InvalidOutput() from again
    for loc, value in _unreadable_dates(model, ()):
        issues.append({"path": _format_path(loc), "raw": value.raw, "message": value.problem})
    if process.check is not None:
        issues.extend(_check(process, model))
    return Validated(model.model_dump(mode="json"), issues)


def _check(process: Process, model: BaseModel) -> list[dict]:
    """処理の check を呼ぶ。check は処理ごとに書くコードなので、止まっても戻り値が正しくなくても
    課金済みの結果は捨てず、その旨を指摘にして要確認にする。"""
    try:
        returned = list(process.check(model))
    except Exception as error:
        message = f"check が例外で止まった: {type(error).__name__}"
        return [{"path": "", "raw": None, "message": message}]
    if not all(
        isinstance(issue, fields.Issue)
        and isinstance(issue.path, str)
        and isinstance(issue.message, str)
        for issue in returned
    ):
        message = "check の戻り値が Issue（path と message が文字）でない"
        return [{"path": "", "raw": None, "message": message}]
    return [{"path": issue.path, "raw": None, "message": issue.message} for issue in returned]


def _unreadable_dates(node: Any, loc: tuple):
    if isinstance(node, fields.DateValue):
        if node.problem is not None:
            yield loc, node
    elif isinstance(node, BaseModel):
        for name in type(node).model_fields:
            yield from _unreadable_dates(getattr(node, name), (*loc, name))
    elif isinstance(node, list | tuple):
        for index, item in enumerate(node):
            yield from _unreadable_dates(item, (*loc, index))


def _get(node: Any, loc: tuple) -> Any:
    for key in loc:
        node = node[key]
    return node


def _set(node: Any, loc: tuple, value: Any) -> None:
    if not loc:
        raise TypeError("ルートは置き換えられない")
    for key in loc[:-1]:
        node = node[key]
    if isinstance(node, dict) and loc[-1] not in node:
        raise KeyError(loc[-1])
    node[loc[-1]] = value


def _format_path(loc: tuple) -> str:
    path = ""
    for key in loc:
        path += f"[{key}]" if isinstance(key, int) else (f".{key}" if path else str(key))
    return path


def _message(err: dict) -> str:
    ctx_error = err.get("ctx", {}).get("error")
    return str(ctx_error) if ctx_error is not None else err["msg"]
