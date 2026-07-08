import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx

from news_summary.collectors import CollectionError
from news_summary.models import ArticleDraft, PressRelease, PressReleaseAsset, Source
from news_summary.scheduler import (
    AUTO_COLLECTION_ANOMALY_STATUS_KEY,
    DEFAULT_AUTO_COLLECT_LIMIT,
    AUTO_DAILY_REPORT_KEY,
    AUTO_OPERATIONS_SUMMARY_STATUS_KEY,
    AutoCollector,
    build_auto_collector_from_env,
    _collection_anomaly_snapshot,
    _next_hourly_run_at,
    _should_run_startup_catchup,
    _wait_seconds_until,
)
from news_summary.service import (
    collect_enabled_sources,
    collection_retention_cutoff_date,
    draft_pending_releases,
    draft_pending_releases_for_date,
    gemini_cooldown_message,
    gemini_cooldown_until,
    prune_decorative_press_release_assets,
    repair_missing_published_dates,
    retention_holidays,
)
from news_summary.storage import Store
from news_summary.writer import GeminiDraftError


def test_draft_pending_releases_keeps_item_pending_when_gemini_required(monkeypatch):
    db_path = Path(f"data/.test_service_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    release_id = store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 군청",
            region="전남",
            title="테스트 군, 새 사업 추진",
            url="https://example.com/press/1",
            content="테스트 군은 새 사업을 추진한다고 밝혔다. 신청은 다음 달부터 가능하다.",
            published_at="2026-05-20",
        )
    )
    assert release_id is not None

    def fail_gemini(item_id, item, model=None, require_gemini=False):
        assert require_gemini is True
        raise GeminiDraftError("Gemini 요청 한도가 찼습니다.", ["gemini-test"])

    monkeypatch.setattr("news_summary.service.generate_draft", fail_gemini)

    messages = draft_pending_releases(store, limit=5, require_gemini=True)

    assert "초안 보류" in messages[0]
    assert len(store.pending_press_releases(5)) == 1
    assert store.drafts(limit=5) == []


def test_draft_pending_releases_starts_cooldown_after_gemini_quota(monkeypatch):
    db_path = Path(f"data/.test_service_cooldown_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    for index in range(2):
        store.add_press_release(
            PressRelease(
                source_id="sample",
                source_name="테스트 군청",
                region="전남",
                title=f"테스트 군, 새 사업 추진 {index}",
                url=f"https://example.com/press/quota-{index}",
                content="테스트 군은 새 사업을 추진한다고 밝혔다. 신청은 다음 달부터 가능하다.",
                published_at="2026-05-20",
            )
        )
    calls = []

    def fail_quota(item_id, item, model=None, require_gemini=False):
        calls.append(item_id)
        raise GeminiDraftError("Gemini 요청 한도가 찼습니다.", ["gemini-3.5-flash"])

    monkeypatch.setattr("news_summary.service.DEFAULT_GEMINI_COOLDOWN_SECONDS", 60)
    monkeypatch.setattr("news_summary.service.generate_draft", fail_quota)

    messages = draft_pending_releases(store, limit=5, require_gemini=True)
    second_messages = draft_pending_releases(store, limit=5, require_gemini=True)

    assert len(calls) == 1
    assert any("초안 보류" in message for message in messages)
    assert any("초안 생성을 보류" in message for message in messages)
    assert len(second_messages) == 1
    assert "초안 생성을 보류" in second_messages[0]
    assert gemini_cooldown_until(store) is not None
    assert len(store.pending_press_releases(5)) == 2


def test_draft_pending_releases_records_generation_failure_queue(monkeypatch):
    db_path = Path(f"data/.test_service_draft_failure_queue_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 군청",
            region="전남",
            title="Gemini 실패 큐 테스트",
            url="https://example.com/press/failure-queue",
            content="테스트 군은 새 사업을 추진한다고 밝혔다.",
            published_at="2026-05-20",
        )
    )

    def fail_gemini(item_id, item, model=None, require_gemini=False):
        raise GeminiDraftError("Gemini 응답이 비어 있습니다.", ["gemini-3.5-flash"])

    monkeypatch.setenv("NEWS_SUMMARY_GEMINI_FAILURE_RETRY_SECONDS", "900")
    monkeypatch.setattr("news_summary.service.generate_draft", fail_gemini)

    first_messages = draft_pending_releases(store, limit=5, require_gemini=True)
    second_messages = draft_pending_releases(store, limit=5, require_gemini=True)
    summary = store.draft_generation_failure_summary()

    assert any("초안 보류" in message for message in first_messages)
    assert summary["total"] == 1
    assert summary["due"] == 0
    assert summary["by_kind"] == [{"kind": "generation_error", "count": 1}]
    assert "Gemini 실패 큐 재시도 대기 중" in second_messages[0]
    assert len(store.pending_press_releases(5)) == 1


def test_gemini_cooldown_message_uses_korean_time_label():
    cooldown_until = datetime(2026, 7, 1, 21, 39, tzinfo=timezone.utc)

    message = gemini_cooldown_message(cooldown_until)

    assert "UTC" not in message
    assert "한국 시간 2026.07.02 06:39" in message
    assert "초안 생성을 보류합니다." in message


def test_store_saves_and_replaces_press_release_assets():
    db_path = Path(f"data/.test_press_release_assets_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    release = PressRelease(
        source_id="sample",
        source_name="테스트 군청",
        region="전남",
        title="첨부 저장 테스트 원문",
        url="https://example.com/assets-release",
        content="테스트 군은 첨부 저장 기능을 점검한다고 밝혔다.",
        published_at="2026-05-20",
        assets=[
            PressReleaseAsset(
                url="https://example.com/photo.jpg",
                title="현장 사진",
                filename="photo.jpg",
                content_type="image/jpeg",
                asset_type="image",
                is_image=True,
            )
        ],
    )

    release_id = store.add_press_release(release)
    assert release_id is not None

    rows = store.press_release_assets(release_id)
    assert len(rows) == 1
    assert rows[0]["title"] == "현장 사진"
    assert rows[0]["is_image"] == 1

    store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 군청",
            region="전남",
            title="첨부 저장 테스트 원문 수정",
            url="https://example.com/assets-release",
            content="테스트 군은 첨부 저장 기능을 다시 점검한다고 밝혔다.",
            published_at="2026-05-20",
            assets=[
                PressReleaseAsset(
                    url="https://example.com/report.hwp",
                    title="보도자료 원문",
                    filename="report.hwp",
                    content_type="application/x-hwp",
                )
            ],
        )
    )

    rows = store.press_release_assets(release_id)
    assert len(rows) == 1
    assert rows[0]["url"] == "https://example.com/report.hwp"
    assert rows[0]["is_image"] == 0


