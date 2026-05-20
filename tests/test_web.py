from datetime import date
from pathlib import Path
from uuid import uuid4

from news_summary.models import ArticleDraft, PressRelease
from news_summary.scheduler import AutoCollectorStatus
from news_summary.storage import Store
from news_summary.web import (
    _date_warning,
    _filter_drafts_by_review,
    _filter_drafts_by_date,
    _filter_drafts_by_query,
    _group_drafts_by_recent_dates,
    _sort_drafts_latest_first,
    approval_checks,
    format_datetime_label,
    interval_label,
    model_label,
    review_flags,
)


def test_group_drafts_by_recent_dates_builds_five_daily_categories():
    drafts = [
        {
            "id": 1,
            "published_at": "2026-05-20 15:36",
            "created_at": "2026-05-20T07:00:00+00:00",
        },
        {
            "id": 2,
            "published_at": "2026-05-19",
            "created_at": "2026-05-20T07:00:00+00:00",
        },
        {
            "id": 3,
            "published_at": None,
            "created_at": "2026-05-18T15:00:00+00:00",
        },
        {
            "id": 4,
            "published_at": "2026-05-10",
            "created_at": "2026-05-20T07:00:00+00:00",
        },
    ]

    groups = _group_drafts_by_recent_dates(drafts, today=date(2026, 5, 20), days=5)

    assert [group["label"] for group in groups] == [
        "2026년 5월 20일 (오늘)",
        "2026년 5월 19일",
        "2026년 5월 18일",
        "2026년 5월 17일",
        "2026년 5월 16일",
    ]
    assert [len(group["drafts"]) for group in groups] == [1, 1, 1, 0, 0]
    assert groups[0]["iso_date"] == "2026-05-20"


def test_filter_drafts_by_date_uses_published_date_first():
    drafts = [
        {
            "id": 1,
            "published_at": "2026-05-20",
            "created_at": "2026-05-19T23:00:00+00:00",
        },
        {
            "id": 2,
            "published_at": None,
            "created_at": "2026-05-20T00:30:00+00:00",
        },
        {
            "id": 3,
            "published_at": "2026-05-19",
            "created_at": "2026-05-20T00:30:00+00:00",
        },
    ]

    filtered = _filter_drafts_by_date(drafts, date(2026, 5, 20))

    assert [draft["id"] for draft in filtered] == [1, 2]


def test_filter_drafts_by_query_searches_reporter_fields():
    drafts = [
        {
            "id": 1,
            "title": "해남 반값여행 접수",
            "source_name": "해남군",
            "region": "전남",
            "original_title": "땅끝해남 반값여행",
            "original_content": "관광객 환급",
            "review_note": "",
        },
        {
            "id": 2,
            "title": "함평 청렴 교육",
            "source_name": "함평군",
            "region": "전남",
            "original_title": "청렴 라이브",
            "original_content": "공직자 교육",
            "review_note": "",
        },
    ]

    filtered = _filter_drafts_by_query(drafts, "해남 환급")

    assert [draft["id"] for draft in filtered] == [1]


def test_review_flags_find_attention_reasons():
    draft = {
        "title": "신안군 행사 안내",
        "body": "신안군이 행사를 엽니다.",
        "review_note": "-",
        "model": "gemini-test:gemini",
        "original_title": "[카드뉴스] 신안군 행사 안내",
        "original_content": "짧은 카드뉴스 본문입니다.",
        "published_at": "(홍길동 / 2026-05-20)",
        "validation_note": "본문 90자, 제목 핵심어 1개 일치",
    }

    flags = review_flags(draft, duplicate_titles={"[카드뉴스] 신안군 행사 안내"})

    assert "사진·카드뉴스" in flags
    assert "게시일 확인" in flags
    assert "중복 제목" in flags
    assert "원문 짧음" not in flags
    assert "메모 보강" not in flags


