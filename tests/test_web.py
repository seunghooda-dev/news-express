from datetime import date, datetime, timedelta, timezone
import json
import os
import re
from pathlib import Path
from uuid import uuid4

import httpx

from news_summary.models import ArticleDraft, PressRelease, PressReleaseAsset, Source
from news_summary.scheduler import (
    AUTO_COLLECTION_ANOMALY_STATUS_KEY,
    AUTO_DAILY_REPORT_KEY,
    AUTO_OPERATIONS_SUMMARY_STATUS_KEY,
    AUTO_QUEUE_DRAIN_STATUS_KEY,
    AUTO_SERVER_HEALTH_STATUS_KEY,
    AutoCollectorStatus,
)
from news_summary.storage import Store
from news_summary.web import (
    _asset_request_headers,
    _backup_health_payload,
    _collection_check_coverage_report,
    _date_warning,
    _db_health_report,
    _draft_conversion_coverage_report,
    _filter_drafts_by_review,
    _filter_drafts_by_date,
    _filter_drafts_by_query,
    _group_drafts_by_recent_dates,
    _max_asset_preview_bytes,
    _operations_snapshot_freshness_payload,
    _recovery_candidate_report,
    _service_health_summary,
    _source_coverage_report,
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
            assets=[
                PressReleaseAsset(
                    url="https://example.com/body-count-photo.jpg",
                    title="현장 사진",
                    filename="body-count-photo.jpg",
                    content_type="image/jpeg",
                    asset_type="image",
                    is_image=True,
                )
            ],
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
    assert 'class="mobile-review-bar" aria-label="빠른 검수 작업"' not in html
    assert '<button type="submit" name="action" value="approved_next">승인 후 다음</button>' not in html
    assert 'class="mobile-review-spacer" aria-hidden="true"' not in html
    assert "승인 전 체크" not in html
    assert "수정 이력" not in html
    assert "첨부 사진/파일" in html
    assert "https://example.com/body-count-photo.jpg" in html
    assert "data-image-fallback" in html
    assert 'data-fallback-src="https://example.com/body-count-photo.jpg"' in html
    assert 'src="/press-releases/assets/' in html
    assert "/preview" in html
    assert "미리보기 없음" in html
    assert "fallbackAttempted" in html
    assert 'image.addEventListener("error", tryFallbackOrShow' in html
    assert "현장 사진" in html
    assert f'href="/press-releases/assets/' in html
    assert "다운로드" in html
    assert "이미지 없음" not in html
    assert html.index("본문 총 글자수:") < html.index("첨부 사진/파일") < html.index("검수 메모")


def test_base_template_versions_static_stylesheet(monkeypatch):
    db_path = Path(f"data/.test_static_asset_version_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_GIT_COMMIT", "abcdef1234567890")

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    html = client.get("/").data.decode("utf-8")

    assert 'href="/static/app.css?v=abcdef1"' in html


def test_article_details_show_no_image_marker_when_only_file_assets(monkeypatch):
    db_path = Path(f"data/.test_no_image_marker_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()
    release_id = store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 군청",
            region="전남",
            title="파일 첨부 테스트 원문",
            url="https://example.com/file-only",
            content="테스트 군은 파일 첨부 기능을 점검한다고 밝혔다.",
            published_at="2026-05-20",
            assets=[
                PressReleaseAsset(
                    url="https://example.com/press-release.hwp",
                    title="보도자료 문서",
                    filename="press-release.hwp",
                    content_type="application/x-hwp",
                    asset_type="file",
                    is_image=False,
                )
            ],
        )
    )
    assert release_id is not None
    draft_id = store.add_article_draft(
        ArticleDraft(
            press_release_id=release_id,
            title="파일 첨부 테스트 초안",
            body="파일 첨부가 있는 테스트 기사입니다.",
            review_note="메모",
            model="gemini-3.5-flash:gemini",
        )
    )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    draft_html = client.get(f"/drafts/{draft_id}").data.decode("utf-8")
    release_html = client.get(f"/press-releases/{release_id}").data.decode("utf-8")

    for html in (draft_html, release_html):
        assert "첨부 사진/파일" in html
        assert "이미지 없음" in html
        assert "보도자료 문서" in html
        assert "press-release.hwp" in html
        assert f'href="/press-releases/assets/' in html
        assert "다운로드" in html


def test_drafts_list_shows_thumbnail_or_no_image_marker(monkeypatch):
    db_path = Path(f"data/.test_draft_list_thumbnails_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()
    image_release_id = store.add_press_release(
        PressRelease(
            source_id="gwangyang",
            source_name="광양시청 보도자료",
            region="전남 광양",
            title="이미지 포함 원문",
            url="https://example.com/image-release",
            content="광양시는 교육생을 모집한다고 밝혔다.",
            published_at="2026-05-20",
            assets=[
                PressReleaseAsset(
                    url="https://example.com/gwangyang-thumb.jpg",
                    title="교육 현장 사진",
                    filename="gwangyang-thumb.jpg",
                    content_type="image/jpeg",
                    asset_type="image",
                    is_image=True,
                )
            ],
        )
    )
    no_image_release_id = store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 군청",
            region="전남",
            title="이미지 없는 원문",
            url="https://example.com/no-image-release",
            content="테스트 군은 보도자료를 배포했다고 밝혔다.",
            published_at="2026-05-19",
        )
    )
    assert image_release_id is not None
    assert no_image_release_id is not None
    image_draft_id = store.add_article_draft(
        ArticleDraft(
            press_release_id=image_release_id,
            title="광양시, 농산물 온라인 홍보 돕는 교육생 모집",
            body="본문입니다.",
            review_note="메모",
            model="gemini-3.5-flash:gemini",
        )
    )
    store.add_article_draft(
        ArticleDraft(
            press_release_id=no_image_release_id,
            title="이미지 없는 초안",
            body="본문입니다.",
            review_note="메모",
            model="gemini-3.5-flash:gemini",
        )
    )
    thumbnail_asset_id = store.press_release_assets(image_release_id)[0]["id"]
    thumbnail_src = f'src="/press-releases/assets/{thumbnail_asset_id}/preview"'

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    html = client.get("/drafts?status=needs_review").data.decode("utf-8")
    dashboard_html = client.get("/").data.decode("utf-8")

    assert 'class="row draft-row"' in html
    assert f'href="/drafts/{image_draft_id}"' in html
    assert thumbnail_src in html
    assert '<img src="https://example.com/gwangyang-thumb.jpg"' not in html
    assert 'data-image-fallback' in html
    assert 'data-fallback-src="https://example.com/gwangyang-thumb.jpg"' in html
    assert 'alt="교육 현장 사진"' in html
    assert "미리보기 없음" in html
    assert html.index(thumbnail_src) < html.index(
        "광양시, 농산물 온라인 홍보 돕는 교육생 모집"
    )
    assert "이미지 없음" in html
    assert 'class="row draft-row"' in dashboard_html
    assert f'href="/drafts/{image_draft_id}"' in dashboard_html
    assert thumbnail_src in dashboard_html
    assert '<img src="https://example.com/gwangyang-thumb.jpg"' not in dashboard_html
    assert 'data-image-fallback' in dashboard_html
    assert 'data-fallback-src="https://example.com/gwangyang-thumb.jpg"' in dashboard_html
    assert dashboard_html.index(thumbnail_src) < dashboard_html.index(
        "광양시, 농산물 온라인 홍보 돕는 교육생 모집"
    )


def test_dashboard_limits_pending_rows_but_counts_all_attention_items(monkeypatch):
    db_path = Path(f"data/.test_dashboard_pending_window_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()
    for index in range(25):
        release_id = store.add_press_release(
            PressRelease(
                source_id="sample",
                source_name="테스트 군청",
                region="전남",
                title=f"대시보드 속도 테스트 원문 {index}",
                url=f"https://example.com/dashboard-speed/{index}",
                content="테스트 군은 보도자료를 배포했다고 밝혔다.",
                published_at="2026-05-20",
                validation_note="제목 핵심어 0개" if index == 0 else "원문 제목과 본문 구조를 확인했습니다.",
            )
        )
        assert release_id is not None
        store.add_article_draft(
            ArticleDraft(
                press_release_id=release_id,
                title=f"대시보드 속도 테스트 초안 {index}",
                body="본문입니다.",
                review_note="메모",
                model="gemini-3.5-flash:gemini",
                created_at=f"2026-05-{index + 1:02d}T09:00:00+09:00",
            )
        )

    from news_summary import web as web_module

    limits = []
    original_listing = web_module._draft_rows_for_listing

    def listing_spy(*args, **kwargs):
        limits.append(kwargs.get("limit"))
        return original_listing(*args, **kwargs)

    monkeypatch.setattr(web_module, "_draft_rows_for_listing", listing_spy)
    monkeypatch.setattr(web_module, "_source_summaries", lambda store, config_path: [])
    app = web_module.create_app()
    app.testing = True

    html = app.test_client().get("/").data.decode("utf-8")

    assert limits[0] == web_module.DASHBOARD_PENDING_LIMIT + 1
    assert html.count('class="row draft-row"') == web_module.DASHBOARD_PENDING_LIMIT
    assert "<span>주의 필요</span><b>1</b>" in html


def test_dashboard_reuses_source_summary_cache(monkeypatch):
    db_path = Path(f"data/.test_dashboard_source_cache_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_DASHBOARD_SOURCE_CACHE_SECONDS", "60")
    store = Store(db_path)
    store.init_db()

    from news_summary import web as web_module

    calls = []

    def fake_source_summaries(store, config_path):
        calls.append((store.display_location, str(config_path)))
        return []

    with web_module._dashboard_source_summary_cache_lock:
        web_module._dashboard_source_summary_cache.clear()
    monkeypatch.setattr(web_module, "_source_summaries", fake_source_summaries)
    app = web_module.create_app()
    app.testing = True
    client = app.test_client()

    assert client.get("/").status_code == 200
    assert client.get("/").status_code == 200
    assert len(calls) == 1


def test_article_views_hide_previously_saved_decorative_images(monkeypatch):
    db_path = Path(f"data/.test_hide_decorative_assets_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()
    release_id = store.add_press_release(
        PressRelease(
            source_id="gwangyang",
            source_name="광양시청 보도자료",
            region="전남 광양",
            title="장식 이미지 제외 테스트 원문",
            url="https://example.com/decorative-assets",
            content="광양시는 농업촬영 교육생을 모집한다고 밝혔다.",
            published_at="2026-05-20",
            assets=[
                PressReleaseAsset(
                    url="https://example.com/images/main-banner.jpg",
                    title="시정 홍보 이미지",
                    filename="main-banner.jpg",
                    content_type="image/jpeg",
                    asset_type="image",
                    is_image=True,
                ),
                PressReleaseAsset(
                    url="https://example.com/upload/editor/press-photo.jpg",
                    title="농업촬영 교육 사진",
                    filename="press-photo.jpg",
                    content_type="image/jpeg",
                    asset_type="image",
                    is_image=True,
                ),
                PressReleaseAsset(
                    url="https://example.com/download?fileId=7&fileName=press.hwp",
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
    press_photo_asset_id = next(
        asset["id"] for asset in store.press_release_assets(release_id) if asset["filename"] == "press-photo.jpg"
    )
    draft_id = store.add_article_draft(
        ArticleDraft(
            press_release_id=release_id,
            title="광양시, 농업촬영 교육생 모집",
            body="본문입니다.",
            review_note="메모",
            model="gemini-3.5-flash:gemini",
        )
    )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    detail_html = client.get(f"/drafts/{draft_id}").data.decode("utf-8")
    drafts_html = client.get("/drafts?status=needs_review").data.decode("utf-8")

    assert "main-banner.jpg" not in detail_html
    assert "main-banner.jpg" not in drafts_html
    assert "press-photo.jpg" in detail_html
    assert "press.hwp" in detail_html
    assert f'src="/press-releases/assets/{press_photo_asset_id}/preview"' in drafts_html
    assert '<img src="https://example.com/upload/editor/press-photo.jpg"' not in drafts_html
    assert 'data-fallback-src="https://example.com/upload/editor/press-photo.jpg"' in drafts_html


def test_article_detail_rewrites_jeonnam_governor_source_links(monkeypatch):
    db_path = Path(f"data/.test_jeonnam_public_source_link_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()
    old_url = "https://governor.jeonnam.go.kr/boardView.do?pageId=jngj22&boardId=JG_0000000003&seq=78"
    public_url = (
        "https://www.jeonnam-gwangju.go.kr/boardView.do?"
        "pageId=jngj22&amp;boardId=JG_0000000003&amp;seq=78"
    )
    release_id = store.add_press_release(
        PressRelease(
            source_id="jeonnam-province",
            source_name="전남광주통합특별시청 보도자료",
            region="전남광주통합특별시",
            title="무등산권 세계지질공원 국제협력",
            url=old_url,
            content="전남광주통합특별시는 세계지질공원 국제협력을 추진한다고 밝혔다.",
            published_at="2026-07-08",
            assets=[
                PressReleaseAsset(
                    url="https://governor.jeonnam.go.kr/imageView/cardnews",
                    title="장마철 안전수칙",
                    filename="cardnews",
                    content_type="image/jpeg",
                    asset_type="image",
                    is_image=True,
                ),
                PressReleaseAsset(
                    url="https://www.jeonnam-gwangju.go.kr/fileDownload.do?fileSe=BB&fileSn=2&boardId=JG_0000000003&seq=78",
                    title="스페인 그라나다 세계지질공원 방문단 (1).jpeg",
                    filename="스페인 그라나다 세계지질공원 방문단 (1).jpeg",
                    content_type="image/jpeg",
                    asset_type="image",
                    is_image=True,
                ),
            ],
        )
    )
    assert release_id is not None
    draft_id = store.add_article_draft(
        ArticleDraft(
            press_release_id=release_id,
            title="전남광주통합특별시, 세계지질공원 국제협력 논의",
            body="본문입니다.",
            review_note="메모",
            model="gemini-3.5-flash:gemini",
        )
    )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    draft_html = client.get(f"/drafts/{draft_id}").data.decode("utf-8")
    release_html = client.get(f"/press-releases/{release_id}").data.decode("utf-8")

    for html in (draft_html, release_html):
        assert public_url in html
        assert old_url not in html
        assert "장마철 안전수칙" not in html
        assert "스페인 그라나다 세계지질공원 방문단 (1).jpeg" in html


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


def test_drafts_page_filters_assets_and_models(monkeypatch):
    db_path = Path(f"data/.test_drafts_asset_model_filters_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()

    flash_release_id = store.add_press_release(
        PressRelease(
            source_id="sample-flash",
            source_name="테스트 기관",
            region="전남",
            title="사진 있는 Flash 초안 원문",
            url="https://example.com/drafts-flash-image",
            content="사진이 포함된 Flash 초안 원문입니다.",
            published_at="2026-07-10",
            assets=[
                PressReleaseAsset(
                    url="https://example.com/drafts-flash-image.jpg",
                    title="Flash 사진",
                    filename="drafts-flash-image.jpg",
                    content_type="image/jpeg",
                    asset_type="image",
                    is_image=True,
                )
            ],
        )
    )
    lite_release_id = store.add_press_release(
        PressRelease(
            source_id="sample-lite",
            source_name="테스트 기관",
            region="전남",
            title="이미지 없는 Lite 초안 원문",
            url="https://example.com/drafts-lite-no-image",
            content="이미지가 없는 Lite 초안 원문입니다.",
            published_at="2026-07-10",
        )
    )
    lite_image_release_id = store.add_press_release(
        PressRelease(
            source_id="sample-lite-image",
            source_name="테스트 기관",
            region="전남",
            title="사진 있는 Lite 초안 원문",
            url="https://example.com/drafts-lite-image",
            content="사진이 포함된 Lite 초안 원문입니다.",
            published_at="2026-07-10",
            validation_note="제목 핵심어 0개",
            assets=[
                PressReleaseAsset(
                    url="https://example.com/drafts-lite-image.jpg",
                    title="Lite 사진",
                    filename="drafts-lite-image.jpg",
                    content_type="image/jpeg",
                    asset_type="image",
                    is_image=True,
                )
            ],
        )
    )
    assert flash_release_id is not None
    assert lite_release_id is not None
    assert lite_image_release_id is not None

    store.add_article_draft(
        ArticleDraft(
            press_release_id=flash_release_id,
            title="사진 있는 Flash 초안",
            body="본문입니다.",
            review_note="",
            model="gemini-3.5-flash:gemini",
        )
    )
    store.add_article_draft(
        ArticleDraft(
            press_release_id=lite_release_id,
            title="이미지 없는 Lite 초안",
            body="본문입니다.",
            review_note="",
            model="gemini-3.1-flash-lite:gemini",
        )
    )
    store.add_article_draft(
        ArticleDraft(
            press_release_id=lite_image_release_id,
            title="사진 있는 Lite 초안",
            body="본문입니다.",
            review_note="",
            model="gemini-3.1-flash-lite:gemini",
        )
    )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    asset_page = client.get("/drafts?asset=with").data.decode("utf-8")
    lite_page = client.get("/drafts?model=lite").data.decode("utf-8")
    combined_page = client.get("/drafts?status=needs_review&asset=with&model=lite").data.decode("utf-8")

    assert "사진 포함 기사" in asset_page
    assert "사진 있는 Flash 초안" in asset_page
    assert "사진 있는 Lite 초안" in asset_page
    assert "이미지 없는 Lite 초안" not in asset_page

    assert "Gemini Lite 기사" in lite_page
    assert "이미지 없는 Lite 초안" in lite_page
    assert "사진 있는 Lite 초안" in lite_page
    assert "사진 있는 Flash 초안" not in lite_page
    assert '<option value="lite" selected>Gemini Lite</option>' in lite_page

    assert "Gemini Lite 검수 대기" in combined_page
    assert "사진 있는 Lite 초안" in combined_page
    assert "사진 있는 Flash 초안" not in combined_page
    assert "이미지 없는 Lite 초안" not in combined_page
    assert 'href="/drafts?status=needs_review"' in combined_page
    assert 'class="row-meta row-meta-main"' in combined_page
    assert 'class="row-meta row-meta-secondary"' in combined_page


def test_drafts_page_shows_active_filter_chips(monkeypatch):
    db_path = Path(f"data/.test_drafts_active_filter_chips_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()

    release_id = store.add_press_release(
        PressRelease(
            source_id="jindo-county",
            source_name="진도군청 보도자료",
            region="전남 진도",
            title="모집 공고가 있는 사진 기사",
            url="https://example.com/drafts-active-filters",
            content="진도군은 모집 공고를 안내했다.",
            published_at="2026-07-10",
            assets=[
                PressReleaseAsset(
                    url="https://example.com/drafts-active.jpg",
                    title="현장 사진",
                    filename="drafts-active.jpg",
                    content_type="image/jpeg",
                    asset_type="image",
                    is_image=True,
                )
            ],
        )
    )
    assert release_id is not None
    store.add_article_draft(
        ArticleDraft(
            press_release_id=release_id,
            title="진도군 모집 기사",
            body="본문입니다.",
            review_note="",
            model="gemini-3.1-flash-lite:gemini",
        )
    )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    html = client.get(
        "/drafts?status=needs_review&review=application&asset=with&model=lite&source=jindo-county&q=모집"
    ).data.decode("utf-8")

    assert 'aria-label="적용 중인 필터"' in html
    assert "진도군청 보도자료" in html
    assert "신청·모집" in html
    assert "사진 포함" in html
    assert "Gemini Lite" in html
    assert "검색: 모집" in html
    assert (
        'href="/drafts?status=needs_review&amp;review=application&amp;asset=with&amp;model=lite&amp;q=%EB%AA%A8%EC%A7%91"'
        in html
    )
    assert (
        'href="/drafts?status=needs_review&amp;review=application&amp;asset=with&amp;model=lite&amp;source=jindo-county"'
        in html
    )


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


def test_press_releases_page_filters_missing_drafts_date_source_and_query(monkeypatch):
    db_path = Path(f"data/.test_releases_filters_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()

    today = datetime.now(LOCAL_TZ).date()
    yesterday = today - timedelta(days=1)
    missing_today_id = store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 기관",
            region="전남",
            title="오늘 미변환 원문",
            url="https://example.com/missing-today",
            content="특별검색 원문 내용입니다.",
            published_at=today.isoformat(),
        )
    )
    assert missing_today_id is not None
    drafted_today_id = store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 기관",
            region="전남",
            title="오늘 초안 생성 원문",
            url="https://example.com/drafted-today",
            content="초안 생성 내용입니다.",
            published_at=today.isoformat(),
        )
    )
    assert drafted_today_id is not None
    store.add_article_draft(
        ArticleDraft(
            press_release_id=drafted_today_id,
            title="오늘 생성 초안",
            body="본문입니다.",
            review_note="",
            model="gemini-3.5-flash:gemini",
        )
    )
    store.add_press_release(
        PressRelease(
            source_id="other",
            source_name="다른 기관",
            region="전남",
            title="어제 미변환 원문",
            url="https://example.com/missing-yesterday",
            content="어제 내용입니다.",
            published_at=yesterday.isoformat(),
        )
    )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    missing_page = client.get("/press-releases?draft=missing").data.decode("utf-8")
    drafted_page = client.get("/press-releases?draft=drafted").data.decode("utf-8")
    today_missing_page = client.get(f"/press-releases?draft=missing&date={today.isoformat()}").data.decode("utf-8")
    source_page = client.get("/press-releases?source=other").data.decode("utf-8")
    query_page = client.get("/press-releases?q=특별검색").data.decode("utf-8")

    assert "초안 없는 원문" in missing_page
    assert "오늘 미변환 원문" in missing_page
    assert "어제 미변환 원문" in missing_page
    assert "오늘 초안 생성 원문" not in missing_page
    assert "초안 생성 원문" in drafted_page
    assert "오늘 초안 생성 원문" in drafted_page
    assert "오늘 미변환 원문" not in drafted_page
    assert "오늘 미변환 원문" in today_missing_page
    assert "어제 미변환 원문" not in today_missing_page
    assert "어제 미변환 원문" in source_page
    assert "오늘 미변환 원문" not in source_page
    assert "오늘 미변환 원문" in query_page
    assert "오늘 초안 생성 원문" not in query_page


def test_press_releases_page_filters_models(monkeypatch):
    db_path = Path(f"data/.test_releases_model_filters_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()

    flash_release_id = store.add_press_release(
        PressRelease(
            source_id="sample-flash",
            source_name="테스트 기관",
            region="전남",
            title="Flash 원문",
            url="https://example.com/release-flash",
            content="Flash 초안이 생성된 원문입니다.",
            published_at="2026-07-10",
        )
    )
    lite_release_id = store.add_press_release(
        PressRelease(
            source_id="sample-lite",
            source_name="테스트 기관",
            region="전남",
            title="Lite 원문",
            url="https://example.com/release-lite",
            content="Lite 초안이 생성된 원문입니다.",
            published_at="2026-07-10",
        )
    )
    missing_release_id = store.add_press_release(
        PressRelease(
            source_id="sample-missing",
            source_name="테스트 기관",
            region="전남",
            title="초안 없는 원문",
            url="https://example.com/release-missing",
            content="초안이 아직 없는 원문입니다.",
            published_at="2026-07-10",
        )
    )
    assert flash_release_id is not None
    assert lite_release_id is not None
    assert missing_release_id is not None

    store.add_article_draft(
        ArticleDraft(
            press_release_id=flash_release_id,
            title="Flash 기사 초안",
            body="본문입니다.",
            review_note="",
            model="gemini-3.5-flash:gemini",
        )
    )
    store.add_article_draft(
        ArticleDraft(
            press_release_id=lite_release_id,
            title="Lite 기사 초안",
            body="본문입니다.",
            review_note="",
            model="gemini-3.1-flash-lite:gemini",
        )
    )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    lite_page = client.get("/press-releases?model=lite").data.decode("utf-8")
    flash_page = client.get("/press-releases?draft=drafted&model=flash").data.decode("utf-8")

    assert "Gemini Lite 원문" in lite_page
    assert "Lite 원문" in lite_page
    assert "Flash 원문" not in lite_page
    assert "초안 없는 원문" not in lite_page
    assert '<option value="lite" selected>Gemini Lite</option>' in lite_page
    assert 'href="/press-releases?draft=drafted&amp;asset=&amp;model=lite' in lite_page or 'href="/press-releases?draft=drafted&amp;model=lite' in lite_page

    assert "초안 생성 원문 · Gemini Flash 원문" in flash_page
    assert "Flash 원문" in flash_page
    assert "Lite 원문" not in flash_page
    assert "초안 없는 원문" not in flash_page
    assert 'class="row-content"' in flash_page
    assert 'class="row-meta row-meta-main"' in flash_page


def test_dashboard_preview_rows_split_primary_and_secondary_meta(monkeypatch):
    db_path = Path(f"data/.test_dashboard_preview_row_meta_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()

    release_id = store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 기관",
            region="전남",
            title="대시보드 미리보기 테스트 원문",
            url="https://example.com/dashboard-preview-meta",
            content="대시보드 메타 정보 테스트입니다.",
            published_at="2026-07-10",
            assets=[
                PressReleaseAsset(
                    url="https://example.com/dashboard-preview.jpg",
                    title="대시보드 사진",
                    filename="dashboard-preview.jpg",
                    content_type="image/jpeg",
                    asset_type="image",
                    is_image=True,
                )
            ],
        )
    )
    assert release_id is not None
    store.add_article_draft(
        ArticleDraft(
            press_release_id=release_id,
            title="대시보드 미리보기 테스트 초안",
            body="본문입니다.",
            review_note="",
            model="gemini-3.5-flash:gemini",
        )
    )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    html = app.test_client().get("/").data.decode("utf-8")

    assert 'class="row draft-row"' in html
    assert 'class="row-content"' in html
    assert html.count('class="row-meta row-meta-main"') >= 2
    assert html.count('class="row-meta row-meta-secondary"') >= 2


def test_press_releases_page_shows_active_filter_chips(monkeypatch):
    db_path = Path(f"data/.test_releases_active_filter_chips_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()

    release_id = store.add_press_release(
        PressRelease(
            source_id="jindo-county",
            source_name="진도군청 보도자료",
            region="전남 진도",
            title="보도 사진이 포함된 원문",
            url="https://example.com/releases-active-filters",
            content="진도군은 보도자료를 배포했다.",
            published_at="2026-07-10",
            assets=[
                PressReleaseAsset(
                    url="https://example.com/releases-active.jpg",
                    title="원문 사진",
                    filename="releases-active.jpg",
                    content_type="image/jpeg",
                    asset_type="image",
                    is_image=True,
                )
            ],
        )
    )
    assert release_id is not None
    store.add_article_draft(
        ArticleDraft(
            press_release_id=release_id,
            title="진도군 보도 기사",
            body="본문입니다.",
            review_note="",
            model="gemini-3.5-flash:gemini",
        )
    )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    html = client.get(
        "/press-releases?draft=drafted&asset=with&model=flash&source=jindo-county&q=보도"
    ).data.decode("utf-8")

    assert 'aria-label="적용 중인 필터"' in html
    assert "진도군청 보도자료" in html
    assert "초안 있음" in html
    assert "첨부 포함" in html
    assert "Gemini Flash" in html
    assert "검색: 보도" in html
    assert (
        'href="/press-releases?draft=drafted&amp;asset=with&amp;model=flash&amp;q=%EB%B3%B4%EB%8F%84"'
        in html
    )
    assert (
        'href="/press-releases?draft=drafted&amp;asset=with&amp;model=flash&amp;source=jindo-county"'
        in html
    )


def test_press_releases_page_filters_date_issues(monkeypatch):
    db_path = Path(f"data/.test_releases_date_issue_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("NEWS_SUMMARY_DATABASE_URL", "")
    store = Store(db_path)
    store.init_db()

    store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 기관",
            region="전남",
            title="게시일 미정 원문",
            url="https://example.com/date-issue",
            content="게시일이 아직 정리되지 않은 원문입니다.",
            published_at="미정",
        )
    )
    store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 기관",
            region="전남",
            title="정상 게시일 원문",
            url="https://example.com/date-ok",
            content="정상 게시일 형식 원문입니다.",
            published_at="2026-07-08 13:20",
        )
    )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    html = client.get("/press-releases?draft=date_issue").data.decode("utf-8")

    assert "게시일 확인 원문" in html
    assert "게시일 미정 원문" in html
    assert "게시일 파싱 실패" in html
    assert "정상 게시일 원문" not in html
    assert '/press-releases?draft=date_issue' in html


def test_press_releases_page_filters_asset_releases(monkeypatch):
    db_path = Path(f"data/.test_releases_asset_filter_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("NEWS_SUMMARY_DATABASE_URL", "")
    store = Store(db_path)
    store.init_db()

    store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 기관",
            region="전남",
            title="첨부 포함 원문",
            url="https://example.com/with-asset",
            content="첨부 파일이 포함된 원문입니다.",
            published_at="2026-07-08",
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
    )
    store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 기관",
            region="전남",
            title="첨부 없는 원문",
            url="https://example.com/without-asset",
            content="첨부 파일이 없는 원문입니다.",
            published_at="2026-07-08",
        )
    )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    html = client.get("/press-releases?asset=with").data.decode("utf-8")

    assert "첨부 포함 원문" in html
    assert "첨부 1개" in html
    assert "첨부 없는 원문" not in html
    assert '/press-releases?asset=with' in html


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
    assert "오늘 원문 1건이 수집돼 정상으로 봅니다" in dashboard_html
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
    release_id = store.add_press_release(
        PressRelease(
            source_id="gwangju-city",
            source_name="광주광역시청 보도자료",
            region="광주",
            title="첨부 표시 테스트 원문",
            url="https://example.com/gwangju-asset-release",
            content="광주시는 첨부 표시 기능을 점검한다고 밝혔다.",
            published_at="2026-05-20",
            assets=[
                PressReleaseAsset(
                    url="https://example.com/gwangju-photo.png",
                    title="광주 현장 사진",
                    filename="gwangju-photo.png",
                    content_type="image/png",
                    asset_type="image",
                    is_image=True,
                )
            ],
        )
    )
    assert release_id is not None
    asset_id = store.press_release_assets(release_id)[0]["id"]
    preview_src = f'src="/press-releases/assets/{asset_id}/preview"'

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
    assert "최근 첨부 사진/파일" in detail_html
    assert preview_src in detail_html
    assert '<img src="https://example.com/gwangju-photo.png"' not in detail_html
    assert 'data-fallback-src="https://example.com/gwangju-photo.png"' in detail_html
    assert "data-image-fallback" in detail_html
    assert "미리보기 없음" in detail_html
    assert "광주 현장 사진" in detail_html
    assert f'href="/press-releases/{release_id}"' in detail_html
    assert 'href="https://example.com/gwangju-photo.png"' not in detail_html
    assert 'href="/press-releases/assets/' in detail_html

    release_html = client.get(f"/press-releases/{release_id}").data.decode("utf-8")
    assert "첨부 표시 테스트 원문" in release_html
    assert "광주시는 첨부 표시 기능을 점검한다고 밝혔다." in release_html
    assert "첨부 사진/파일" in release_html
    assert "광주 현장 사진" in release_html
    assert "data-image-fallback" in release_html
    assert 'data-fallback-src="https://example.com/gwangju-photo.png"' in release_html
    assert preview_src in release_html
    assert 'href="https://example.com/gwangju-photo.png"' in release_html
    assert 'href="/press-releases/assets/' in release_html

    class FakeAssetResponse:
        content = b"fake image"
        headers = {"content-type": "image/png"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield self.content

    class FakeAssetClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url):
            assert method == "GET"
            assert url == "https://example.com/gwangju-photo.png"
            return FakeAssetResponse()

    monkeypatch.setattr("news_summary.web.httpx.Client", FakeAssetClient)
    download = client.get(f"/press-releases/assets/{asset_id}/download")
    assert download.status_code == 200
    assert download.data == b"fake image"
    assert download.headers["Content-Disposition"].startswith("attachment;")


def test_asset_download_rejects_oversized_response(monkeypatch):
    db_path = Path(f"data/.test_asset_download_too_large_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_MAX_ASSET_DOWNLOAD_MB", "1")
    store = Store(db_path)
    store.init_db()
    release_id = store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 군청",
            region="전남",
            title="큰 첨부 차단 테스트 원문",
            url="https://example.com/large-asset-release",
            content="테스트 군은 큰 첨부파일 차단 기능을 점검한다고 밝혔다.",
            published_at="2026-05-20",
            assets=[
                PressReleaseAsset(
                    url="https://example.com/large-photo.jpg",
                    title="큰 사진",
                    filename="large-photo.jpg",
                    content_type="image/jpeg",
                    asset_type="image",
                    is_image=True,
                )
            ],
        )
    )
    assert release_id is not None
    asset_id = store.press_release_assets(release_id)[0]["id"]

    class FakeLargeResponse:
        headers = {"content-type": "image/jpeg", "content-length": str(2 * 1024 * 1024)}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield b"not reached"

    class FakeAssetClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url):
            return FakeLargeResponse()

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    monkeypatch.setattr("news_summary.web.httpx.Client", FakeAssetClient)
    response = app.test_client().get(f"/press-releases/assets/{asset_id}/download")

    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/press-releases/{release_id}")