def test_prune_decorative_press_release_assets_removes_stored_homepage_images():
    db_path = Path(f"data/.test_prune_decorative_assets_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    release_id = store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 군청",
            region="전남",
            title="첨부 정리 테스트 원문",
            url="https://example.com/cleanup-assets-release",
            content="테스트 군은 보도자료 첨부 이미지 정리 기능을 점검한다고 밝혔다.",
            published_at="2026-05-20",
            assets=[
                PressReleaseAsset(
                    url="https://example.com/images/main-banner.jpg",
                    title="홈페이지 상단 홍보 이미지",
                    filename="main-banner.jpg",
                    content_type="image/jpeg",
                    asset_type="image",
                    is_image=True,
                ),
                PressReleaseAsset(
                    url="https://example.com/upload/editor/press-photo.jpg",
                    title="현장 사진",
                    filename="press-photo.jpg",
                    content_type="image/jpeg",
                    asset_type="image",
                    is_image=True,
                ),
                PressReleaseAsset(
                    url="https://example.com/download?fileId=1&fileName=press.hwp",
                    title="보도자료 원문",
                    filename="press.hwp",
                    content_type="application/x-hwp",
                    asset_type="file",
                    is_image=False,
                ),
            ],
        )
    )
    assert release_id is not None

    result = prune_decorative_press_release_assets(store)
    rows = store.press_release_assets(release_id)

    assert result == {"checked": 2, "deleted": 1}
    assert [row["filename"] for row in rows] == ["press-photo.jpg", "press.hwp"]


def test_collect_enabled_sources_reports_source_progress(monkeypatch):
    db_path = Path(f"data/.test_service_progress_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    sources = [
        Source(id="one", name="첫 기관", region="전남", type="html_board"),
        Source(id="two", name="둘째 기관", region="전남", type="html_board"),
    ]

    def fake_collect(source, limit):
        return [
            PressRelease(
                source_id=source.id,
                source_name=source.name,
                region=source.region,
                title=f"{source.name} 보도자료",
                url=f"https://example.com/{source.id}/{uuid4().hex}",
                content=f"{source.name}은 새 사업을 추진한다고 밝혔다.",
            )
        ]

    events = []
    monkeypatch.setattr("news_summary.service.load_sources", lambda config_path: sources)
    monkeypatch.setattr("news_summary.service.collect_source", fake_collect)

    messages = collect_enabled_sources(store, Path("unused.yaml"), limit=3, progress_callback=events.append)

    assert "새 원문 2건" in messages[-1]
    assert any(event["message"].startswith("1/2 첫 기관") for event in events)
    assert any(event["message"].startswith("2/2 둘째 기관") for event in events)
    assert events[-1]["message"] == "수집 완료"


def test_collection_retention_cutoff_skips_weekends_and_holidays(monkeypatch):
    monkeypatch.setenv("NEWS_SUMMARY_RETENTION_DAYS", "3")
    monkeypatch.setenv("NEWS_SUMMARY_RETENTION_HOLIDAYS", "")

    assert collection_retention_cutoff_date(today=date(2026, 6, 29)) == date(2026, 6, 25)

    monkeypatch.setenv("NEWS_SUMMARY_RETENTION_HOLIDAYS", "2026-06-26")

    assert collection_retention_cutoff_date(today=date(2026, 6, 29)) == date(2026, 6, 24)


def test_retention_holidays_use_current_builtin_fallback_when_library_fails(monkeypatch):
    from news_summary import service as service_module

    class BrokenHolidayLibrary:
        @staticmethod
        def country_holidays(*args, **kwargs):
            raise ImportError("simulated holiday package failure")

    monkeypatch.setattr(service_module, "holidays_lib", BrokenHolidayLibrary)
    monkeypatch.setattr(service_module, "_LIBRARY_KOREA_HOLIDAYS_CACHE", {})
    monkeypatch.setattr(service_module, "_LIBRARY_KOREA_HOLIDAYS_FAILED_KEYS", set())
    monkeypatch.setenv("NEWS_SUMMARY_RETENTION_HOLIDAYS", "")

    holidays = retention_holidays({2026, 2027})

    assert date(2026, 5, 1) in holidays
    assert date(2026, 7, 17) in holidays
    assert date(2026, 9, 24) in holidays
    assert date(2026, 9, 25) in holidays
    assert date(2026, 10, 6) not in holidays
    assert date(2027, 5, 3) in holidays
    assert date(2027, 6, 7) in holidays
    assert date(2027, 12, 27) in holidays


def test_collection_retention_cutoff_skips_new_labor_day_public_holiday(monkeypatch):
    monkeypatch.setenv("NEWS_SUMMARY_RETENTION_DAYS", "2")
    monkeypatch.setenv("NEWS_SUMMARY_RETENTION_HOLIDAYS", "")

    assert collection_retention_cutoff_date(today=date(2026, 5, 4)) == date(2026, 4, 30)


def test_startup_catchup_runs_after_weekend_but_not_on_holiday():
    friday_finished = "2026-07-03T05:15:00+00:00"
    monday_noon = datetime(2026, 7, 6, 3, 0, tzinfo=timezone.utc)
    sunday_noon = datetime(2026, 7, 5, 3, 0, tzinfo=timezone.utc)

    assert _should_run_startup_catchup(friday_finished, now=monday_noon)
    assert not _should_run_startup_catchup(friday_finished, now=sunday_noon)


def test_startup_catchup_runs_when_hourly_runs_were_missed():
    previous = "2026-07-06T00:00:00+00:00"
    late_same_day = datetime(2026, 7, 6, 2, 45, tzinfo=timezone.utc)
    fresh_same_day = datetime(2026, 7, 6, 0, 50, tzinfo=timezone.utc)

    assert _should_run_startup_catchup(previous, now=late_same_day)
    assert not _should_run_startup_catchup(previous, now=fresh_same_day)


