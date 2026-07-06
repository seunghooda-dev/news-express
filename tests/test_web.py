from datetime import date, datetime, timedelta, timezone
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
    LOCAL_TZ,
    approval_checks,
    body_character_count,
    format_datetime_label,
    interval_label,
    model_badge_class,
    model_label,
    region_display_label,
    review_flags,
    source_display_label,
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


def test_group_drafts_by_recent_dates_can_include_older_bucket():
    drafts = [
        {"id": 1, "published_at": "2026-05-20", "created_at": "2026-05-20T07:00:00+00:00"},
        {"id": 2, "published_at": "2026-05-10", "created_at": "2026-05-20T07:00:00+00:00"},
    ]

    groups = _group_drafts_by_recent_dates(drafts, today=date(2026, 5, 20), days=5, include_older=True)

    assert groups[-1]["label"] == "이전 검수 대기"
    assert groups[-1]["iso_date"] == ""
    assert [draft["id"] for draft in groups[-1]["drafts"]] == [2]


def test_group_drafts_by_recent_dates_can_use_created_date_for_dashboard():
    drafts = [
        {"id": 1, "published_at": "2026-05-10", "created_at": "2026-05-20T07:00:00+00:00"},
        {"id": 2, "published_at": "2026-05-20", "created_at": "2026-05-18T07:00:00+00:00"},
    ]

    groups = _group_drafts_by_recent_dates(drafts, today=date(2026, 5, 20), days=5, date_source="created")

    assert [draft["id"] for draft in groups[0]["drafts"]] == [1]
    assert [draft["id"] for draft in groups[2]["drafts"]] == [2]


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

    assert "게시일 확인" in flags
    assert "중복 제목" in flags
    assert "사진·카드뉴스" not in flags
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
    assert model_label("gemini-3.5-flash:gemini") == "Gemini Flash"
    assert model_label("gemini-3.1-flash-lite:gemini") == "Gemini Lite"
    assert model_badge_class("gemini-3.5-flash:gemini") == "badge-gemini-flash"
    assert model_badge_class("gemini-3.1-flash-lite:gemini") == "badge-gemini-lite"
    assert model_label("gpt-4.1-mini:rule-based") == "규칙 기반"
    assert interval_label(3600) == "매시간 정각"
    assert interval_label(7200) == "2시간마다"
    assert interval_label(600) == "10분마다"
    assert body_character_count("첫 문단\r\n둘째 문단") == len("첫 문단\n둘째 문단")
    assert body_character_count(None) == 0


def test_draft_detail_shows_body_character_count(monkeypatch):
    db_path = Path(f"data/.test_body_character_count_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()
    body = "첫 문단입니다.\n\n둘째 문단입니다.\n\n셋째 문단입니다."
    release_id = store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 군청",
            region="전남",
            title="글자수 테스트 원문",
            url="https://example.com/body-count",
            content="테스트 원문 내용입니다.",
            published_at="2026-05-20",
        )
    )
    assert release_id is not None
    draft_id = store.add_article_draft(
        ArticleDraft(
            press_release_id=release_id,
            title="글자수 테스트 초안",
            body=body,
            review_note="메모",
            model="gemini-3.5-flash:gemini",
        )
    )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    html = client.get(f"/drafts/{draft_id}").data.decode("utf-8")

    assert "본문 총 글자수:" in html
    assert f'<span id="body-char-count-value">{body_character_count(body)}</span>자' in html
    assert 'body?.addEventListener("input", updateBodyCount);' in html
    assert '<details class="original original-details" open>' in html
    assert "originalDetails.open = false;" in html
    assert 'class="mobile-review-bar" aria-label="빠른 검수 작업"' in html
    assert '<button type="submit" name="action" value="approved_next">승인 후 다음</button>' in html
    assert 'class="mobile-review-spacer" aria-hidden="true"' in html


def test_dashboard_metric_cards_link_to_full_lists(monkeypatch):
    db_path = Path(f"data/.test_dashboard_metric_links_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))

    store = Store(db_path)
    store.init_db()
    release_id = store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 군청",
            region="전남",
            title="테스트 원문",
            url="https://example.com/original",
            content="테스트 원문 내용입니다.",
            published_at="2026-05-20",
        )
    )
    assert release_id is not None
    draft_id = store.add_article_draft(
        ArticleDraft(
            press_release_id=release_id,
            title="테스트 초안",
            body="본문입니다.",
            review_note="메모",
            model="gemini-3.5-flash:gemini",
        )
    )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    dashboard_html = client.get("/").data.decode("utf-8")
    assert 'href="/press-releases"' in dashboard_html
    assert 'href="/drafts"' in dashboard_html
    assert 'href="/drafts?status=needs_review"' in dashboard_html
    assert "테스트 원문" in dashboard_html
    assert "검수 완료" not in dashboard_html

    releases_html = client.get("/press-releases").data.decode("utf-8")
    assert "테스트 원문" in releases_html
    assert f'href="/drafts/{draft_id}"' in releases_html