def test_asset_download_rejects_html_error_response(monkeypatch):
    db_path = Path(f"data/.test_asset_download_html_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()
    release_id = store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 군청",
            region="전남",
            title="HTML 첨부 차단 테스트 원문",
            url="https://example.com/html-asset-release",
            content="테스트 군은 HTML 오류 응답 차단 기능을 점검한다고 밝혔다.",
            published_at="2026-05-20",
            assets=[
                PressReleaseAsset(
                    url="https://example.com/photo.jpg",
                    title="사진",
                    filename="photo.jpg",
                    content_type="image/jpeg",
                    asset_type="image",
                    is_image=True,
                )
            ],
        )
    )
    assert release_id is not None
    asset_id = store.press_release_assets(release_id)[0]["id"]

    class FakeHtmlResponse:
        content = b"<!doctype html><html><body>error</body></html>"
        headers = {"content-type": "text/html; charset=utf-8"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield self.content

    class FakeAssetClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url):
            return FakeHtmlResponse()

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    monkeypatch.setattr("news_summary.web.httpx.Client", FakeAssetClient)
    response = app.test_client().get(f"/press-releases/assets/{asset_id}/download")

    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/press-releases/{release_id}")


def test_asset_download_accepts_octet_stream_when_image_magic_matches(monkeypatch):
    db_path = Path(f"data/.test_asset_download_octet_image_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()
    release_id = store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 군청",
            region="전남",
            title="이미지 시그니처 테스트 원문",
            url="https://example.com/octet-image-release",
            content="테스트 군은 이미지 첨부 기능을 점검한다고 밝혔다.",
            published_at="2026-05-20",
            assets=[
                PressReleaseAsset(
                    url="https://example.com/download?fileId=1",
                    title="첨부 사진",
                    filename="",
                    content_type="",
                    asset_type="image",
                    is_image=True,
                )
            ],
        )
    )
    assert release_id is not None
    asset_id = store.press_release_assets(release_id)[0]["id"]

    class FakeOctetImageResponse:
        content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 12
        headers = {"content-type": "application/octet-stream"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield self.content

    streamed_urls = []

    class FakeAssetClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url):
            streamed_urls.append(url)
            return FakeOctetImageResponse()

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    monkeypatch.setattr("news_summary.web.httpx.Client", FakeAssetClient)
    client = app.test_client()
    response = client.get(f"/press-releases/assets/{asset_id}/download")

    assert response.status_code == 200
    assert response.data == FakeOctetImageResponse.content
    assert response.headers["Content-Type"].startswith("image/png")
    assert ".png" in response.headers["Content-Disposition"]

    preview = client.get(f"/press-releases/assets/{asset_id}/preview")
    assert preview.status_code == 200
    assert preview.data == FakeOctetImageResponse.content
    assert preview.headers["Content-Type"].startswith("image/png")
    assert preview.headers["Cache-Control"] == "public, max-age=3600, stale-if-error=21600"
    assert preview.headers["ETag"].startswith('"asset-preview-')
    assert preview.headers["X-News-Express-Preview-Cache"] == "MISS"
    assert "Content-Disposition" not in preview.headers

    cached_preview = client.get(f"/press-releases/assets/{asset_id}/preview")
    assert cached_preview.status_code == 200
    assert cached_preview.data == FakeOctetImageResponse.content
    assert cached_preview.headers["ETag"] == preview.headers["ETag"]
    assert cached_preview.headers["X-News-Express-Preview-Cache"] == "HIT"

    not_modified = client.get(
        f"/press-releases/assets/{asset_id}/preview",
        headers={"If-None-Match": preview.headers["ETag"]},
    )
    assert not_modified.status_code == 304
    assert not_modified.data == b""
    assert not_modified.headers["ETag"] == preview.headers["ETag"]
    assert not_modified.headers["X-News-Express-Preview-Cache"] == "HIT"
    assert streamed_urls == [
        "https://example.com/download?fileId=1",
        "https://example.com/download?fileId=1",
    ]


def test_asset_request_headers_include_press_referer_for_image_assets():
    headers = _asset_request_headers(
        "https://files.example.go.kr/download/photo.jpg",
        {
            "is_image": 1,
            "press_url": "https://www.example.go.kr/news/press?idx=10&mode=view",
        },
    )

    assert headers["Referer"] == "https://www.example.go.kr/news/press?idx=10&mode=view"
    assert headers["Accept"].startswith("image/")
    assert "NewsExpress" in headers["User-Agent"]
    assert "ko-KR" in headers["Accept-Language"]


def test_asset_preview_sends_browser_like_headers(monkeypatch):
    db_path = Path(f"data/.test_asset_preview_headers_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()
    release_url = "https://www.example.go.kr/news/press?idx=20&mode=view"
    release_id = store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 군청",
            region="전남",
            title="헤더 첨부 테스트 원문",
            url=release_url,
            content="테스트 군은 첨부 미리보기 요청 헤더를 점검한다고 밝혔다.",
            published_at="2026-05-20",
            assets=[
                PressReleaseAsset(
                    url="https://files.example.go.kr/download/photo.jpg",
                    title="첨부 사진",
                    filename="photo.jpg",
                    content_type="image/jpeg",
                    asset_type="image",
                    is_image=True,
                )
            ],
        )
    )
    assert release_id is not None
    asset_id = store.press_release_assets(release_id)[0]["id"]

    class FakeImageResponse:
        content = b"\xff\xd8\xff" + b"\x00" * 12
        headers = {"content-type": "image/jpeg"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield self.content

    client_headers = []

    class FakeAssetClient:
        def __init__(self, **kwargs):
            client_headers.append(kwargs.get("headers") or {})

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url):
            assert method == "GET"
            assert url == "https://files.example.go.kr/download/photo.jpg"
            return FakeImageResponse()

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    monkeypatch.setattr("news_summary.web.httpx.Client", FakeAssetClient)
    response = app.test_client().get(f"/press-releases/assets/{asset_id}/preview")

    assert response.status_code == 200
    assert client_headers
    assert client_headers[0]["Referer"] == release_url
    assert client_headers[0]["Accept"].startswith("image/")
    assert "NewsExpress" in client_headers[0]["User-Agent"]


def test_default_asset_preview_limit_allows_large_press_photos(monkeypatch):
    monkeypatch.delenv("NEWS_SUMMARY_MAX_ASSET_PREVIEW_MB", raising=False)

    assert _max_asset_preview_bytes() == 12 * 1024 * 1024


def test_gangjin_asset_preview_redirects_to_source_image(monkeypatch):
    db_path = Path(f"data/.test_gangjin_asset_preview_redirect_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()
    gangjin_image_url = (
        "https://www.gangjin.go.kr/www/government/news/"
        "ybmodule.file/board_www/www_press/980x1x100/1783500277.jpg"
    )
    release_id = store.add_press_release(
        PressRelease(
            source_id="gangjin-county",
            source_name="강진군청 보도자료",
            region="전남 강진",
            title="강진 이미지 미리보기 테스트 원문",
            url="https://www.gangjin.go.kr/www/government/news/press?idx=664241&mode=view",
            content="강진군은 보도자료 첨부 이미지 미리보기 기능을 점검한다고 밝혔다.",
            published_at="2026-07-08",
            assets=[
                PressReleaseAsset(
                    url=gangjin_image_url,
                    title="강진 현장 사진",
                    filename="1783500277.jpg",
                    content_type="image/jpeg",
                    asset_type="image",
                    is_image=True,
                )
            ],
        )
    )
    assert release_id is not None
    asset_id = store.press_release_assets(release_id)[0]["id"]

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    response = app.test_client().get(f"/press-releases/assets/{asset_id}/preview")

    assert response.status_code == 302
    assert response.headers["Location"] == gangjin_image_url

    from news_summary.web import _should_redirect_asset_preview

    assert _should_redirect_asset_preview(
        "https://files.gangjin.go.kr/www/ybmodule.file/board_www/2026/field-photo.JPG?download=1"
    )
    assert _should_redirect_asset_preview(
        "https://www.gangjin.go.kr/www/government/news/ybmodule.file/board_www/www_press/1783500702.jpg"
    )
    assert _should_redirect_asset_preview(
        "https://www.gangjin.go.kr/www/government/news/ybmodule.file/board_www/www_press/980x1x100/1783500876.JPG"
    )
    assert not _should_redirect_asset_preview("https://www.gangjin.go.kr/download?file=1783500277.jpg")


def test_asset_preview_retries_ssl_certificate_failure_without_verification(monkeypatch):
    db_path = Path(f"data/.test_asset_preview_ssl_retry_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()
    release_id = store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 군청",
            region="전남",
            title="인증서 재시도 테스트 원문",
            url="https://example.com/ssl-retry-release",
            content="테스트 군은 이미지 인증서 재시도 기능을 점검한다고 밝혔다.",
            published_at="2026-05-20",
            assets=[
                PressReleaseAsset(
                    url="https://bad-chain.example.com/press-photo.jpg",
                    title="첨부 사진",
                    filename="press-photo.jpg",
                    content_type="image/jpeg",
                    asset_type="image",
                    is_image=True,
                )
            ],
        )
    )
    assert release_id is not None
    asset_id = store.press_release_assets(release_id)[0]["id"]

    class FakeImageResponse:
        content = b"\xff\xd8\xff" + b"\x00" * 12
        headers = {"content-type": "image/jpeg"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield self.content

    client_verify_values = []

    class FakeAssetClient:
        def __init__(self, **kwargs):
            self.verify = kwargs.get("verify", True)
            client_verify_values.append(self.verify)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url):
            if self.verify is not False:
                raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
            return FakeImageResponse()

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    monkeypatch.setattr("news_summary.web.httpx.Client", FakeAssetClient)
    response = app.test_client().get(f"/press-releases/assets/{asset_id}/preview")

    assert response.status_code == 200
    assert response.data == FakeImageResponse.content
    assert response.headers["Content-Type"].startswith("image/jpeg")
    assert response.headers["X-News-Express-Preview-Cache"] == "MISS"
    assert client_verify_values == [True, False]


def test_asset_preview_serves_stale_cache_when_refresh_fails(monkeypatch):
    db_path = Path(f"data/.test_asset_preview_stale_cache_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_ASSET_PREVIEW_CACHE_SECONDS", "1")
    monkeypatch.setenv("NEWS_SUMMARY_ASSET_PREVIEW_STALE_SECONDS", "5")
    store = Store(db_path)
    store.init_db()
    release_id = store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 군청",
            region="전남",
            title="만료 캐시 테스트 원문",
            url="https://example.com/stale-image-release",
            content="테스트 군은 이미지 미리보기 안정화 기능을 점검한다고 밝혔다.",
            published_at="2026-05-20",
            assets=[
                PressReleaseAsset(
                    url="https://example.com/download?fileId=stale",
                    title="첨부 사진",
                    filename="",
                    content_type="",
                    asset_type="image",
                    is_image=True,
                )
            ],
        )
    )
    assert release_id is not None
    asset_id = store.press_release_assets(release_id)[0]["id"]

    class FakeOctetImageResponse:
        content = b"\x89PNG\r\n\x1a\n" + b"\x11" * 12
        headers = {"content-type": "application/octet-stream"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield self.content

    stream_count = {"value": 0}

    class FakeAssetClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url):
            stream_count["value"] += 1
            if stream_count["value"] == 1:
                return FakeOctetImageResponse()
            raise httpx.ReadTimeout("temporary image host timeout")

    clock = {"value": 1000.0}

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    monkeypatch.setattr("news_summary.web.httpx.Client", FakeAssetClient)
    monkeypatch.setattr("news_summary.web.time.time", lambda: clock["value"])
    client = app.test_client()

    first = client.get(f"/press-releases/assets/{asset_id}/preview")
    assert first.status_code == 200
    assert first.data == FakeOctetImageResponse.content
    assert first.headers["X-News-Express-Preview-Cache"] == "MISS"

    clock["value"] = 1002.0
    stale = client.get(f"/press-releases/assets/{asset_id}/preview")
    assert stale.status_code == 200
    assert stale.data == FakeOctetImageResponse.content
    assert stale.headers["X-News-Express-Preview-Cache"] == "STALE"
    assert stale.headers["Cache-Control"] == "public, max-age=1, stale-if-error=5"
    assert stream_count["value"] == 2

    clock["value"] = 1007.0
    expired = client.get(f"/press-releases/assets/{asset_id}/preview")
    assert expired.status_code == 502
    assert stream_count["value"] == 3


def test_asset_download_rejects_octet_stream_when_image_magic_is_missing(monkeypatch):
    db_path = Path(f"data/.test_asset_download_bad_octet_image_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()
    release_id = store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 군청",
            region="전남",
            title="이미지 아닌 첨부 차단 테스트 원문",
            url="https://example.com/bad-octet-image-release",
            content="테스트 군은 잘못된 이미지 응답 차단 기능을 점검한다고 밝혔다.",
            published_at="2026-05-20",
            assets=[
                PressReleaseAsset(
                    url="https://example.com/download?fileId=2",
                    title="첨부 사진",
                    filename="",
                    content_type="",
                    asset_type="image",
                    is_image=True,
                )
            ],
        )
    )
    assert release_id is not None
    asset_id = store.press_release_assets(release_id)[0]["id"]

    class FakeBadOctetImageResponse:
        content = b"not an image"
        headers = {"content-type": "application/octet-stream"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield self.content

    class FakeAssetClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url):
            return FakeBadOctetImageResponse()

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    monkeypatch.setattr("news_summary.web.httpx.Client", FakeAssetClient)
    response = app.test_client().get(f"/press-releases/assets/{asset_id}/download")

    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/press-releases/{release_id}")


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
    assert "News Express" in html
    assert "로그아웃" in html

    logout = client.post("/logout", follow_redirects=False)
    assert logout.status_code == 302


def test_sensitive_routes_require_login_when_auth_is_enabled(monkeypatch):
    db_path = Path(f"data/.test_sensitive_route_auth_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_ADMIN_PASSWORD", "secret1234")
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_DISABLED", "0")

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    protected_get_paths = [
        "/",
        "/drafts",
        "/press-releases",
        "/gemini-usage",
        "/ops-logs",
        "/operations",
        "/operations/backups/example.zip",
        "/healthz/details",
    ]
    for path in protected_get_paths:
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 302, path
        assert "/login" in response.headers["Location"], path

    reset_response = client.post("/gemini-usage/reset", follow_redirects=False)
    assert reset_response.status_code == 302
    assert "/login" in reset_response.headers["Location"]

    health_response = client.get("/healthz", follow_redirects=False)
    assert health_response.status_code == 200