def test_auto_maintenance_drains_pending_queue(monkeypatch):
    db_path = Path(f"data/.test_auto_queue_drain_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 기관",
            region="전남",
            title="자동 큐 소진 원문",
            url="https://example.com/auto-queue-drain",
            content="자동 큐 소진으로 초안을 만들 원문입니다.",
            published_at="2026-07-06",
        )
    )

    def fake_generate_draft(item_id, item, require_gemini=False):
        return ArticleDraft(
            press_release_id=item_id,
            title=item.title,
            body="자동 큐 소진 초안입니다.",
            review_note="",
            model="gemini-3.5-flash:gemini",
        )

    monkeypatch.setenv("NEWS_SUMMARY_AUTO_QUEUE_DRAIN", "1")
    monkeypatch.setenv("NEWS_SUMMARY_AUTO_QUEUE_DRAIN_LIMIT", "1")
    monkeypatch.setenv("NEWS_SUMMARY_AUTO_RECOVERY_LIMIT", "0")
    monkeypatch.setattr("news_summary.scheduler.load_sources", lambda config_path: [])
    monkeypatch.setattr("news_summary.service.generate_draft", fake_generate_draft)

    collector = AutoCollector(store, Path("unused.yaml"), enabled=True, require_gemini=True)
    messages = collector._drain_pending_queue_once()

    assert any("Gemini 미변환 큐 자동 소진" in message for message in messages)
    assert store.pending_press_release_summary()["total"] == 0


def test_auto_maintenance_recovers_failed_sources(monkeypatch):
    db_path = Path(f"data/.test_auto_source_recovery_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    source = Source(id="sample", name="테스트 기관", region="전남", type="html_board")
    store.record_source_collection_status(
        source.id,
        source.name,
        "failed",
        "테스트 기관 수집 실패: TLS 연결 시간 초과",
        failure_stage="외부 사이트 응답 지연",
        failure_reason="TLS 연결 시간 초과",
    )
    with store.connect() as conn:
        conn.execute(
            "UPDATE source_collection_runs SET checked_at = ? WHERE source_id = ?",
            ("2000-01-01T00:00:00+00:00", source.id),
        )

    def fake_collect_source_with_fallback(source, limit):
        return [
            PressRelease(
                source_id=source.id,
                source_name=source.name,
                region=source.region,
                title="자동 복구 원문",
                url="https://example.com/auto-source-recovery",
                content="자동 복구 재검증으로 확인된 원문입니다.",
                published_at="2026-07-06",
            )
        ]

    monkeypatch.setenv("NEWS_SUMMARY_AUTO_RECOVERY_LIMIT", "1")
    monkeypatch.setattr("news_summary.scheduler.load_sources", lambda config_path: [source])
    monkeypatch.setattr("news_summary.scheduler.collect_source_with_fallback", fake_collect_source_with_fallback)
    monkeypatch.setattr("news_summary.scheduler.repair_missing_published_dates", lambda store, source, limit=20: 0)

    collector = AutoCollector(store, Path("unused.yaml"), enabled=True)
    messages = collector._recover_failed_sources_once()
    status = store.latest_source_collection_statuses()[source.id]

    assert any("자동 복구 재검증 통과" in message for message in messages)
    assert status["status"] == "ok"
    assert status["releases_found"] == 1


def test_auto_maintenance_skips_recent_transient_network_failures(monkeypatch):
    db_path = Path(f"data/.test_auto_source_recovery_cooldown_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    source = Source(id="sample", name="테스트 기관", region="전남", type="html_board")
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO source_collection_runs
            (source_id, source_name, status, message, failure_stage, failure_reason,
             releases_found, inserted_count, repaired_dates, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source.id,
                source.name,
                "failed",
                "테스트 기관 수집 실패: TLS 연결 시간 초과",
                "외부 사이트 응답 지연",
                "TLS 연결 시간 초과",
                0,
                0,
                0,
                "2026-07-07T00:50:00+00:00",
            ),
        )

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 7, 7, 10, 0, tzinfo=timezone(timedelta(hours=9)))
            return value if tz is None else value.astimezone(tz)

    calls = {"count": 0}

    def fake_collect_source_with_fallback(source, limit):
        calls["count"] += 1
        return []

    monkeypatch.setenv("NEWS_SUMMARY_AUTO_RECOVERY_LIMIT", "1")
    monkeypatch.setenv("NEWS_SUMMARY_AUTO_NETWORK_FAILURE_RECHECK_COOLDOWN_SECONDS", "21600")
    monkeypatch.setattr("news_summary.scheduler.datetime", FixedDatetime)
    monkeypatch.setattr("news_summary.scheduler.load_sources", lambda config_path: [source])
    monkeypatch.setattr("news_summary.scheduler.collect_source_with_fallback", fake_collect_source_with_fallback)

    collector = AutoCollector(store, Path("unused.yaml"), enabled=True)
    messages = collector._recover_failed_sources_once()

    assert messages == []
    assert calls["count"] == 0


def test_auto_maintenance_rechecks_quiet_business_day_sources(monkeypatch):
    db_path = Path(f"data/.test_auto_quiet_source_recheck_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    source = Source(id="quiet", name="조용한 기관", region="전남", type="html_board")
    store.record_source_collection_status(
        source.id,
        source.name,
        "ok",
        "원문 검증 통과 0건, 새로 저장 0건",
        releases_found=0,
    )

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 7, 6, 10, 0, tzinfo=timezone(timedelta(hours=9)))
            return value if tz is None else value.astimezone(tz)

    def fake_collect_source_with_fallback(source, limit):
        return [
            PressRelease(
                source_id=source.id,
                source_name=source.name,
                region=source.region,
                title="업무일 보정 원문",
                url="https://example.com/quiet-source-recheck",
                content="업무일 보정 재점검으로 확인된 원문입니다.",
                published_at="2026-07-06",
            )
        ]

    monkeypatch.setenv("NEWS_SUMMARY_AUTO_RECOVERY_LIMIT", "1")
    monkeypatch.setenv("NEWS_SUMMARY_AUTO_QUIET_SOURCE_RECHECK", "1")
    monkeypatch.setenv("NEWS_SUMMARY_AUTO_QUIET_SOURCE_RECHECK_HOUR", "9")
    monkeypatch.setattr("news_summary.scheduler.datetime", FixedDatetime)
    monkeypatch.setattr("news_summary.scheduler.load_sources", lambda config_path: [source])
    monkeypatch.setattr("news_summary.scheduler.collection_retention_cutoff_date", lambda today=None: date(2026, 7, 2))
    monkeypatch.setattr("news_summary.scheduler.collect_source_with_fallback", fake_collect_source_with_fallback)
    monkeypatch.setattr("news_summary.scheduler.repair_missing_published_dates", lambda store, source, limit=20: 0)

    collector = AutoCollector(store, Path("unused.yaml"), enabled=True)
    messages = collector._recover_failed_sources_once()
    status = store.latest_source_collection_statuses()[source.id]

    assert any("업무일 무수집 보정 점검 통과" in message for message in messages)
    assert status["status"] == "ok"
    assert status["inserted_count"] == 1


