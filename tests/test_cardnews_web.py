import io
from pathlib import Path
from uuid import uuid4

from PIL import Image

from news_summary.cardnews import CardCopy
from news_summary.models import ArticleDraft, PressRelease, PressReleaseAsset
from news_summary.storage import Store


def photo_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2000, 1500), (110, 140, 170)).save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def prepare(monkeypatch, tmp_path: Path):
    db_path = Path(f"data/.test_cardnews_web_{uuid4().hex}.sqlite").resolve()
    monkeypatch.setenv("NEWS_SUMMARY_DB", str(db_path))
    monkeypatch.setenv("NEWS_SUMMARY_CARDNEWS_DIR", str(tmp_path / "cardnews"))
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    store = Store(db_path)
    store.init_db()
    release_id = store.add_press_release(
        PressRelease(
            source_id="damyang-county",
            source_name="담양군청 보도자료",
            region="전남 담양",
            title="담양군, 폭염 취약 현장 점검",
            url=f"https://example.com/{uuid4().hex}",
            content="담양군은 폭염 취약 현장을 점검했다고 밝혔습니다.",
            published_at="2026-08-09",
            assets=[
                PressReleaseAsset(
                    url="https://example.com/photo.jpg",
                    title="첨부 사진",
                    filename="",
                    content_type="image/jpeg",
                    asset_type="image",
                    is_image=True,
                )
            ],
        )
    )
    draft_id = store.add_article_draft(
        ArticleDraft(
            press_release_id=release_id,
            title="담양군, 폭염 취약 현장 점검",
            body="담양군은 야외 근로자 안전을 위해 현장을 점검했습니다.",
            review_note="",
            model="gemini-3.5-flash",
        )
    )
    return store, draft_id


def stub_generation(monkeypatch):
    monkeypatch.setattr("news_summary.web._download_card_photo", lambda asset: photo_bytes())
    monkeypatch.setattr(
        "news_summary.cardnews_service.build_card_copy",
        lambda request, api_key: CardCopy(
            cover="담양 무더위쉼터 전면 점검",
            cards=["야외 근로자 안전 점검에 나섰습니다.", "냉방기 가동 상태를 확인했습니다."],
            source_label=request.source_label,
            date_label=request.date_label,
            tags=["담양"],
        ),
    )


def make_client(app_testing=True):
    from news_summary.web import create_app

    app = create_app()
    app.testing = app_testing
    return app.test_client()


def test_public_card_news_page_opens_without_login(monkeypatch, tmp_path):
    prepare(monkeypatch, tmp_path)
    client = make_client()

    response = client.get("/card-news")

    assert response.status_code == 200
    assert "오늘의 카드뉴스" in response.data.decode("utf-8")


def test_build_publish_and_public_listing(monkeypatch, tmp_path):
    store, draft_id = prepare(monkeypatch, tmp_path)
    stub_generation(monkeypatch)
    client = make_client()

    built = client.post(
        "/card-news/build",
        data={"draft_id": str(draft_id), "publish_date": "2026-08-09"},
        follow_redirects=True,
    )
    assert built.status_code == 200

    row = store.card_news_set_by_draft(draft_id)
    assert row is not None and row["status"] == "draft"

    # 발행 전에는 주민 화면에 안 뜬다.
    before = client.get("/card-news?date=2026-08-09").data.decode("utf-8")
    assert "담양 무더위쉼터" not in before

    client.post(f"/card-news/{row['id']}/publish", data={"publish": "1"}, follow_redirects=True)

    after = client.get("/card-news?date=2026-08-09").data.decode("utf-8")
    assert "담양 무더위쉼터" in after


def test_unpublished_card_image_is_hidden_from_public(monkeypatch, tmp_path):
    store, draft_id = prepare(monkeypatch, tmp_path)
    stub_generation(monkeypatch)
    client = make_client()
    client.post(
        "/card-news/build",
        data={"draft_id": str(draft_id), "publish_date": "2026-08-09"},
        follow_redirects=True,
    )
    set_id = store.card_news_set_by_draft(draft_id)["id"]

    assert client.get(f"/card-news/{set_id}/1.png").status_code == 404

    store.set_card_news_status(set_id, "published")
    assert client.get(f"/card-news/{set_id}/1.png").status_code == 200


def test_published_card_image_is_served_as_png(monkeypatch, tmp_path):
    store, draft_id = prepare(monkeypatch, tmp_path)
    stub_generation(monkeypatch)
    client = make_client()
    client.post(
        "/card-news/build",
        data={"draft_id": str(draft_id), "publish_date": "2026-08-09"},
        follow_redirects=True,
    )
    set_id = store.card_news_set_by_draft(draft_id)["id"]
    store.set_card_news_status(set_id, "published")

    response = client.get(f"/card-news/{set_id}/1.png")

    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("image/png")
    assert Image.open(io.BytesIO(response.data)).size == (1080, 1350)


def test_open_graph_image_points_at_cover(monkeypatch, tmp_path):
    """카톡에 링크를 붙이면 표지 카드가 미리보기로 떠야 확산된다."""
    store, draft_id = prepare(monkeypatch, tmp_path)
    stub_generation(monkeypatch)
    client = make_client()
    client.post(
        "/card-news/build",
        data={"draft_id": str(draft_id), "publish_date": "2026-08-09"},
        follow_redirects=True,
    )
    set_id = store.card_news_set_by_draft(draft_id)["id"]
    store.set_card_news_status(set_id, "published")

    html = client.get("/card-news?date=2026-08-09").data.decode("utf-8")

    assert 'property="og:image"' in html
    assert f"/card-news/{set_id}/1.png" in html