def test_security_headers_are_applied(monkeypatch):
    db_path = Path(f"data/.test_security_headers_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    response = client.get("/")
    assert response.headers["X-Request-ID"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "same-origin"
    assert "camera=()" in response.headers["Permissions-Policy"]
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]

    operations = client.get("/operations")
    assert operations.headers["Cache-Control"] == "no-store, max-age=0"
    assert operations.headers["Pragma"] == "no-cache"
    assert operations.headers["Expires"] == "0"

    gemini_usage = client.get("/gemini-usage")
    assert gemini_usage.headers["Cache-Control"] == "no-store, max-age=0"

    health_details = client.get("/healthz/details")
    assert health_details.headers["Cache-Control"] == "no-store, max-age=0"


def test_request_id_header_accepts_safe_incoming_value(monkeypatch):
    db_path = Path(f"data/.test_request_id_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    response = app.test_client().get("/", headers={"X-Request-ID": "support-case-123"})

    assert response.headers["X-Request-ID"] == "support-case-123"


def test_unhandled_error_response_includes_request_id(monkeypatch):
    db_path = Path(f"data/.test_request_id_error_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))

    from news_summary.web import create_app

    app = create_app()
    app.testing = False

    @app.get("/boom")
    def boom():
        raise RuntimeError("forced failure")

    response = app.test_client().get("/boom", headers={"X-Request-ID": "support-case-500"})

    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "support-case-500"
    assert "요청 ID: support-case-500" in response.data.decode("utf-8")


def test_https_security_headers_and_session_cookie_defaults_on_render(monkeypatch):
    db_path = Path(f"data/.test_render_security_headers_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-test")
    monkeypatch.setenv("NEWS_SUMMARY_ADMIN_PASSWORD", "secret1234")
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_DISABLED", "0")
    monkeypatch.setenv("NEWS_SUMMARY_SECRET_KEY", "test-secret-key-long-enough-for-session")

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    response = client.get("/", base_url="https://news-express.example.com")
    assert response.headers["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains"
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert app.config["SESSION_COOKIE_SECURE"] is True

    login = client.post(
        "/login",
        data={"password": "secret1234", "next": "/"},
        base_url="https://news-express.example.com",
    )
    cookie = login.headers["Set-Cookie"]
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie


def test_csrf_protection_rejects_missing_token_when_enabled(monkeypatch):
    db_path = Path(f"data/.test_csrf_missing_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_TEST_CSRF", "1")
    monkeypatch.setenv("NEWS_SUMMARY_ADMIN_PASSWORD", "secret1234")
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_DISABLED", "0")

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    response = client.post("/login", data={"password": "secret1234"}, follow_redirects=False)

    assert response.status_code == 400
    assert "요청 보안 토큰이 유효하지 않습니다." in response.data.decode("utf-8")


def test_csrf_protection_accepts_rendered_token_when_enabled(monkeypatch):
    db_path = Path(f"data/.test_csrf_valid_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_TEST_CSRF", "1")
    monkeypatch.setenv("NEWS_SUMMARY_ADMIN_PASSWORD", "secret1234")
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_DISABLED", "0")

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    page = client.get("/login")
    token_match = re.search(r'name="_csrf_token" value="([^"]+)"', page.data.decode("utf-8"))
    assert token_match

    response = client.post(
        "/login",
        data={"password": "secret1234", "next": "/", "_csrf_token": token_match.group(1)},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")


def test_auth_rate_limit_blocks_repeated_failed_login(monkeypatch):
    db_path = Path(f"data/.test_auth_rate_limit_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_TEST_AUTH_RATE_LIMIT", "1")
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_RATE_LIMIT_MAX_FAILURES", "2")
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_RATE_LIMIT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_RATE_LIMIT_LOCK_SECONDS", "60")
    monkeypatch.setenv("NEWS_SUMMARY_ADMIN_PASSWORD", "secret1234")
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_DISABLED", "0")

    from news_summary import web as web_module

    web_module._auth_rate_limit_attempts.clear()
    app = web_module.create_app()
    app.testing = True
    client = app.test_client()

    first = client.post("/login", data={"password": "wrong"}, follow_redirects=False)
    second = client.post("/login", data={"password": "wrong-again"}, follow_redirects=False)
    blocked = client.post("/login", data={"password": "secret1234", "next": "/"}, follow_redirects=False)

    assert first.status_code == 200
    assert second.status_code == 200
    assert blocked.status_code == 429
    assert "비밀번호 입력 시도가 많아 잠시 제한했습니다." in blocked.data.decode("utf-8")


def test_auth_rate_limit_success_clears_failed_login_count(monkeypatch):
    db_path = Path(f"data/.test_auth_rate_limit_clear_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_TEST_AUTH_RATE_LIMIT", "1")
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_RATE_LIMIT_MAX_FAILURES", "2")
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_RATE_LIMIT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_RATE_LIMIT_LOCK_SECONDS", "60")
    monkeypatch.setenv("NEWS_SUMMARY_ADMIN_PASSWORD", "secret1234")
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_DISABLED", "0")

    from news_summary import web as web_module

    web_module._auth_rate_limit_attempts.clear()
    app = web_module.create_app()
    app.testing = True
    client = app.test_client()

    client.post("/login", data={"password": "wrong"}, follow_redirects=False)
    first_success = client.post("/login", data={"password": "secret1234", "next": "/"}, follow_redirects=False)
    client.post("/logout", follow_redirects=False)
    client.post("/login", data={"password": "wrong"}, follow_redirects=False)
    second_success = client.post("/login", data={"password": "secret1234", "next": "/"}, follow_redirects=False)

    assert first_success.status_code == 302
    assert second_success.status_code == 302


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
        data={"password": "PressRoom-47-Delta", "confirm_password": "PressRoom-47-Delta"},
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
        data={"password": "PressRoom-48-Delta", "confirm_password": "PressRoom-48-Delta"},
        follow_redirects=True,
    )
    assert "관리자 로그인을 활성화했습니다." in setup.data.decode("utf-8")


def test_admin_setup_rejects_weak_password(monkeypatch):
    db_path = Path(f"data/.test_admin_setup_weak_password_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_DISABLED", "0")
    monkeypatch.delenv("NEWS_SUMMARY_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("NEWS_SUMMARY_ADMIN_PASSWORD_HASH", raising=False)

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    response = client.post(
        "/admin/setup",
        data={"password": "news1234", "confirm_password": "news1234"},
        follow_redirects=True,
    )

    html = response.data.decode("utf-8")
    assert "관리자 비밀번호가 상용 운영 기준에 약합니다" in html
    assert "12자 미만" in html
    assert "예측 쉬운 단어" in html

    protected = client.get("/", follow_redirects=False)
    assert protected.status_code == 200


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

    weak_change = client.post(
        "/operations/admin-password",
        data={"new_password": "newpass123", "confirm_password": "newpass123"},
        follow_redirects=True,
    )
    assert "새 관리자 비밀번호가 상용 운영 기준에 약합니다" in weak_change.data.decode("utf-8")

    changed = client.post(
        "/operations/admin-password",
        data={"new_password": "NextPass-48!", "confirm_password": "NextPass-48!"},
        follow_redirects=True,
    )
    assert "관리자 비밀번호를 변경했습니다." in changed.data.decode("utf-8")

    client.post("/logout")
    old_login = client.post("/login", data={"password": "oldpass123"}, follow_redirects=True)
    assert "관리자 비밀번호가 올바르지 않습니다." in old_login.data.decode("utf-8")

    new_login = client.post("/login", data={"password": "NextPass-48!", "next": "/"}, follow_redirects=True)
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
    assert 'href="/press-releases?draft=missing&amp;source=jindo-county"' in html
    assert 'href="/press-releases?draft=date_issue&amp;source=jindo-county"' in html
    assert 'href="/press-releases?asset=with&amp;source=jindo-county"' in html
    assert 'href="/press-releases?source=jindo-county"' in html
    assert 'href="/ops-logs?tab=collector&amp;q=source_id%3Djindo-county"' in html


def test_source_detail_shows_operational_counts_and_shortcuts(monkeypatch):
    db_path = Path(f"data/.test_source_detail_shortcuts_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("NEWS_SUMMARY_DATABASE_URL", "")
    store = Store(db_path)
    store.init_db()
    today_iso = datetime.now(LOCAL_TZ).date().isoformat()
    drafted_id = store.add_press_release(
        PressRelease(
            source_id="jindo-county",
            source_name="진도군청 보도자료",
            region="전남 진도",
            title="오늘 수집 원문",
            url="https://example.com/jindo-today",
            content="오늘 수집된 정상 보도자료입니다.",
            published_at=today_iso,
            assets=[
                PressReleaseAsset(
                    url="https://example.com/jindo-thumb.jpg",
                    title="현장 사진",
                    filename="jindo-thumb.jpg",
                    content_type="image/jpeg",
                    asset_type="image",
                    is_image=True,
                )
            ],
        )
    )
    assert drafted_id is not None
    store.add_article_draft(
        ArticleDraft(
            press_release_id=drafted_id,
            title="오늘 수집 원문",
            body="오늘 수집된 정상 보도자료입니다.",
            review_note="",
            model="gemini-3.5-flash:gemini",
        )
    )
    store.add_press_release(
        PressRelease(
            source_id="jindo-county",
            source_name="진도군청 보도자료",
            region="전남 진도",
            title="게시일 미정 원문",
            url="https://example.com/jindo-date-issue",
            content="게시일이 아직 정리되지 않은 보도자료입니다.",
            published_at="미정",
        )
    )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    html = client.get("/sources/jindo-county").data.decode("utf-8")

    assert f'href="/press-releases?source=jindo-county&amp;date={today_iso}"' in html
    assert 'href="/press-releases?draft=missing&amp;source=jindo-county"' in html
    assert 'href="/press-releases?draft=date_issue&amp;source=jindo-county"' in html
    assert 'href="/press-releases?asset=with&amp;source=jindo-county"' in html
    assert "운영 확인:" in html
    assert "초안 없는 원문 1건" in html
    assert "게시일 확인 원문 1건" in html
    assert "게시일 파싱 실패" in html
    assert "첨부 1개" in html


def test_source_detail_shows_recent_activity_trend(monkeypatch):
    db_path = Path(f"data/.test_source_detail_activity_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("NEWS_SUMMARY_DATABASE_URL", "")
    store = Store(db_path)
    store.init_db()
    today = datetime.now(LOCAL_TZ).date()
    yesterday = today - timedelta(days=1)
    two_days_ago = today - timedelta(days=2)

    today_release_id = store.add_press_release(
        PressRelease(
            source_id="jindo-county",
            source_name="진도군청 보도자료",
            region="전남 진도",
            title="오늘 원문",
            url="https://example.com/jindo-trend-today",
            content="오늘 원문입니다.",
            published_at=today.isoformat(),
        )
    )
    yesterday_release_id = store.add_press_release(
        PressRelease(
            source_id="jindo-county",
            source_name="진도군청 보도자료",
            region="전남 진도",
            title="어제 원문",
            url="https://example.com/jindo-trend-yesterday",
            content="어제 원문입니다.",
            published_at=yesterday.isoformat(),
        )
    )
    store.add_press_release(
        PressRelease(
            source_id="jindo-county",
            source_name="진도군청 보도자료",
            region="전남 진도",
            title="그제 원문",
            url="https://example.com/jindo-trend-two-days",
            content="그제 원문입니다.",
            published_at=two_days_ago.isoformat(),
        )
    )
    assert today_release_id is not None
    assert yesterday_release_id is not None

    today_draft_id = store.add_article_draft(
        ArticleDraft(
            press_release_id=today_release_id,
            title="오늘 초안",
            body="오늘 초안입니다.",
            review_note="",
            model="gemini-3.5-flash:gemini",
        )
    )
    yesterday_draft_id = store.add_article_draft(
        ArticleDraft(
            press_release_id=yesterday_release_id,
            title="어제 초안",
            body="어제 초안입니다.",
            review_note="",
            model="gemini-3.5-flash:gemini",
        )
    )
    assert today_draft_id is not None
    assert yesterday_draft_id is not None
    store.set_draft_status(yesterday_draft_id, "approved")

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    html = app.test_client().get("/sources/jindo-county").data.decode("utf-8")

    assert "최근 3일 수집/초안 추이" in html
    assert "원문 1건 · 초안 1건" in html
    assert "미변환 0건" in html
    assert "검수 대기 1건" in html
    assert "미변환 1건" in html
    assert f'href="/press-releases?source=jindo-county&amp;date={today.isoformat()}"' in html
    assert f'href="/press-releases?source=jindo-county&amp;date={yesterday.isoformat()}"' in html
    assert f'href="/press-releases?source=jindo-county&amp;date={two_days_ago.isoformat()}"' in html
    assert f'href="/drafts?source=jindo-county&amp;date={today.isoformat()}"' in html


def test_source_detail_highlights_activity_alerts(monkeypatch):
    db_path = Path(f"data/.test_source_detail_activity_alerts_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("NEWS_SUMMARY_DATABASE_URL", "")
    store = Store(db_path)
    store.init_db()
    today = datetime.now(LOCAL_TZ).date()
    yesterday = today - timedelta(days=1)
    two_days_ago = today - timedelta(days=2)

    yesterday_release_id = store.add_press_release(
        PressRelease(
            source_id="jindo-county",
            source_name="진도군청 보도자료",
            region="전남 진도",
            title="어제 원문",
            url="https://example.com/jindo-alert-yesterday",
            content="어제 원문입니다.",
            published_at=yesterday.isoformat(),
        )
    )
    store.add_press_release(
        PressRelease(
            source_id="jindo-county",
            source_name="진도군청 보도자료",
            region="전남 진도",
            title="그제 원문",
            url="https://example.com/jindo-alert-two-days",
            content="그제 원문입니다.",
            published_at=two_days_ago.isoformat(),
        )
    )
    assert yesterday_release_id is not None
    store.add_article_draft(
        ArticleDraft(
            press_release_id=yesterday_release_id,
            title="어제 초안",
            body="어제 초안입니다.",
            review_note="",
            model="gemini-3.5-flash:gemini",
        )
    )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    html = app.test_client().get("/sources/jindo-county").data.decode("utf-8")

    assert "오늘 수집 없음" in html
    assert "최근 3일 미변환 1건" in html
    assert "미변환 1건" in html
    assert f'href="/press-releases?source=jindo-county&amp;date={today.isoformat()}"' in html


def test_source_detail_shows_recent_collection_history(monkeypatch):
    db_path = Path(f"data/.test_source_detail_history_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("NEWS_SUMMARY_DATABASE_URL", "")
    store = Store(db_path)
    store.init_db()
    now_utc = datetime.now(timezone.utc)
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO source_collection_runs
            (source_id, source_name, status, message, failure_stage, failure_reason,
             releases_found, inserted_count, repaired_dates, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "jindo-county",
                "진도군청 보도자료",
                "failed",
                "TLS 연결 시간 초과",
                "외부 사이트 응답 지연",
                "TLS 연결 시간 초과",
                0,
                0,
                0,
                (now_utc - timedelta(minutes=40)).isoformat(),
            ),
        )
        conn.execute(
            """
            INSERT INTO source_collection_runs
            (source_id, source_name, status, message, failure_stage, failure_reason,
             releases_found, inserted_count, repaired_dates, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "jindo-county",
                "진도군청 보도자료",
                "ok",
                "자동 재검증 통과 · 원문 검증 통과 2건, 새로 저장 1건",
                "",
                "",
                2,
                1,
                0,
                (now_utc - timedelta(minutes=20)).isoformat(),
            ),
        )
        conn.execute(
            """
            INSERT INTO source_collection_runs
            (source_id, source_name, status, message, failure_stage, failure_reason,
             releases_found, inserted_count, repaired_dates, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "jindo-county",
                "진도군청 보도자료",
                "ok",
                "원문 검증 통과 3건, 새로 저장 2건",
                "",
                "",
                3,
                2,
                1,
                (now_utc - timedelta(minutes=5)).isoformat(),
            ),
        )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    html = client.get("/sources/jindo-county").data.decode("utf-8")

    assert "최근 수집 이력" in html
    assert "최근 24시간 실패 1건" in html
    assert "자동 복구 1건" in html
    assert "일시 지연" in html
    assert "자동 복구" in html
    assert "정상" in html
    assert "외부 사이트 응답 지연" in html
    assert "확인 2건 · 저장 1건" in html
    assert "확인 3건 · 저장 2건 · 보정 1건" in html


def test_source_detail_shows_failure_summary(monkeypatch):
    db_path = Path(f"data/.test_source_detail_failure_summary_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("NEWS_SUMMARY_DATABASE_URL", "")
    store = Store(db_path)
    store.init_db()
    now_utc = datetime.now(timezone.utc)
    with store.connect() as conn:
        for payload in (
            (
                "failed",
                "TLS 연결 시간 초과",
                "외부 사이트 응답 지연",
                "TLS 연결 시간 초과",
                0,
                0,
                0,
                (now_utc - timedelta(hours=6)).isoformat(),
            ),
            (
                "failed",
                "TLS 연결 시간 초과",
                "외부 사이트 응답 지연",
                "TLS 연결 시간 초과",
                0,
                0,
                0,
                (now_utc - timedelta(hours=5)).isoformat(),
            ),
            (
                "failed",
                "본문 파싱 실패",
                "본문 파싱 실패",
                "선택자 불일치",
                0,
                0,
                0,
                (now_utc - timedelta(hours=2)).isoformat(),
            ),
            (
                "ok",
                "원문 검증 통과 2건, 새로 저장 1건",
                "",
                "",
                2,
                1,
                0,
                (now_utc - timedelta(minutes=20)).isoformat(),
            ),
        ):
            status, message, failure_stage, failure_reason, releases_found, inserted_count, repaired_dates, checked_at = payload
            conn.execute(
                """
                INSERT INTO source_collection_runs
                (source_id, source_name, status, message, failure_stage, failure_reason,
                 releases_found, inserted_count, repaired_dates, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "jindo-county",
                    "진도군청 보도자료",
                    status,
                    message,
                    failure_stage,
                    failure_reason,
                    releases_found,
                    inserted_count,
                    repaired_dates,
                    checked_at,
                ),
            )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    html = client.get("/sources/jindo-county").data.decode("utf-8")

    assert "최근 실패 원인 요약" in html
    assert "최근 7일 실패 3건" in html
    assert "일시 장애 2건" in html
    assert "구조 문제 1건" in html
    assert "외부 사이트 응답 지연" in html
    assert "2건" in html
    assert "본문 파싱 실패" in html
    assert "선택자 불일치" in html
    assert "마지막 정상 수집" in html


def test_source_detail_shows_route_diagnostics(monkeypatch):
    db_path = Path(f"data/.test_source_detail_routes_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("NEWS_SUMMARY_DATABASE_URL", "")
    store = Store(db_path)
    store.init_db()
    store.set_app_metadata(
        "auto_url_discovery_snapshot",
        json.dumps(
            {
                "updated_at": "2026-07-10T14:30:00+09:00",
                "discoveries": [
                    {
                        "source_id": "route-source",
                        "source_name": "전남광주통합특별시 테스트군청 보도자료",
                        "urls": [
                            "https://example.com/discovered/press",
                            "https://example.com/discovered/news",
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
    )

    from news_summary import web as web_module

    source = Source(
        id="route-source",
        name="전남광주통합특별시 테스트군청 보도자료",
        region="전남 테스트",
        type="html",
        list_url="https://example.com/list",
        feed_url="https://example.com/feed.xml",
        base_url="https://example.com/",
        fallback_urls=[
            "https://example.com/fallback/press",
            "https://example.com/fallback/news",
        ],
    )
    monkeypatch.setattr(web_module, "_source_by_id", lambda config_path, source_id: source if source_id == "route-source" else None)
    monkeypatch.setattr(
        web_module,
        "_source_summary_by_id",
        lambda store, config_path, source_id: {
            "id": "route-source",
            "name": source.name,
            "region": source.region,
            "releases": 0,
            "yesterday_releases": 0,
            "today_releases": 0,
            "last_collected": None,
            "issue": "",
            "status_label": "점검 전",
            "status_level": "warning",
            "status_detail": "기관 설정은 있지만 아직 수집 점검 기록이 없습니다.",
            "business_gap": None,
            "consecutive_failures": 0,
            "last_status": "unknown",
            "last_checked_at": None,
            "last_message": "",
            "failure_stage": "",
            "failure_reason": "",
        },
    )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    html = app.test_client().get("/sources/route-source").data.decode("utf-8")

    assert "수집 경로 점검" in html
    assert "목록 주소" in html
    assert "기본 사이트" in html
    assert "피드 주소" in html
    assert "대체 경로:" in html
    assert "자동 탐색 후보 2개" in html
    assert "https://example.com/list" in html
    assert "https://example.com/feed.xml" in html
    assert "https://example.com/fallback/press" in html
    assert "https://example.com/discovered/press" in html
    assert "테스트군청 보도자료" in html


def test_source_detail_shows_action_recommendations(monkeypatch):
    db_path = Path(f"data/.test_source_detail_actions_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("NEWS_SUMMARY_DATABASE_URL", "")
    store = Store(db_path)
    store.init_db()
    store.set_app_metadata(
        "auto_url_discovery_snapshot",
        json.dumps(
            {
                "updated_at": "2026-07-10T15:00:00+09:00",
                "discoveries": [
                    {
                        "source_id": "action-source",
                        "source_name": "테스트시청 보도자료",
                        "urls": ["https://example.com/candidate/press"],
                    }
                ],
            },
            ensure_ascii=False,
        ),
    )

    from news_summary import web as web_module

    source = Source(
        id="action-source",
        name="테스트시청 보도자료",
        region="전남 테스트",
        type="html",
        list_url="https://example.com/list",
        fallback_urls=["https://example.com/fallback/press"],
    )
    monkeypatch.setattr(web_module, "_source_by_id", lambda config_path, source_id: source if source_id == "action-source" else None)
    monkeypatch.setattr(
        web_module,
        "_source_summary_by_id",
        lambda store, config_path, source_id: {
            "id": "action-source",
            "name": source.name,
            "region": source.region,
            "releases": 5,
            "yesterday_releases": 2,
            "today_releases": 0,
            "last_collected": None,
            "issue": "수집 실패",
            "status_label": "수집 실패",
            "status_level": "error",
            "status_detail": "4회 연속 실패했습니다.",
            "business_gap": 2,
            "consecutive_failures": 4,
            "last_status": "failed",
            "last_checked_at": "2026-07-10T14:40:00+09:00",
            "last_message": "본문 파싱 실패",
            "failure_stage": "본문 파싱 실패",
            "failure_reason": "선택자 불일치",
        },
    )
    monkeypatch.setattr(
        web_module,
        "_source_detail_metrics",
        lambda store, source_id: {
            "missing_drafts": 3,
            "date_issues": 1,
            "assets": 0,
        },
    )
    monkeypatch.setattr(
        web_module,
        "_source_failure_summary",
        lambda store, source_id, days=7, limit=4: {
            "days": 7,
            "total_failures": 4,
            "transient_failures": 1,
            "structural_failures": 3,
            "last_success_at": "2026-07-09T11:00:00+09:00",
            "stages": [
                {"stage": "본문 파싱 실패", "count": 3, "reason": "선택자 불일치"},
            ],
        },
    )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    html = app.test_client().get("/sources/action-source").data.decode("utf-8")

    assert "권장 조치" in html
    assert "후보 URL 검토" in html
    assert 'href="#source-routes"' in html
    assert "기관 로그 확인" in html
    assert 'href="/ops-logs?tab=collector&amp;q=source_id%3Daction-source"' in html
    assert "구조 변경 점검" in html
    assert 'href="#source-failure-summary"' in html
    assert "초안 변환 확인" in html
    assert 'href="/press-releases?draft=missing&amp;source=action-source"' in html
    assert "게시일 점검" in html
    assert 'href="/press-releases?draft=date_issue&amp;source=action-source"' in html


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


def test_ops_logs_page_filters_by_query_and_preserves_tab(monkeypatch, tmp_path):
    db_path = Path(f"data/.test_ops_logs_query_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_LOG_DIR", str(tmp_path))

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    log_path = tmp_path / "news_summary.log"
    log_path.write_text(
        "2026-06-02 WARNING [news_summary.service] source collection failed source_id=gangjin source_name=강진군청 보도자료 error=ConnectTimeout\n"
        "2026-06-02 INFO [news_summary.service] source collection succeeded source_id=sinan source_name=신안군청 보도자료 releases=3 inserted=2\n",
        encoding="utf-8",
    )
    client = app.test_client()

    html = client.get("/ops-logs?tab=collector&q=source_id=gangjin").data.decode("utf-8")

    assert "강진군청 보도자료" in html
    assert "신안군청 보도자료" not in html
    assert 'value="source_id=gangjin"' in html
    assert 'href="/ops-logs?tab=collector&amp;q=source_id%3Dgangjin"' in html
    assert "현재 필터:" in html
    assert "검색 1줄 / 전체 2줄" in html


def test_operations_page_shows_recent_log_summary(monkeypatch, tmp_path):
    db_path = Path(f"data/.test_operations_log_summary_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_LOG_DIR", str(tmp_path))

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    log_path = tmp_path / "news_summary.log"
    log_path.write_text(
        "2026-07-10 INFO [news_summary.scheduler] auto collector waiting\n"
        "2026-07-10 INFO [news_summary.writer] Gemini 초안 생성 완료\n"
        "2026-07-10 WARNING [news_summary.web] slow web request path=/drafts\n",
        encoding="utf-8",
    )
    html = app.test_client().get("/operations").data.decode("utf-8")

    assert "최근 운영 로그" in html
    assert "최근 운영 로그 3줄 중 오류/경고 1줄을 확인했습니다." in html
    assert "최근 3줄 기준" in html
    assert 'href="/ops-logs?tab=errors"' in html
    assert 'href="/ops-logs?tab=gemini"' in html
    assert 'href="/ops-logs?tab=collector"' in html
    assert "slow web request path=/drafts" in html
    assert "Gemini 초안 생성 완료" in html
    assert "auto collector waiting" in html


def test_operations_page_toggles_auto_collection(monkeypatch):
    db_path = Path(f"data/.test_operations_auto_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_ADMIN_PASSWORD", "secret1234")
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_DISABLED", "1")

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
    assert "운영 변경 잠금" in html

    locked_response = client.post("/operations/auto-collect", data={"enabled": "true"}, follow_redirects=True)
    assert "운영 변경 기능은 관리자 비밀번호 확인 후 사용할 수 있습니다." in locked_response.data.decode("utf-8")
    assert collector.calls == []

    unlock_response = client.post(
        "/operations/write-access/unlock",
        data={"current_password": "secret1234"},
        follow_redirects=True,
    )
    assert "운영 변경 기능 잠금을 해제했습니다." in unlock_response.data.decode("utf-8")

    enabled_response = client.post("/operations/auto-collect", data={"enabled": "true"}, follow_redirects=True)
    disabled_response = client.post("/operations/auto-collect", data={"enabled": "false"}, follow_redirects=True)

    assert enabled_response.status_code == 200
    assert disabled_response.status_code == 200
    assert collector.calls == [True, False]


def test_operations_page_reuses_short_diagnostics_cache(monkeypatch):
    db_path = Path(f"data/.test_operations_report_cache_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_OPERATIONS_REPORT_CACHE_SECONDS", "60")

    from news_summary import web as web_module

    web_module._clear_operations_report_cache()
    deployment_calls = []

    def fake_deployment_version_report():
        deployment_calls.append("called")
        return {
            "status_label": "최신 배포",
            "status_level": "ok",
            "running_commit": "abc1234",
            "latest_commit": "abc1234",
            "repo": "seunghooda-dev/news-express",
            "branch": "codex/news-express",
            "auto_deploy_label": "커밋 시 자동 배포",
            "auto_deploy_trigger": "commit",
            "auto_deploy_level": "ok",
        }

    monkeypatch.setattr(web_module, "_deployment_version_report", fake_deployment_version_report)
    monkeypatch.setattr(
        web_module,
        "_cloudflare_quick_tunnel_status",
        lambda: {"running": False, "public_url": "", "log_path": "", "updated_at": None, "label": "터널 미감지"},
    )

    app = web_module.create_app()
    app.testing = True
    client = app.test_client()

    first = client.get("/operations")
    second = client.get("/operations")

    assert first.status_code == 200
    assert second.status_code == 200
    assert deployment_calls == ["called"]


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
    store.record_draft_generation_failure(
        1,
        "generation_error",
        "Gemini 응답이 비어 있습니다.",
        "gemini-3.5-flash",
        (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
    )
    store.set_app_metadata(
        "auto_queue_drain_status_snapshot",
        json.dumps(
            {
                "updated_at": "2026-07-09T09:00:00+09:00",
                "queue_pending_before": 8,
                "queue_pending_after": 5,
                "queue_processed_count": 3,
                "queue_drain_messages": ["Gemini 미변환 큐 자동 소진"],
            },
            ensure_ascii=False,
        ),
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
            "auto_deploy_label": "커밋 시 자동 배포",
            "auto_deploy_trigger": "commit",
            "auto_deploy_level": "ok",
            "change_report": {
                "changed_count": 2,
                "runtime_change_count": 1,
                "sample_files": ["README.md", "src/news_summary/web.py"],
                "runtime_sample_files": ["src/news_summary/web.py"],
                "compare_url": "https://github.com/seunghooda-dev/news-express/compare/abc1234...abc1234",
            },
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
    assert "반복 실패 기관 우선순위" in html
    assert "0곳" in html
    assert "실패 상위 기관" in html
    assert "테스트 기관 1건 · 외부 사이트 응답 지연" in html
    assert "배포 버전" in html
    assert "최신 배포" in html
    assert "자동 배포 커밋 시 자동 배포" in html
    assert "상용 준비 점검" in html
    assert "GitHub 변경 비교" in html
    assert "런타임 파일:" in html
    assert "src/news_summary/web.py" in html
    assert "백업 자동 생성" in html
    assert "최근 7개 유지" in html


def test_production_readiness_report_flags_render_operating_gaps(monkeypatch):
    db_path = Path(f"data/.test_production_readiness_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-test")
    monkeypatch.setenv("NEWS_SUMMARY_PUBLIC_URL", "https://news-express.example.com")
    monkeypatch.setenv("NEWS_SUMMARY_LOG_DIR", "/tmp/news-express/logs")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NEWS_SUMMARY_DATABASE_URL", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("NEWS_SUMMARY_SECRET_KEY", raising=False)

    store = Store(db_path)
    store.init_db()

    from news_summary import web as web_module

    monkeypatch.setattr(
        web_module,
        "_source_coverage_report",
        lambda config_path: {"status_level": "ok", "message": "필수 기관 설정 정상"},
    )
    monkeypatch.setattr(
        web_module,
        "_render_deploy_config_report",
        lambda: {
            "auto_deploy_trigger": "commit",
            "auto_deploy_label": "커밋 시 자동 배포",
            "auto_deploy_level": "ok",
        },
    )

    report = web_module._production_readiness_report(
        store,
        Path("/tmp/news-express/backups"),
        {"enabled": False, "thread_alive": False},
        Path("config/municipalities.yaml"),
    )

    assert report["status_level"] == "error"
    assert report["error_count"] >= 3
    issue_names = {item["name"] for item in report["issue_items"]}
    assert {"데이터베이스", "Gemini 키", "자동 수집", "접근 보호", "백업 보관", "운영 로그"} <= issue_names


def test_production_readiness_accepts_google_api_key_and_reports_gemini_models(monkeypatch):
    db_path = Path(f"data/.test_production_readiness_google_key_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    store = Store(db_path)
    store.init_db()

    from news_summary import web as web_module

    monkeypatch.setattr(web_module, "_source_coverage_report", lambda config_path: {"status_level": "ok", "message": "필수 기관 설정 정상"})
    monkeypatch.setattr(web_module, "_render_deploy_config_report", lambda: {"auto_deploy_level": "ok", "auto_deploy_label": "커밋 시 자동 배포"})

    report = web_module._production_readiness_report(
        store,
        Path("data/backups"),
        {"enabled": True, "thread_alive": True},
        Path("config/municipalities.yaml"),
    )

    items = {item["name"]: item for item in report["items"]}
    assert items["Gemini 키"]["status_level"] == "ok"
    assert items["Gemini 모델"]["status_level"] == "ok"
    assert items["Gemini 모델"]["status_label"] == "스마트 선택"


def test_production_readiness_flags_auth_disabled_on_render(monkeypatch):
    db_path = Path(f"data/.test_production_readiness_auth_disabled_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-test")
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_DISABLED", "1")
    store = Store(db_path)
    store.init_db()

    from news_summary import web as web_module

    monkeypatch.setattr(web_module, "_source_coverage_report", lambda config_path: {"status_level": "ok", "message": "필수 기관 설정 정상"})
    monkeypatch.setattr(web_module, "_render_deploy_config_report", lambda: {"auto_deploy_level": "ok", "auto_deploy_label": "커밋 시 자동 배포"})

    report = web_module._production_readiness_report(
        store,
        Path("data/backups"),
        {"enabled": True, "thread_alive": True},
        Path("config/municipalities.yaml"),
    )

    items = {item["name"]: item for item in report["items"]}
    assert items["접근 보호"]["status_level"] == "error"
    assert items["접근 보호"]["status_label"] == "강제 비활성"
    assert items["상세 헬스체크"]["status_level"] == "error"
    assert items["상세 헬스체크"]["status_label"] == "공개"
    assert "접근 보호" in {item["name"] for item in report["issue_items"]}
    assert "상세 헬스체크" in {item["name"] for item in report["issue_items"]}


def test_production_readiness_flags_http_public_url_on_render(monkeypatch):
    db_path = Path(f"data/.test_production_readiness_http_url_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-test")
    monkeypatch.setenv("NEWS_SUMMARY_PUBLIC_URL", "http://news-express.example.com")
    store = Store(db_path)
    store.init_db()

    from news_summary import web as web_module

    monkeypatch.setattr(web_module, "_source_coverage_report", lambda config_path: {"status_level": "ok", "message": "필수 기관 설정 정상"})
    monkeypatch.setattr(web_module, "_render_deploy_config_report", lambda: {"auto_deploy_level": "ok", "auto_deploy_label": "커밋 시 자동 배포"})

    report = web_module._production_readiness_report(
        store,
        Path("data/backups"),
        {"enabled": True, "thread_alive": True},
        Path("config/municipalities.yaml"),
    )

    items = {item["name"]: item for item in report["items"]}
    assert items["공개 URL"]["status_level"] == "error"
    assert items["공개 URL"]["status_label"] == "HTTP"
    assert "https:// 주소" in items["공개 URL"]["message"]
    assert "공개 URL" in {item["name"] for item in report["issue_items"]}


def test_production_readiness_warns_for_weak_plain_admin_password(monkeypatch):
    db_path = Path(f"data/.test_production_readiness_weak_admin_password_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-test")
    monkeypatch.setenv("NEWS_SUMMARY_ADMIN_PASSWORD", "news1234")
    store = Store(db_path)
    store.init_db()

    from news_summary import web as web_module

    monkeypatch.setattr(web_module, "_source_coverage_report", lambda config_path: {"status_level": "ok", "message": "필수 기관 설정 정상"})
    monkeypatch.setattr(web_module, "_render_deploy_config_report", lambda: {"auto_deploy_level": "ok", "auto_deploy_label": "커밋 시 자동 배포"})

    report = web_module._production_readiness_report(
        store,
        Path("data/backups"),
        {"enabled": True, "thread_alive": True},
        Path("config/municipalities.yaml"),
    )

    items = {item["name"]: item for item in report["items"]}
    assert items["관리자 비밀번호"]["status_level"] == "error"
    assert items["관리자 비밀번호"]["status_label"] == "강도 낮음"
    assert "12자 미만" in items["관리자 비밀번호"]["message"]
    assert "예측 쉬운 단어" in items["관리자 비밀번호"]["message"]

    monkeypatch.setenv("NEWS_SUMMARY_ADMIN_PASSWORD", "PressRoom-47-Delta")
    report = web_module._production_readiness_report(
        store,
        Path("data/backups"),
        {"enabled": True, "thread_alive": True},
        Path("config/municipalities.yaml"),
    )

    items = {item["name"]: item for item in report["items"]}
    assert items["관리자 비밀번호"]["status_level"] == "warning"
    assert items["관리자 비밀번호"]["status_label"] == "평문 설정"


def test_production_readiness_accepts_admin_password_hash_on_render(monkeypatch):
    db_path = Path(f"data/.test_production_readiness_admin_hash_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-test")
    monkeypatch.setenv("NEWS_SUMMARY_ADMIN_PASSWORD_HASH", "scrypt:32768:8:1$sample$safe")
    monkeypatch.delenv("NEWS_SUMMARY_ADMIN_PASSWORD", raising=False)
    store = Store(db_path)
    store.init_db()

    from news_summary import web as web_module

    monkeypatch.setattr(web_module, "_source_coverage_report", lambda config_path: {"status_level": "ok", "message": "필수 기관 설정 정상"})
    monkeypatch.setattr(web_module, "_render_deploy_config_report", lambda: {"auto_deploy_level": "ok", "auto_deploy_label": "커밋 시 자동 배포"})

    report = web_module._production_readiness_report(
        store,
        Path("data/backups"),
        {"enabled": True, "thread_alive": True},
        Path("config/municipalities.yaml"),
    )

    items = {item["name"]: item for item in report["items"]}
    assert items["관리자 비밀번호"]["status_level"] == "ok"
    assert items["관리자 비밀번호"]["status_label"] == "해시 설정"


def test_production_readiness_reports_env_backup_policy(monkeypatch):
    db_path = Path(f"data/.test_production_readiness_backup_policy_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-test")
    monkeypatch.setenv("NEWS_SUMMARY_BACKUP_INCLUDE_ENV", "1")
    store = Store(db_path)
    store.init_db()

    from news_summary import web as web_module

    monkeypatch.setattr(web_module, "_source_coverage_report", lambda config_path: {"status_level": "ok", "message": "필수 기관 설정 정상"})
    monkeypatch.setattr(web_module, "_render_deploy_config_report", lambda: {"auto_deploy_level": "ok", "auto_deploy_label": "커밋 시 자동 배포"})

    report = web_module._production_readiness_report(
        store,
        Path("data/backups"),
        {"enabled": True, "thread_alive": True},
        Path("config/municipalities.yaml"),
    )

    items = {item["name"]: item for item in report["items"]}
    assert items["백업 보안"]["status_level"] == "warning"
    assert items["백업 보안"]["status_label"] == ".env 포함"

    monkeypatch.setenv("NEWS_SUMMARY_BACKUP_INCLUDE_ENV", "0")
    report = web_module._production_readiness_report(
        store,
        Path("data/backups"),
        {"enabled": True, "thread_alive": True},
        Path("config/municipalities.yaml"),
    )

    items = {item["name"]: item for item in report["items"]}
    assert items["백업 보안"]["status_level"] == "ok"
    assert items["백업 보안"]["status_label"] == ".env 제외"


def test_production_readiness_warns_when_gemini_flash_model_is_missing(monkeypatch):
    db_path = Path(f"data/.test_production_readiness_gemini_model_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    store = Store(db_path)
    store.init_db()

    from news_summary import web as web_module

    monkeypatch.setattr(web_module, "current_gemini_models", lambda: ["gemini-3.1-flash-lite"])
    monkeypatch.setattr(web_module, "_source_coverage_report", lambda config_path: {"status_level": "ok", "message": "필수 기관 설정 정상"})
    monkeypatch.setattr(web_module, "_render_deploy_config_report", lambda: {"auto_deploy_level": "ok", "auto_deploy_label": "커밋 시 자동 배포"})

    report = web_module._production_readiness_report(
        store,
        Path("data/backups"),
        {"enabled": True, "thread_alive": True},
        Path("config/municipalities.yaml"),
    )

    items = {item["name"]: item for item in report["items"]}
    assert items["Gemini 모델"]["status_level"] == "warning"
    assert items["Gemini 모델"]["status_label"] == "Flash 없음"
    assert "Gemini 모델" in {item["name"] for item in report["issue_items"]}


def test_service_health_summary_includes_production_readiness_warning():
    summary = _service_health_summary(
        {
            "ok": True,
            "database": "ok",
            "production_readiness_status": "warning",
            "production_readiness_message": "상용 운영 전 권장 보완 2건이 있습니다.",
        }
    )

    assert summary["service_status_level"] == "warning"
    assert summary["service_status_message"] == "상용 운영 전 권장 보완 2건이 있습니다."
    assert summary["service_status_issues"][0]["component"] == "production_readiness"


def test_service_health_summary_includes_stale_operations_snapshot_warning():
    summary = _service_health_summary(
        {
            "ok": True,
            "database": "ok",
            "operations_snapshot_status": "warning",
            "operations_snapshot_message": "운영 스냅샷 갱신 지연: Gemini 큐 점검 241분 전",
        }
    )

    assert summary["service_status_level"] == "warning"
    assert summary["service_status_message"] == "운영 스냅샷 갱신 지연: Gemini 큐 점검 241분 전"
    assert summary["service_status_issues"][0]["component"] == "operations_snapshot"


def test_service_health_summary_includes_backup_warning():
    summary = _service_health_summary(
        {
            "ok": True,
            "database": "ok",
            "backup_status": "warning",
            "backup_message": "최근 DB 백업이 31시간 전입니다. 자동 백업 상태를 확인하세요.",
        }
    )

    assert summary["service_status_level"] == "warning"
    assert summary["service_status_message"] == "최근 DB 백업이 31시간 전입니다. 자동 백업 상태를 확인하세요."
    assert summary["service_status_issues"][0]["component"] == "backup"


def test_operations_snapshot_freshness_warns_when_metadata_is_stale(monkeypatch):
    db_path = Path(f"data/.test_operations_snapshot_stale_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    now = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    stale_at = (now - timedelta(hours=4, minutes=1)).isoformat()
    fresh_at = (now - timedelta(minutes=20)).isoformat()

    for key in (
        AUTO_DAILY_REPORT_KEY,
        AUTO_OPERATIONS_SUMMARY_STATUS_KEY,
        AUTO_COLLECTION_ANOMALY_STATUS_KEY,
        AUTO_SERVER_HEALTH_STATUS_KEY,
    ):
        store.set_app_metadata(key, json.dumps({"updated_at": fresh_at}, ensure_ascii=False))
    store.set_app_metadata(
        AUTO_QUEUE_DRAIN_STATUS_KEY,
        json.dumps({"updated_at": stale_at}, ensure_ascii=False),
    )
    monkeypatch.setenv("NEWS_SUMMARY_OPERATIONS_SNAPSHOT_STALE_MINUTES", "180")

    payload = _operations_snapshot_freshness_payload(store, now=now)

    assert payload["operations_snapshot_status"] == "warning"
    assert payload["operations_snapshot_stale_count"] == 1
    assert payload["operations_snapshot_oldest_age_minutes"] == 241
    assert payload["operations_snapshot_message"] == "운영 스냅샷 갱신 지연: Gemini 큐 점검 241분 전"


def test_operations_page_shows_attention_source_queue(monkeypatch):
    db_path = Path(f"data/.test_operations_attention_sources_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()
    store.add_press_release(
        PressRelease(
            source_id="alpha",
            source_name="전남광주통합특별시 테스트군청 보도자료",
            region="전남 테스트",
            title="게시일 미정 원문",
            url="https://example.com/alpha-missing",
            content="게시일 점검이 필요한 원문입니다.",
            published_at="미정",
        )
    )
    drafted_id = store.add_press_release(
        PressRelease(
            source_id="beta",
            source_name="테스트시청 보도자료",
            region="전남 테스트",
            title="정상 원문",
            url="https://example.com/beta-normal",
            content="정상 원문입니다.",
            published_at="2026-07-10",
        )
    )
    assert drafted_id is not None
    store.add_article_draft(
        ArticleDraft(
            press_release_id=drafted_id,
            title="정상 초안",
            body="정상 초안입니다.",
            review_note="",
            model="gemini-3.5-flash:gemini",
        )
    )
    store.set_app_metadata(
        "auto_url_discovery_snapshot",
        json.dumps(
            {
                "updated_at": "2026-07-10T15:10:00+09:00",
                "discoveries": [
                    {
                        "source_id": "alpha",
                        "source_name": "전남광주통합특별시 테스트군청 보도자료",
                        "urls": [
                            "https://example.com/alpha/candidate-1",
                            "https://example.com/alpha/candidate-2",
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
    )

    from news_summary import web as web_module

    original_load_sources = web_module.load_sources
    monkeypatch.setattr(
        web_module,
        "_source_summaries",
        lambda store, config_path: [
            {
                "id": "alpha",
                "name": "전남광주통합특별시 테스트군청 보도자료",
                "region": "전남 테스트",
                "releases": 1,
                "yesterday_releases": 0,
                "today_releases": 0,
                "last_collected": None,
                "issue": "수집 실패",
                "status_label": "수집 실패",
                "status_level": "error",
                "status_detail": "4회 연속 실패했습니다. 선택자 불일치",
                "business_gap": 2,
                "consecutive_failures": 4,
                "last_status": "failed",
                "last_checked_at": "2026-07-10T15:00:00+09:00",
                "last_message": "본문 파싱 실패",
                "failure_stage": "본문 파싱 실패",
                "failure_reason": "선택자 불일치",
            },
            {
                "id": "beta",
                "name": "테스트시청 보도자료",
                "region": "전남 테스트",
                "releases": 1,
                "yesterday_releases": 0,
                "today_releases": 1,
                "last_collected": None,
                "issue": "",
                "status_label": "정상",
                "status_level": "ok",
                "status_detail": "",
                "business_gap": 0,
                "consecutive_failures": 0,
                "last_status": "ok",
                "last_checked_at": "2026-07-10T15:00:00+09:00",
                "last_message": "",
                "failure_stage": "",
                "failure_reason": "",
            },
        ],
    )
    monkeypatch.setattr(
        web_module,
        "load_sources",
        lambda config_path: [
            Source(
                id="alpha",
                name="전남광주통합특별시 테스트군청 보도자료",
                region="전남 테스트",
                type="html",
                fallback_urls=["https://example.com/alpha/fallback"],
            ),
            *[source for source in original_load_sources(config_path) if source.id != "alpha"],
        ],
    )

    app = web_module.create_app()
    app.testing = True
    html = app.test_client().get("/operations").data.decode("utf-8")

    assert "즉시 확인 기관" in html
    assert "테스트군청 보도자료" in html
    assert "연속 실패 4회" in html
    assert "미변환 1건" in html
    assert "게시일 1건" in html
    assert "대체 URL 1개" in html
    assert "후보 URL 2개" in html
    assert "4회 연속 실패했습니다. 선택자 불일치" in html
    assert 'href="/sources/alpha"' in html
    assert 'href="/press-releases?draft=missing&amp;source=alpha"' in html
    assert 'href="/press-releases?draft=date_issue&amp;source=alpha"' in html
    assert 'href="/sources/alpha#source-routes"' in html
    assert 'href="/ops-logs?tab=collector&amp;q=source_id%3Dalpha"' in html


def test_operations_page_shows_actionable_overview_links(monkeypatch):
    db_path = Path(f"data/.test_operations_overview_links_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))

    from news_summary import web as web_module

    monkeypatch.setattr(
        web_module.Store,
        "pending_press_release_summary",
        lambda self, limit=5: {
            "total": 7,
            "by_date": [{"date": "2026-07-10", "count": 4}],
            "by_source": [{"source_id": "gwangyang", "source_name": "광양시청 보도자료", "count": 4}],
            "latest_published_at": "2026-07-10 15:00",
            "oldest_published_at": "2026-07-09 08:00",
        },
    )
    monkeypatch.setattr(
        web_module.Store,
        "draft_generation_failure_summary",
        lambda self, limit=5: {
            "total": 5,
            "due": 3,
            "next_retry_at": "2026-07-10T06:30:00+00:00",
            "by_kind": [],
            "latest": [],
        },
    )
    monkeypatch.setattr(
        web_module,
        "_draft_conversion_coverage_report",
        lambda store: {
            "status_level": "warning",
            "status_label": "주의",
            "message": "오늘 수집 원문 중 초안 미변환이 남아 있습니다.",
            "date": "2026-07-10",
            "today_releases": 12,
            "today_drafted": 8,
            "drafted_percent": 67,
            "today_pending": 4,
            "retry_ready_pending": 3,
            "retry_scheduled_pending": 1,
            "retry_ready_label": "자동 처리 대기",
            "effective_next_retry_at": "2026-07-10T15:30:00+09:00",
            "next_retry_at": "2026-07-10T15:30:00+09:00",
            "latest_pending_at": "2026-07-10 15:00",
            "oldest_pending_at": "2026-07-09 08:00",
            "pending_sources": [],
            "pending_source_total": 0,
        },
    )
    monkeypatch.setattr(
        web_module,
        "_date_issue_report",
        lambda store: {
            "status_label": "확인 필요",
            "status_level": "warning",
            "issue_count": 2,
            "by_source": [],
            "samples": [],
        },
    )
    monkeypatch.setattr(
        web_module,
        "_operations_health_report",
        lambda store, auto_status, pending_queue: {
            "status_label": "주의",
            "status_level": "warning",
            "failure_count": 4,
            "retry_success_count": 1,
            "unresolved_count": 2,
            "last_auto_finished_at": "2026-07-10T14:00:00+09:00",
            "top_failure_stages": [],
            "top_failure_sources": [],
            "issues": [],
            "priority_sources": [
                {
                    "source_id": "gangjin",
                    "source_name": "강진군청 보도자료",
                    "priority_label": "우선 확인",
                    "consecutive_failures": 4,
                    "failure_stage": "외부 사이트 응답 지연",
                    "failure_reason": "TLS 연결 시간 초과",
                    "checked_at": "2026-07-10T14:05:00+09:00",
                    "level": "error",
                }
            ],
        },
    )
    monkeypatch.setattr(
        web_module,
        "_operations_service_status_report",
        lambda store, config_path, auto_status, reports: {
            "status_level": "warning",
            "status_label": "주의",
            "message": "즉시 확인이 필요한 운영 항목이 있습니다.",
            "issues": [],
        },
    )

    app = web_module.create_app()
    app.testing = True
    html = app.test_client().get("/operations").data.decode("utf-8")

    assert "운영 핵심 현황" in html
    assert "전체 미변환" in html
    assert "7건" in html
    assert 'href="/press-releases?draft=missing"' in html
    assert "오늘 미변환" in html
    assert 'href="/press-releases?draft=missing&amp;date=2026-07-10"' in html
    assert "재처리 대기" in html
    assert 'href="#ops-gemini-retry-queue"' in html
    assert "자동 처리 대기 3건" in html
    assert "게시일 확인" in html
    assert 'href="/press-releases?draft=date_issue"' in html
    assert "반복 실패" in html
    assert 'href="#ops-priority-sources"' in html


def test_operations_page_collapses_secondary_reference_cards_by_default(monkeypatch):
    db_path = Path(f"data/.test_operations_secondary_section_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    html = app.test_client().get("/operations").data.decode("utf-8")

    assert '<details class="ops-secondary-section">' in html
    assert '<details class="ops-secondary-section" open' not in html
    assert "참고 항목 펼치기" in html
    assert "16개" in html
    assert "일일 운영 리포트" in html
    assert "운영 요약" in html
    assert "서버 상태 점검" in html
    assert "수집 이상치" in html
    assert "대체 URL 준비" in html
    assert "URL 후보 탐색" in html
    assert "접속자 현황" in html
    assert "DB 백업" in html


def test_operations_page_shows_repeated_failure_priority_sources(monkeypatch):
    db_path = Path(f"data/.test_operations_priority_sources_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("NEWS_SUMMARY_DATABASE_URL", "")
    store = Store(db_path)
    store.init_db()
    now_utc = datetime.now(timezone.utc)
    with store.connect() as conn:
        for checked_at in (
            now_utc - timedelta(hours=3),
            now_utc - timedelta(hours=2),
            now_utc - timedelta(hours=1),
        ):
            conn.execute(
                """
                INSERT INTO source_collection_runs
                (source_id, source_name, status, message, failure_stage, failure_reason,
                 releases_found, inserted_count, repaired_dates, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "alpha",
                    "알파군청 보도자료",
                    "failed",
                    "본문 파싱 실패",
                    "본문 파싱 실패",
                    "선택자 불일치",
                    0,
                    0,
                    0,
                    checked_at.isoformat(),
                ),
            )
        conn.execute(
            """
            INSERT INTO source_collection_runs
            (source_id, source_name, status, message, failure_stage, failure_reason,
             releases_found, inserted_count, repaired_dates, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "beta",
                "베타군청 보도자료",
                "failed",
                "TLS 연결 시간 초과",
                "외부 사이트 응답 지연",
                "TLS 연결 시간 초과",
                0,
                0,
                0,
                (now_utc - timedelta(minutes=30)).isoformat(),
            ),
        )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    html = app.test_client().get("/operations").data.decode("utf-8")

    assert "반복 실패 기관 우선순위" in html
    assert "알파군청 보도자료" in html
    assert "미복구 우선" in html
    assert "3회 연속" in html
    assert "본문 파싱 실패" in html
    assert "선택자 불일치" in html
    assert "베타군청 보도자료" in html
    assert "재검증 대기" in html
    assert html.index("알파군청 보도자료") < html.index("베타군청 보도자료")
    assert 'href="/sources/alpha"' in html
    assert 'href="/ops-logs?tab=collector&amp;q=source_id%3Dalpha"' in html


def test_operations_page_links_date_issue_items_to_filtered_press_releases(monkeypatch):
    db_path = Path(f"data/.test_operations_date_issue_links_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("NEWS_SUMMARY_DATABASE_URL", "")

    from news_summary import web as web_module

    monkeypatch.setattr(
        web_module,
        "_date_issue_report",
        lambda store: {
            "status_label": "확인 필요",
            "status_level": "warning",
            "issue_count": 2,
            "by_source": [{"source_id": "sample", "source_name": "테스트 기관", "count": 2}],
            "samples": [
                {
                    "id": 17,
                    "source_id": "sample",
                    "source_name": "테스트 기관",
                    "title": "작성일 표기 원문",
                    "published_at": "작성일 2026.07.08 09:00",
                    "warning": "게시일 앞 문구 확인",
                }
            ],
        },
    )
    app = web_module.create_app()
    app.testing = True

    html = app.test_client().get("/operations").data.decode("utf-8")

    assert 'href="/press-releases?draft=date_issue"' in html
    assert 'href="/press-releases?draft=date_issue&amp;source=sample"' in html
    assert 'href="/press-releases/17"' in html
    assert "게시일 앞 문구 확인" in html


def test_operations_page_warns_when_gemini_retry_failures_are_due(monkeypatch):
    db_path = Path(f"data/.test_operations_gemini_retry_due_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("NEWS_SUMMARY_DATABASE_URL", "")
    monkeypatch.setenv("NEWS_SUMMARY_GEMINI_RETRY_DUE_WARNING_COUNT", "2")
    store = Store(db_path)
    store.init_db()

    due_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    for index in range(2):
        release_id = store.add_press_release(
            PressRelease(
                source_id="sample",
                source_name="테스트 기관",
                region="전남",
                title=f"재시도 대기 원문 {index + 1}",
                url=f"https://example.com/retry-due-{index}",
                content="Gemini 재시도가 필요한 원문입니다.",
                published_at="2026-06-26 09:00",
            )
        )
        assert release_id is not None
        store.record_draft_generation_failure(
            release_id,
            "quota_or_cooldown",
            "Gemini 요청 한도 감지",
            "gemini-3.5-flash",
            due_at,
        )

    from news_summary import web as web_module

    monkeypatch.setattr(
        web_module,
        "_cloudflare_quick_tunnel_status",
        lambda: {"running": False, "public_url": "", "log_path": "", "updated_at": None, "label": "터널 미감지"},
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
            "auto_deploy_label": "커밋 시 자동 배포",
            "auto_deploy_trigger": "commit",
            "auto_deploy_level": "ok",
        },
    )

    app = web_module.create_app()
    app.testing = True
    client = app.test_client()

    html = client.get("/operations").data.decode("utf-8")

    assert "자동 복구 점검" in html
    assert "주의" in html
    assert "Gemini 자동 재처리 대기 원문 2건" in html
    assert "자동 처리 대기 2건" in html
    assert "재시도 대기 원문 1" in html
    assert "재시도 대기 원문 2" in html
    assert "/press-releases/1" in html


def test_operations_page_formats_collection_anomaly_counts_without_duplication(monkeypatch):
    db_path = Path(f"data/.test_operations_anomaly_counts_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))

    from news_summary import web as web_module

    monkeypatch.setattr(
        web_module,
        "_collection_anomaly_report",
        lambda store: {
            "updated_at": "2026-07-09T10:00:00+09:00",
            "status_level": "warning",
            "status_label": "확인 필요",
            "issue_count": 2,
            "issues": [
                {
                    "source_id": "zero-source",
                    "source_name": "강진군청 보도자료",
                    "type": "today_zero",
                    "label": "오늘 0건",
                    "today_count": 0,
                    "average": 12.0,
                },
                {
                    "source_id": "drop-source",
                    "source_name": "무안군청 보도자료",
                    "type": "drop",
                    "label": "평소 대비 급감",
                    "today_count": 1,
                    "average": 5.5,
                },
            ],
        },
    )
    app = web_module.create_app()
    app.testing = True
    html = app.test_client().get("/operations").data.decode("utf-8")

    assert 'href="/sources/zero-source"' in html
    assert "강진군청 보도자료</a>" in html
    assert "오늘 0건 / 평균 12.0건" in html
    assert "오늘 0건 0건/평균" not in html
    assert 'href="/sources/drop-source"' in html
    assert "무안군청 보도자료</a>" in html
    assert "평소 대비 급감 · 오늘 1건 / 평균 5.5건" in html


def test_operations_page_shows_gemini_cooldown_reason(monkeypatch):
    db_path = Path(f"data/.test_operations_gemini_cooldown_reason_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()
    store.set_app_metadata(
        "gemini_cooldown_until",
        (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat(),
    )
    store.set_app_metadata("gemini_cooldown_reason", "자동 초안 생성 재개 대기")

    from news_summary import web as web_module

    monkeypatch.setattr(
        web_module,
        "_cloudflare_quick_tunnel_status",
        lambda: {"running": False, "public_url": "", "log_path": "", "updated_at": None, "label": "터널 미감지"},
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
            "auto_deploy_label": "커밋 시 자동 배포",
            "auto_deploy_trigger": "commit",
            "auto_deploy_level": "ok",
        },
    )

    app = web_module.create_app()
    app.testing = True
    html = app.test_client().get("/operations").data.decode("utf-8")

    assert "Gemini 처리 재개 대기:" in html
    assert "메모: 자동 초안 생성 재개 대기" in html


def test_operations_page_prefetches_metadata_once(monkeypatch):
    from contextlib import contextmanager

    db_path = Path(f"data/.test_operations_metadata_cache_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))

    from news_summary import web as web_module

    calls = []
    original_scope = Store.app_metadata_cache_scope

    @contextmanager
    def spy_metadata_cache_scope(self):
        calls.append(self.display_location)
        with original_scope(self):
            yield

    monkeypatch.setattr(Store, "app_metadata_cache_scope", spy_metadata_cache_scope)
    monkeypatch.setattr(
        web_module,
        "_cloudflare_quick_tunnel_status",
        lambda: {"running": False, "public_url": "", "log_path": "", "updated_at": None, "label": "터널 미감지"},
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
            "auto_deploy_label": "커밋 시 자동 배포",
            "auto_deploy_trigger": "commit",
            "auto_deploy_level": "ok",
        },
    )

    app = web_module.create_app()
    app.testing = True
    response = app.test_client().get("/operations")

    assert response.status_code == 200
    assert calls == [str(db_path)]


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
    assert "오늘 원문 1건" in summary["status_detail"]
    assert "정상으로 봅니다" in summary["status_detail"]


def test_source_summary_keeps_current_day_releases_normal_after_repeated_transient_failures(monkeypatch):
    db_path = Path(f"data/.test_source_today_release_repeated_failure_{uuid4().hex}.sqlite").resolve()
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
            title="강진군, 여름철 안전 점검",
            url="https://example.com/gangjin/2",
            content="강진군은 여름철 안전 점검을 추진한다고 밝혔다. 관계 기관과 함께 시설 점검을 이어갈 계획이다.",
            published_at="2026-07-06",
            collected_at="2026-07-06T05:05:00+00:00",
        )
    )
    with store.connect() as conn:
        for checked_at in (
            "2026-07-06T05:10:00+00:00",
            "2026-07-06T05:20:00+00:00",
            "2026-07-06T05:30:00+00:00",
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

    assert summary["issue"] == ""
    assert summary["status_label"] == "정상"
    assert summary["status_level"] == "ok"
    assert summary["consecutive_failures"] == 3
    assert "오늘 원문 1건" in summary["status_detail"]
    assert "자동 복구 대상" in summary["status_detail"]


def test_source_summary_marks_repeated_network_failures_as_connection_waiting(monkeypatch):
    db_path = Path(f"data/.test_source_repeated_network_failure_{uuid4().hex}.sqlite").resolve()
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
                    "gangjin",
                    "강진군청 보도자료",
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

    assert summary["issue"] == "연결 대기"
    assert summary["status_label"] == "연결 대기"
    assert summary["status_level"] == "warning"
    assert summary["consecutive_failures"] == 3
    assert "외부 사이트 연결 장애" in summary["status_detail"]


def test_source_summary_marks_three_consecutive_structural_failures_as_failed(monkeypatch):
    db_path = Path(f"data/.test_source_three_structural_failures_{uuid4().hex}.sqlite").resolve()
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
                    "목록 후보를 찾지 못했습니다",
                    "사이트 구조 변경",
                    "목록/본문 선택자 확인 필요",
                    0,
                    0,
                    0,
                    checked_at,
                ),
            )

    summary = web_module._source_summaries(store, Path("unused.yaml"))[0]

    assert summary["issue"] == "사이트 구조 변경"
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


def test_visitor_access_prune_is_throttled(monkeypatch):
    db_path = Path(f"data/.test_visitor_prune_throttle_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))

    from news_summary import web as web_module

    monkeypatch.setattr(web_module, "_visitor_access_last_pruned_at", 0.0)
    prune_calls = []

    def fake_prune(self, cutoff_iso):
        prune_calls.append(cutoff_iso)
        return 0

    monkeypatch.setattr(Store, "prune_visitor_access_logs", fake_prune)

    app = web_module.create_app()
    app.testing = True
    client = app.test_client()

    first = client.get("/drafts")
    second = client.get("/press-releases")

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(prune_calls) == 1


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


def test_operations_page_summarizes_top_visitor_paths_and_errors(monkeypatch):
    db_path = Path(f"data/.test_visitor_access_summary_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()

    now_utc = datetime.now(timezone.utc).isoformat()
    store.record_visitor_access("10.10.xxx.xxx", "GET", "/drafts", "drafts", 200, "Chrome", visited_at=now_utc)
    store.record_visitor_access("11.11.xxx.xxx", "GET", "/drafts", "drafts", 200, "Chrome", visited_at=now_utc)
    store.record_visitor_access("12.12.xxx.xxx", "GET", "/operations", "operations", 500, "Chrome", visited_at=now_utc)

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    html = app.test_client().get("/operations").data.decode("utf-8")

    assert "오류 응답 1건" in html
    assert "자주 열린 경로:" in html
    assert "GET /drafts 2건" in html
    assert "GET /operations 1건" in html


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


def test_deployment_report_treats_docs_only_changes_as_current(monkeypatch):
    from news_summary import web as web_module

    monkeypatch.setenv("NEWS_SUMMARY_GITHUB_REPO", "owner/repo")
    monkeypatch.setenv("NEWS_SUMMARY_GITHUB_BRANCH", "codex/news-express")
    monkeypatch.setenv("NEWS_SUMMARY_GIT_COMMIT", "aaa111")
    monkeypatch.setattr(web_module, "_latest_github_commit", lambda repo, branch: "bbb222")
    monkeypatch.setattr(web_module, "_github_compare_files", lambda repo, base, head: ["README.md", ".github/workflows/render-deploy.yml"])

    report = web_module._deployment_version_report()

    assert report["status_label"] == "문서 변경만 미배포"
    assert report["status_level"] == "ok"
    assert report["change_report"]["changed_count"] == 2
    assert report["change_report"]["runtime_change_count"] == 0


def test_deployment_report_marks_runtime_changes_as_deploy_needed(monkeypatch):
    from news_summary import web as web_module

    monkeypatch.setenv("NEWS_SUMMARY_GITHUB_REPO", "owner/repo")
    monkeypatch.setenv("NEWS_SUMMARY_GITHUB_BRANCH", "codex/news-express")
    monkeypatch.setenv("NEWS_SUMMARY_GIT_COMMIT", "aaa111")
    monkeypatch.setattr(web_module, "_latest_github_commit", lambda repo, branch: "bbb222")
    monkeypatch.setattr(web_module, "_github_compare_files", lambda repo, base, head: ["README.md", "src/news_summary/web.py"])

    report = web_module._deployment_version_report()

    assert report["status_label"] == "배포 필요"
    assert report["status_level"] == "warning"
    assert report["change_report"]["runtime_change_count"] == 1
    assert report["change_report"]["runtime_sample_files"] == ["src/news_summary/web.py"]
    assert report["change_report"]["compare_url"] == "https://github.com/owner/repo/compare/aaa111...bbb222"


def test_cloudflare_tunnel_status_skips_process_check_on_render(monkeypatch):
    from news_summary import web as web_module

    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-test")
    monkeypatch.setenv("NEWS_SUMMARY_PUBLIC_URL", "https://news-express.example.com")
    monkeypatch.setattr(
        web_module,
        "_cloudflared_running",
        lambda: (_ for _ in ()).throw(AssertionError("cloudflared process check should be skipped on Render")),
    )

    status = web_module._cloudflare_quick_tunnel_status()

    assert status["label"] == "Render 공개 URL 사용"
    assert status["public_url"] == "https://news-express.example.com"
    assert status["running"] is False


def test_latest_github_commit_uses_short_process_cache(monkeypatch):
    from news_summary import web as web_module

    web_module._latest_github_commit_cache.clear()
    calls = []

    class FakeGithubResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"sha": "abcdef1234567890"}

    def fake_get(url, timeout, headers):
        calls.append((url, timeout, headers))
        return FakeGithubResponse()

    monkeypatch.setattr(web_module.httpx, "get", fake_get)

    assert web_module._latest_github_commit("owner/repo", "main") == "abcdef1234567890"
    assert web_module._latest_github_commit("owner/repo", "main") == "abcdef1234567890"
    assert len(calls) == 1


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
    assert payload["generated_at"]
    assert payload["generated_at_label"]
    assert payload["timezone"] == "Asia/Seoul"
    assert payload["auto_collector"] in {"enabled", "running", "disabled", "stopped", "unavailable"}
    assert payload["details_url"] == "/healthz/details"
    assert payload["service_status_level"] in {"ok", "warning", "error"}
    assert payload["service_status_label"]
    assert isinstance(payload["service_status_issues"], list)
    assert "gemini_queue_status" not in payload
    assert "source_collection_status" not in payload
    assert "collection_check_coverage_status" not in payload
    assert "draft_conversion_coverage_status" not in payload
    if payload["auto_collector"] != "unavailable":
        assert "auto_collector_thread_alive" in payload


def test_healthz_details_requires_login_when_auth_is_enabled(monkeypatch):
    db_path = Path(f"data/.test_healthz_details_auth_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_ADMIN_PASSWORD", "secret1234")
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_DISABLED", "0")

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    health = client.get("/healthz")
    details = client.get("/healthz/details", follow_redirects=False)

    assert health.status_code == 200
    assert details.status_code == 302
    assert "/login" in details.headers["Location"]


def test_healthz_details_can_be_explicitly_public_for_external_monitoring(monkeypatch):
    db_path = Path(f"data/.test_healthz_details_public_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_ADMIN_PASSWORD", "secret1234")
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_DISABLED", "0")
    monkeypatch.setenv("NEWS_SUMMARY_PUBLIC_HEALTH_DETAILS", "1")

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    payload = app.test_client().get("/healthz/details").get_json()

    assert payload["database"] == "ok"
    assert payload["ok"] is True
    assert "gemini_queue_status" in payload
    assert payload["database_schema_status"] == "ok"
    assert payload["database_schema_missing_tables"] == []
    assert payload["database_schema_missing_indexes"] == []


def test_healthz_details_warns_when_database_index_is_missing(monkeypatch):
    db_path = Path(f"data/.test_healthz_missing_index_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))

    from news_summary.storage import Store
    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    with Store(db_path).connect() as conn:
        conn.execute("DROP INDEX IF EXISTS idx_operation_events_created")

    payload = app.test_client().get("/healthz/details").get_json()

    assert payload["database"] == "ok"
    assert payload["database_schema_status"] == "warning"
    assert "idx_operation_events_created" in payload["database_schema_missing_indexes"]
    assert any(issue["component"] == "database_schema" for issue in payload["service_status_issues"])


def test_healthz_details_reports_deployment_version_warning(monkeypatch):
    db_path = Path(f"data/.test_healthz_deployment_version_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_GIT_COMMIT", "aaa111")
    monkeypatch.setenv("NEWS_SUMMARY_GITHUB_REPO", "owner/repo")
    monkeypatch.setenv("NEWS_SUMMARY_GITHUB_BRANCH", "codex/news-express")

    from news_summary import web as web_module

    monkeypatch.setattr(web_module, "_latest_github_commit", lambda repo, branch: "bbb222")
    monkeypatch.setattr(web_module, "_github_compare_files", lambda repo, base, head: ["src/news_summary/web.py"])
    app = web_module.create_app()
    app.testing = True
    payload = app.test_client().get("/healthz/details").get_json()

    assert payload["deployment_version_status"] == "warning"
    assert payload["deployment_version_label"] == "배포 필요"
    assert payload["deployment_running_commit"] == "aaa111"
    assert payload["deployment_latest_commit"] == "bbb222"
    assert payload["deployment_runtime_change_count"] == 1
    assert payload["deployment_version_message"] == "운영 버전이 GitHub 최신 커밋보다 뒤처져 있습니다: 운영 aaa111 · GitHub bbb222"
    assert any(
        issue["component"] == "deployment_version"
        and issue["message"] == payload["deployment_version_message"]
        for issue in payload["service_status_issues"]
    )


def test_service_health_summary_merges_duplicate_operational_warnings():
    summary = _service_health_summary(
        {
            "ok": True,
            "database": "ok",
            "source_collection_status": "warning",
            "source_collection_message": "일시 장애 재검증 대상 1곳",
            "collection_check_coverage_status": "warning",
            "collection_check_coverage_message": "전체 기관은 점검됐고 실패 기록 1곳은 자동 복구 대상입니다.",
            "collection_check_coverage_failed_today": 1,
            "collection_check_coverage_unchecked_count": 0,
            "draft_conversion_coverage_status": "warning",
            "draft_conversion_coverage_message": "오늘 수집 원문 중 초안 미변환 13건이 남아 있습니다.",
            "draft_conversion_today_pending": 13,
            "gemini_queue_status": "warning",
            "gemini_queue_message": "Gemini 처리 재개 대기: 2026.07.09 14:23까지",
            "gemini_cooldown_active": True,
        }
    )

    assert summary["service_status_level"] == "warning"
    assert summary["service_status_message"] == "일시 장애 재검증 대상 1곳 외 1건"
    assert [issue["component"] for issue in summary["service_status_issues"]] == [
        "source_collection",
        "draft_conversion_coverage",
    ]


def test_service_health_summary_keeps_distinct_collection_check_warning():
    summary = _service_health_summary(
        {
            "ok": True,
            "database": "ok",
            "source_collection_status": "ok",
            "collection_check_coverage_status": "warning",
            "collection_check_coverage_message": "미점검 기관 2곳",
            "collection_check_coverage_failed_today": 0,
            "collection_check_coverage_unchecked_count": 2,
        }
    )

    assert summary["service_status_level"] == "warning"
    assert summary["service_status_message"] == "미점검 기관 2곳"
    assert [issue["component"] for issue in summary["service_status_issues"]] == [
        "collection_check_coverage"
    ]


def test_service_health_summary_merges_retry_due_when_today_pending_covers_it():
    summary = _service_health_summary(
        {
            "ok": True,
            "database": "ok",
            "draft_conversion_coverage_status": "warning",
            "draft_conversion_coverage_message": "오늘 수집 원문 중 초안 미변환 29건이 남아 있습니다. 자동 처리 대기 29건입니다.",
            "draft_conversion_today_pending": 29,
            "gemini_queue_status": "warning",
            "gemini_queue_message": "Gemini 자동 재처리 대기 원문 29건",
            "gemini_retry_due": 29,
        }
    )

    assert summary["service_status_level"] == "warning"
    assert summary["service_status_message"] == "오늘 수집 원문 중 초안 미변환 29건이 남아 있습니다. 자동 처리 대기 29건입니다."
    assert [issue["component"] for issue in summary["service_status_issues"]] == [
        "draft_conversion_coverage"
    ]


def test_service_health_summary_keeps_retry_due_when_it_exceeds_today_pending():
    summary = _service_health_summary(
        {
            "ok": True,
            "database": "ok",
            "draft_conversion_coverage_status": "warning",
            "draft_conversion_coverage_message": "오늘 수집 원문 중 초안 미변환 29건이 남아 있습니다.",
            "draft_conversion_today_pending": 29,
            "gemini_queue_status": "warning",
            "gemini_queue_message": "Gemini 자동 재처리 대기 원문 35건",
            "gemini_retry_due": 35,
        }
    )

    assert summary["service_status_level"] == "warning"
    assert summary["service_status_message"] == "오늘 수집 원문 중 초안 미변환 29건이 남아 있습니다. 외 1건"
    assert [issue["component"] for issue in summary["service_status_issues"]] == [
        "draft_conversion_coverage",
        "gemini_queue",
    ]


def test_service_health_summary_keeps_gemini_wait_when_queue_exceeds_today_pending():
    summary = _service_health_summary(
        {
            "ok": True,
            "database": "ok",
            "draft_conversion_coverage_status": "warning",
            "draft_conversion_coverage_message": "오늘 수집 원문 중 초안 미변환 10건이 남아 있습니다.",
            "draft_conversion_today_pending": 10,
            "draft_conversion_retry_ready_pending": 10,
            "gemini_queue_status": "warning",
            "gemini_queue_message": "Gemini 처리 재개 대기: 2026.07.09 16:29까지 · 전체 대기 14건, 처리 재개 대기 10건",
            "gemini_cooldown_active": True,
            "gemini_pending_total": 14,
            "gemini_retry_due": 10,
        }
    )

    assert summary["service_status_level"] == "warning"
    assert summary["service_status_message"] == "오늘 수집 원문 중 초안 미변환 10건이 남아 있습니다. 외 1건"
    assert [issue["component"] for issue in summary["service_status_issues"]] == [
        "draft_conversion_coverage",
        "gemini_queue",
    ]


def test_healthz_reports_collection_check_coverage(monkeypatch):
    db_path = Path(f"data/.test_healthz_collection_check_coverage_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_COLLECTION_COVERAGE_CHECK_HOUR", "9")
    store = Store(db_path)
    store.init_db()
    sources = [
        Source(id="checked", name="점검 기관", region="전남", type="html_board"),
        Source(id="missing", name="미점검 기관", region="전남", type="html_board"),
    ]

    from news_summary import web as web_module

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 7, 6, 10, 0, tzinfo=LOCAL_TZ)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(web_module, "datetime", FixedDatetime)
    monkeypatch.setattr(web_module, "load_sources", lambda config_path: sources)
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO source_collection_runs
            (source_id, source_name, status, message, failure_stage, failure_reason,
             releases_found, inserted_count, repaired_dates, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "checked",
                "점검 기관",
                "ok",
                "원문 검증 통과 1건, 새로 저장 1건",
                "",
                "",
                1,
                1,
                0,
                "2026-07-06T00:30:00+00:00",
            ),
        )

    app = web_module.create_app()
    app.testing = True
    payload = app.test_client().get("/healthz/details").get_json()

    assert payload["collection_check_coverage_status"] == "warning"
    assert payload["collection_check_coverage_label"] == "미점검"
    assert payload["collection_check_coverage_enabled_total"] == 2
    assert payload["collection_check_coverage_checked_today"] == 1
    assert payload["collection_check_coverage_success_today"] == 1
    assert payload["collection_check_coverage_failed_today"] == 0
    assert payload["collection_check_coverage_unchecked_count"] == 1
    assert payload["collection_check_coverage_unchecked_sources"] == ["미점검 기관"]


def test_healthz_collection_check_coverage_uses_latest_source_status(monkeypatch):
    db_path = Path(f"data/.test_healthz_collection_latest_status_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_COLLECTION_COVERAGE_CHECK_HOUR", "9")
    store = Store(db_path)
    store.init_db()
    sources = [Source(id="flaky", name="불안정 기관", region="전남", type="html_board")]

    from news_summary import web as web_module

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 7, 6, 10, 0, tzinfo=LOCAL_TZ)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(web_module, "datetime", FixedDatetime)
    monkeypatch.setattr(web_module, "load_sources", lambda config_path: sources)
    with store.connect() as conn:
        for status, checked_at in (
            ("ok", "2026-07-06T00:10:00+00:00"),
            ("failed", "2026-07-06T00:30:00+00:00"),
        ):
            conn.execute(
                """
                INSERT INTO source_collection_runs
                (source_id, source_name, status, message, failure_stage, failure_reason,
                 releases_found, inserted_count, repaired_dates, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "flaky",
                    "불안정 기관",
                    status,
                    "점검 기록",
                    "",
                    "",
                    0,
                    0,
                    0,
                    checked_at,
                ),
            )

    app = web_module.create_app()
    app.testing = True
    payload = app.test_client().get("/healthz/details").get_json()

    assert payload["collection_check_coverage_status"] == "warning"
    assert payload["collection_check_coverage_label"] == "실패 포함"
    assert payload["collection_check_coverage_checked_today"] == 1
    assert payload["collection_check_coverage_success_today"] == 0
    assert payload["collection_check_coverage_failed_today"] == 1
    assert payload["collection_check_coverage_failed_sources"] == ["불안정 기관"]


def test_healthz_collection_check_coverage_marks_complete_before_check_hour(monkeypatch):
    db_path = Path(f"data/.test_healthz_collection_complete_early_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_COLLECTION_COVERAGE_CHECK_HOUR", "9")
    store = Store(db_path)
    store.init_db()
    sources = [
        Source(id="first", name="첫 기관", region="전남", type="html_board"),
        Source(id="second", name="둘째 기관", region="전남", type="html_board"),
    ]

    from news_summary import web as web_module

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 7, 6, 8, 30, tzinfo=LOCAL_TZ)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(web_module, "datetime", FixedDatetime)
    monkeypatch.setattr(web_module, "load_sources", lambda config_path: sources)
    with store.connect() as conn:
        for source in sources:
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
                    "ok",
                    "점검 완료",
                    "",
                    "",
                    0,
                    0,
                    0,
                    "2026-07-05T23:30:00+00:00",
                ),
            )

    app = web_module.create_app()
    app.testing = True
    payload = app.test_client().get("/healthz/details").get_json()

    assert payload["collection_check_coverage_status"] == "ok"
    assert payload["collection_check_coverage_label"] == "정상"
    assert payload["collection_check_coverage_checked_today"] == 2
    assert payload["collection_check_coverage_success_today"] == 2
    assert payload["collection_check_coverage_failed_today"] == 0


def test_healthz_reports_draft_conversion_coverage(monkeypatch):
    db_path = Path(f"data/.test_healthz_draft_conversion_coverage_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()

    from news_summary import web as web_module

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 7, 6, 14, 0, tzinfo=LOCAL_TZ)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(web_module, "datetime", FixedDatetime)
    drafted_id = store.add_press_release(
        PressRelease(
            source_id="gwangyang",
            source_name="광양시청 보도자료",
            region="전남",
            title="초안 생성 완료 원문",
            url="https://example.com/healthz-drafted",
            content="오늘 초안 변환 커버리지 점검용 원문입니다.",
            published_at="2026.07.06 09:30",
        )
    )
    store.add_press_release(
        PressRelease(
            source_id="suncheon",
            source_name="순천시청 보도자료",
            region="전남",
            title="초안 미변환 원문",
            url="https://example.com/healthz-pending",
            content="오늘 초안 변환 미처리 점검용 원문입니다.",
            published_at="2026-07-06 11:00",
        )
    )
    assert drafted_id is not None
    store.add_article_draft(
        ArticleDraft(
            press_release_id=drafted_id,
            title="초안 제목",
            body="초안 본문입니다.",
            review_note="검수 필요",
            model="gemini-3.5-flash",
        )
    )

    app = web_module.create_app()
    app.testing = True
    payload = app.test_client().get("/healthz/details").get_json()

    assert payload["draft_conversion_coverage_status"] == "warning"
    assert payload["draft_conversion_coverage_label"] == "미변환"
    assert payload["draft_conversion_today_releases"] == 2
    assert payload["draft_conversion_today_drafted"] == 1
    assert payload["draft_conversion_today_pending"] == 1
    assert payload["draft_conversion_retry_ready_pending"] == 1
    assert payload["draft_conversion_retry_scheduled_pending"] == 0
    assert payload["draft_conversion_retry_scope"] == "today_releases"
    assert payload["draft_conversion_next_retry_at"] is None
    assert payload["draft_conversion_effective_next_retry_at"] is None
    assert payload["draft_conversion_drafted_percent"] == 50
    assert payload["draft_conversion_pending_sources"] == [
        {"source_id": "suncheon", "source_name": "순천시청 보도자료", "count": 1}
    ]
    assert payload["draft_conversion_pending_source_total"] == 1
    assert payload["service_status_level"] in {"warning", "error"}
    assert any(
        issue["component"] == "draft_conversion_coverage"
        and issue["message"] == payload["draft_conversion_coverage_message"]
        for issue in payload["service_status_issues"]
    )


def test_healthz_reports_unresolved_source_collection_failures(monkeypatch):
    db_path = Path(f"data/.test_healthz_source_collection_failures_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()
    for _ in range(3):
        store.record_source_collection_status(
            "sample-source",
            "전남광주통합특별시 테스트 기관 보도자료",
            "failed",
            "테스트 기관 수집 실패: 본문 구조 변경",
            failure_stage="본문 파싱 실패",
            failure_reason="본문 선택자를 찾지 못했습니다",
        )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    payload = app.test_client().get("/healthz/details").get_json()

    assert payload["ok"] is True
    assert payload["source_collection_status"] == "error"
    assert payload["source_collection_recent_failure_count"] == 3
    assert payload["source_collection_unresolved_count"] == 1
    assert payload["source_collection_temporary_count"] == 0
    assert payload["source_collection_message"] == "미복구 수집 실패 기관 1곳"
    assert payload["source_collection_recent_failed_sources"] == [
        {
            "source_id": "sample-source",
            "source_name": "테스트 기관 보도자료",
            "failure_count": 3,
            "latest_failure_stage": "본문 파싱 실패",
        }
    ]
    assert payload["source_collection_recovered_recent_sources"] == []
    assert payload["source_collection_failure_stages"] == [
        {"stage": "본문 파싱 실패", "count": 3}
    ]
    assert payload["source_collection_unresolved_sources"] == [
        {
            "source_id": "sample-source",
            "source_name": "테스트 기관 보도자료",
            "consecutive_failures": 3,
            "failure_stage": "본문 파싱 실패",
        }
    ]
    assert payload["service_status_level"] == "error"
    assert any(
        issue["component"] == "source_collection"
        and issue["message"] == "미복구 수집 실패 기관 1곳"
        for issue in payload["service_status_issues"]
    )


def test_healthz_treats_recent_collection_failures_recovered_by_latest_ok_as_ok(monkeypatch):
    db_path = Path(f"data/.test_healthz_source_collection_recovered_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()
    with store.connect() as conn:
        for status, checked_at in (
            ("failed", "2026-07-08T23:00:00+00:00"),
            ("failed", "2026-07-08T23:05:00+00:00"),
            ("ok", "2026-07-08T23:10:00+00:00"),
        ):
            conn.execute(
                """
                INSERT INTO source_collection_runs
                (source_id, source_name, status, message, failure_stage, failure_reason,
                 releases_found, inserted_count, repaired_dates, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "sample-source",
                    "전남광주통합특별시 테스트 기관 보도자료",
                    status,
                    "최신 점검 정상" if status == "ok" else "일시 연결 지연",
                    "외부 사이트 응답 지연" if status == "failed" else "",
                    "ReadTimeout" if status == "failed" else "",
                    1 if status == "ok" else 0,
                    1 if status == "ok" else 0,
                    0,
                    checked_at,
                ),
            )

    from news_summary import web as web_module

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 7, 9, 8, 30, tzinfo=LOCAL_TZ)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(web_module, "datetime", FixedDatetime)
    app = web_module.create_app()
    app.testing = True
    payload = app.test_client().get("/healthz/details").get_json()

    assert payload["source_collection_status"] == "ok"
    assert payload["source_collection_recent_failure_count"] == 2
    assert payload["source_collection_recovered_recent_failure_count"] == 2
    assert payload["source_collection_unresolved_count"] == 0
    assert payload["source_collection_temporary_count"] == 0
    assert payload["source_collection_recent_failed_sources"] == []
    assert payload["source_collection_recovered_recent_sources"] == [
        {
            "source_id": "sample-source",
            "source_name": "테스트 기관 보도자료",
            "failure_count": 2,
            "latest_failure_stage": "외부 사이트 응답 지연",
        }
    ]
    assert payload["source_collection_message"] == "최근 실패 2건은 최신 점검에서 복구됐습니다."


def test_healthz_reports_gemini_queue_warning(monkeypatch):
    db_path = Path(f"data/.test_healthz_gemini_queue_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_GEMINI_RETRY_DUE_WARNING_COUNT", "2")
    store = Store(db_path)
    store.init_db()
    due_at = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    store.set_app_metadata(
        "auto_queue_drain_status_snapshot",
        json.dumps(
            {
                "updated_at": "2026-07-09T00:30:00+00:00",
                "queue_pending_before": 7,
                "queue_pending_after": 5,
                "queue_processed_count": 2,
                "queue_failure_before": 4,
                "queue_failure_after": 3,
                "queue_retry_due_before": 4,
                "queue_retry_due_after": 3,
                "queue_drain_messages": [
                    "오래된 메시지",
                    "Gemini 미변환 큐 자동 소진: 대기 7건, 처리 한도 25건",
                    "초안 생성 완료 2건",
                ],
            },
            ensure_ascii=False,
        ),
    )
    for index in range(2):
        release_id = store.add_press_release(
            PressRelease(
                source_id="sample",
                source_name="테스트 기관",
                region="전남",
                title=f"헬스 체크 재시도 원문 {index + 1}",
                url=f"https://example.com/healthz-retry-{index}",
                content="Gemini 헬스 체크 대기열 테스트 원문입니다.",
                published_at="2026-06-26 09:00",
            )
        )
        assert release_id is not None
        store.record_draft_generation_failure(
            release_id,
            "quota",
            "Gemini 처리 재개 대기",
            "gemini-3.5-flash",
            due_at,
        )

    from news_summary import web as web_module

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 7, 9, 0, 31, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(web_module, "datetime", FixedDatetime)
    app = web_module.create_app()
    app.testing = True
    payload = app.test_client().get("/healthz/details").get_json()

    assert payload["ok"] is True
    assert payload["gemini_queue_status"] == "warning"
    assert payload["gemini_pending_total"] == 2
    assert payload["gemini_failure_total"] == 2
    assert payload["gemini_retry_due"] == 2
    assert payload["gemini_queue_scope"] == "all_releases"
    assert payload["gemini_next_retry_at"] == due_at
    assert payload["gemini_effective_next_retry_at"] == due_at
    assert payload["gemini_oldest_first_failed_at"]
    assert payload["gemini_last_queue_drain_at"] == "2026-07-09T00:30:00+00:00"
    assert payload["gemini_next_queue_drain_at"] == "2026-07-09T00:35:00+00:00"
    assert payload["gemini_last_queue_pending_before"] == 7
    assert payload["gemini_last_queue_pending_after"] == 5
    assert payload["gemini_last_queue_processed_count"] == 2
    assert payload["gemini_last_queue_failure_before"] == 4
    assert payload["gemini_last_queue_failure_after"] == 3
    assert payload["gemini_last_queue_retry_due_before"] == 4
    assert payload["gemini_last_queue_retry_due_after"] == 3
    assert payload["gemini_last_queue_drain_messages"] == [
        "오래된 메시지",
        "Gemini 미변환 큐 자동 소진: 대기 7건, 처리 기준 25건",
        "초안 생성 완료 2건",
    ]
    assert payload["gemini_queue_message"] == "Gemini 자동 재처리 대기 원문 2건"
    assert payload["gemini_cooldown_active"] is False


def test_queue_drain_next_run_does_not_return_past_time(monkeypatch):
    from news_summary import web as web_module

    monkeypatch.setenv("NEWS_SUMMARY_AUTO_QUEUE_DRAIN_INTERVAL_SECONDS", "900")
    now = datetime(2026, 7, 9, 2, 0, tzinfo=timezone.utc)

    next_run_at = web_module._queue_drain_next_run_at(
        "2026-07-09T01:30:00+00:00",
        now=now,
    )

    assert next_run_at == "2026-07-09T02:00:00+00:00"


def test_queue_drain_next_run_waits_for_later_gemini_resume(monkeypatch):
    from news_summary import web as web_module

    monkeypatch.setenv("NEWS_SUMMARY_AUTO_QUEUE_DRAIN_INTERVAL_SECONDS", "900")
    next_run_at = web_module._queue_drain_next_run_at(
        "2026-07-09T01:30:00+00:00",
        cooldown_until=datetime(2026, 7, 9, 2, 0, tzinfo=timezone.utc),
        now=datetime(2026, 7, 9, 1, 40, tzinfo=timezone.utc),
    )

    assert next_run_at == "2026-07-09T02:00:00+00:00"


def test_healthz_reports_small_gemini_queue_without_warning(monkeypatch):
    db_path = Path(f"data/.test_healthz_small_gemini_queue_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_GEMINI_RETRY_DUE_WARNING_COUNT", "2")
    store = Store(db_path)
    store.init_db()
    due_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    release_id = store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 기관",
            region="전남",
            title="소량 재처리 가능 원문",
            url="https://example.com/healthz-small-retry",
            content="Gemini 헬스 체크 소량 대기열 테스트 원문입니다.",
            published_at="2026-07-09 09:00",
        )
    )
    assert release_id is not None
    store.record_draft_generation_failure(
        release_id,
        "quota",
        "Gemini 처리 재개 대기",
        "gemini-3.5-flash",
        due_at,
    )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    payload = app.test_client().get("/healthz/details").get_json()

    assert payload["ok"] is True
    assert payload["gemini_queue_status"] == "ok"
    assert payload["gemini_pending_total"] == 1
    assert payload["gemini_failure_total"] == 1
    assert payload["gemini_retry_due"] == 1
    assert payload["gemini_queue_message"] == "Gemini 자동 재처리 대기 원문 1건"


def test_healthz_reports_gemini_cooldown_window(monkeypatch):
    db_path = Path(f"data/.test_healthz_gemini_cooldown_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()
    cooldown_until = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    store.set_app_metadata("gemini_cooldown_until", cooldown_until)
    store.set_app_metadata("gemini_cooldown_reason", "자동 초안 생성 재개 대기")
    store.set_app_metadata(
        "auto_queue_drain_status_snapshot",
        json.dumps(
            {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "queue_pending_before": 5,
                "queue_pending_after": 5,
                "queue_processed_count": 0,
                "queue_drain_messages": [],
            },
            ensure_ascii=False,
        ),
    )

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    payload = app.test_client().get("/healthz/details").get_json()

    assert payload["ok"] is True
    assert payload["gemini_queue_status"] == "warning"
    assert payload["gemini_cooldown_active"] is True
    assert payload["gemini_cooldown_until"] == cooldown_until
    assert payload["gemini_cooldown_reason"] == "자동 초안 생성 재개 대기"
    assert payload["gemini_effective_next_retry_at"] is None
    assert payload["gemini_queue_message"].startswith("Gemini 처리 재개 대기:")
    assert payload["gemini_queue_message"].endswith("까지")
    assert payload["gemini_next_queue_drain_at"] == cooldown_until


def test_healthz_reports_effective_gemini_retry_time_during_wait(monkeypatch):
    db_path = Path(f"data/.test_healthz_effective_gemini_retry_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    store = Store(db_path)
    store.init_db()
    retry_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    cooldown_until = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    release_id = store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 기관",
            region="전남",
            title="재개 대기 중 재처리 원문",
            url="https://example.com/effective-gemini-retry",
            content="Gemini 재개 대기 시각 계산용 원문입니다.",
            published_at="2026-07-09 09:00",
        )
    )
    assert release_id is not None
    store.record_draft_generation_failure(
        release_id,
        "quota",
        "Gemini 처리 재개 대기",
        "gemini-3.5-flash",
        retry_at,
    )
    store.set_app_metadata("gemini_cooldown_until", cooldown_until)

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    payload = app.test_client().get("/healthz/details").get_json()

    assert payload["gemini_next_retry_at"] == retry_at
    assert payload["gemini_effective_next_retry_at"] == cooldown_until
    assert payload["gemini_queue_status"] == "warning"
    assert payload["gemini_queue_message"].startswith("Gemini 처리 재개 대기:")
    assert "전체 대기 1건" in payload["gemini_queue_message"]
    assert "처리 재개 대기 1건" in payload["gemini_queue_message"]


def test_healthz_warns_when_gemini_queue_resume_is_overdue(monkeypatch):
    db_path = Path(f"data/.test_healthz_gemini_queue_resume_overdue_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_GEMINI_RETRY_DUE_WARNING_COUNT", "50")
    monkeypatch.setenv("NEWS_SUMMARY_AUTO_QUEUE_DRAIN_READY_RECHECK_SECONDS", "300")
    store = Store(db_path)
    store.init_db()
    due_at = datetime(2026, 7, 9, 1, 0, tzinfo=timezone.utc).isoformat()
    store.set_app_metadata(
        "auto_queue_drain_status_snapshot",
        json.dumps(
            {
                "updated_at": "2026-07-09T00:50:00+00:00",
                "queue_pending_before": 1,
                "queue_pending_after": 1,
                "queue_processed_count": 0,
                "queue_failure_before": 1,
                "queue_failure_after": 1,
                "queue_retry_due_before": 1,
                "queue_retry_due_after": 1,
                "queue_drain_messages": [],
            },
            ensure_ascii=False,
        ),
    )
    release_id = store.add_press_release(
        PressRelease(
            source_id="sample",
            source_name="테스트 기관",
            region="전남",
            title="재처리 점검 지연 원문",
            url="https://example.com/gemini-queue-overdue",
            content="Gemini 재처리 점검 지연 테스트 원문입니다.",
            published_at="2026-07-09 09:00",
        )
    )
    assert release_id is not None
    store.record_draft_generation_failure(
        release_id,
        "quota",
        "Gemini 처리 재개 대기",
        "gemini-3.5-flash",
        due_at,
    )

    from news_summary import web as web_module

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 7, 9, 1, 10, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(web_module, "datetime", FixedDatetime)
    app = web_module.create_app()
    app.testing = True
    payload = app.test_client().get("/healthz/details").get_json()

    assert payload["gemini_queue_status"] == "warning"
    assert payload["gemini_queue_resume_overdue"] is True
    assert payload["gemini_queue_resume_overdue_minutes"] == 10
    assert payload["gemini_retry_due"] == 1
    assert payload["gemini_queue_message"].startswith("Gemini 자동 재처리 점검 지연:")
    assert "전체 대기 1건" in payload["gemini_queue_message"]
    assert "처리 재개 대기 1건" in payload["gemini_queue_message"]


def test_healthz_and_operations_report_stopped_auto_collector_thread(monkeypatch):
    db_path = Path(f"data/.test_healthz_stopped_collector_thread_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))

    from news_summary.web import create_app

    class StoppedCollector:
        def snapshot(self):
            return AutoCollectorStatus(
                enabled=True,
                running=False,
                thread_alive=False,
                progress_total=29,
                progress_message="다음 정각 자동 수집 대기 중",
            )

    app = create_app()
    app.config["AUTO_COLLECTOR"] = StoppedCollector()
    app.testing = True
    client = app.test_client()

    health = client.get("/healthz").get_json()
    operations_html = client.get("/operations").data.decode("utf-8")

    assert health["auto_collector"] == "stopped"
    assert health["auto_collector_thread_alive"] is False
    assert "자동 수집 백그라운드 스레드 중단" in operations_html


def test_healthz_restarts_enabled_auto_collector_thread(monkeypatch):
    db_path = Path(f"data/.test_healthz_restart_collector_thread_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_STARTUP_CATCHUP", "false")
    monkeypatch.setattr("news_summary.scheduler.load_sources", lambda config_path: [])

    from news_summary.scheduler import AutoCollector
    from news_summary.web import create_app

    store = Store(db_path)
    store.init_db()
    collector = AutoCollector(store, Path("unused.yaml"), enabled=True)
    app = create_app()
    app.config["AUTO_COLLECTOR"] = collector
    app.testing = True
    client = app.test_client()

    try:
        health = client.get("/healthz").get_json()

        assert health["auto_collector"] == "enabled"
        assert health["auto_collector_thread_alive"] is True
        assert collector.snapshot().thread_alive is True
    finally:
        collector.stop()
        if collector._thread:
            collector._thread.join(timeout=1)


def test_healthz_reports_overdue_auto_collection_timing(monkeypatch):
    db_path = Path(f"data/.test_healthz_overdue_auto_collection_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_AUTO_FINISH_OVERDUE_MINUTES", "90")

    from news_summary.web import create_app

    old_finished_at = (datetime.now(LOCAL_TZ) - timedelta(hours=3)).isoformat()
    next_run_at = (datetime.now(LOCAL_TZ) + timedelta(minutes=20)).isoformat()

    class IdleCollector:
        def snapshot(self):
            return AutoCollectorStatus(
                enabled=True,
                running=False,
                thread_alive=True,
                interval_seconds=3600,
                last_auto_finished_at=old_finished_at,
                next_run_at=next_run_at,
                progress_total=29,
                progress_message="다음 정각 자동 수집 대기 중",
            )

    app = create_app()
    app.config["AUTO_COLLECTOR"] = IdleCollector()
    app.testing = True
    health = app.test_client().get("/healthz").get_json()

    assert health["ok"] is True
    assert health["auto_collector"] == "enabled"
    assert health["auto_collector_timing"] == "warning"
    assert health["auto_collector_overdue"] is True
    assert health["auto_collector_lag_minutes"] >= 170
    assert "마지막 자동 수집 후" in health["auto_collector_health_message"]


def test_healthz_and_operations_report_missed_next_auto_run(monkeypatch):
    db_path = Path(f"data/.test_healthz_missed_next_auto_run_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_AUTO_NEXT_RUN_GRACE_MINUTES", "5")

    from news_summary.web import create_app

    missed_next_run_at = (datetime.now(LOCAL_TZ) - timedelta(minutes=20)).isoformat()

    class IdleCollector:
        def snapshot(self):
            return AutoCollectorStatus(
                enabled=True,
                running=False,
                thread_alive=True,
                interval_seconds=3600,
                next_run_at=missed_next_run_at,
                progress_total=29,
                progress_message="다음 정각 자동 수집 대기 중",
            )

    app = create_app()
    app.config["AUTO_COLLECTOR"] = IdleCollector()
    app.testing = True
    client = app.test_client()

    health = client.get("/healthz").get_json()
    operations_html = client.get("/operations").data.decode("utf-8")

    assert health["auto_collector_timing"] == "warning"
    assert health["auto_collector_overdue"] is True
    assert health["auto_collector_schedule_delay_minutes"] >= 19
    assert "다음 실행 예정 시각" in health["auto_collector_health_message"]
    assert "다음 실행 예정 시각" in operations_html


def test_healthz_and_operations_warn_long_running_auto_collection(monkeypatch):
    db_path = Path(f"data/.test_healthz_long_running_auto_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_AUTO_RUNNING_WARN_MINUTES", "60")

    from news_summary.web import create_app

    old_started_at = (datetime.now(LOCAL_TZ) - timedelta(hours=2)).isoformat()

    class RunningCollector:
        def snapshot(self):
            return AutoCollectorStatus(
                enabled=True,
                running=True,
                thread_alive=True,
                interval_seconds=3600,
                last_started_at=old_started_at,
                progress_current=4,
                progress_total=29,
                progress_message="4/29 광주 남구청 보도자료 연결 확인 중",
                progress_source_name="광주 남구청 보도자료",
            )

    app = create_app()
    app.config["AUTO_COLLECTOR"] = RunningCollector()
    app.testing = True
    client = app.test_client()

    health = client.get("/healthz").get_json()
    operations_html = client.get("/operations").data.decode("utf-8")

    assert health["auto_collector"] == "running"
    assert health["auto_collector_timing"] == "warning"
    assert health["auto_collector_overdue"] is True
    assert health["auto_collector_run_minutes"] >= 119
    assert "분째 실행 중" in health["auto_collector_health_message"]
    assert "분째 실행 중" in operations_html


def test_source_coverage_report_covers_required_municipal_sources():
    report = _source_coverage_report(Path("config/municipalities.yaml"))

    assert report["status_level"] == "ok"
    assert report["expected_total"] == 29
    assert report["configured_required_count"] == 29
    assert report["enabled_required_count"] == 29
    assert report["missing_labels"] == []
    assert report["disabled_labels"] == []
    assert report["duplicate_ids"] == []


def test_source_coverage_report_flags_missing_disabled_and_duplicate_sources(tmp_path):
    config_path = tmp_path / "sources.yaml"
    config_path.write_text(
        """
sources:
  - id: gwangju-city
    name: 광주청사
    region: 광주
    type: html_board
    enabled: false
  - id: gwangju-city
    name: 광주청사 중복
    region: 광주
    type: html_board
  - id: unexpected-source
    name: 추가 소스
    region: 기타
    type: html_board
""",
        encoding="utf-8",
    )

    report = _source_coverage_report(config_path)

    assert report["status_level"] == "warning"
    assert report["expected_total"] == 29
    assert report["configured_required_count"] == 1
    assert report["enabled_required_count"] == 1
    assert "광주 동구" in report["missing_labels"]
    assert "gwangju-city" in report["duplicate_ids"]
    assert report["extra_ids"] == ["unexpected-source"]


def test_operations_page_shows_source_coverage_card(monkeypatch):
    db_path = Path(f"data/.test_operations_source_coverage_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    html = client.get("/operations").data.decode("utf-8")

    assert "수집 대상 커버리지" in html
    assert "29/29" in html
    assert "광주·전남 필수 수집 대상이 모두 포함되어 있습니다." in html


def test_collection_check_coverage_report_flags_unchecked_business_day_sources(monkeypatch):
    db_path = Path(f"data/.test_collection_check_coverage_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    sources = [
        Source(id="checked", name="점검 기관", region="전남", type="html_board"),
        Source(id="missing", name="미점검 기관", region="전남", type="html_board"),
    ]

    from news_summary import web as web_module

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 7, 6, 10, 0, tzinfo=LOCAL_TZ)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(web_module, "datetime", FixedDatetime)
    monkeypatch.setenv("NEWS_SUMMARY_COLLECTION_COVERAGE_CHECK_HOUR", "9")
    monkeypatch.setattr(web_module, "load_sources", lambda config_path: sources)
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO source_collection_runs
            (source_id, source_name, status, message, failure_stage, failure_reason,
             releases_found, inserted_count, repaired_dates, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "checked",
                "점검 기관",
                "ok",
                "원문 검증 통과 1건, 새로 저장 1건",
                "",
                "",
                1,
                1,
                0,
                "2026-07-06T00:30:00+00:00",
            ),
        )
    store.add_press_release(
        PressRelease(
            source_id="checked",
            source_name="점검 기관",
            region="전남",
            title="오늘 원문",
            url="https://example.com/today-release",
            content="오늘 수집 커버리지 점검용 원문입니다.",
            published_at="2026-07-06",
        )
    )

    report = _collection_check_coverage_report(store, Path("unused.yaml"))

    assert report["status_level"] == "warning"
    assert report["status_label"] == "미점검"
    assert report["enabled_total"] == 2
    assert report["checked_today"] == 1
    assert report["success_today"] == 1
    assert report["today_release_sources"] == 1
    assert report["unchecked_items"] == [{"source_id": "missing", "source_name": "미점검 기관"}]
    assert report["unchecked_labels"] == ["미점검 기관"]


def test_collection_check_coverage_report_does_not_warn_on_holidays(monkeypatch):
    db_path = Path(f"data/.test_collection_check_coverage_holiday_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    source = Source(id="holiday", name="휴일 기관", region="전남", type="html_board")

    from news_summary import web as web_module

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 7, 5, 12, 0, tzinfo=LOCAL_TZ)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(web_module, "datetime", FixedDatetime)
    monkeypatch.setattr(web_module, "load_sources", lambda config_path: [source])

    report = _collection_check_coverage_report(store, Path("unused.yaml"))

    assert report["status_level"] == "ok"
    assert report["status_label"] == "휴일 대기"
    assert report["enabled_total"] == 1
    assert report["checked_today"] == 0
    assert "공휴일" in report["message"]


def test_operations_page_shows_collection_check_coverage_card(monkeypatch):
    db_path = Path(f"data/.test_operations_collection_check_coverage_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))

    from news_summary import web as web_module
    from news_summary.web import create_app

    monkeypatch.setattr(
        web_module,
        "_collection_check_coverage_report",
        lambda store, config_path: {
            "status_level": "warning",
            "status_label": "미점검",
            "date": "2026-07-06",
            "enabled_total": 2,
            "checked_today": 1,
            "success_today": 1,
            "failed_today": 0,
            "today_release_sources": 1,
            "unchecked_items": [{"source_id": "missing", "source_name": "미점검 기관"}],
            "failed_items": [],
            "unchecked_labels": ["미점검 기관"],
            "failed_labels": [],
            "message": "오늘 아직 점검되지 않은 기관이 1곳 있습니다.",
        },
    )

    app = create_app()
    app.testing = True
    html = app.test_client().get("/operations").data.decode("utf-8")

    assert "오늘 수집 점검 커버리지" in html
    assert "1/2" in html
    assert "오늘 아직 점검되지 않은 기관이 1곳 있습니다." in html
    assert 'href="/sources/missing"' in html
    assert "미점검 기관" in html


def test_draft_conversion_coverage_report_flags_today_pending_releases(monkeypatch):
    db_path = Path(f"data/.test_draft_conversion_coverage_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()

    from news_summary import web as web_module

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 7, 6, 14, 0, tzinfo=LOCAL_TZ)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(web_module, "datetime", FixedDatetime)
    drafted_id = store.add_press_release(
        PressRelease(
            source_id="gwangyang",
            source_name="광양시청 보도자료",
            region="전남",
            title="초안 생성 완료 원문",
            url="https://example.com/drafted",
            content="오늘 초안 변환 커버리지 점검용 원문입니다.",
            published_at="2026.07.06 09:30",
        )
    )
    pending_id = store.add_press_release(
        PressRelease(
            source_id="suncheon",
            source_name="순천시청 보도자료",
            region="전남",
            title="초안 미변환 원문",
            url="https://example.com/pending",
            content="오늘 초안 변환 미처리 점검용 원문입니다.",
            published_at="2026-07-06 11:00",
        )
    )
    store.add_press_release(
        PressRelease(
            source_id="old",
            source_name="이전 기관 보도자료",
            region="전남",
            title="이전 날짜 원문",
            url="https://example.com/old",
            content="이전 날짜 원문입니다.",
            published_at="2026-07-05 11:00",
        )
    )
    assert drafted_id is not None
    assert pending_id is not None
    store.add_article_draft(
        ArticleDraft(
            press_release_id=drafted_id,
            title="초안 제목",
            body="초안 본문입니다.",
            review_note="검수 필요",
            model="gemini-3.5-flash",
        )
    )

    report = _draft_conversion_coverage_report(store)

    assert report["status_level"] == "warning"
    assert report["status_label"] == "미변환"
    assert report["today_releases"] == 2
    assert report["today_drafted"] == 1
    assert report["today_pending"] == 1
    assert report["retry_ready_pending"] == 1
    assert report["retry_scheduled_pending"] == 0
    assert report["next_retry_at"] is None
    assert report["drafted_percent"] == 50
    assert report["pending_sources"] == [{"source_id": "suncheon", "source_name": "순천시청 보도자료", "count": 1}]
    assert "미변환 1건" in report["message"]


def test_draft_conversion_coverage_report_splits_ready_and_scheduled_pending(monkeypatch):
    db_path = Path(f"data/.test_draft_conversion_retry_split_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()

    from news_summary import web as web_module

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 7, 6, 14, 0, tzinfo=LOCAL_TZ)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(web_module, "datetime", FixedDatetime)
    ready_id = store.add_press_release(
        PressRelease(
            source_id="ready",
            source_name="즉시 기관 보도자료",
            region="전남",
            title="즉시 재시도 원문",
            url="https://example.com/ready-draft",
            content="즉시 재시도 가능한 오늘 원문입니다.",
            published_at="2026-07-06 10:00",
        )
    )
    scheduled_id = store.add_press_release(
        PressRelease(
            source_id="scheduled",
            source_name="예약 기관 보도자료",
            region="전남",
            title="예약 대기 원문",
            url="https://example.com/scheduled-draft",
            content="예약 대기 중인 오늘 원문입니다.",
            published_at="2026-07-06 11:00",
        )
    )
    assert ready_id is not None
    assert scheduled_id is not None
    store.record_draft_generation_failure(
        scheduled_id,
        "quota",
        "예약 대기 테스트",
        "gemini-3.5-flash",
        "2026-07-06T06:30:00+00:00",
    )

    report = _draft_conversion_coverage_report(store)

    assert report["today_releases"] == 2
    assert report["today_pending"] == 2
    assert report["retry_ready_pending"] == 1
    assert report["retry_ready_label"] == "자동 처리 대기"
    assert report["retry_scheduled_pending"] == 1
    assert report["pending_source_total"] == 2
    assert report["next_retry_at"] == "2026-07-06T06:30:00+00:00"
    assert report["effective_next_retry_at"] == "2026-07-06T06:30:00+00:00"
    assert "자동 처리 대기 1건" in report["message"]
    assert "예약 대기 1건" in report["message"]


def test_draft_conversion_coverage_report_uses_collected_time_for_date_only_pending(monkeypatch):
    db_path = Path(f"data/.test_draft_conversion_date_only_pending_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()

    from news_summary import web as web_module

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 7, 6, 14, 0, tzinfo=LOCAL_TZ)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(web_module, "datetime", FixedDatetime)
    store.add_press_release(
        PressRelease(
            source_id="date-only",
            source_name="날짜 기관 보도자료",
            region="전남",
            title="게시일 날짜만 있는 원문",
            url="https://example.com/date-only-pending",
            content="게시일은 날짜만 있고 수집 시간은 별도로 있는 원문입니다.",
            published_at="2026-07-06",
            collected_at="2026-07-06T04:30:00+00:00",
        )
    )

    report = _draft_conversion_coverage_report(store)

    assert report["today_pending"] == 1
    assert report["latest_pending_at"] == "2026-07-06T04:30:00+00:00"
    assert report["oldest_pending_at"] == "2026-07-06T04:30:00+00:00"


def test_draft_conversion_coverage_report_uses_latest_unresolved_failure_per_release(monkeypatch):
    db_path = Path(f"data/.test_draft_conversion_latest_failure_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()

    from news_summary import web as web_module

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 7, 6, 14, 0, tzinfo=LOCAL_TZ)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(web_module, "datetime", FixedDatetime)
    release_id = store.add_press_release(
        PressRelease(
            source_id="retry",
            source_name="재처리 기관 보도자료",
            region="전남",
            title="중복 실패 기록 원문",
            url="https://example.com/latest-failure-draft-coverage",
            content="오늘 초안 변환 현황에서 중복 실패 기록이 한 번만 집계되어야 합니다.",
            published_at="2026-07-06 11:00",
        )
    )
    assert release_id is not None
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO draft_generation_failures
            (press_release_id, failure_kind, message, attempted_models,
             attempts, first_failed_at, last_failed_at, next_retry_at, resolved_at)
            VALUES (?, 'generation_error', '이전 실패', 'gemini-3.5-flash', 1,
                    '2026-07-06T04:00:00+00:00', '2026-07-06T04:00:00+00:00',
                    '2026-07-06T04:30:00+00:00', NULL)
            """,
            (release_id,),
        )
        conn.execute(
            """
            INSERT INTO draft_generation_failures
            (press_release_id, failure_kind, message, attempted_models,
             attempts, first_failed_at, last_failed_at, next_retry_at, resolved_at)
            VALUES (?, 'quota', '최신 실패', 'gemini-3.5-flash', 2,
                    '2026-07-06T04:00:00+00:00', '2026-07-06T05:00:00+00:00',
                    '2026-07-06T06:30:00+00:00', NULL)
            """,
            (release_id,),
        )

    report = _draft_conversion_coverage_report(store)

    assert report["today_pending"] == 1
    assert report["retry_ready_pending"] == 0
    assert report["retry_scheduled_pending"] == 1
    assert report["next_retry_at"] == "2026-07-06T06:30:00+00:00"
    assert "예약 대기 1건" in report["message"]


def test_draft_conversion_coverage_report_uses_effective_retry_time_during_gemini_wait(monkeypatch):
    db_path = Path(f"data/.test_draft_conversion_effective_retry_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()

    from news_summary import web as web_module

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 7, 6, 14, 0, tzinfo=LOCAL_TZ)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(web_module, "datetime", FixedDatetime)
    scheduled_id = store.add_press_release(
        PressRelease(
            source_id="scheduled",
            source_name="예약 기관 보도자료",
            region="전남",
            title="예약 대기 원문",
            url="https://example.com/effective-scheduled-draft",
            content="재개 시각 기준 메시지 점검용 원문입니다.",
            published_at="2026-07-06 11:00",
        )
    )
    assert scheduled_id is not None
    store.record_draft_generation_failure(
        scheduled_id,
        "quota",
        "예약 대기 테스트",
        "gemini-3.5-flash",
        "2026-07-06T06:30:00+00:00",
    )
    monkeypatch.setattr(
        web_module,
        "gemini_cooldown_until",
        lambda store: datetime(2026, 7, 6, 7, 0, tzinfo=timezone.utc),
    )

    report = _draft_conversion_coverage_report(store)

    assert report["next_retry_at"] == "2026-07-06T06:30:00+00:00"
    assert report["effective_next_retry_at"] == "2026-07-06T07:00:00+00:00"
    assert "다음 처리 가능 시각은 2026.07.06 16:00" in report["message"]
    assert "초안 생성 재개 예정" not in report["message"]


def test_draft_conversion_coverage_report_marks_complete_when_all_today_releases_have_drafts(monkeypatch):
    db_path = Path(f"data/.test_draft_conversion_coverage_complete_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()

    from news_summary import web as web_module

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 7, 6, 14, 0, tzinfo=LOCAL_TZ)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(web_module, "datetime", FixedDatetime)
    release_id = store.add_press_release(
        PressRelease(
            source_id="mokpo",
            source_name="목포시청 보도자료",
            region="전남",
            title="오늘 원문",
            url="https://example.com/complete",
            content="오늘 초안 변환 완료 점검용 원문입니다.",
            published_at="2026-07-06 10:00",
        )
    )
    assert release_id is not None
    store.add_article_draft(
        ArticleDraft(
            press_release_id=release_id,
            title="초안 제목",
            body="초안 본문입니다.",
            review_note="검수 필요",
            model="gemini-3.5-flash",
        )
    )

    report = _draft_conversion_coverage_report(store)

    assert report["status_level"] == "ok"
    assert report["status_label"] == "정상"
    assert report["today_releases"] == 1
    assert report["today_drafted"] == 1
    assert report["today_pending"] == 0
    assert report["drafted_percent"] == 100
    assert report["pending_sources"] == []


def test_operations_page_shows_draft_conversion_coverage_card(monkeypatch):
    db_path = Path(f"data/.test_operations_draft_conversion_coverage_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))

    from news_summary import web as web_module

    monkeypatch.setattr(
        web_module,
        "_draft_conversion_coverage_report",
        lambda store: {
            "status_level": "warning",
            "status_label": "미변환",
            "date": "2026-07-06",
            "today_releases": 4,
            "today_drafted": 3,
            "today_pending": 1,
            "retry_ready_pending": 1,
            "retry_scheduled_pending": 0,
            "next_retry_at": None,
            "drafted_percent": 75,
            "pending_sources": [{"source_id": "suncheon-city", "source_name": "순천시청 보도자료", "count": 1}],
            "latest_pending_at": "2026-07-06 11:00",
            "oldest_pending_at": "2026-07-06 11:00",
            "message": "오늘 수집 원문 중 초안 미변환 1건이 남아 있습니다.",
        },
    )
    app = web_module.create_app()
    app.testing = True
    html = app.test_client().get("/operations").data.decode("utf-8")

    assert "오늘 초안 변환 커버리지" in html
    assert "3/4" in html
    assert "변환율 75%" in html
    assert "자동 처리 대기 1건" in html
    assert "예약 대기 0건" not in html
    assert "오늘 수집 원문 중 초안 미변환 1건이 남아 있습니다." in html
    assert "순천시청 보도자료 1건" in html
    assert 'href="/press-releases?draft=missing&amp;date=2026-07-06&amp;source=suncheon-city"' in html


def test_operations_page_shows_hidden_pending_source_count(monkeypatch):
    db_path = Path(f"data/.test_operations_pending_source_total_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))

    from news_summary import web as web_module

    monkeypatch.setattr(
        web_module,
        "_draft_conversion_coverage_report",
        lambda store: {
            "status_level": "warning",
            "status_label": "미변환",
            "date": "2026-07-06",
            "today_releases": 12,
            "today_drafted": 5,
            "today_pending": 7,
            "retry_ready_pending": 7,
            "retry_ready_label": "자동 처리 대기",
            "retry_scheduled_pending": 0,
            "next_retry_at": None,
            "effective_next_retry_at": None,
            "drafted_percent": 42,
            "pending_sources": [
                {"source_id": f"source-{i}", "source_name": f"기관{i} 보도자료", "count": 1}
                for i in range(5)
            ],
            "pending_source_total": 7,
            "latest_pending_at": None,
            "oldest_pending_at": None,
            "message": "오늘 수집 원문 중 초안 미변환 7건이 남아 있습니다.",
        },
    )
    app = web_module.create_app()
    app.testing = True
    html = app.test_client().get("/operations").data.decode("utf-8")

    assert "미변환 기관" in html
    assert "기관0 보도자료 1건" in html
    assert "기관4 보도자료 1건" in html
    assert 'href="/press-releases?draft=missing&amp;date=2026-07-06&amp;source=source-0"' in html
    assert "외 2곳" in html


def test_operations_page_hides_zero_ready_retry_counts(monkeypatch):
    db_path = Path(f"data/.test_operations_draft_conversion_scheduled_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))

    from news_summary import web as web_module

    monkeypatch.setattr(
        web_module,
        "_draft_conversion_coverage_report",
        lambda store: {
            "status_level": "warning",
            "status_label": "미변환",
            "date": "2026-07-06",
            "today_releases": 4,
            "today_drafted": 3,
            "today_pending": 1,
            "retry_ready_pending": 0,
            "retry_scheduled_pending": 1,
            "next_retry_at": "2026-07-06T06:30:00+00:00",
            "effective_next_retry_at": "2026-07-06T06:30:00+00:00",
            "drafted_percent": 75,
            "pending_sources": [],
            "latest_pending_at": None,
            "oldest_pending_at": None,
            "message": "오늘 수집 원문 중 초안 미변환 1건이 남아 있습니다.",
        },
    )
    app = web_module.create_app()
    app.testing = True
    html = app.test_client().get("/operations").data.decode("utf-8")

    assert "자동 처리 대기 0건" not in html
    assert "예약 대기 1건" in html
    assert "다음 처리 2026.07.06 15:30" in html


def test_recovery_candidate_report_lists_sources_due_for_recheck(monkeypatch):
    db_path = Path(f"data/.test_recovery_candidate_report_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()
    source = Source(id="sample-source", name="테스트 기관 보도자료", region="전남", type="html_board")
    store.record_source_collection_status(
        source.id,
        source.name,
        "failed",
        "목록 후보를 찾지 못했습니다",
        failure_stage="사이트 구조 변경",
        failure_reason="목록/본문 선택자 확인 필요",
    )

    monkeypatch.setenv("NEWS_SUMMARY_AUTO_RECOVERY_LIMIT", "3")
    monkeypatch.setattr("news_summary.scheduler.load_sources", lambda config_path: [source])

    report = _recovery_candidate_report(store, Path("unused.yaml"))

    assert report["status_level"] == "warning"
    assert report["status_label"] == "대기"
    assert report["count"] == 1
    assert report["limit"] == 3
    assert report["candidates"][0]["source_name"] == "테스트 기관 보도자료"
    assert report["candidates"][0]["reason_label"] == "실패 재검증"


def test_recovery_candidate_report_handles_disabled_recovery(monkeypatch):
    db_path = Path(f"data/.test_recovery_candidate_report_disabled_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()

    monkeypatch.setenv("NEWS_SUMMARY_AUTO_RECOVERY_LIMIT", "0")

    report = _recovery_candidate_report(store, Path("unused.yaml"))

    assert report["status_level"] == "ok"
    assert report["status_label"] == "꺼짐"
    assert report["count"] == 0
    assert report["message"] == "자동 복구 후보 재검증이 꺼져 있습니다."


def test_operations_page_shows_recovery_candidate_card(monkeypatch):
    db_path = Path(f"data/.test_operations_recovery_candidates_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))

    from news_summary import web as web_module
    from news_summary.web import create_app

    monkeypatch.setattr(
        web_module,
        "_recovery_candidate_report",
        lambda store, config_path: {
            "status_level": "warning",
            "status_label": "대기",
            "count": 1,
            "limit": 5,
            "candidates": [
                {
                    "source_id": "sample-source",
                    "source_name": "전남광주통합특별시 테스트 기관",
                    "region": "전남",
                    "reason": "focused",
                    "reason_label": "이상치 집중 재수집",
                }
            ],
            "message": "다음 자동 유지보수에서 1곳을 우선 재검증합니다.",
        },
    )

    app = create_app()
    app.testing = True
    html = app.test_client().get("/operations").data.decode("utf-8")

    assert "자동 복구 예정" in html
    assert "다음 자동 유지보수에서 1곳을 우선 재검증합니다." in html
    assert "테스트 기관 · 이상치 집중 재수집" in html
    assert 'href="/sources/sample-source"' in html


def test_operations_page_shows_service_status_summary_card(monkeypatch):
    db_path = Path(f"data/.test_operations_service_status_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))

    from news_summary import web as web_module
    from news_summary.web import create_app

    monkeypatch.setattr(
        web_module,
        "_operations_service_status_report",
        lambda store, config_path, auto_status, reports: {
            "status_level": "warning",
            "status_label": "주의",
            "message": "오늘 수집 원문 중 초안 미변환 4건이 남아 있습니다. 외 1건",
            "issues": [
                {
                    "level": "warning",
                    "component": "draft_conversion_coverage",
                    "message": "오늘 수집 원문 중 초안 미변환 4건이 남아 있습니다.",
                    "anchor_id": "ops-draft-conversion",
                    "card_label": "오늘 초안 변환 커버리지",
                },
                {
                    "level": "warning",
                    "component": "gemini_queue",
                    "message": "Gemini 자동 재처리 대기 원문 2건",
                    "anchor_id": "ops-gemini-retry-queue",
                    "card_label": "Gemini 재처리 대기열",
                },
            ],
        },
    )

    app = create_app()
    app.testing = True
    html = app.test_client().get("/operations").data.decode("utf-8")

    assert "서비스 상태 요약" in html
    assert "오늘 수집 원문 중 초안 미변환 4건이 남아 있습니다. 외 1건" in html
    assert 'href="#ops-draft-conversion"' in html
    assert 'href="#ops-gemini-retry-queue"' in html
    assert "오늘 초안 변환 커버리지" in html
    assert "Gemini 재처리 대기열" in html


def test_operations_page_links_fallback_and_url_discovery_items(monkeypatch):
    db_path = Path(f"data/.test_operations_url_cards_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))

    from news_summary import web as web_module
    from news_summary.web import create_app

    monkeypatch.setattr(
        web_module,
        "_fallback_url_report",
        lambda config_path: {
            "prepared_count": 1,
            "total_count": 29,
            "sources": [
                {
                    "source_id": "gangjin",
                    "name": "강진군청 보도자료",
                    "count": 2,
                    "first_url": "https://example.com/gangjin/fallback",
                }
            ],
        },
    )
    monkeypatch.setattr(
        web_module,
        "_url_discovery_report",
        lambda store: {
            "updated_at": "2026-07-10T09:00:00+09:00",
            "discoveries": [
                {
                    "source_id": "gangjin",
                    "source_name": "강진군청 보도자료",
                    "url_count": 3,
                    "preview_urls": [
                        "https://example.com/gangjin/press",
                        "https://example.com/gangjin/news",
                    ],
                    "urls": [
                        "https://example.com/gangjin/press",
                        "https://example.com/gangjin/news",
                        "https://example.com/gangjin/board",
                    ],
                }
            ],
        },
    )

    app = create_app()
    app.testing = True
    html = app.test_client().get("/operations").data.decode("utf-8")

    assert "대체 URL 준비" in html
    assert 'href="/sources/gangjin"' in html
    assert 'href="https://example.com/gangjin/fallback"' in html
    assert "URL 후보 탐색" in html
    assert 'href="/ops-logs?tab=collector&amp;q=source_id%3Dgangjin"' in html
    assert 'href="https://example.com/gangjin/press"' in html
    assert 'href="https://example.com/gangjin/news"' in html
    assert "후보 3개" in html


def test_operations_page_creates_and_restores_backup(monkeypatch):
    db_path = Path(f"data/.test_operations_backup_{uuid4().hex}.sqlite").resolve()
    backup_dir = Path(f"data/tmp/test_operations_backups_{uuid4().hex}").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("NEWS_SUMMARY_ADMIN_PASSWORD", "secret1234")
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_DISABLED", "1")

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    locked_response = client.post("/operations/backup", follow_redirects=True)
    locked_html = locked_response.data.decode("utf-8")

    assert "운영 변경 기능은 관리자 비밀번호 확인 후 사용할 수 있습니다." in locked_html
    assert not list(backup_dir.glob("*.zip"))

    unlock_response = client.post(
        "/operations/write-access/unlock",
        data={"current_password": "secret1234"},
        follow_redirects=True,
    )
    assert "운영 변경 기능 잠금을 해제했습니다." in unlock_response.data.decode("utf-8")

    backup_response = client.post("/operations/backup", follow_redirects=True)
    backup_html = backup_response.data.decode("utf-8")
    backups = sorted(backup_dir.glob("*.zip"))

    assert backup_response.status_code == 200
    assert "백업을 생성했습니다" in backup_html
    assert len(backups) == 1

    backup_name = backups[0].name
    operations_html = client.get("/operations").data.decode("utf-8")
    assert f"검증 대상 {backup_name}" in operations_html
    assert "SQLite 무결성" in operations_html
    assert "최신 백업 다운로드" in operations_html
    assert "최신 백업 복구 대상 확인" in operations_html
    assert f'value="{backup_name}"' in operations_html
    assert "최근 운영 변경 이력" in operations_html
    assert "백업 생성" in operations_html
    assert backup_name in operations_html

    download_response = client.get(f"/operations/backups/{backup_name}")
    assert download_response.status_code == 200
    assert download_response.headers["Cache-Control"] == "no-store, max-age=0"
    assert download_response.headers["Pragma"] == "no-cache"
    assert download_response.headers["Expires"] == "0"
    assert f"filename={backup_name}" in download_response.headers["Content-Disposition"]

    preview_response = client.post(
        "/operations/restore",
        data={"backup_name": backup_name, "action": "preview"},
        follow_redirects=True,
    )
    preview_html = preview_response.data.decode("utf-8")

    assert "복구 대상:" in preview_html
    assert "data/.test_operations_backup_" in preview_html
    assert "주의: 설정 파일도 덮어씁니다" in preview_html

    blocked_response = client.post(
        "/operations/restore",
        data={"backup_name": backup_name, "action": "restore"},
        follow_redirects=True,
    )
    blocked_html = blocked_response.data.decode("utf-8")

    assert "확인 체크박스" in blocked_html

    config_blocked_response = client.post(
        "/operations/restore",
        data={"backup_name": backup_name, "action": "restore", "confirm_restore": "yes"},
        follow_redirects=True,
    )
    config_blocked_html = config_blocked_response.data.decode("utf-8")

    assert "설정 파일 덮어쓰기 확인 체크박스" in config_blocked_html

    restore_response = client.post(
        "/operations/restore",
        data={
            "backup_name": backup_name,
            "action": "restore",
            "confirm_restore": "yes",
            "confirm_config_restore": "yes",
        },
        follow_redirects=True,
    )
    restore_html = restore_response.data.decode("utf-8")

    assert restore_response.status_code == 200
    assert "백업을 복구했습니다" in restore_html
    assert len(list(backup_dir.glob("*.zip"))) >= 2
    event_types = [row["event_type"] for row in Store(db_path).operation_events(limit=5)]
    assert "backup_restored" in event_types


def test_public_operations_write_access_can_be_locked_again(monkeypatch):
    db_path = Path(f"data/.test_operations_write_lock_{uuid4().hex}.sqlite").resolve()
    backup_dir = Path(f"data/tmp/test_operations_write_lock_{uuid4().hex}").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("NEWS_SUMMARY_ADMIN_PASSWORD", "secret1234")
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_DISABLED", "1")

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    operations_html = client.get("/operations").data.decode("utf-8")
    assert "운영 변경 잠금" in operations_html
    assert "운영 변경 잠금 해제" in operations_html
    assert "disabled>지금 백업 생성</button>" in operations_html

    wrong_unlock = client.post(
        "/operations/write-access/unlock",
        data={"current_password": "wrong"},
        follow_redirects=True,
    )
    assert "관리자 비밀번호가 올바르지 않습니다." in wrong_unlock.data.decode("utf-8")

    right_unlock = client.post(
        "/operations/write-access/unlock",
        data={"current_password": "secret1234"},
        follow_redirects=True,
    )
    assert "운영 변경 기능 잠금을 해제했습니다." in right_unlock.data.decode("utf-8")
    assert "해제됨" in right_unlock.data.decode("utf-8")

    backup_response = client.post("/operations/backup", follow_redirects=True)
    backup_html = backup_response.data.decode("utf-8")
    assert "백업을 생성했습니다" in backup_html
    backup_name = sorted(backup_dir.glob("*.zip"))[0].name

    lock_response = client.post("/operations/write-access/lock", follow_redirects=True)
    assert "운영 변경 기능을 다시 잠갔습니다." in lock_response.data.decode("utf-8")

    download_response = client.get(f"/operations/backups/{backup_name}", follow_redirects=True)
    assert "운영 변경 기능은 관리자 비밀번호 확인 후 사용할 수 있습니다." in download_response.data.decode("utf-8")


def test_operations_write_access_expires_automatically(monkeypatch):
    db_path = Path(f"data/.test_operations_write_expiry_{uuid4().hex}.sqlite").resolve()
    backup_dir = Path(f"data/tmp/test_operations_write_expiry_{uuid4().hex}").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("NEWS_SUMMARY_ADMIN_PASSWORD", "secret1234")
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_DISABLED", "1")
    monkeypatch.setenv("NEWS_SUMMARY_OPERATIONS_WRITE_UNLOCK_MINUTES", "5")

    from news_summary.web import OPERATIONS_WRITE_UNLOCKED_AT_KEY, create_app

    app = create_app()
    app.testing = True
    client = app.test_client()

    locked_html = client.get("/operations").data.decode("utf-8")
    assert "5분 동안 사용할 수 있습니다." in locked_html

    unlocked = client.post(
        "/operations/write-access/unlock",
        data={"current_password": "secret1234"},
        follow_redirects=True,
    )
    assert "해제됨" in unlocked.data.decode("utf-8")
    assert "까지 유지됩니다." in unlocked.data.decode("utf-8")

    first_backup = client.post("/operations/backup", follow_redirects=True)
    assert "백업을 생성했습니다" in first_backup.data.decode("utf-8")
    assert len(list(backup_dir.glob("*.zip"))) == 1

    with client.session_transaction() as session_data:
        session_data[OPERATIONS_WRITE_UNLOCKED_AT_KEY] = (
            datetime.now(timezone.utc) - timedelta(minutes=10)
        ).isoformat()

    expired_backup = client.post("/operations/backup", follow_redirects=True)
    expired_html = expired_backup.data.decode("utf-8")

    assert "운영 변경 기능은 관리자 비밀번호 확인 후 사용할 수 있습니다." in expired_html
    assert "잠김" in expired_html
    assert len(list(backup_dir.glob("*.zip"))) == 1


def test_backup_verify_report_refreshes_stale_metadata(monkeypatch):
    db_path = Path(f"data/.test_backup_verify_stale_{uuid4().hex}.sqlite").resolve()
    backup_dir = Path(f"data/tmp/test_backup_verify_stale_{uuid4().hex}").resolve()
    store = Store(db_path)
    store.init_db()

    from news_summary.backup import create_backup
    from news_summary.scheduler import AUTO_BACKUP_VERIFY_STATUS_KEY
    from news_summary.web import _backup_verify_report

    backup_path = create_backup(Path.cwd(), db_path, backup_dir)
    store.set_app_metadata(
        AUTO_BACKUP_VERIFY_STATUS_KEY,
        json.dumps(
            {
                "ok": False,
                "status_label": "백업 없음",
                "message": "검증할 백업 파일이 없습니다.",
                "backup_name": "old-backup.zip",
                "updated_at": "2026-07-08T00:00:00+00:00",
            },
            ensure_ascii=False,
        ),
    )

    report = _backup_verify_report(store, backup_dir)

    assert report["ok"] is True
    assert report["status_label"] == "검증 정상"
    assert report["backup_name"] == backup_path.name
    assert "SQLite 무결성" in str(report["message"])


def test_backup_health_payload_warns_when_latest_backup_is_stale(monkeypatch):
    db_path = Path(f"data/.test_backup_health_stale_{uuid4().hex}.sqlite").resolve()
    backup_dir = Path(f"data/tmp/test_backup_health_stale_{uuid4().hex}").resolve()
    store = Store(db_path)
    store.init_db()

    from news_summary.backup import create_backup

    backup_path = create_backup(Path.cwd(), db_path, backup_dir)
    now = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    stale_timestamp = (now - timedelta(hours=31, minutes=5)).timestamp()
    backup_path.touch()

    os.utime(backup_path, (stale_timestamp, stale_timestamp))
    monkeypatch.setenv("NEWS_SUMMARY_AUTO_BACKUP_MAX_AGE_HOURS", "24")

    payload = _backup_health_payload(
        backup_dir,
        {
            "ok": True,
            "status_label": "검증 정상",
            "message": "백업 검증 정상",
            "backup_name": backup_path.name,
        },
        now=now,
    )

    assert payload["backup_status"] == "warning"
    assert payload["backup_label"] == "백업 지연"
    assert payload["backup_age_hours"] == 31
    assert payload["backup_message"] == "최근 DB 백업이 31시간 전입니다. 자동 백업 상태를 확인하세요."


def test_backup_health_payload_warns_when_backup_contains_sensitive_config_keys():
    backup_dir = Path(f"data/tmp/test_backup_health_sensitive_{uuid4().hex}").resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / "news-express-sensitive.zip"
    backup_path.write_text("placeholder", encoding="utf-8")

    payload = _backup_health_payload(
        backup_dir,
        {
            "ok": True,
            "status_label": "검증 정상",
            "message": "압축과 SQLite 무결성을 확인했습니다.",
            "sensitive_config_keys": ["GEMINI_API_KEY"],
        },
        now=datetime.now(timezone.utc),
    )

    assert payload["backup_status"] == "warning"
    assert payload["backup_label"] == "보안 주의"
    assert payload["backup_sensitive_config_keys"] == ["GEMINI_API_KEY"]


def test_backup_health_payload_reports_env_backup_policy(monkeypatch):
    backup_dir = Path(f"data/tmp/test_backup_health_policy_{uuid4().hex}").resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("NEWS_SUMMARY_BACKUP_INCLUDE_ENV", "0")
    payload = _backup_health_payload(
        backup_dir,
        {
            "ok": False,
            "status_label": "백업 없음",
            "message": "검증할 백업 파일이 없습니다.",
            "sensitive_config_keys": [],
        },
        now=datetime.now(timezone.utc),
    )

    assert payload["backup_include_env"] is False
    assert payload["backup_env_policy_label"] == ".env 제외"
    assert ".env 설정 파일을 제외" in payload["backup_env_policy_message"]

    monkeypatch.setenv("NEWS_SUMMARY_BACKUP_INCLUDE_ENV", "1")
    payload = _backup_health_payload(
        backup_dir,
        {
            "ok": False,
            "status_label": "백업 없음",
            "message": "검증할 백업 파일이 없습니다.",
            "sensitive_config_keys": [],
        },
        now=datetime.now(timezone.utc),
    )

    assert payload["backup_include_env"] is True
    assert payload["backup_env_policy_label"] == ".env 포함"


def test_db_health_report_warns_when_backup_dir_is_temporary():
    db_path = Path(f"data/.test_db_health_temp_backup_{uuid4().hex}.sqlite").resolve()
    store = Store(db_path)
    store.init_db()

    report = _db_health_report(store, Path("/tmp/news-express/backups"))

    assert report["status_level"] == "warning"
    assert report["status_label"] == "백업 필요"
    assert "임시 경로" in str(report["note"])


def test_operations_page_warns_for_temporary_backup_storage(monkeypatch):
    db_path = Path(f"data/.test_operations_temp_backup_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_BACKUP_DIR", "/tmp/news-express/backups")

    from news_summary.web import create_app

    app = create_app()
    app.testing = True
    html = app.test_client().get("/operations").data.decode("utf-8")

    assert "DB 백업" in html
    assert "백업 필요" in html
    assert "임시 경로" in html


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
    monkeypatch.setenv("NEWS_SUMMARY_AUTO_RUNNING_STALE_MINUTES", "100000")

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


def test_recrawl_status_ignores_stale_running_snapshot(monkeypatch):
    db_path = Path(f"data/.test_recrawl_stale_running_status_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_AUTO_RUNNING_STALE_MINUTES", "60")

    from news_summary.scheduler import AUTO_COLLECT_STATUS_KEY
    from news_summary.web import create_app

    store = Store(db_path)
    store.init_db()
    old_status_updated_at = (datetime.now(LOCAL_TZ) - timedelta(hours=3)).isoformat()
    store.set_app_metadata(
        AUTO_COLLECT_STATUS_KEY,
        json.dumps(
            {
                "enabled": True,
                "running": True,
                "active_label": "자동 수집",
                "progress_current": 7,
                "progress_total": 29,
                "progress_message": "7/29 수집 중",
                "progress_source_name": "전남광주통합특별시청 보도자료",
                "progress_phase": "collecting",
                "last_error": None,
                "last_started_at": old_status_updated_at,
                "last_finished_at": None,
                "last_auto_finished_at": "2026-07-03T04:15:43+00:00",
                "next_run_at": None,
                "run_count": 0,
                "status_updated_at": old_status_updated_at,
            },
            ensure_ascii=False,
        ),
    )

    class IdleCollector:
        def snapshot(self):
            return AutoCollectorStatus(
                enabled=True,
                running=False,
                progress_total=29,
                progress_message="다음 정각 자동 수집 대기 중",
                next_run_at="2026-07-06T05:00:00+00:00",
            )

    app = create_app()
    app.config["AUTO_COLLECTOR"] = IdleCollector()
    app.testing = True
    client = app.test_client()

    status = client.get("/recrawl/status").get_json()
    html = client.get("/operations").data.decode("utf-8")
    health = client.get("/healthz").get_json()

    assert status["running"] is False
    assert status["progress_current"] == 0
    assert status["progress_message"] == "이전 실행 상태 만료, 다음 정각 자동 수집 대기 중"
    assert status["stale_running_snapshot"] is True
    assert "오래된 자동 수집 실행 표시 자동 보정" in html
    assert health["auto_collector"] == "enabled"


def test_operations_uses_persisted_next_auto_run_time(monkeypatch):
    db_path = Path(f"data/.test_operations_next_run_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))

    from news_summary.scheduler import AUTO_COLLECT_STATUS_KEY
    from news_summary.web import create_app

    store = Store(db_path)
    store.init_db()
    store.set_app_metadata(
        AUTO_COLLECT_STATUS_KEY,
        (
            '{"enabled": true, "running": false, "active_label": "", '
            '"progress_current": 0, "progress_total": 29, '
            '"progress_message": "다음 정각 자동 수집 대기 중", "progress_source_name": "", '
            '"progress_phase": "idle", "last_error": null, '
            '"last_started_at": null, "last_finished_at": null, '
            '"last_auto_finished_at": "2026-07-06T04:15:43+00:00", '
            '"next_run_at": "2026-07-06T05:00:00+00:00", '
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

    html = client.get("/operations").data.decode("utf-8")
    health = client.get("/healthz").get_json()

    assert "매시간 정각 실행" in html
    assert "다음 실행 2026.07.06 14:00" in html
    assert "주기 미상" not in html
    assert "다음 실행 일시 미상" not in html
    assert "마지막 자동 수집 2026.07.06 13:15" in html
    assert health["next_run_at"] == "2026-07-06T05:00:00+00:00"


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
    assert "승인 후 다음" not in detail_html

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
    assert "수정 이력" not in html
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
    assert "최초 초안 복원" in detail_html
    assert "제목 간결화" in detail_html
    assert "본문 간결화" in detail_html
    assert "90% 수준으로 분량을 줄여 간결하게 작성해줘." in detail_html
    assert "본문 보강" in detail_html
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
    assert "Gemini 처리 재개 대기:" in detail_html
    assert "수동 다듬기를 기다립니다." in detail_html
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
    assert "Gemini 처리 재개 대기:" in html
    assert "수동 다듬기를 기다립니다." in html
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