def test_auto_maintenance_focused_recrawls_anomaly_sources(monkeypatch):
    db_path = Path(f"data/.test_auto_focused_recrawl_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    source = Source(id="focus", name="집중 기관", region="전남", type="html_board")
    for index in range(2):
        store.add_press_release(
            PressRelease(
                source_id=source.id,
                source_name=source.name,
                region=source.region,
                title=f"이전 원문 {index}",
                url=f"https://example.com/focus-old-{index}",
                content="이전 영업일 원문입니다.",
                published_at="2026-07-03",
            )
        )

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 7, 6, 11, 0, tzinfo=timezone(timedelta(hours=9)))
            return value if tz is None else value.astimezone(tz)

    def fake_collect_source_with_fallback(source, limit):
        return [
            PressRelease(
                source_id=source.id,
                source_name=source.name,
                region=source.region,
                title="집중 재수집 원문",
                url="https://example.com/focused-recrawl",
                content="이상치 집중 재수집으로 확인된 원문입니다.",
                published_at="2026-07-06",
            )
        ]

    monkeypatch.setenv("NEWS_SUMMARY_AUTO_RECOVERY_LIMIT", "1")
    monkeypatch.setenv("NEWS_SUMMARY_AUTO_FOCUSED_RECRAWL_LIMIT", "1")
    monkeypatch.setenv("NEWS_SUMMARY_AUTO_ANOMALY_CHECK_HOUR", "10")
    monkeypatch.setenv("NEWS_SUMMARY_AUTO_QUIET_SOURCE_RECHECK", "0")
    monkeypatch.setattr("news_summary.scheduler.datetime", FixedDatetime)
    monkeypatch.setattr("news_summary.scheduler.load_sources", lambda config_path: [source])
    monkeypatch.setattr("news_summary.scheduler.collection_retention_cutoff_date", lambda today=None: date(2026, 7, 3))
    monkeypatch.setattr("news_summary.scheduler.collect_source_with_fallback", fake_collect_source_with_fallback)
    monkeypatch.setattr("news_summary.scheduler.repair_missing_published_dates", lambda store, source, limit=20: 0)

    collector = AutoCollector(store, Path("unused.yaml"), enabled=True)
    messages = collector._recover_failed_sources_once()
    anomaly = _collection_anomaly_snapshot(store, Path("unused.yaml"), now=FixedDatetime.now(timezone.utc))

    assert any("이상치 집중 재수집 통과" in message for message in messages)
    assert anomaly["issue_count"] == 0


def test_store_deduplicates_press_releases_by_title_date():
    db_path = Path(f"data/.test_storage_title_date_dedupe_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    first_id = store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 기관",
            region="전남",
            title="중복 제목",
            url="https://example.com/dedupe-title-1",
            content="첫 번째 원문입니다.",
            published_at="2026-07-06",
        )
    )
    second_id = store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 기관",
            region="전남",
            title="중복 제목",
            url="https://example.com/dedupe-title-2",
            content="두 번째 원문입니다.",
            published_at="2026-07-06",
        )
    )
    assert first_id is not None
    assert second_id is not None

    result = store.deduplicate_press_releases(limit=10)

    assert result["merged"] == 1
    with store.connect() as conn:
        rows = conn.execute("SELECT id, title, content FROM press_releases").fetchall()
    assert len(rows) == 1
    assert rows[0]["title"] == "중복 제목"


def test_collect_enabled_sources_keeps_only_retention_window(monkeypatch):
    db_path = Path(f"data/.test_service_retention_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    sources = [Source(id="sample", name="테스트 기관", region="전남", type="html_board")]

    def fake_collect(source, limit):
        return [
            PressRelease(
                source_id=source.id,
                source_name=source.name,
                region=source.region,
                title="보관 기준 이전 원문",
                url="https://example.com/retention-old",
                content="기준 이전 원문입니다.",
                published_at="2026-06-24",
            ),
            PressRelease(
                source_id=source.id,
                source_name=source.name,
                region=source.region,
                title="보관 기준 안 원문",
                url="https://example.com/retention-new",
                content="기준 안 원문입니다.",
                published_at="2026-06-25",
            ),
            PressRelease(
                source_id=source.id,
                source_name=source.name,
                region=source.region,
                title="게시일 없는 원문",
                url="https://example.com/retention-no-date",
                content="게시일 없는 원문입니다.",
                published_at=None,
            ),
        ]

    monkeypatch.setattr("news_summary.service.load_sources", lambda config_path: sources)
    monkeypatch.setattr("news_summary.service.collect_source", fake_collect)
    monkeypatch.setattr("news_summary.service.collection_retention_cutoff_date", lambda: date(2026, 6, 25))
    monkeypatch.setattr("news_summary.service.repair_missing_published_dates", lambda store, source, limit=20: 0)

    messages = collect_enabled_sources(store, Path("unused.yaml"), limit=3)

    with store.connect() as conn:
        titles = [row["title"] for row in conn.execute("SELECT title FROM press_releases ORDER BY title").fetchall()]
    status = store.latest_source_collection_statuses()["sample"]

    assert titles == ["게시일 없는 원문", "보관 기준 안 원문"]
    assert any("보관 기준 제외 1건" in message for message in messages)
    assert status["releases_found"] == 3
    assert status["inserted_count"] == 2


def test_store_deletes_old_press_releases_with_linked_drafts():
    db_path = Path(f"data/.test_storage_retention_delete_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    old_id = store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 기관",
            region="전남",
            title="오래된 원문",
            url="https://example.com/delete-old",
            content="오래된 원문입니다.",
            published_at="2026.06.24 10:00",
        )
    )
    new_id = store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 기관",
            region="전남",
            title="보관할 원문",
            url="https://example.com/delete-new",
            content="보관할 원문입니다.",
            published_at="2026-06-25",
        )
    )
    assert old_id is not None
    assert new_id is not None
    draft_id = store.add_article_draft(
        ArticleDraft(
            press_release_id=old_id,
            title="오래된 초안",
            body="본문입니다.",
            review_note="메모",
            model="gemini-3.5-flash:gemini",
        )
    )
    store.update_draft(draft_id, "오래된 초안 수정", "본문 수정", "메모", "needs_review")

    deleted = store.delete_press_releases_before("2026-06-25")

    assert deleted == {"press_releases": 1, "drafts": 1, "draft_history": 1}
    with store.connect() as conn:
        releases = conn.execute("SELECT title FROM press_releases").fetchall()
        drafts = conn.execute("SELECT title FROM article_drafts").fetchall()
        history = conn.execute("SELECT * FROM draft_history").fetchall()
    assert [row["title"] for row in releases] == ["보관할 원문"]
    assert drafts == []
    assert history == []


