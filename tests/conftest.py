import pytest


@pytest.fixture(autouse=True)
def isolate_auth_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("NEWS_SUMMARY_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("NEWS_SUMMARY_ADMIN_PASSWORD_HASH", raising=False)
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_REQUIRED", "0")
    monkeypatch.delenv("NEWS_SUMMARY_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("NEWS_SUMMARY_DATABASE_URL", "")
    monkeypatch.setenv("NEWS_SUMMARY_LOG_DIR", str(tmp_path / "logs"))
