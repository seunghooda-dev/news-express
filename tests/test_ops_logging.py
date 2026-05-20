from pathlib import Path

from news_summary.ops_logging import configure_logging, get_logger


def test_configure_logging_writes_rotating_log(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWS_SUMMARY_LOG_LEVEL", "INFO")
    log_path = configure_logging(tmp_path)
    logger = get_logger("test")

    logger.info("logging smoke test")

    assert log_path == Path(tmp_path) / "news_summary.log"
    assert log_path.exists()
    assert "logging smoke test" in log_path.read_text(encoding="utf-8")
