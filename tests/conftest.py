import os
import time
from pathlib import Path

import pytest

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture(scope="session", autouse=True)
def sweep_leftover_test_databases():
    """테스트가 만든 `data/.test_*.sqlite`를 세션 끝에 걷는다.

    많은 테스트가 `Path(f"data/.test_..._{uuid4().hex}.sqlite")`로 실제 data/ 에
    DB를 만들고 지우지 않는다. 실행마다 수십 개씩 쌓여 **25,986개 · 3.9GB**까지
    갔고(2026-08-11 실측) 디렉터리 조작이 느려져 게이트 시간이 270초에서 600초로
    늘었다. 시작 시점 목록과 비교해 **이번 세션이 만든 것만** 지운다.
    """
    # 시작할 때는 **오래 묵은 것**을 걷는다. 윈도우에서는 연결이 열린 채면 세션
    # 끝에 못 지우는 것이 몇 개씩 남는데, "이번 세션이 만든 것"만 보면 그것들이
    # 영영 안 걷힌다. 1시간 문턱을 두어 동시에 도는 다른 실행은 건드리지 않는다.
    _sweep(lambda entry: time.time() - entry.stat().st_mtime > 3600)
    before = {entry.name for entry in os.scandir(DATA_DIR)} if DATA_DIR.is_dir() else set()
    yield
    _sweep(lambda entry: entry.name not in before)


def _sweep(should_remove) -> None:
    if not DATA_DIR.is_dir():
        return
    for entry in os.scandir(DATA_DIR):
        name = entry.name
        if entry.is_dir() or not (name.startswith(".test_") and ".sqlite" in name):
            continue
        try:
            if should_remove(entry):
                os.unlink(entry.path)
        except OSError:
            # 열려 있으면 못 지운다 — 다음 세션이 문턱을 넘겨 걷는다.
            pass


@pytest.fixture(autouse=True)
def isolate_auth_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("NEWS_SUMMARY_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("NEWS_SUMMARY_ADMIN_PASSWORD_HASH", raising=False)
    # .env가 값을 정의할 수 있으므로 삭제 대신 빈 값으로 고정한다.
    # (load_dotenv는 이미 설정된 변수는 덮어쓰지 않는다.)
    monkeypatch.setenv("NEWS_SUMMARY_OPERATIONS_PASSWORD", "")
    monkeypatch.setenv("NEWS_SUMMARY_OPERATIONS_PASSWORD_HASH", "")
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_REQUIRED", "0")
    # 여기만 delenv였다. 지우면 create_app의 load_dotenv가 .env의 값을 **되살려서**,
    # "인증 켜고 확인" 하려고 AUTH_REQUIRED=1만 설정한 테스트가 조용히 인증이 꺼진
    # 채로 통과한다(2026-08-11 실측). 바로 위 주석이 말하는 이유와 같으므로 맞춘다.
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_DISABLED", "")
    monkeypatch.delenv("NEWS_SUMMARY_TEST_OPERATIONS_AUTH", raising=False)
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("NEWS_SUMMARY_DATABASE_URL", "")
    monkeypatch.setenv("NEWS_SUMMARY_COLLECT_EXCLUDE_SOURCES", "")
    monkeypatch.setenv("NEWS_SUMMARY_LOG_DIR", str(tmp_path / "logs"))
    # 썸네일 디스크 캐시가 실제 data/previews를 공유하면 테스트끼리 오염된다 —
    # 이전 실행이 남긴 파일을 다음 실행이 DISK 히트로 읽어 다운로드 검증이 깨진다.
    monkeypatch.setenv("NEWS_SUMMARY_PREVIEW_CACHE_DIR", str(tmp_path / "previews"))
