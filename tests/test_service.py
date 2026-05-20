from pathlib import Path
from uuid import uuid4

from news_summary.models import PressRelease, Source
from news_summary.service import collect_enabled_sources, draft_pending_releases
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