def test_deleting_set_removes_images(monkeypatch, tmp_path):
    store, draft_id = prepare(monkeypatch, tmp_path)
    stub_generation(monkeypatch)
    client = make_client()
    client.post(
        "/card-news/build",
        data={"draft_id": str(draft_id), "publish_date": "2026-08-09"},
        follow_redirects=True,
    )
    set_id = store.card_news_set_by_draft(draft_id)["id"]

    client.post(f"/card-news/{set_id}/delete", follow_redirects=True)

    assert store.card_news_set(set_id) is None
    assert not (tmp_path / "cardnews" / "2026-08-09" / str(set_id)).exists()


def test_manage_page_lists_candidates(monkeypatch, tmp_path):
    prepare(monkeypatch, tmp_path)
    client = make_client()

    html = client.get("/card-news/manage?date=2026-08-09").data.decode("utf-8")

    assert "카드뉴스 관리" in html
    assert "담양군, 폭염 취약 현장 점검" in html


def test_build_failure_is_reported_not_crashed(monkeypatch, tmp_path):
    _, draft_id = prepare(monkeypatch, tmp_path)
    monkeypatch.setattr("news_summary.web._download_card_photo", lambda asset: photo_bytes())
    monkeypatch.setattr(
        "news_summary.cardnews_service.build_card_copy",
        lambda request, api_key: (_ for _ in ()).throw(RuntimeError("모델 응답 없음")),
    )
    client = make_client()

    response = client.post(
        "/card-news/build",
        data={"draft_id": str(draft_id), "publish_date": "2026-08-09"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "카드뉴스 생성 실패" in response.data.decode("utf-8")


def build_one(client, draft_id):
    client.post(
        "/card-news/build",
        data={"draft_id": str(draft_id), "publish_date": "2026-08-09"},
        follow_redirects=True,
    )


def test_editing_copy_saves_and_redraws_without_calling_ai(monkeypatch, tmp_path):
    """AI 재생성 말고 한 글자만 고치고 싶을 때가 대부분이다(Plan 성공기준 3)."""
    store, draft_id = prepare(monkeypatch, tmp_path)
    stub_generation(monkeypatch)
    client = make_client()
    build_one(client, draft_id)
    set_id = store.card_news_set_by_draft(draft_id)["id"]

    calls = {"ai": 0}
    monkeypatch.setattr(
        "news_summary.cardnews_service.build_card_copy",
        lambda request, api_key: calls.__setitem__("ai", calls["ai"] + 1),
    )

    response = client.post(
        f"/card-news/{set_id}/copy",
        data={"cover": "사람이 고친 표지", "card": ["첫 카드 문구입니다.", "둘째 카드 문구입니다."]},
        follow_redirects=True,
    )

    assert response.status_code == 200
    row = store.card_news_set(set_id)
    assert row["cover"] == "사람이 고친 표지"
    assert calls["ai"] == 0, "문안 손질에 AI를 다시 부르면 고친 글자가 덮인다"

    from news_summary.cardnews_service import load_set_images

    assert len(load_set_images(tmp_path / "cardnews", "2026-08-09", set_id)) == 3


def test_editing_copy_rejects_empty_input(monkeypatch, tmp_path):
    store, draft_id = prepare(monkeypatch, tmp_path)
    stub_generation(monkeypatch)
    client = make_client()
    build_one(client, draft_id)
    set_id = store.card_news_set_by_draft(draft_id)["id"]

    client.post(f"/card-news/{set_id}/copy", data={"cover": "   ", "card": [""]}, follow_redirects=True)

    assert store.card_news_set(set_id)["cover"] == "담양 무더위쉼터 전면 점검", "빈 입력으로 덮이면 안 된다"


def test_public_page_offers_share_link_and_download(monkeypatch, tmp_path):
    """카톡·밴드로 옮기는 것이 실제 확산 경로다."""
    store, draft_id = prepare(monkeypatch, tmp_path)
    stub_generation(monkeypatch)
    client = make_client()
    build_one(client, draft_id)
    set_id = store.card_news_set_by_draft(draft_id)["id"]
    store.set_card_news_status(set_id, "published")

    html = client.get("/card-news?date=2026-08-09").data.decode("utf-8")

    assert "cardnews-copy-link" in html
    assert "download=" in html


def test_manage_routes_require_login_when_auth_enabled(monkeypatch, tmp_path):
    """공개는 누구나, 변경은 관리자만 — 인증이 켜진 상태에서 확인한다."""
    prepare(monkeypatch, tmp_path)
    # conftest가 인증을 꺼 두므로 이 테스트에서만 되살린다.
    # .env가 AUTH_DISABLED를 정의하면 create_app의 load_environment가 되살리므로
    # 삭제가 아니라 빈 값으로 눌러야 한다(conftest가 다른 값에 쓰는 방식과 같다).
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_DISABLED", "")
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_REQUIRED", "1")
    monkeypatch.setenv("NEWS_SUMMARY_ADMIN_PASSWORD", "pw-for-test")
    client = make_client(app_testing=False)

    assert client.get("/card-news").status_code == 200, "열람은 로그인 없이 열려야 한다"
    for path in ("/card-news/manage",):
        assert client.get(path).status_code == 302, f"{path}는 로그인이 필요하다"
    for path in ("/card-news/build", "/card-news/1/publish", "/card-news/1/copy", "/card-news/1/delete"):
        assert client.post(path).status_code in {302, 400}, f"{path}는 로그인이 필요하다"


def test_nav_links_to_card_news(monkeypatch, tmp_path):
    prepare(monkeypatch, tmp_path)
    client = make_client()

    html = client.get("/").data.decode("utf-8")

    assert 'href="/card-news"' in html
