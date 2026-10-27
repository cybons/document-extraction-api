"""起動の入口。

    python -m document_extraction_api serve <設定ファイル>
    python -m document_extraction_api new-key

起動の失敗（設定の誤り、ポート使用中、多重起動）は、必ずログに残してから終了する。
"""

import logging
import logging.handlers
import os
import secrets
import socket
import sys
from pathlib import Path

from .settings import Settings, SettingsError, hash_client_key, load_settings

logger = logging.getLogger("document_extraction_api")


class StartupError(Exception):
    pass


def setup_logging(log_file: Path | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
        )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
        force=True,
    )
    # SDK のログには要求の中身が出ることがあるため、警告以上に絞る
    for name in ("anthropic", "httpx2", "httpx", "httpcore", "multipart", "python_multipart"):
        logging.getLogger(name).setLevel(logging.WARNING)


class InstanceLock:
    """同じデータフォルダでの多重起動を防ぐ。プロセスが終われば OS が解放する。"""

    def __init__(self, path: Path):
        self.path = path
        self._file = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = self.path.open("a+")
        try:
            if os.name == "nt":
                import msvcrt

                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            f.close()
            raise StartupError(f"すでに起動している（ロック: {self.path}）") from error
        self._file = f

    def release(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


def bind_socket(host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == "nt":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
        sock.listen(128)
    except OSError as error:
        sock.close()
        raise StartupError(f"ポート {host}:{port} を使えない（使用中の可能性）: {error}") from error
    return sock


def prepare(settings: Settings):
    """起動の準備。失敗したら StartupError か SettingsError を投げる。"""
    from .api import create_app
    from .claude import AnthropicExtractor
    from .processes import ProcessError, load_processes

    lock = InstanceLock(settings.data_dir / "service.lock")
    lock.acquire()
    try:
        try:
            registry = load_processes(settings.processes_dir, list(settings.sites))
        except ProcessError as error:
            raise StartupError(f"処理の定義を読み込めない: {error}") from error
        app = create_app(settings, registry, AnthropicExtractor(settings))
        sock = bind_socket(settings.host, settings.port)
    except BaseException:
        lock.release()
        raise
    return app, sock, lock


def serve(settings_path: Path) -> int:
    setup_logging(None)
    try:
        settings = load_settings(settings_path)
    except SettingsError as error:
        logger.error("起動に失敗: %s", error)
        return 1
    setup_logging(settings.log_file)
    try:
        app, sock, lock = prepare(settings)
    except (StartupError, SettingsError) as error:
        logger.error("起動に失敗: %s", error)
        return 1
    except Exception as error:
        logger.error("起動に失敗: %s", type(error).__name__)
        return 1

    import uvicorn

    logger.info("起動: %s:%s", settings.host, settings.port)
    try:
        server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=True))
        server.run(sockets=[sock])
    finally:
        lock.release()
    logger.info("停止")
    return 0


def new_key() -> int:
    key = secrets.token_urlsafe(32)
    print(f"呼び出し側に渡すキー: {key}")
    print(f"設定ファイルに書く値（client_key_sha256）: {hash_client_key(key)}")
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["serve"] and len(argv) == 2:
        return serve(Path(argv[1]))
    if argv == ["new-key"]:
        return new_key()
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
