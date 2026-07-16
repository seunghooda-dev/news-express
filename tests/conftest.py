import pytest


@pytest.fixture(autouse=True)
def isolate_auth_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("NEWS_SUMMARY_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("NEWS_SUMMARY_ADMIN_PASSWORD_HASH", raising=False)
    # .env가 값을 정의할 수 있으므로 삭제 대신 빈 값으로 고정한다.
    # (load_dotenv는 이미 설정된 변수는 덮어쓰지 않는다.)
    monkeypatch.setenv("NEWS_SUMMARY_OPERATIONS_PASSWORD", "")
    monkeypatch.setenv("NEWS_SUMMARY_OPERATIONS_PASSWORD_HASH", "")
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_REQUIRED", "0")
    monkeypatch.delenv("NEWS_SUMMARY_AUTH_DISABLED", raising=False)
    monkeypatch.delenv("NEWS_SUMMARY_TEST_OPERATIONS_AUTH", raising=False)
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("NEWS_SUMMARY_DATABASE_URL", "")
    monkeypatch.setenv("NEWS_SUMMARY_COLLECT_EXCLUDE_SOURCES", "")
    monkeypatch.setenv("NEWS_SUMMARY_LOG_DIR", str(tmp_path / "logs"))