def test_filter_drafts_by_review_supports_attention_and_topic_filters():
    drafts = [
        {
            "id": 1,
            "title": "군민 지원금 신청",
            "body": "지원합니다.",
            "review_note": "확인 완료",
            "model": "gemini-test:gemini",
            "original_title": "군민 지원금 신청",
            "original_content": "군민 지원금 신청 접수를 시작한다. 대상자는 신청하면 된다.",
            "published_at": "2026-05-20",
            "validation_note": "본문 100자",
        },
        {
            "id": 2,
            "title": "사진뉴스",
            "body": "사진뉴스입니다.",
            "review_note": "-",
            "model": "gemini-test:gemini",
            "original_title": "〈사진뉴스〉 행사",
            "original_content": "짧음",
            "published_at": "담당자 2026-05-20",
            "validation_note": "본문 20자",
        },
    ]

    assert [draft["id"] for draft in _filter_drafts_by_review(drafts, "application", set())] == [1]
    assert [draft["id"] for draft in _filter_drafts_by_review(drafts, "support", set())] == [1]
    assert [draft["id"] for draft in _filter_drafts_by_review(drafts, "attention", set())] == [2]
    assert _date_warning(drafts[1]) == "게시일 앞 문구 확인"


def test_sort_drafts_latest_first_uses_published_date_before_id():
    drafts = [
        {"id": 10, "published_at": "2026-05-18", "created_at": "2026-05-20T00:00:00+00:00"},
        {"id": 11, "published_at": "2026-05-20", "created_at": "2026-05-18T00:00:00+00:00"},
        {"id": 12, "published_at": "담당자 2026-05-19", "created_at": "2026-05-21T00:00:00+00:00"},
    ]

    sorted_drafts = _sort_drafts_latest_first(drafts)

    assert [draft["id"] for draft in sorted_drafts] == [11, 12, 10]


def test_approval_checks_warn_before_approval():
    draft = {
        "title": "[뉴스 단신] 제목",
        "body": "[뉴스 단신] 제목\n\n본문",
        "review_note": "없음",
        "model": "gemini-test:gemini",
        "original_title": "모집 안내",
        "original_content": "군은 신청 대상자를 모집한다고 밝혔다.",
        "published_at": "담당자 2026-05-20",
        "validation_note": "본문 30자",
    }

    checks = approval_checks(draft, set())

    assert any(check["label"] == "주의 필요 표시 없음" and not check["ok"] for check in checks)
    assert any(check["label"] == "본문 3~4문단" and not check["ok"] for check in checks)
    assert any(check["label"] == "신청·모집 정보 반영" and not check["ok"] for check in checks)


def test_display_helpers_make_labels_readable():
    assert format_datetime_label("2026-05-20T07:30:00+00:00") == "2026.05.20 16:30"
    assert format_datetime_label("2026-05-20") == "2026.05.20"
    assert format_datetime_label("2026.05.14 13:56") == "2026.05.14 13:56"
    assert model_label("gemini-3.5-flash:gemini") == "Gemini"
    assert model_label("gpt-4.1-mini:rule-based") == "규칙 기반"
    assert interval_label(3600) == "1시간마다"
    assert interval_label(7200) == "2시간마다"
    assert interval_label(600) == "10분마다"


