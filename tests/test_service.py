from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from news_summary.models import PressRelease, Source
from news_summary.scheduler import AutoCollector, _next_auto_wait_seconds
from news_summary.service import collect_enabled_sources, draft_pending_releases, gemini_cooldown_until, repair_missing_published_dates
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


def test_auto_collector_startup_wait_uses_last_successful_auto_finish():
    now = datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc)
    recent = (now - timedelta(minutes=20)).isoformat()
    stale = (now - timedelta(hours=2)).isoformat()

    assert _next_auto_wait_seconds(recent, 3600, now=now) == 2400
    assert _next_auto_wait_seconds(stale, 3600, now=now) == 0
    assert _next_auto_wait_seconds(None, 3600, now=now) == 0
