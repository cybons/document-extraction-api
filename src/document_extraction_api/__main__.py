import logging
from logging.handlers import RotatingFileHandler

import uvicorn

from .app import create_app
from .config import Settings


def main():
    try:
        settings = Settings()
    except Exception:
        raise SystemExit("Invalid configuration. Check .env against .env.example.") from None
    log_dir = settings.data_dir.resolve() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "service.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.getLogger("document_extraction")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    # Third-party HTTP logs may contain headers; never enable debug in production.
    for name in ("httpx", "httpcore", "anthropic"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        workers=1,
        access_log=False,
        log_level="critical",
    )


if __name__ == "__main__":
    main()
