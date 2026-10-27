"""前処理（PDFの画像化・回転・透過の合成）を子プロセスで実行する。

変換ライブラリが止まったり暴走したりしてもサービスを巻き込まないよう、別プロセスで動かし、
時間を超えたらプロセスごと（孫プロセスも含めて）終了させる。
"""

import contextlib
import os
import signal
import subprocess
import sys
from pathlib import Path

from . import render_worker
from .settings import Settings


class RenderError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def render_pages(source: Path, out_dir: Path, settings: Settings) -> list[Path]:
    """ページごとの PNG のパスを返す。失敗したら RenderError。"""
    args = [
        str(source),
        str(out_dir),
        str(settings.max_pages),
        str(settings.render_dpi),
        str(settings.image_long_edge),
        str(settings.render_timeout_seconds),
    ]
    command = [sys.executable, "-m", "document_extraction_api.render_worker", *args]
    returncode = run_with_timeout(command, settings.render_timeout_seconds + 5)
    if returncode is None or returncode == render_worker.EXIT_TIMEOUT:
        raise RenderError("render_timeout")
    if returncode == render_worker.EXIT_TOO_MANY_PAGES:
        raise RenderError("too_many_pages")
    if returncode != 0:
        raise RenderError("unreadable_file")
    pages = sorted(out_dir.glob("page-*.png"))
    if not pages:
        raise RenderError("unreadable_file")
    return pages


def run_with_timeout(command: list[str], timeout: float) -> int | None:
    """子プロセスを実行し、終了コードを返す。時間切れならプロセスツリーごと終了して None。"""
    if os.name == "nt":
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    else:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        return None
    except BaseException:
        _kill_tree(process)
        raise


def _kill_tree(process: subprocess.Popen) -> None:
    if os.name == "nt":
        # venv の python.exe は本体を孫プロセスとして起動するため、/T でツリーごと終了する
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
