import pytest


@pytest.fixture(autouse=True)
def isolate_auth_environment(monkeypatch):
    monkeypatch.delenv("NEWS_SUMMARY_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("NEWS_SUMMARY_ADMIN_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("NEWS_SUMMARY_AUTH_REQUIRED", raising=False)
    monkeypatch.delenv("NEWS_SUMMARY_AUTH_DISABLED", raising=False)