def test_dashboard_renders_only_recent_release_preview_rows(monkeypatch):
    db_path = Path(f"data/.test_dashboard_release_preview_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()

    for index in range(12):
        store.add_press_release(
            PressRelease(
                source_id="sample",
                source_name="테스트 기관",
                region="전남",
                title=f"홈 원문 미리보기 {index:02d}",
                url=f"https://example.com/dashboard-release-preview-{index}",
                content="테스트 원문 내용입니다.",
                published_at=f"2026-05-{index + 1:02d}",
            )
        )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    dashboard_html = client.get("/").data.decode("utf-8")

    assert "홈 원문 미리보기 11" in dashboard_html
    assert "홈 원문 미리보기 02" in dashboard_html
    assert "홈 원문 미리보기 01" not in dashboard_html
    assert "홈 원문 미리보기 00" not in dashboard_html
    assert 'href="/press-releases"' in dashboard_html


def test_drafts_page_uses_load_more_pagination(monkeypatch):
    db_path = Path(f"data/.test_drafts_load_more_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()

    for index in range(55):
        release_id = store.add_press_release(
            PressRelease(
                source_id="sample",
                source_name="테스트 기관",
                region="전남",
                title=f"원문 {index:02d}",
                url=f"https://example.com/load-more-draft-{index}",
                content="테스트 원문 내용입니다.",
                published_at="2026-05-20",
            )
        )
        assert release_id is not None
        store.add_article_draft(
            ArticleDraft(
                press_release_id=release_id,
                title=f"초안 {index:02d}",
                body="본문입니다.",
                review_note="",
                model="gemini-3.5-flash:gemini",
            )
        )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    first_page = client.get("/drafts").data.decode("utf-8")
    expanded_page = client.get("/drafts?limit=100").data.decode("utf-8")

    assert "현재 50개를 표시하고 있습니다." in first_page
    assert "더보기" in first_page
    assert "limit=100" in first_page
    assert "초안 00" not in first_page
    assert "현재 55개를 표시하고 있습니다." in expanded_page
    assert "초안 00" in expanded_page
    assert "더보기" not in expanded_page


def test_press_releases_page_uses_load_more_pagination(monkeypatch):
    db_path = Path(f"data/.test_releases_load_more_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()

    for index in range(55):
        store.add_press_release(
            PressRelease(
                source_id="sample",
                source_name="테스트 기관",
                region="전남",
                title=f"원문 목록 {index:02d}",
                url=f"https://example.com/load-more-release-{index}",
                content="테스트 원문 내용입니다.",
                published_at="2026-05-20",
            )
        )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    first_page = client.get("/press-releases").data.decode("utf-8")
    expanded_page = client.get("/press-releases?limit=100").data.decode("utf-8")

    assert "현재 50개를 표시하고 있습니다." in first_page
    assert "더보기" in first_page
    assert "limit=100" in first_page
    assert "원문 목록 00" not in first_page
    assert "현재 55개를 표시하고 있습니다." in expanded_page
    assert "원문 목록 00" in expanded_page
    assert "더보기" not in expanded_page


def test_dashboard_source_cards_show_yesterday_and_today_counts(monkeypatch):
    db_path = Path(f"data/.test_dashboard_source_counts_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    today = datetime.now(LOCAL_TZ).date()
    yesterday = today - timedelta(days=1)

    store = Store(db_path)
    store.init_db()
    for title, published_at in (("오늘 보도자료", today), ("어제 보도자료", yesterday)):
        store.add_press_release(
            PressRelease(
                source_id="gwangju-city",
                source_name="광주광역시청 보도자료",
                region="광주",
                title=title,
                url=f"https://example.com/{title}",
                content="기관별 수집 상태 테스트 본문입니다.",
                published_at=published_at.isoformat(),
            )
        )
    store.record_source_collection_status(
        source_id="gwangju-city",
        source_name="광주광역시청 보도자료",
        status="failed",
        message="ReadTimeout",
        failure_stage="외부 사이트 응답 지연",
        failure_reason="응답 지연 또는 타임아웃",
    )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    dashboard_html = client.get("/").data.decode("utf-8")

    assert '<details class="source-board">' in dashboard_html
    assert '<details class="source-board" open' not in dashboard_html
    assert "광주 ·" in dashboard_html
    assert "전남광주통합특별시 광주 ·" not in dashboard_html
    assert "어제 1건 · 오늘 1건" in dashboard_html
    assert "누적 2건" not in dashboard_html
    assert "mobile-source-board" not in dashboard_html
    assert "광주청사 보도자료" in dashboard_html
    assert "전남광주통합특별시 광주청사 보도자료" not in dashboard_html
    assert "일시 지연" in dashboard_html
    assert 'href="/sources/gwangju-city"' in dashboard_html


def test_region_display_label_removes_common_integrated_city_prefix():
    assert region_display_label("전남광주통합특별시 진도") == "진도"
    assert region_display_label("전남광주특별시 목포") == "목포"
    assert region_display_label("전남광주통합특별시") == "광주·전남 전체"
    assert region_display_label("광주 북구") == "광주 북구"


def test_source_display_label_removes_common_integrated_city_prefix():
    assert source_display_label("전남광주통합특별시 광주청사 보도자료") == "광주청사 보도자료"
    assert source_display_label("전남광주특별시 목포시청 보도자료") == "목포시청 보도자료"
    assert source_display_label("전남광주통합특별시청 보도자료") == "시청 보도자료"
    assert source_display_label("광주 북구청 보도자료") == "광주 북구청 보도자료"


def test_region_checkbox_filter_limits_dashboard_drafts_and_releases(monkeypatch):
    db_path = Path(f"data/.test_region_filter_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()
    today = datetime.now(LOCAL_TZ).date().isoformat()

    release_id_gwangju = store.add_press_release(
        PressRelease(
            source_id="gwangju-city",
            source_name="광주광역시청 보도자료",
            region="광주",
            title="광주 지역 원문",
            url="https://example.com/gwangju-region",
            content="광주시는 지역 사업을 추진한다고 밝혔다.",
            published_at=today,
        )
    )
    release_id_jindo = store.add_press_release(
        PressRelease(
            source_id="jindo-county",
            source_name="진도군청 보도자료",
            region="전남 진도",
            title="진도 지역 원문",
            url="https://example.com/jindo-region",
            content="진도군은 지역 사업을 추진한다고 밝혔다.",
            published_at=today,
        )
    )
    assert release_id_gwangju is not None
    assert release_id_jindo is not None
    store.add_article_draft(
        ArticleDraft(
            press_release_id=release_id_gwangju,
            title="광주 지역 초안",
            body="본문입니다.",
            review_note="메모",
            model="gemini-3.5-flash:gemini",
        )
    )
    store.add_article_draft(
        ArticleDraft(
            press_release_id=release_id_jindo,
            title="진도 지역 초안",
            body="본문입니다.",
            review_note="메모",
            model="gemini-3.5-flash:gemini",
        )
    )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    dashboard_html = client.get("/?region=전남광주통합특별시+진도").data.decode("utf-8")
    assert 'name="region" value="전남광주통합특별시 진도" checked' in dashboard_html
    assert "<span>진도</span>" in dashboard_html
    assert "<span>광주·전남 전체</span>" in dashboard_html
    assert '<details class="region-filter-panel">' in dashboard_html
    assert '<details class="region-filter-panel" open' not in dashboard_html
    assert "data-auto-submit" not in dashboard_html
    assert "선택 변경됨. 적용을 눌러 반영하세요." in dashboard_html
    assert "진도군청 보도자료" in dashboard_html
    assert "진도 지역 초안" in dashboard_html
    assert "전남광주통합특별시 광주청사 보도자료" not in dashboard_html
    assert "광주 지역 초안" not in dashboard_html
    assert "/press-releases?region=" in dashboard_html

    drafts_html = client.get("/drafts?region=전남광주통합특별시+진도").data.decode("utf-8")
    assert "진도 지역 초안" in drafts_html
    assert "광주 지역 초안" not in drafts_html

    releases_html = client.get("/press-releases?region=전남광주통합특별시+진도").data.decode("utf-8")
    assert "진도 지역 원문" in releases_html
    assert "광주 지역 원문" not in releases_html


def test_parent_region_filter_shows_child_region_pending_drafts_on_dashboard(monkeypatch):
    db_path = Path(f"data/.test_parent_region_filter_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()

    release_id = store.add_press_release(
        PressRelease(
            source_id="jindo-county",
            source_name="진도군청 보도자료",
            region="전남 진도",
            title="진도 오래된 원문",
            url="https://example.com/jindo-old-region",
            content="진도군은 지역 사업을 추진한다고 밝혔다.",
            published_at="2026-05-10",
        )
    )
    assert release_id is not None
    store.add_article_draft(
        ArticleDraft(
            press_release_id=release_id,
            title="진도 오래된 검수 대기 초안",
            body="본문입니다.",
            review_note="메모",
            model="gemini-3.5-flash:gemini",
        )
    )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    dashboard_html = client.get("/?region=전남광주통합특별시").data.decode("utf-8")

    assert 'name="region" value="전남광주통합특별시" checked' in dashboard_html
    assert "진도 오래된 검수 대기 초안" in dashboard_html
    assert "초안 " in dashboard_html
    assert "게시 2026.05.10" in dashboard_html

    drafts_html = client.get("/drafts?status=needs_review&region=전남광주통합특별시").data.decode("utf-8")
    assert "진도 오래된 검수 대기 초안" in drafts_html


def test_dashboard_shows_latest_pending_drafts_across_dates(monkeypatch):
    db_path = Path(f"data/.test_dashboard_latest_pending_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()
    today = datetime.now(LOCAL_TZ)
    yesterday = today - timedelta(days=1)

    for index in range(4):
        release_id = store.add_press_release(
            PressRelease(
                source_id="sample",
                source_name="테스트 군청",
                region="전남",
                title=f"오늘 원문 {index:02d}",
                url=f"https://example.com/today-pending-{index}",
                content="테스트 군은 지역 사업을 추진한다고 밝혔다.",
                published_at=today.date().isoformat(),
            )
        )
        assert release_id is not None
        store.add_article_draft(
            ArticleDraft(
                press_release_id=release_id,
                title=f"오늘 생성된 검수 대기 초안 {index:02d}",
                body="본문입니다.",
                review_note="메모",
                model="gemini-3.5-flash:gemini",
                created_at=(today - timedelta(minutes=index)).isoformat(),
            )
        )

    for index in range(21):
        release_id = store.add_press_release(
            PressRelease(
                source_id="sample",
                source_name="테스트 군청",
                region="전남",
                title=f"어제 원문 {index:02d}",
                url=f"https://example.com/yesterday-pending-{index}",
                content="테스트 군은 지역 사업을 추진한다고 밝혔다.",
                published_at=yesterday.date().isoformat(),
            )
        )
        assert release_id is not None
        store.add_article_draft(
            ArticleDraft(
                press_release_id=release_id,
                title=f"어제 생성된 검수 대기 초안 {index:02d}",
                body="본문입니다.",
                review_note="메모",
                model="gemini-3.5-flash:gemini",
                created_at=(yesterday - timedelta(minutes=index)).isoformat(),
            )
        )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    dashboard_html = client.get("/").data.decode("utf-8")

    assert "오늘 생성된 검수 대기 초안 00" in dashboard_html
    assert "어제 생성된 검수 대기 초안 00" in dashboard_html
    assert "어제 생성된 검수 대기 초안 16" not in dashboard_html
    assert f"{today.year}년 {today.month}월 {today.day}일 (오늘)" not in dashboard_html
    assert "초안 " in dashboard_html


def test_source_status_records_collection_failures(monkeypatch):
    db_path = Path(f"data/.test_source_status_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()
    store.record_source_collection_status(
        "gwangju-city",
        "광주광역시청 보도자료",
        "failed",
        "광주광역시청 보도자료 수집 실패: 타임아웃",
        failure_stage="사이트 접속",
        failure_reason="응답 지연 또는 타임아웃",
    )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    dashboard_html = client.get("/").data.decode("utf-8")
    detail_html = client.get("/sources/gwangju-city").data.decode("utf-8")

    assert "외부 사이트 응답 지연" in dashboard_html
    assert "응답 지연 또는 타임아웃" in dashboard_html
    assert "최근 수집 점검" in detail_html
    assert "외부 사이트 응답 지연" in detail_html
    assert "광주광역시청 보도자료 수집 실패: 타임아웃" in detail_html


def test_admin_login_is_required_when_password_is_configured(monkeypatch):
    db_path = Path(f"data/.test_admin_login_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_ADMIN_PASSWORD", "secret1234")
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_DISABLED", "0")

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]

    wrong = client.post("/login", data={"password": "wrong"}, follow_redirects=True)
    assert "관리자 비밀번호가 올바르지 않습니다." in wrong.data.decode("utf-8")

    right = client.post("/login", data={"password": "secret1234", "next": "/"}, follow_redirects=True)
    html = right.data.decode("utf-8")
    assert right.status_code == 200
    assert "Home" in html
    assert "로그아웃" in html

    logout = client.post("/logout", follow_redirects=False)
    assert logout.status_code == 302


def test_security_headers_are_applied(monkeypatch):
    db_path = Path(f"data/.test_security_headers_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    response = client.get("/")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "same-origin"
    assert "camera=()" in response.headers["Permissions-Policy"]

    operations = client.get("/operations")
    assert operations.headers["Cache-Control"] == "no-store"


def test_dangerous_operations_render_confirmation_prompts(monkeypatch):
    db_path = Path(f"data/.test_operation_confirmations_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    dashboard_html = client.get("/").data.decode("utf-8")
    operations_html = client.get("/operations").data.decode("utf-8")
    gemini_html = client.get("/gemini-usage").data.decode("utf-8")

    assert "수동 재수집을 시작할까요?" in dashboard_html
    assert "자동 수집 설정을 변경할까요?" in operations_html
    assert "백업 복구 작업을 진행할까요?" in operations_html
    assert "로컬 Gemini 사용량 기록을 초기화할까요?" in gemini_html


def test_admin_setup_enables_login_without_env_password(monkeypatch):
    db_path = Path(f"data/.test_admin_setup_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_DISABLED", "0")
    monkeypatch.delenv("NEWS_SUMMARY_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("NEWS_SUMMARY_ADMIN_PASSWORD_HASH", raising=False)

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    setup = client.post(
        "/admin/setup",
        data={"password": "secret1234", "confirm_password": "secret1234"},
        follow_redirects=True,
    )
    assert "관리자 로그인을 활성화했습니다." in setup.data.decode("utf-8")

    client.post("/logout")
    protected = client.get("/", follow_redirects=False)
    assert protected.status_code == 302
    assert "/login" in protected.headers["Location"]


def test_admin_setup_is_accessible_when_auth_required_without_password(monkeypatch):
    db_path = Path(f"data/.test_admin_required_setup_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_REQUIRED", "1")
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_DISABLED", "0")
    monkeypatch.delenv("NEWS_SUMMARY_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("NEWS_SUMMARY_ADMIN_PASSWORD_HASH", raising=False)

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    setup_page = client.get("/admin/setup", follow_redirects=False)
    assert setup_page.status_code == 200
    assert "관리자 비밀번호" in setup_page.data.decode("utf-8")

    setup = client.post(
        "/admin/setup",
        data={"password": "secret1234", "confirm_password": "secret1234"},
        follow_redirects=True,
    )
    assert "관리자 로그인을 활성화했습니다." in setup.data.decode("utf-8")


def test_operations_page_changes_database_admin_password(monkeypatch):
    db_path = Path(f"data/.test_admin_password_change_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_DISABLED", "0")
    monkeypatch.delenv("NEWS_SUMMARY_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("NEWS_SUMMARY_ADMIN_PASSWORD_HASH", raising=False)

    from news_summary.auth import set_admin_password
    from news_summary.web import create_app

    store = Store(db_path)
    store.init_db()
    set_admin_password(store, "oldpass123")

    app = create_app()
    app.testing = True
    client = app.test_client()

    client.post("/login", data={"password": "oldpass123", "next": "/"})
    operations_html = client.get("/operations").data.decode("utf-8")
    assert "관리자 비밀번호" in operations_html
    assert "확인 후 변경 열기" in operations_html
    assert "new_password" not in operations_html

    wrong_unlock = client.post(
        "/operations/admin-password/unlock",
        data={"current_password": "wrong"},
        follow_redirects=True,
    )
    wrong_unlock_html = wrong_unlock.data.decode("utf-8")
    assert "현재 관리자 비밀번호가 올바르지 않습니다." in wrong_unlock_html
    assert "new_password" not in wrong_unlock_html

    unlock = client.post(
        "/operations/admin-password/unlock",
        data={"current_password": "oldpass123"},
        follow_redirects=True,
    )
    unlocked_html = unlock.data.decode("utf-8")
    assert "관리자 비밀번호 변경 입력칸을 열었습니다." in unlocked_html
    assert "비밀번호 변경" in unlocked_html
    assert "new_password" in unlocked_html

    changed = client.post(
        "/operations/admin-password",
        data={"new_password": "newpass123", "confirm_password": "newpass123"},
        follow_redirects=True,
    )
    assert "관리자 비밀번호를 변경했습니다." in changed.data.decode("utf-8")

    client.post("/logout")
    old_login = client.post("/login", data={"password": "oldpass123"}, follow_redirects=True)
    assert "관리자 비밀번호가 올바르지 않습니다." in old_login.data.decode("utf-8")

    new_login = client.post("/login", data={"password": "newpass123", "next": "/"}, follow_redirects=True)
    assert "로그아웃" in new_login.data.decode("utf-8")


def test_operations_page_does_not_override_environment_admin_password(monkeypatch):
    db_path = Path(f"data/.test_env_admin_password_change_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_ADMIN_PASSWORD", "envpass123")
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_DISABLED", "0")

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    client.post("/login", data={"password": "envpass123", "next": "/"})
    operations_html = client.get("/operations").data.decode("utf-8")
    assert ".env 관리" in operations_html

    response = client.post(
        "/operations/admin-password",
        data={"current_password": "envpass123", "new_password": "newpass123", "confirm_password": "newpass123"},
        follow_redirects=True,
    )
    html = response.data.decode("utf-8")
    assert ".env의 관리자 비밀번호 설정이 우선 적용 중" in html

    client.post("/logout")
    new_login = client.post("/login", data={"password": "newpass123"}, follow_redirects=True)
    assert "관리자 비밀번호가 올바르지 않습니다." in new_login.data.decode("utf-8")


def test_environment_admin_password_takes_priority_over_stored_hash(monkeypatch):
    db_path = Path(f"data/.test_env_admin_password_priority_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_ADMIN_PASSWORD", "envpass123")
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_DISABLED", "0")

    store = Store(db_path)
    store.init_db()
    from news_summary.auth import set_admin_password

    set_admin_password(store, "dbpass123")

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    db_login = client.post("/login", data={"password": "dbpass123"}, follow_redirects=True)
    assert "관리자 비밀번호가 올바르지 않습니다." in db_login.data.decode("utf-8")

    env_login = client.post("/login", data={"password": "envpass123", "next": "/"}, follow_redirects=True)
    assert "로그아웃" in env_login.data.decode("utf-8")


def test_source_detail_uses_current_config_name_for_existing_rows(monkeypatch):
    db_path = Path(f"data/.test_source_detail_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()
    release_id = store.add_press_release(
        PressRelease(
            source_id="jindo-county",
            source_name="진도군 농업기술센터 보도자료",
            region="전남 진도",
            title="진도군 군정뉴스",
            url="https://example.com/jindo-news",
            content="진도군은 군정 소식을 안내한다고 밝혔다.",
            published_at="2026-05-20",
        )
    )
    assert release_id is not None
    store.add_article_draft(
        ArticleDraft(
            press_release_id=release_id,
            title="진도군 군정뉴스",
            body="진도군이 군정 소식을 안내했습니다.",
            review_note="",
            model="gemini-3.1-flash-lite:gemini",
        )
    )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    html = client.get("/sources/jindo-county").data.decode("utf-8")
    releases_html = client.get("/press-releases").data.decode("utf-8")

    assert "진도군청 보도자료" in html
    assert "Gemini Lite" in html
    assert "Gemini Lite" in releases_html
    assert "진도군 농업기술센터 보도자료" not in releases_html


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
    assert "<summary>메뉴</summary>" in dashboard_html
    assert "대시 모드" not in dashboard_html
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

    default_response = client.post("/recrawl", data={}, follow_redirects=True)
    response = client.post("/recrawl", data={"limit": "7"}, follow_redirects=True)

    assert default_response.status_code == 200
    assert response.status_code == 200
    assert calls[0]["collect_limit"] == 30
    assert calls[0]["draft_limit"] >= 30
    assert calls[0]["require_gemini"] is True
    assert calls[1] == {"collect_limit": 7, "draft_limit": 250, "require_gemini": True}


def test_ops_logs_page_shows_recent_warnings(monkeypatch, tmp_path):
    db_path = Path(f"data/.test_ops_logs_page_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_LOG_DIR", str(tmp_path))

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    log_path = tmp_path / "news_summary.log"
    log_path.write_text(
        "2026-06-02 INFO [news_summary.test] 정상 로그\n"
        "2026-06-02 WARNING [news_summary.test] 수집 실패 테스트\n",
        encoding="utf-8",
    )
    client = app.test_client()

    html = client.get("/ops-logs").data.decode("utf-8")

    assert "운영 로그" in html
    assert "오류" in html
    assert "Gemini" in html
    assert "자동수집" in html
    assert "전체" in html
    assert "수집 실패 테스트" in html


def test_ops_logs_page_filters_categories(monkeypatch, tmp_path):
    db_path = Path(f"data/.test_ops_logs_filters_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_LOG_DIR", str(tmp_path))

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    log_path = tmp_path / "news_summary.log"
    log_path.write_text(
        "2026-06-02 INFO [news_summary.writer] Gemini 초안 생성 완료\n"
        "2026-06-02 INFO [news_summary.scheduler] auto collector waiting\n"
        "2026-06-02 WARNING [news_summary.web] slow web request path=/drafts\n",
        encoding="utf-8",
    )
    client = app.test_client()

    gemini_html = client.get("/ops-logs?tab=gemini").data.decode("utf-8")
    collector_html = client.get("/ops-logs?tab=collector").data.decode("utf-8")
    invalid_html = client.get("/ops-logs?tab=unknown").data.decode("utf-8")

    assert "Gemini 초안 생성 완료" in gemini_html
    assert "auto collector waiting" not in gemini_html
    assert "auto collector waiting" in collector_html
    assert "Gemini 초안 생성 완료" not in collector_html
    assert "slow web request path=/drafts" in invalid_html


def test_operations_page_toggles_auto_collection(monkeypatch):
    db_path = Path(f"data/.test_operations_auto_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))

    from news_summary.web import create_app

    class FakeCollector:
        def __init__(self):
            self.enabled = False
            self.calls = []

        def snapshot(self):
            return AutoCollectorStatus(enabled=self.enabled, progress_message="자동 수집 꺼짐")

        def set_enabled(self, enabled):
            self.enabled = enabled
            self.calls.append(enabled)

    collector = FakeCollector()
    app = create_app()
    app.config["AUTO_COLLECTOR"] = collector
    app.testing = True
    client = app.test_client()

    html = client.get("/operations").data.decode("utf-8")

    assert "운영 관리" in html
    assert "자동 수집 켜기" in html

    enabled_response = client.post("/operations/auto-collect", data={"enabled": "true"}, follow_redirects=True)
    disabled_response = client.post("/operations/auto-collect", data={"enabled": "false"}, follow_redirects=True)

    assert enabled_response.status_code == 200
    assert disabled_response.status_code == 200
    assert collector.calls == [True, False]


def test_operations_page_shows_retention_queue_and_tunnel_status(monkeypatch):
    db_path = Path(f"data/.test_operations_status_cards_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()
    store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 기관",
            region="전남",
            title="Gemini 대기 원문",
            url="https://example.com/pending-queue",
            content="Gemini 초안을 기다리는 원문입니다.",
            published_at="2026-06-26 09:00",
        )
    )
    store.record_source_collection_status(
        "sample",
        "테스트 기관",
        "failed",
        "테스트 기관 수집 실패: ReadTimeout",
        failure_stage="외부 사이트 응답 지연",
        failure_reason="응답 지연 또는 타임아웃",
    )
    store.record_source_collection_status(
        "sample",
        "테스트 기관",
        "ok",
        "일시 장애 1차 자동 재검증 통과, 원문 검증 통과 1건, 새로 저장 0건",
        releases_found=1,
    )

    from news_summary import web as web_module

    monkeypatch.setattr(
        web_module,
        "_cloudflare_quick_tunnel_status",
        lambda: {
            "running": True,
            "public_url": "https://sample.trycloudflare.com",
            "log_path": "data/tmp/cloudflare_quick_tunnel.err.log",
            "updated_at": "2026-06-29T10:00:00+09:00",
            "label": "외부 접속 정상",
        },
    )
    monkeypatch.setattr(
        web_module,
        "_deployment_version_report",
        lambda: {
            "status_label": "최신 배포",
            "status_level": "ok",
            "running_commit": "abc1234",
            "latest_commit": "abc1234",
            "repo": "seunghooda-dev/news-express",
            "branch": "codex/news-express",
        },
    )
    app = web_module.create_app()
    app.testing = True
    client = app.test_client()

    html = client.get("/operations").data.decode("utf-8")

    assert "수집 보관 기준" in html
    assert "공휴일이 있으면" in html
    assert "자동 복구 점검" in html
    assert "최근 24시간 실패 1건" in html
    assert "자동 복구 1건" in html
    assert "외부 사이트 응답 지연 1건" in html
    assert "배포 버전" in html
    assert "최신 배포" in html
    assert "일일 운영 리포트" in html
    assert "게시일 점검" in html
    assert "대체 URL 준비" in html
    assert "Gemini 미변환 큐" in html
    assert "Gemini 대기 원문" not in html
    assert "테스트 기관 1건" in html
    assert "외부 접속" in html
    assert "외부 접속 정상" in html
    assert "https://sample.trycloudflare.com" in html


def test_source_summary_treats_weekend_gap_as_holiday_wait(monkeypatch):
    db_path = Path(f"data/.test_source_holiday_wait_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()

    from news_summary import web as web_module
    from news_summary.models import Source

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 7, 6, 12, 0, tzinfo=LOCAL_TZ)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(web_module, "datetime", FixedDatetime)
    monkeypatch.setattr(
        web_module,
        "load_sources",
        lambda config_path: [Source(id="sample", name="테스트 기관", region="전남", type="html_board")],
    )
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO source_collection_runs
            (source_id, source_name, status, message, failure_stage, failure_reason,
             releases_found, inserted_count, repaired_dates, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "sample",
                "테스트 기관",
                "ok",
                "원문 검증 통과 0건, 새로 저장 0건",
                "",
                "",
                0,
                0,
                0,
                "2026-07-03T05:00:00+00:00",
            ),
        )

    summary = web_module._source_summaries(store, Path("unused.yaml"))[0]

    assert summary["issue"] == ""
    assert summary["status_label"] == "휴일 이후 대기"
    assert summary["status_level"] == "ok"


def test_source_summary_marks_stale_business_day_gap_as_delayed(monkeypatch):
    db_path = Path(f"data/.test_source_business_delay_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()

    from news_summary import web as web_module
    from news_summary.models import Source

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 7, 7, 12, 0, tzinfo=LOCAL_TZ)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(web_module, "datetime", FixedDatetime)
    monkeypatch.setattr(
        web_module,
        "load_sources",
        lambda config_path: [Source(id="sample", name="테스트 기관", region="전남", type="html_board")],
    )
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO source_collection_runs
            (source_id, source_name, status, message, failure_stage, failure_reason,
             releases_found, inserted_count, repaired_dates, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "sample",
                "테스트 기관",
                "ok",
                "원문 검증 통과 0건, 새로 저장 0건",
                "",
                "",
                0,
                0,
                0,
                "2026-07-03T05:00:00+00:00",
            ),
        )

    summary = web_module._source_summaries(store, Path("unused.yaml"))[0]

    assert summary["issue"] == "점검 지연"
    assert summary["status_label"] == "점검 지연"
    assert summary["status_level"] == "warning"


def test_source_summary_marks_transient_failure_after_today_success_as_temporary_delay(monkeypatch):
    db_path = Path(f"data/.test_source_temporary_delay_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()

    from news_summary import web as web_module
    from news_summary.models import Source

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 7, 6, 15, 0, tzinfo=LOCAL_TZ)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(web_module, "datetime", FixedDatetime)
    monkeypatch.setattr(
        web_module,
        "load_sources",
        lambda config_path: [Source(id="gangjin", name="강진군청 보도자료", region="전남 강진", type="html_board")],
    )
    store.add_press_release(
        PressRelease(
            source_id="gangjin",
            source_name="강진군청 보도자료",
            region="전남 강진",
            title="강진군, 지역 사업 추진",
            url="https://example.com/gangjin/1",
            content="강진군은 지역 사업을 추진한다고 밝혔다. 주민 편의를 높이기 위해 현장 점검을 이어갈 계획이라고 설명했다.",
            published_at="2026-07-06",
            collected_at="2026-07-06T05:05:00+00:00",
        )
    )
    with store.connect() as conn:
        for status, checked_at in (
            ("ok", "2026-07-06T05:05:00+00:00"),
            ("failed", "2026-07-06T05:11:00+00:00"),
        ):
            conn.execute(
                """
                INSERT INTO source_collection_runs
                (source_id, source_name, status, message, failure_stage, failure_reason,
                 releases_found, inserted_count, repaired_dates, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "gangjin",
                    "강진군청 보도자료",
                    status,
                    "TLS 연결 시간 초과" if status == "failed" else "원문 검증 통과 1건, 새로 저장 1건",
                    "외부 사이트 응답 지연" if status == "failed" else "",
                    "TLS 연결 시간 초과" if status == "failed" else "",
                    0 if status == "failed" else 1,
                    0 if status == "failed" else 1,
                    0,
                    checked_at,
                ),
            )

    summary = web_module._source_summaries(store, Path("unused.yaml"))[0]

    assert summary["issue"] == ""
    assert summary["status_label"] == "정상"
    assert summary["status_level"] == "ok"
    assert "오늘 원문은 수집" in summary["status_detail"]
    assert "정상 수집" in summary["status_detail"]


def test_source_summary_marks_three_consecutive_failures_as_failed(monkeypatch):
    db_path = Path(f"data/.test_source_three_failures_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()

    from news_summary import web as web_module
    from news_summary.models import Source

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 7, 6, 15, 0, tzinfo=LOCAL_TZ)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(web_module, "datetime", FixedDatetime)
    monkeypatch.setattr(
        web_module,
        "load_sources",
        lambda config_path: [Source(id="suncheon", name="순천시청 보도자료", region="전남 순천", type="html_board")],
    )
    with store.connect() as conn:
        for checked_at in (
            "2026-07-06T05:00:00+00:00",
            "2026-07-06T05:10:00+00:00",
            "2026-07-06T05:20:00+00:00",
        ):
            conn.execute(
                """
                INSERT INTO source_collection_runs
                (source_id, source_name, status, message, failure_stage, failure_reason,
                 releases_found, inserted_count, repaired_dates, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "suncheon",
                    "순천시청 보도자료",
                    "failed",
                    "TLS 연결 시간 초과",
                    "외부 사이트 응답 지연",
                    "TLS 연결 시간 초과",
                    0,
                    0,
                    0,
                    checked_at,
                ),
            )

    summary = web_module._source_summaries(store, Path("unused.yaml"))[0]

    assert summary["issue"] == "외부 사이트 응답 지연"
    assert summary["status_label"] == "수집 실패"
    assert summary["status_level"] == "error"
    assert summary["consecutive_failures"] == 3


def test_operations_page_records_masked_visitor_access(monkeypatch):
    db_path = Path(f"data/.test_visitor_access_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    client.get(
        "/drafts",
        headers={
            "X-Forwarded-For": "123.45.67.89",
            "User-Agent": "Mozilla/5.0 Chrome/120.0",
        },
    )
    html = client.get("/operations").data.decode("utf-8")

    assert "접속자 현황" in html
    assert "최근 7일" in html
    assert "123.45.xxx.xxx" in html
    assert "123.45.67.89" not in html
    assert "GET /drafts" in html
    assert "Chrome" in html


def test_operations_page_filters_visitor_access_by_recent_date(monkeypatch):
    db_path = Path(f"data/.test_visitor_access_date_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()

    yesterday = datetime.now(LOCAL_TZ).date() - timedelta(days=1)
    yesterday_visited_at = datetime.combine(yesterday, datetime.min.time(), tzinfo=LOCAL_TZ).astimezone(timezone.utc)
    old_visited_at = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
    store.record_visitor_access(
        "10.20.xxx.xxx",
        "GET",
        "/yesterday",
        "drafts",
        200,
        "Mobile Chrome",
        visited_at=yesterday_visited_at.isoformat(),
    )
    store.record_visitor_access(
        "88.99.xxx.xxx",
        "GET",
        "/too-old",
        "drafts",
        200,
        "Chrome",
        visited_at=old_visited_at,
    )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    html = client.get(f"/operations?access_date={yesterday.isoformat()}").data.decode("utf-8")

    assert "접속자 현황" in html
    assert "10.20.xxx.xxx" in html
    assert "GET /yesterday" in html
    assert "88.99.xxx.xxx" not in html
    assert "/too-old" not in html


def test_cloudflare_tunnel_status_detects_active_connection(tmp_path, monkeypatch):
    log_path = tmp_path / "cloudflare.log"
    log_path.write_text(
        "\n".join(
            [
                "ERR Serve tunnel error",
                "INF | https://active-sample.trycloudflare.com |",
                "INF Registered tunnel connection",
            ]
        ),
        encoding="utf-8",
    )

    from news_summary import web as web_module

    monkeypatch.setattr(web_module, "_cloudflared_running", lambda: True)

    status = web_module._cloudflare_quick_tunnel_status(log_path)

    assert status["public_url"] == "https://active-sample.trycloudflare.com"
    assert status["label"] == "외부 접속 정상"


def test_cloudflare_tunnel_status_detects_reconnecting_log(tmp_path, monkeypatch):
    log_path = tmp_path / "cloudflare.log"
    log_path.write_text(
        "\n".join(
            [
                "INF | https://stale-sample.trycloudflare.com |",
                "INF Registered tunnel connection",
                "ERR failed to serve tunnel connection",
                "ERR Serve tunnel error",
            ]
        ),
        encoding="utf-8",
    )

    from news_summary import web as web_module

    monkeypatch.setattr(web_module, "_cloudflared_running", lambda: True)

    status = web_module._cloudflare_quick_tunnel_status(log_path)

    assert status["public_url"] == "https://stale-sample.trycloudflare.com"
    assert status["label"] == "터널 재연결 중"


def test_healthz_reports_database_status(monkeypatch):
    db_path = Path(f"data/.test_healthz_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    response = client.get("/healthz")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["database"] == "ok"
    assert payload["ok"] is True
    assert payload["auto_collector"] in {"enabled", "running", "disabled", "unavailable"}


def test_operations_page_creates_and_restores_backup(monkeypatch):
    db_path = Path(f"data/.test_operations_backup_{uuid4().hex}.sqlite").resolve()
    backup_dir = Path(f"data/tmp/test_operations_backups_{uuid4().hex}").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_BACKUP_DIR", str(backup_dir))

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    backup_response = client.post("/operations/backup", follow_redirects=True)
    backup_html = backup_response.data.decode("utf-8")
    backups = sorted(backup_dir.glob("*.zip"))

    assert backup_response.status_code == 200
    assert "백업을 생성했습니다" in backup_html
    assert len(backups) == 1

    backup_name = backups[0].name
    preview_response = client.post(
        "/operations/restore",
        data={"backup_name": backup_name, "action": "preview"},
        follow_redirects=True,
    )
    preview_html = preview_response.data.decode("utf-8")

    assert "복구 대상:" in preview_html
    assert "data/.test_operations_backup_" in preview_html

    blocked_response = client.post(
        "/operations/restore",
        data={"backup_name": backup_name, "action": "restore"},
        follow_redirects=True,
    )
    blocked_html = blocked_response.data.decode("utf-8")

    assert "확인 체크박스" in blocked_html

    restore_response = client.post(
        "/operations/restore",
        data={"backup_name": backup_name, "action": "restore", "confirm_restore": "yes"},
        follow_redirects=True,
    )
    restore_html = restore_response.data.decode("utf-8")

    assert restore_response.status_code == 200
    assert "백업을 복구했습니다" in restore_html
    assert len(list(backup_dir.glob("*.zip"))) >= 2


def test_gemini_usage_page_is_separate_from_dashboard(monkeypatch):
    db_path = Path(f"data/.test_gemini_usage_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_GEMINI_LITE_UNTIL", "")
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
    legacy_release_id = store.add_press_release(
        PressRelease(
            source_id="test",
            source_name="테스트",
            region="전남",
            title="Gemini 과거 사용량 테스트",
            url="https://example.com/gemini-usage-legacy",
            content="테스트 본문입니다.",
            published_at="2026-05-20",
        )
    )
    assert legacy_release_id is not None
    store.add_article_draft(
        ArticleDraft(
            press_release_id=legacy_release_id,
            title="Gemini 과거 초안",
            body="본문입니다.",
            review_note="메모",
            model="gemini-3.1-flash-lite:gemini",
        )
    )

    app = create_app()
    app.testing = True
    client = app.test_client()

    response = client.get("/gemini-usage")
    html = response.data.decode("utf-8")

    assert response.status_code == 200
    assert 'class="gemini-usage"' in html
    assert "전체 2회" in html
    assert "현재 사용 모델:" in html
    assert "Gemini Flash 1회" in html
    assert "과거 사용 기록:" not in html
    assert "Gemini Lite 1회" in html
    assert "많이 쓴 모델" not in html
    assert "사용량 초기화" in html
    assert "Google AI Studio 사용량 확인" in html
    assert "운영 로그" not in html

    reset_response = client.post("/gemini-usage/reset", follow_redirects=True)
    reset_html = reset_response.data.decode("utf-8")

    assert reset_response.status_code == 200
    assert "전체 0회" in reset_html
    assert "초기화 시각:" in reset_html
    assert Store(db_path).counts()["drafts"] == 2


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
    assert "window.setInterval" not in html
    assert "window.setTimeout(poll" in html

    response = client.post("/recrawl", data={"limit": "10"}, follow_redirects=True)
    assert response.status_code == 200
    assert collector.calls == [(10, 290, "수동 재수집")]

    status = client.get("/recrawl/status").get_json()
    assert status["progress_current"] == 2
    assert status["progress_total"] == 19
    assert "next_run_at" in status
    assert "gemini_cooldown_until" in status


def test_recrawl_status_uses_persisted_auto_collector_snapshot(monkeypatch):
    db_path = Path(f"data/.test_recrawl_persisted_status_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))

    from news_summary.scheduler import AUTO_COLLECT_STATUS_KEY
    from news_summary.web import create_app

    store = Store(db_path)
    store.init_db()
    store.set_app_metadata(
        AUTO_COLLECT_STATUS_KEY,
        (
            '{"enabled": true, "running": true, "active_label": "자동 수집", '
            '"progress_current": 7, "progress_total": 29, '
            '"progress_message": "7/29 수집 중", "progress_source_name": "전남광주통합특별시청 보도자료", '
            '"progress_phase": "collecting", "last_error": null, '
            '"last_started_at": "2026-07-06T04:36:22+00:00", "last_finished_at": null, '
            '"last_auto_finished_at": "2026-07-03T04:15:43+00:00", "next_run_at": null, '
            '"run_count": 0, "status_updated_at": "2026-07-06T04:39:54+00:00"}'
        ),
    )

    class IdleCollector:
        def snapshot(self):
            return AutoCollectorStatus(enabled=True, progress_total=29, progress_message="대기 중")

    app = create_app()
    app.config["AUTO_COLLECTOR"] = IdleCollector()
    app.testing = True
    client = app.test_client()

    status = client.get("/recrawl/status").get_json()

    assert status["running"] is True
    assert status["active_label"] == "자동 수집"
    assert status["progress_current"] == 7
    assert status["progress_message"] == "7/29 수집 중"
    assert status["progress_source_name"] == "시청 보도자료"


def test_draft_actions_can_advance_to_next_review_item(monkeypatch):
    db_path = Path(f"data/.test_next_review_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()
    draft_ids = []
    for index in range(2):
        release_id = store.add_press_release(
            PressRelease(
                source_id="gwangju-city",
                source_name="광주광역시청 보도자료",
                region="광주",
                title=f"검수 큐 테스트 {index}",
                url=f"https://example.com/next-review-{index}",
                content="광주시는 새 사업을 추진한다고 밝혔다.",
                published_at="2026-05-20",
            )
        )
        assert release_id is not None
        draft_ids.append(
            store.add_article_draft(
                ArticleDraft(
                    press_release_id=release_id,
                    title=f"검수 큐 초안 {index}",
                    body="첫 문단입니다.\n\n둘째 문단입니다.\n\n셋째 문단입니다.",
                    review_note="메모",
                    model="gemini-3.5-flash:gemini",
                )
            )
        )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    next_response = client.get("/drafts/next", follow_redirects=False)
    assert next_response.headers["Location"].endswith(f"/drafts/{draft_ids[-1]}")

    detail_html = client.get(f"/drafts/{draft_ids[-1]}").data.decode("utf-8")
    assert "다음 검수할 기사" in detail_html
    assert "승인 후 다음" in detail_html

    response = client.post(
        f"/drafts/{draft_ids[-1]}",
        data={
            "title": "승인할 제목",
            "body": "첫 문단입니다.\n\n둘째 문단입니다.\n\n셋째 문단입니다.",
            "review_note": "확인",
            "status": "needs_review",
            "action": "approved_next",
        },
        follow_redirects=False,
    )

    assert response.headers["Location"].endswith(f"/drafts/{draft_ids[0]}")
    assert Store(db_path).get_draft(draft_ids[-1])["status"] == "approved"


def test_draft_history_records_and_restores_previous_version(monkeypatch):
    db_path = Path(f"data/.test_draft_history_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()
    release_id = store.add_press_release(
        PressRelease(
            source_id="gwangju-city",
            source_name="광주광역시청 보도자료",
            region="광주",
            title="이력 테스트 원문",
            url="https://example.com/history-test",
            content="광주시는 새 사업을 추진한다고 밝혔다.",
            published_at="2026-05-20",
        )
    )
    assert release_id is not None
    draft_id = store.add_article_draft(
        ArticleDraft(
            press_release_id=release_id,
            title="처음 제목",
            body="처음 본문입니다.",
            review_note="처음 메모",
            model="gemini-3.5-flash:gemini",
        )
    )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    update = client.post(
        f"/drafts/{draft_id}",
        data={
            "title": "수정 제목",
            "body": "수정 본문입니다.",
            "review_note": "수정 메모",
            "status": "needs_review",
        },
        follow_redirects=True,
    )
    html = update.data.decode("utf-8")
    assert "수정 이력" in html
    assert "처음 제목" in html
    history = Store(db_path).draft_history(draft_id)
    assert len(history) == 1

    restore = client.post(
        f"/drafts/{draft_id}/history/{history[0]['id']}/restore",
        follow_redirects=True,
    )

    assert restore.status_code == 200
    restored = Store(db_path).get_draft(draft_id)
    assert restored["title"] == "처음 제목"
    assert len(Store(db_path).draft_history(draft_id)) == 2


def test_export_defaults_to_unexported_and_supports_today_scope(monkeypatch):
    db_path = Path(f"data/.test_export_scopes_{uuid4().hex}.sqlite").resolve()
    export_dir = Path(f"data/tmp/.test_exports_{uuid4().hex}").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_EXPORT_DIR", str(export_dir))
    store = Store(db_path)
    store.init_db()
    draft_ids = []
    for index in range(2):
        release_id = store.add_press_release(
            PressRelease(
                source_id="gwangju-city",
                source_name="광주광역시청 보도자료",
                region="광주",
                title=f"내보내기 테스트 {index}",
                url=f"https://example.com/export-{index}",
                content="광주시는 새 사업을 추진한다고 밝혔다.",
                published_at="2026-05-20",
            )
        )
        assert release_id is not None
        draft_id = store.add_article_draft(
            ArticleDraft(
                press_release_id=release_id,
                title=f"내보내기 초안 {index}",
                body="첫 문단입니다.\n\n둘째 문단입니다.\n\n셋째 문단입니다.",
                review_note="메모",
                model="gemini-3.5-flash:gemini",
            )
        )
        store.update_draft(
            draft_id,
            title=f"내보내기 초안 {index}",
            body="첫 문단입니다.\n\n둘째 문단입니다.\n\n셋째 문단입니다.",
            review_note="메모",
            status="approved",
        )
        draft_ids.append(draft_id)
    store.mark_exported([draft_ids[0]])

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    response = client.post("/export", data={"scope": "unexported"}, follow_redirects=True)
    html = response.data.decode("utf-8")

    assert response.status_code == 200
    assert "미내보내기 승인 기사 1건" in html

    today_response = client.post("/export", data={"scope": "today"}, follow_redirects=True)
    today_html = today_response.data.decode("utf-8")

    assert today_response.status_code == 200
    assert "오늘 승인 기사 2건" in today_html


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


def test_refine_route_reports_gemini_cooldown_without_calling_api(monkeypatch):
    db_path = Path(f"data/.test_refine_cooldown_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))

    store = Store(db_path)
    store.init_db()
    press_release_id = store.add_press_release(
        PressRelease(
            source_id="sinan",
            source_name="신안군청 보도자료",
            region="전남 신안",
            title="교육 프로그램 운영",
            url="https://example.com/refine-cooldown-test",
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
    cooldown_until = datetime.now(LOCAL_TZ) + timedelta(minutes=20)
    store.set_app_metadata("gemini_cooldown_until", cooldown_until.isoformat())

    calls = []

    def fail_refine(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Gemini refine should not run during cooldown")

    monkeypatch.setattr("news_summary.web.refine_draft_with_gemini", fail_refine)
    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    detail_html = client.get(f"/drafts/{draft_id}").data.decode("utf-8")
    assert "Gemini 쿨다운 중:" in detail_html
    assert "수동 다듬기를 보류합니다." in detail_html
    assert 'data-gemini-cooldown-until="' in detail_html

    response = client.post(
        f"/drafts/{draft_id}/refine",
        data={
            "title": "수정 중 제목",
            "body": "수정 중 본문",
            "review_note": "수정 중 메모",
            "status": "needs_review",
            "refine_instruction": "",
            "preset_instruction": "",
        },
        follow_redirects=True,
    )
    html = response.data.decode("utf-8")

    assert response.status_code == 200
    assert "Gemini 쿨다운 중:" in html
    assert "수동 다듬기를 보류합니다." in html
    assert calls == []
    assert Store(db_path).get_draft(draft_id)["title"] == "기존 제목"


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
