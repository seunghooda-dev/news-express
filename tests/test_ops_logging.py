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


def test_configure_logging_replaces_previous_file_handler(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWS_SUMMARY_LOG_LEVEL", "INFO")
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_log = configure_logging(first_dir)
    logger = get_logger("test")
    logger.info("first log entry")

    second_log = configure_logging(second_dir)
    logger.info("second log entry")

    assert first_log == first_dir / "news_summary.log"
    assert second_log == second_dir / "news_summary.log"
    assert "first log entry" in first_log.read_text(encoding="utf-8")
    assert "second log entry" not in first_log.read_text(encoding="utf-8")
    assert "second log entry" in second_log.read_text(encoding="utf-8")