def test_recrawl_route_runs_collect_and_gemini_draft_cycle(monkeypatch):
    db_path = Path(f"data/.test_recrawl_route_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    calls = []

    def fake_cycle(store, config_path, collect_limit, draft_limit, require_gemini):
        calls.append(
            {
                "collect_limit": collect_limit,
                "draft_limit": draft_limit,
                "require_gemini": require_gemini,
            }
        )
        return ["수동 재수집 완료"]

    monkeypatch.setattr("news_summary.web.collect_and_draft_cycle", fake_cycle)
    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    dashboard = client.get("/")
    dashboard_html = dashboard.data.decode("utf-8")
    assert "수동 재수집" in dashboard_html
    assert "초안 검수" in dashboard_html
    assert "Gemini 사용량" in dashboard_html
    assert 'href="/gemini-usage"' in dashboard_html
    assert "초안 목록" not in dashboard_html
    assert "승인 기사</a>" not in dashboard_html
    assert 'href="/drafts?status=approved"' in dashboard_html
    assert 'class="gemini-usage"' not in dashboard_html
    assert "운영 로그" not in dashboard_html
    assert "news_summary.log" not in dashboard_html
    assert "원문 수집" not in dashboard_html
    assert "초안 생성" not in dashboard_html
    assert "승인 기사 내보내기" not in dashboard_html

    response = client.post("/recrawl", data={"limit": "7"}, follow_redirects=True)

    assert response.status_code == 200
    assert calls == [{"collect_limit": 7, "draft_limit": 250, "require_gemini": True}]


def test_gemini_usage_page_is_separate_from_dashboard(monkeypatch):
    db_path = Path(f"data/.test_gemini_usage_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    from news_summary.web import create_app

    store = Store(db_path)
    store.init_db()
    press_release_id = store.add_press_release(
        PressRelease(
            source_id="test",
            source_name="테스트",
            region="전남",
            title="Gemini 사용량 테스트",
            url="https://example.com/gemini-usage",
            content="테스트 본문입니다.",
            published_at="2026-05-20",
        )
    )
    assert press_release_id is not None
    store.add_article_draft(
        ArticleDraft(
            press_release_id=press_release_id,
            title="Gemini 초안",
            body="본문입니다.",
            review_note="메모",
            model="gemini-3.5-flash:gemini",
        )
    )

    app = create_app()
    app.testing = True
    client = app.test_client()

    response = client.get("/gemini-usage")
    html = response.data.decode("utf-8")

    assert response.status_code == 200
    assert 'class="gemini-usage"' in html
    assert "전체 1회" in html
    assert "사용량 초기화" in html
    assert "Google AI Studio 사용량 확인" in html
    assert "운영 로그" not in html

    reset_response = client.post("/gemini-usage/reset", follow_redirects=True)
    reset_html = reset_response.data.decode("utf-8")

    assert reset_response.status_code == 200
    assert "전체 0회" in reset_html
    assert "초기화 시각:" in reset_html
    assert Store(db_path).counts()["drafts"] == 1


def test_recrawl_dashboard_shows_live_progress_and_starts_background_job(monkeypatch):
    db_path = Path(f"data/.test_recrawl_progress_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    from news_summary.web import create_app

    class FakeCollector:
        def __init__(self):
            self.calls = []

        def snapshot(self):
            return AutoCollectorStatus(
                enabled=True,
                running=True,
                progress_current=2,
                progress_total=19,
                progress_source_name="전라남도청 보도자료",
                progress_message="2/19 전라남도청 보도자료 연결 확인 중",
                progress_phase="collecting",
            )

        def run_async_once(self, collect_limit=None, draft_limit=None, label="수동 재수집"):
            self.calls.append((collect_limit, draft_limit, label))
            return True

    collector = FakeCollector()
    app = create_app()
    app.config["AUTO_COLLECTOR"] = collector
    app.testing = True
    client = app.test_client()

    dashboard = client.get("/")
    html = dashboard.data.decode("utf-8")
    assert "2/19" in html
    assert "전라남도청 보도자료" in html
    assert "수집 범위:" not in html
    assert "마지막 자동 수집 아직 없음" not in html
    assert "재수집 건수" not in html
    assert 'name="limit"' not in html
    assert 'class="manual-recrawl"' in html
    assert html.index('class="auto-status"') < html.index("수동 재수집")
    assert "/recrawl/status" in html

    response = client.post("/recrawl", data={"limit": "10"}, follow_redirects=True)
    assert response.status_code == 200
    assert collector.calls == [(10, 250, "수동 재수집")]

    status = client.get("/recrawl/status").get_json()
    assert status["progress_current"] == 2
    assert status["progress_total"] == 19


def test_refine_route_updates_current_draft_with_gemini(monkeypatch):
    db_path = Path(f"data/.test_refine_route_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))

    store = Store(db_path)
    store.init_db()
    press_release_id = store.add_press_release(
        PressRelease(
            source_id="sinan",
            source_name="신안군청 보도자료",
            region="전남 신안",
            title="교육 프로그램 운영",
            url="https://example.com/refine-test",
            content="신안군 저녁노을미술관이 교육 프로그램을 운영한다.",
            published_at="2026-05-20",
        )
    )
    assert press_release_id is not None
    draft_id = store.add_article_draft(
        ArticleDraft(
            press_release_id=press_release_id,
            title="기존 제목",
            body="기존 본문입니다.\n\n둘째 문단입니다.\n\n셋째 문단입니다.",
            review_note="기존 메모",
            model="gemini-test:gemini",
        )
    )

    def fake_refine(draft, instruction, current_title, current_body, current_review_note):
        assert "신청 방법" in instruction
        assert current_title == "현재 화면 제목"
        return ArticleDraft(
            press_release_id=draft["press_release_id"],
            title="Gemini 재작성 제목",
            body="다듬은 첫 문단입니다.\n\n다듬은 둘째 문단입니다.\n\n다듬은 셋째 문단입니다.",
            review_note="Gemini 재다듬기 완료",
            model="gemini-test:gemini-refine",
        )

    monkeypatch.setattr("news_summary.web.refine_draft_with_gemini", fake_refine)
    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    detail = client.get(f"/drafts/{draft_id}")
    detail_html = detail.data.decode("utf-8")
    assert 'data-refine-url="' in detail_html
    assert 'data-initial-url="' in detail_html
    assert "처음으로" in detail_html
    assert "내용 90%" in detail_html
    assert "90% 수준으로 분량을 줄여 간결하게 작성해줘." in detail_html
    assert "내용 110%" in detail_html
    assert "다듬는 중..." in detail_html
    assert "현재 원문에서 빠진 핵심 정보가 있으면 보충해서 문장 내용을 110% 로 더 풍부하게 다듬어줘. 단, 원문에 없는 사실은 추가하지 말고 문단 형식은 유지해줘." in detail_html
    assert "3문단 재정리" not in detail_html
    assert "신청 정보 중심" not in detail_html
    assert 'data-refine-instruction="대상, 비용' not in detail_html

    response = client.post(
        f"/drafts/{draft_id}/refine",
        data={
            "title": "현재 화면 제목",
            "body": "현재 화면 본문",
            "review_note": "현재 화면 메모",
            "status": "needs_review",
            "refine_instruction": "",
            "preset_instruction": "신청 방법 중심으로 다듬기",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    updated = store.get_draft(draft_id)
    assert updated["title"] == "Gemini 재작성 제목"
    assert updated["model"] == "gemini-test:gemini-refine"


def test_restore_initial_draft_route_returns_first_gemini_version(monkeypatch):
    db_path = Path(f"data/.test_restore_initial_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))

    store = Store(db_path)
    store.init_db()
    press_release_id = store.add_press_release(
        PressRelease(
            source_id="sinan",
            source_name="신안군청 보도자료",
            region="전남 신안",
            title="교육 프로그램 운영",
            url="https://example.com/restore-test",
            content="신안군 저녁노을미술관이 교육 프로그램을 운영한다.",
            published_at="2026-05-20",
        )
    )
    assert press_release_id is not None
    draft_id = store.add_article_draft(
        ArticleDraft(
            press_release_id=press_release_id,
            title="처음 제목",
            body="처음 첫 문단입니다.\n\n처음 둘째 문단입니다.\n\n처음 셋째 문단입니다.",
            review_note="처음 메모",
            model="gemini-test:gemini",
        )
    )
    store.update_draft(
        draft_id=draft_id,
        title="수정 제목",
        body="수정 본문",
        review_note="수정 메모",
        status="needs_review",
        model="gemini-test:gemini-refine",
    )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    response = client.post(
        f"/drafts/{draft_id}/restore-initial",
        data={"status": "needs_review"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    restored = store.get_draft(draft_id)
    assert restored["title"] == "처음 제목"
    assert restored["body"].startswith("처음 첫 문단")
    assert restored["review_note"] == "처음 메모"
    assert restored["model"] == "gemini-test:gemini"