def test_collect_enabled_sources_classifies_connection_failures(monkeypatch):
    db_path = Path(f"data/.test_service_failure_reason_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    sources = [Source(id="timeout-source", name="응답 지연 기관", region="전남", type="html_board")]

    def fail_collect(source, limit):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr("news_summary.service.load_sources", lambda config_path: sources)
    monkeypatch.setattr("news_summary.service.collect_source", fail_collect)

    messages = collect_enabled_sources(store, Path("unused.yaml"), limit=3)
    status = store.latest_source_collection_statuses()["timeout-source"]

    assert "수집 실패" in messages[0]
    assert status["status"] == "failed"
    assert status["failure_stage"] == "외부 사이트 응답 지연"
    assert status["failure_reason"] == "응답 지연 또는 타임아웃"


def test_collect_enabled_sources_retries_transient_dns_failures(monkeypatch):
    db_path = Path(f"data/.test_service_dns_retry_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    sources = [Source(id="dns-source", name="DNS 임시 장애 기관", region="전남", type="html_board")]
    calls = {"count": 0}

    def collect_after_dns_retry(source, limit):
        calls["count"] += 1
        if calls["count"] == 1:
            raise httpx.ConnectError("[Errno 11002] getaddrinfo failed")
        return [
            PressRelease(
                source_id=source.id,
                source_name=source.name,
                region=source.region,
                title="DNS 복구 보도자료",
                url="https://example.com/dns-recovered",
                content="DNS 장애가 복구된 뒤 수집된 보도자료입니다.",
            )
        ]

    monkeypatch.setattr("news_summary.service.load_sources", lambda config_path: sources)
    monkeypatch.setattr("news_summary.service.collect_source", collect_after_dns_retry)
    monkeypatch.setattr("news_summary.service.repair_missing_published_dates", lambda store, source, limit=20: 0)
    monkeypatch.setattr("news_summary.service.TRANSIENT_COLLECTION_RETRY_DELAY_SECONDS", 0)

    messages = collect_enabled_sources(store, Path("unused.yaml"), limit=3)
    status = store.latest_source_collection_statuses()["dns-source"]

    assert calls["count"] == 2
    assert any("자동 재검증 통과" in message for message in messages)
    assert "새 원문 1건" in messages[-1]
    assert status["status"] == "ok"
    assert status["failure_stage"] == ""
    assert "자동 재검증 통과" in status["message"]


def test_collect_enabled_sources_retries_tls_timeouts(monkeypatch):
    db_path = Path(f"data/.test_service_tls_retry_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    sources = [Source(id="tls-source", name="TLS 지연 기관", region="전남", type="html_board")]
    calls = {"count": 0}

    def collect_after_tls_retry(source, limit):
        calls["count"] += 1
        if calls["count"] == 1:
            raise httpx.ConnectTimeout("TLS 연결 시간 초과")
        return [
            PressRelease(
                source_id=source.id,
                source_name=source.name,
                region=source.region,
                title="TLS 복구 보도자료",
                url="https://example.com/tls-recovered",
                content="TLS 연결 지연 뒤 다시 수집된 보도자료입니다.",
            )
        ]

    monkeypatch.setattr("news_summary.service.load_sources", lambda config_path: sources)
    monkeypatch.setattr("news_summary.service.collect_source", collect_after_tls_retry)
    monkeypatch.setattr("news_summary.service.repair_missing_published_dates", lambda store, source, limit=20: 0)
    monkeypatch.setattr("news_summary.service.TRANSIENT_COLLECTION_RETRY_DELAY_SECONDS", 0)

    messages = collect_enabled_sources(store, Path("unused.yaml"), limit=3)
    status = store.latest_source_collection_statuses()["tls-source"]

    assert calls["count"] == 2
    assert any("자동 재검증 통과" in message for message in messages)
    assert "새 원문 1건" in messages[-1]
    assert status["status"] == "ok"
    assert status["failure_stage"] == ""


def test_collect_enabled_sources_retries_http_503_failures(monkeypatch):
    db_path = Path(f"data/.test_service_http_503_retry_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    sources = [Source(id="http-source", name="HTTP 임시 장애 기관", region="전남", type="html_board")]
    calls = {"count": 0}

    def collect_after_http_retry(source, limit):
        calls["count"] += 1
        if calls["count"] == 1:
            request = httpx.Request("GET", "https://example.com/http-503")
            response = httpx.Response(503, request=request)
            raise httpx.HTTPStatusError("Service Unavailable", request=request, response=response)
        return [
            PressRelease(
                source_id=source.id,
                source_name=source.name,
                region=source.region,
                title="HTTP 복구 보도자료",
                url="https://example.com/http-recovered",
                content="HTTP 503 이후 다시 수집된 보도자료입니다.",
            )
        ]

    monkeypatch.setattr("news_summary.service.load_sources", lambda config_path: sources)
    monkeypatch.setattr("news_summary.service.collect_source", collect_after_http_retry)
    monkeypatch.setattr("news_summary.service.repair_missing_published_dates", lambda store, source, limit=20: 0)
    monkeypatch.setattr("news_summary.service.TRANSIENT_COLLECTION_RETRY_DELAY_SECONDS", 0)

    messages = collect_enabled_sources(store, Path("unused.yaml"), limit=3)
    status = store.latest_source_collection_statuses()["http-source"]

    assert calls["count"] == 2
    assert any("자동 재검증 통과" in message for message in messages)
    assert "새 원문 1건" in messages[-1]
    assert status["status"] == "ok"
    assert status["failure_stage"] == ""


def test_collect_enabled_sources_uses_fallback_url_after_structure_failure(monkeypatch):
    db_path = Path(f"data/.test_service_fallback_url_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    sources = [
        Source(
            id="fallback-source",
            name="대체 URL 기관",
            region="전남",
            type="html_board",
            list_url="https://example.com/old-board",
            fallback_urls=["https://example.com/new-board"],
        )
    ]
    requested_urls: list[str] = []

    def collect_after_fallback(source, limit):
        requested_urls.append(source.list_url)
        if source.list_url == "https://example.com/old-board":
            raise CollectionError("목록에서 보도자료 후보를 찾지 못했습니다. 사이트 구조 변경 가능성")
        return [
            PressRelease(
                source_id=source.id,
                source_name=source.name,
                region=source.region,
                title="대체 URL 보도자료",
                url="https://example.com/fallback-recovered",
                content="대체 URL로 수집된 보도자료입니다.",
            )
        ]

    monkeypatch.setattr("news_summary.service.load_sources", lambda config_path: sources)
    monkeypatch.setattr("news_summary.service.collect_source", collect_after_fallback)
    monkeypatch.setattr("news_summary.service.repair_missing_published_dates", lambda store, source, limit=20: 0)

    messages = collect_enabled_sources(store, Path("unused.yaml"), limit=3)
    status = store.latest_source_collection_statuses()["fallback-source"]

    assert requested_urls == ["https://example.com/old-board", "https://example.com/new-board"]
    assert "새 원문 1건" in messages[-1]
    assert status["status"] == "ok"
    assert status["releases_found"] == 1


def test_store_normalizes_existing_published_at_metadata():
    db_path = Path(f"data/.test_storage_dates_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    release_id = store.add_press_release(
        PressRelease(
            source_id="yeongam",
            source_name="영암군",
            region="전남",
            title="영암군 소식",
            url="https://example.com/date-normalize",
            content="영암군은 새 사업을 추진한다고 밝혔다.",
            published_at="(이용우 / 2026-05-20 14:03)",
        )
    )
    assert release_id is not None

    store.init_db()

    with store.connect() as conn:
        row = conn.execute("SELECT published_at FROM press_releases WHERE id = ?", (release_id,)).fetchone()
    assert row["published_at"] == "2026-05-20 14:03"


def test_store_canonicalizes_press_release_urls_for_duplicate_detection():
    db_path = Path(f"data/.test_storage_canonical_urls_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    first_id = store.add_press_release(
        PressRelease(
            source_id="gwangju-city",
            source_name="광주광역시청 보도자료",
            region="광주",
            title="광주시 보도자료",
            url="https://www.gwangju.go.kr/boardView.do?pageId=www789&boardId=BD_0000000027&seq=22205&movePage=1&recordCnt=15",
            content="광주시는 시민 생활 안전을 위해 관련 교육과 현장 점검을 추진한다고 밝혔다.",
            published_at="2026-06-26",
        )
    )
    second_id = store.add_press_release(
        PressRelease(
            source_id="gwangju-city",
            source_name="광주광역시청 보도자료",
            region="광주",
            title="광주시 보도자료 수정",
            url="https://www.gwangju.go.kr/boardView.do?pageId=www789&boardId=BD_0000000027&seq=22205&movePage=1&recordCnt=30",
            content="광주시는 시민 생활 안전을 위해 관련 교육과 현장 점검을 추진한다고 설명했다.",
            published_at="2026-06-26",
        )
    )

    assert first_id is not None
    assert second_id is None
    with store.connect() as conn:
        rows = conn.execute("SELECT title, url FROM press_releases").fetchall()
    assert len(rows) == 1
    assert rows[0]["title"] == "광주시 보도자료 수정"
    assert "recordCnt" not in rows[0]["url"]
    assert "movePage" not in rows[0]["url"]


def test_store_merges_existing_press_release_urls_after_canonicalization():
    db_path = Path(f"data/.test_storage_merge_canonical_urls_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    with store.connect() as conn:
        for record_count in (15, 30):
            conn.execute(
                """
                INSERT INTO press_releases
                (source_id, source_name, region, title, url, content, published_at, collected_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "gwangju-city",
                    "광주광역시청 보도자료",
                    "광주",
                    f"광주시 보도자료 {record_count}",
                    f"https://www.gwangju.go.kr/boardView.do?pageId=www789&boardId=BD_0000000027&seq=22205&movePage=1&recordCnt={record_count}",
                    "광주시는 시민 생활 안전을 위해 관련 교육과 현장 점검을 추진한다고 밝혔다.",
                    "2026-06-26",
                    "2026-06-26T10:00:00+00:00",
                ),
            )

    store.init_db()

    with store.connect() as conn:
        rows = conn.execute("SELECT title, url FROM press_releases").fetchall()
    assert len(rows) == 1
    assert rows[0]["title"] == "광주시 보도자료 30"
    assert "recordCnt" not in rows[0]["url"]


def test_store_configures_sqlite_for_concurrent_app_usage():
    db_path = Path(f"data/.test_storage_pragmas_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()

    with store.connect() as conn:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_store_reuses_existing_draft_for_same_press_release():
    db_path = Path(f"data/.test_storage_single_draft_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    release_id = store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 군청",
            region="전남",
            title="테스트 군, 새 사업 추진",
            url="https://example.com/single-draft",
            content="테스트 군은 새 사업을 추진한다고 밝혔다.",
            published_at="2026-05-20",
        )
    )
    assert release_id is not None

    first_id = store.add_article_draft(
        ArticleDraft(
            press_release_id=release_id,
            title="첫 초안",
            body="본문입니다.",
            review_note="메모",
            model="gemini-3.5-flash:gemini",
        )
    )
    second_id = store.add_article_draft(
        ArticleDraft(
            press_release_id=release_id,
            title="중복 초안",
            body="중복 본문입니다.",
            review_note="중복 메모",
            model="gemini-3.5-flash:gemini",
        )
    )

    assert second_id == first_id
    assert store.counts()["drafts"] == 1
    assert store.get_draft(first_id)["title"] == "첫 초안"


def test_pending_press_releases_are_sorted_by_published_time_not_id():
    db_path = Path(f"data/.test_storage_pending_release_sort_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 군청",
            region="전남",
            title="먼저 저장된 오래된 원문",
            url="https://example.com/pending-old",
            content="테스트 군은 오래된 원문을 안내한다고 밝혔다.",
            published_at="2026-05-20 09:00",
        )
    )
    store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 군청",
            region="전남",
            title="나중에 저장된 최신 원문",
            url="https://example.com/pending-new",
            content="테스트 군은 최신 원문을 안내한다고 밝혔다.",
            published_at="2026-05-20 10:00",
        )
    )
    store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 군청",
            region="전남",
            title="게시일 없는 원문",
            url="https://example.com/pending-no-date",
            content="테스트 군은 게시일 없는 원문을 안내한다고 밝혔다.",
            published_at=None,
        )
    )

    assert [row["title"] for row in store.pending_press_releases(limit=10)] == [
        "나중에 저장된 최신 원문",
        "먼저 저장된 오래된 원문",
        "게시일 없는 원문",
    ]


def test_draft_pending_releases_for_date_filters_date_and_supports_oldest_first(monkeypatch):
    db_path = Path(f"data/.test_service_draft_date_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    older_id = store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 군청",
            region="전남",
            title="오전 보도자료",
            url="https://example.com/date-old",
            content="테스트 군은 오전 보도자료를 안내한다고 밝혔다.",
            published_at="2026-06-26 09:00",
        )
    )
    newer_id = store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 군청",
            region="전남",
            title="오후 보도자료",
            url="https://example.com/date-new",
            content="테스트 군은 오후 보도자료를 안내한다고 밝혔다.",
            published_at="2026-06-26 15:00",
        )
    )
    other_date_id = store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 군청",
            region="전남",
            title="전날 보도자료",
            url="https://example.com/date-other",
            content="테스트 군은 전날 보도자료를 안내한다고 밝혔다.",
            published_at="2026-06-25 17:00",
        )
    )
    assert older_id is not None
    assert newer_id is not None
    assert other_date_id is not None
    generated_titles = []

    def fake_generate_draft(item_id, item, model=None, require_gemini=False):
        assert require_gemini is True
        generated_titles.append(item.title)
        return ArticleDraft(
            press_release_id=item_id,
            title=f"{item.title} 초안",
            body="첫 문단입니다.\n\n둘째 문단입니다.\n\n셋째 문단입니다.",
            review_note="메모",
            model="gemini-3.5-flash:gemini",
        )

    monkeypatch.setattr("news_summary.service.generate_draft", fake_generate_draft)

    messages = draft_pending_releases_for_date(
        store,
        "2026-06-26",
        limit=10,
        require_gemini=True,
        oldest_first=True,
    )

    assert generated_titles == ["오전 보도자료", "오후 보도자료"]
    assert messages == ["초안 #1 생성: 오전 보도자료 초안", "초안 #2 생성: 오후 보도자료 초안"]
    assert store.count_pending_press_releases_for_date("2026-06-26") == 0
    assert store.count_pending_press_releases_for_date("2026-06-25") == 1


def test_drafts_are_sorted_by_draft_activity_time_not_id():
    db_path = Path(f"data/.test_storage_draft_sort_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    newer_release_id = store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 군청",
            region="전남",
            title="먼저 저장된 원문",
            url="https://example.com/draft-sort-newer",
            content="테스트 군은 먼저 저장된 원문 내용을 안내한다고 밝혔다.",
            published_at="2026-05-20",
        )
    )
    older_release_id = store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 군청",
            region="전남",
            title="나중에 저장된 원문",
            url="https://example.com/draft-sort-older",
            content="테스트 군은 나중에 저장된 원문 내용을 안내한다고 밝혔다.",
            published_at="2026-05-20",
        )
    )
    assert newer_release_id is not None
    assert older_release_id is not None
    store.add_article_draft(
        ArticleDraft(
            press_release_id=newer_release_id,
            title="생성일이 최신인 초안",
            body="본문입니다.",
            review_note="메모",
            model="gemini-3.5-flash:gemini",
            created_at="2026-05-20T10:00:00+00:00",
        )
    )
    store.add_article_draft(
        ArticleDraft(
            press_release_id=older_release_id,
            title="id는 크지만 생성일이 오래된 초안",
            body="본문입니다.",
            review_note="메모",
            model="gemini-3.5-flash:gemini",
            created_at="2026-05-20T09:00:00+00:00",
        )
    )

    assert [row["title"] for row in store.recent_drafts(limit=10)] == [
        "생성일이 최신인 초안",
        "id는 크지만 생성일이 오래된 초안",
    ]
    assert [row["title"] for row in store.drafts(limit=10)] == [
        "생성일이 최신인 초안",
        "id는 크지만 생성일이 오래된 초안",
    ]
    assert [row["title"] for row in store.drafts_by_source("sample", limit=10)] == [
        "생성일이 최신인 초안",
        "id는 크지만 생성일이 오래된 초안",
    ]


def test_store_keeps_existing_published_at_when_recrawl_has_no_date():
    db_path = Path(f"data/.test_storage_keep_date_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    url = "https://example.com/keep-date"
    release_id = store.add_press_release(
        PressRelease(
            source_id="shinan",
            source_name="신안군",
            region="전남",
            title="신안군 소식",
            url=url,
            content="신안군은 새 사업을 추진한다고 밝혔다.",
            published_at="2026-05-11 17:28:00",
        )
    )
    assert release_id is not None

    store.add_press_release(
        PressRelease(
            source_id="shinan",
            source_name="신안군",
            region="전남",
            title="신안군 소식",
            url=url,
            content="신안군은 새 사업을 다시 안내했다.",
            published_at=None,
        )
    )

    with store.connect() as conn:
        row = conn.execute("SELECT published_at FROM press_releases WHERE id = ?", (release_id,)).fetchone()
    assert row["published_at"] == "2026-05-11 17:28:00"


def test_press_releases_are_sorted_by_published_time_not_collection_order():
    db_path = Path(f"data/.test_storage_release_sort_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 군청",
            region="전남",
            title="먼저 수집된 오래된 보도자료",
            url="https://example.com/old-first",
            content="테스트 군은 먼저 수집된 오래된 보도자료를 안내한다고 밝혔다.",
            published_at="2026-05-19 09:00",
            collected_at="2026-05-20T10:00:00+00:00",
        )
    )
    store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 군청",
            region="전남",
            title="나중에 수집된 최신 보도자료",
            url="https://example.com/new-second",
            content="테스트 군은 나중에 수집된 최신 보도자료를 안내한다고 밝혔다.",
            published_at="2026-05-20 08:00",
            collected_at="2026-05-20T09:00:00+00:00",
        )
    )
    store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 군청",
            region="전남",
            title="게시일 없는 원문",
            url="https://example.com/no-date",
            content="테스트 군은 게시일이 없는 원문을 안내한다고 밝혔다.",
            published_at=None,
            collected_at="2026-05-21T09:00:00+00:00",
        )
    )

    releases = store.press_releases(limit=10)
    source_releases = store.press_releases_by_source("sample", limit=10)

    expected = ["나중에 수집된 최신 보도자료", "먼저 수집된 오래된 보도자료", "게시일 없는 원문"]
    assert [row["title"] for row in releases] == expected
    assert [row["title"] for row in source_releases] == expected


def test_repair_missing_published_dates_reads_detail_registration_date(monkeypatch):
    db_path = Path(f"data/.test_repair_dates_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    release_id = store.add_press_release(
        PressRelease(
            source_id="shinan-county",
            source_name="신안군청 보도자료/해명",
            region="전남 신안",
            title="신안군 소식",
            url="https://example.com/shinan/141205",
            content="신안군은 나눔 봉사를 진행했다고 밝혔다.",
            published_at=None,
        )
    )
    assert release_id is not None

    class FakeResponse:
        text = """
        <table class="show_form">
          <tr><th><label>등록일</label></th><td><span>2026-05-08 13:08:00</span></td></tr>
          <tr><th><label>내용</label></th><td>본문</td></tr>
        </table>
        """

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url):
            assert url == "https://example.com/shinan/141205"
            return FakeResponse()

    monkeypatch.setattr("news_summary.service.httpx.Client", FakeClient)
    source = Source(
        id="shinan-county",
        name="신안군청 보도자료/해명",
        region="전남 신안",
        type="html_board",
        selectors={"detail_published_at": ".show_form"},
    )

    repaired = repair_missing_published_dates(store, source)

    assert repaired == 1
    with store.connect() as conn:
        row = conn.execute("SELECT published_at FROM press_releases WHERE id = ?", (release_id,)).fetchone()
    assert row["published_at"] == "2026-05-08 13:08:00"


def test_auto_collector_tracks_last_automatic_finish_separately(monkeypatch):
    db_path = Path(f"data/.test_auto_collect_metadata_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()

    monkeypatch.setattr("news_summary.scheduler.load_sources", lambda config_path: [])
    monkeypatch.setattr("news_summary.scheduler.collect_and_draft_cycle", lambda *args, **kwargs: ["수집 완료"])

    collector = AutoCollector(store, Path("unused.yaml"))
    collector.run_once(label="수동 재수집")

    assert collector.snapshot().last_finished_at is not None
    assert collector.snapshot().last_auto_finished_at is None

    collector.run_once(label="자동 수집")
    last_auto_finished_at = collector.snapshot().last_auto_finished_at

    assert last_auto_finished_at is not None
    restored = AutoCollector(store, Path("unused.yaml"))
    assert restored.snapshot().last_auto_finished_at == last_auto_finished_at


def test_auto_collector_refreshes_operations_snapshots_after_collection(monkeypatch):
    db_path = Path(f"data/.test_auto_collect_report_refresh_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    source = Source(id="gangjin-county", name="강진군청 보도자료", region="전남 강진", type="html_board")

    store.add_press_release(
        PressRelease(
            source_id=source.id,
            source_name=source.name,
            region=source.region,
            title="이전 강진 원문",
            url="https://example.com/gangjin-old",
            content="이전 업무일 원문입니다.",
            published_at="2026-07-07",
        )
    )
    store.record_source_collection_status(
        source.id,
        source.name,
        "failed",
        "강진군청 보도자료 수집 실패",
        failure_stage="외부 사이트 응답 지연",
        failure_reason="TLS 연결 시간 초과",
    )
    store.set_app_metadata(
        AUTO_COLLECTION_ANOMALY_STATUS_KEY,
        json.dumps({"issue_count": 1, "status_level": "warning", "issues": [{"source_id": source.id}]}),
    )

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 7, 8, 12, 0, tzinfo=timezone(timedelta(hours=9)))
            return value if tz is None else value.astimezone(tz)

    def fake_collect_and_draft_cycle(*args, **kwargs):
        store.add_press_release(
            PressRelease(
                source_id=source.id,
                source_name=source.name,
                region=source.region,
                title="오늘 강진 원문",
                url="https://example.com/gangjin-today",
                content="오늘 수집된 원문입니다.",
                published_at="2026-07-08",
            )
        )
        store.record_source_collection_status(
            source.id,
            source.name,
            "ok",
            "원문 검증 통과 1건, 새로 저장 1건",
            releases_found=1,
            inserted_count=1,
        )
        return ["수집 완료"]

    monkeypatch.setattr("news_summary.scheduler.datetime", FixedDatetime)
    monkeypatch.setattr("news_summary.scheduler.load_sources", lambda config_path: [source])
    monkeypatch.setattr("news_summary.scheduler.collect_and_draft_cycle", fake_collect_and_draft_cycle)

    collector = AutoCollector(store, Path("unused.yaml"))
    collector.run_once(label="자동 수집")

    anomaly = json.loads(store.get_app_metadata(AUTO_COLLECTION_ANOMALY_STATUS_KEY) or "{}")
    daily_report = json.loads(store.get_app_metadata(AUTO_DAILY_REPORT_KEY) or "{}")
    operations_summary = json.loads(store.get_app_metadata(AUTO_OPERATIONS_SUMMARY_STATUS_KEY) or "{}")

    assert anomaly["issue_count"] == 0
    assert daily_report["today_releases"] == 1
    assert daily_report["failed_sources"] == 0
    assert operations_summary["anomaly_count"] == 0
    assert operations_summary["today_active_sources"] == 1


def test_auto_collector_waits_until_the_next_hourly_boundary():
    exact_hour = datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc)
    middle_of_hour = datetime(2026, 5, 21, 9, 20, 10, tzinfo=timezone.utc)
    almost_next_hour = datetime(2026, 5, 21, 9, 59, 59, 500000, tzinfo=timezone.utc)

    assert _next_hourly_run_at(exact_hour) == exact_hour
    assert _next_hourly_run_at(middle_of_hour) == datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc)
    assert _wait_seconds_until(_next_hourly_run_at(middle_of_hour), now=middle_of_hour) == 2390
    assert _wait_seconds_until(_next_hourly_run_at(almost_next_hour), now=almost_next_hour) == 1


def test_auto_collector_interval_is_fixed_to_hourly_boundary(monkeypatch):
    db_path = Path(f"data/.test_auto_collect_fixed_interval_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    monkeypatch.setenv("NEWS_SUMMARY_AUTO_INTERVAL_SECONDS", "600")
    monkeypatch.setattr("news_summary.scheduler.load_sources", lambda config_path: [])

    collector = build_auto_collector_from_env(store, Path("unused.yaml"))

    assert collector is not None
    assert collector.snapshot().interval_seconds == 3600
    assert collector.snapshot().collect_limit == DEFAULT_AUTO_COLLECT_LIMIT


def test_auto_collector_default_collect_limit_covers_full_board_pages(monkeypatch):
    db_path = Path(f"data/.test_auto_collect_default_limit_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    monkeypatch.delenv("NEWS_SUMMARY_AUTO_COLLECT_LIMIT", raising=False)
    monkeypatch.setattr("news_summary.scheduler.load_sources", lambda config_path: [])

    collector = build_auto_collector_from_env(store, Path("unused.yaml"))

    assert collector is not None
    assert collector.snapshot().collect_limit == 30


def test_auto_collector_enabled_state_can_be_persisted(monkeypatch):
    db_path = Path(f"data/.test_auto_collect_enabled_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    monkeypatch.setenv("NEWS_SUMMARY_AUTO_COLLECT", "false")
    monkeypatch.setattr("news_summary.scheduler.load_sources", lambda config_path: [])

    collector = build_auto_collector_from_env(store, Path("unused.yaml"))

    assert collector is not None
    assert collector.snapshot().enabled is False

    collector.set_enabled(True)
    restored = build_auto_collector_from_env(store, Path("unused.yaml"))

    assert restored is not None
    assert restored.snapshot().enabled is True
