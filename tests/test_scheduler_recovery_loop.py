"""자동 복구 루프 — 한 곳이 터져도 나머지 26곳은 계속 봐야 한다.

`scheduler.py` 691~704(소스별 예외 처리)은 커버리지 0이었다(2026-08-12 실측).
여기서 예외가 새면 **먼저 걸린 지자체 하나 때문에 그 회차의 복구가 통째로
멈춘다** — 정작 이 루프는 깨진 소스를 되살리려고 있는 것이다.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from news_summary import scheduler
from news_summary.models import PressRelease, Source
from news_summary.scheduler import (
    AUTO_RECOVERY_LIMIT_ENV,
    AUTO_RECOVERY_STATUS_KEY,
    AutoCollector,
    SourceRecoveryCandidate,
)
from news_summary.storage import Store


def make_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / f"recovery_{uuid4().hex}.sqlite")
    store.init_db()
    return store


def make_source(source_id: str, name: str) -> Source:
    return Source(
        id=source_id,
        name=name,
        region="전남",
        type="html",
        base_url=f"https://{source_id}.go.kr",
        list_url=f"https://{source_id}.go.kr/board",
    )


def _within_retention_date() -> str:
    """보관 기간(영업일 3일) 밖 날짜는 복구가 저장 전에 걸러낸다 — 오늘(KST)로 시간 의존을 없앤다."""
    return datetime.now(timezone(timedelta(hours=9))).date().isoformat()


def make_release(source: Source) -> PressRelease:
    return PressRelease(
        source_id=source.id,
        source_name=source.name,
        region=source.region,
        title=f"{source.name} 소식",
        url=f"https://{source.id}.go.kr/{uuid4().hex}",
        content="본문입니다.",
        published_at=_within_retention_date(),
        assets=[],
    )


@pytest.fixture
def two_sources(monkeypatch):
    broken = make_source("gangjin-county", "강진군청 보도자료")
    healthy = make_source("damyang-county", "담양군청 보도자료")
    monkeypatch.setenv(AUTO_RECOVERY_LIMIT_ENV, "5")
    monkeypatch.setattr(
        scheduler,
        "_source_recovery_candidates",
        lambda store, config_path, limit: [
            SourceRecoveryCandidate(broken, "failed"),
            SourceRecoveryCandidate(healthy, "failed"),
        ],
    )
    monkeypatch.setattr(scheduler, "repair_missing_published_dates", lambda *a, **k: 0)
    return broken, healthy


def test_one_broken_source_does_not_stop_the_others(tmp_path, monkeypatch, two_sources):
    broken, healthy = two_sources
    store = make_store(tmp_path)

    def collect(source, limit):
        if source.id == broken.id:
            raise httpx.ConnectError("사이트가 응답하지 않는다")
        return [make_release(source)]

    monkeypatch.setattr(scheduler, "collect_source_with_fallback", collect)
    collector = AutoCollector(store, Path("unused.yaml"), enabled=True)

    messages = collector._recover_failed_sources_once()

    assert len(messages) == 2, f"터진 소스에서 루프가 멈췄다 — {messages}"
    assert any("강진군청" in message and "실패" in message for message in messages)
    assert any("담양군청" in message and "통과" in message for message in messages)


def test_failure_is_recorded_so_the_source_stays_visible(tmp_path, monkeypatch, two_sources):
    """실패를 기록하지 않으면 운영 화면에서 그 소스가 조용해진다 — 가장 나쁜 상태다."""
    broken, healthy = two_sources
    store = make_store(tmp_path)

    def collect(source, limit):
        if source.id == broken.id:
            raise httpx.ConnectError("사이트가 응답하지 않는다")
        return [make_release(source)]

    monkeypatch.setattr(scheduler, "collect_source_with_fallback", collect)
    collector = AutoCollector(store, Path("unused.yaml"), enabled=True)

    collector._recover_failed_sources_once()

    statuses = {
        str(row["source_id"]): str(row["status"])
        for row in store.recent_source_run_statuses(per_source=1)
    }
    assert statuses[broken.id] == "failed"
    assert statuses[healthy.id] == "ok"


def test_recovered_releases_are_actually_saved(tmp_path, monkeypatch, two_sources):
    """복구 재수집이 기사를 저장하지 않으면 '통과'라는 말만 남는다."""
    broken, healthy = two_sources
    store = make_store(tmp_path)
    monkeypatch.setattr(
        scheduler, "collect_source_with_fallback", lambda source, limit: [make_release(source)]
    )
    collector = AutoCollector(store, Path("unused.yaml"), enabled=True)

    collector._recover_failed_sources_once()

    saved = {str(row["source_id"]) for row in store.press_releases(limit=10)}
    assert saved == {broken.id, healthy.id}


def test_recovery_snapshot_is_written_for_the_operations_screen(tmp_path, monkeypatch, two_sources):
    broken, healthy = two_sources
    store = make_store(tmp_path)
    monkeypatch.setattr(
        scheduler, "collect_source_with_fallback", lambda source, limit: [make_release(source)]
    )
    collector = AutoCollector(store, Path("unused.yaml"), enabled=True)

    collector._recover_failed_sources_once()

    assert store.get_app_metadata(AUTO_RECOVERY_STATUS_KEY), "복구 결과가 화면에 안 간다"


def test_recovery_can_be_turned_off(tmp_path, monkeypatch):
    """상한 0이면 후보 조회조차 하지 않는다 — 끄면 정말 꺼져야 한다."""
    store = make_store(tmp_path)
    monkeypatch.setenv(AUTO_RECOVERY_LIMIT_ENV, "0")
    called = []
    monkeypatch.setattr(
        scheduler,
        "_source_recovery_candidates",
        lambda *a, **k: called.append(1) or [],
    )
    collector = AutoCollector(store, Path("unused.yaml"), enabled=True)

    assert collector._recover_failed_sources_once() == []
    assert called == [], "꺼 뒀는데 후보를 조회했다"
