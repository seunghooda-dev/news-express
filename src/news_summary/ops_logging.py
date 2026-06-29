from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
DEFAULT_LOG_DIR = Path("data/logs")
DEFAULT_LOG_FILE = "news_summary.log"


def configure_logging(log_dir: str | Path | None = None) -> Path:
    target_dir = Path(log_dir or os.getenv("NEWS_SUMMARY_LOG_DIR") or DEFAULT_LOG_DIR)
    target_dir.mkdir(parents=True, exist_ok=True)
    log_path = target_dir / DEFAULT_LOG_FILE
    level_name = os.getenv("NEWS_SUMMARY_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger("news_summary")
    root.setLevel(level)
    root.propagate = False

    resolved = str(log_path.resolve())
    for handler in list(root.handlers):
        if isinstance(handler, RotatingFileHandler) and handler.baseFilename == resolved:
            handler.setLevel(level)
            handler.setFormatter(logging.Formatter(LOG_FORMAT))
            return log_path
        if isinstance(handler, RotatingFileHandler):
            root.removeHandler(handler)
            handler.close()

    handler = RotatingFileHandler(
        log_path,
        maxBytes=_env_int("NEWS_SUMMARY_LOG_MAX_BYTES", 2_000_000),
        backupCount=_env_int("NEWS_SUMMARY_LOG_BACKUP_COUNT", 5),
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.setLevel(level)
    root.addHandler(handler)
    root.info("logging configured path=%s level=%s", log_path, logging.getLevelName(level))
    return log_path


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"news_summary.{name}")


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default
