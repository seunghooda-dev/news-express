import io
import json
from pathlib import Path
from uuid import uuid4

from PIL import Image

from news_summary.cardnews import CardCopy, CardSlide
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
            cards=[
                CardSlide(heading="야외 근로자 점검", body="야외 근로자 안전 점검에 나섰습니다."),
                CardSlide(heading="냉방기 상태 확인", body="냉방기 가동 상태를 확인했습니다."),
            ],
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


def test_public_page_offers_manage_link_only_to_logged_in_operator(monkeypatch, tmp_path):
    """메뉴의 '카드뉴스'는 이 공개 화면으로 온다.

    여기서 관리 화면으로 갈 길이 없어 로그인한 사람이 만들 곳을 못 찾았다
    (2026-08-10 사용자: "카드관리에 들어가면 카드만들기가 안보여 로그인해도").
    주민에게는 보이면 안 된다 — 눌러도 로그인으로 튕길 뿐이다.
    """
    prepare(monkeypatch, tmp_path)
    # 로그인 보호가 꺼져 있으면 익명 쪽 단언이 헛돈다 — 명시적으로 켠다.
    monkeypatch.setenv("NEWS_SUMMARY_AUTH_DISABLED", "")
    monkeypatch.setenv("NEWS_SUMMARY_ADMIN_PASSWORD", "test-password")
    client = make_client()

    anonymous = client.get("/card-news").get_data(as_text=True)
    assert "/card-news/manage" not in anonymous, "주민 화면에 관리 링크가 노출됐다"

    with client.session_transaction() as session:
        session["admin_authenticated"] = True
    logged_in = client.get("/card-news").get_data(as_text=True)

    assert "/card-news/manage" in logged_in, "로그인해도 만들 곳으로 갈 링크가 없다"
    assert "카드 만들기" in logged_in


def test_candidate_score_sees_photos_through_the_real_query(monkeypatch, tmp_path):
    """후보 점수는 랭킹 SQL이 돌려준 행만 본다.

    2026-08-10 적발: 그 SQL에 asset_count가 없어 card_picks의 "사진 있음 +2"가
    프로덕션에서 한 번도 붙지 않았다. test_card_picks는 행을 직접 만들어 넣어
    통과하고 있었으므로 드리프트를 못 잡았다 — 실제 질의를 태워서 확인한다.
    """
    from news_summary.card_picks import rank_candidates
    from news_summary.web import _draft_rows_for_listing

    store, _ = prepare(monkeypatch, tmp_path)

    rows = _draft_rows_for_listing(store, limit=10)

    assert rows, "초안이 조회되지 않았다"
    assert "asset_count" in rows[0].keys(), "랭킹 질의에 asset_count가 없다 — 사진 점수가 죽는다"
    assert rows[0]["asset_count"] == 1

    picks = rank_candidates(rows)
    assert any("사진 있음" in reason for reason in picks[0].reasons), "점수에 사진이 반영되지 않았다"


def test_redraw_applies_current_layout_without_calling_ai(monkeypatch, tmp_path):
    """배치를 바꾸면 이미 만든 세트는 옛 모습 그대로 남는다.

    2026-08-10에 한 장 카드로 바꿨는데, 그 전에 만들어 발행한 세트는 4장인 채로
    주민에게 보이고 있었다. 문안은 건드리지 않고 그림만 지금 배치로 다시 그린다 —
    AI를 부르면 손질한 글자가 덮이고 한도도 깎인다.
    """
    from news_summary.cardnews_service import load_set_images, set_directory

    store, draft_id = prepare(monkeypatch, tmp_path)
    stub_generation(monkeypatch)
    client = make_client()
    client.post(
        "/card-news/build",
        data={"draft_id": str(draft_id), "publish_date": "2026-08-09"},
        follow_redirects=True,
    )
    row = store.card_news_set_by_draft(draft_id)
    set_id = int(row["id"])
    original_cards = json.loads(row["cards"])

    # 옛 형식(여러 장)으로 남아 있는 상태를 만든다.
    directory = set_directory(tmp_path / "cardnews", "2026-08-09", set_id)
    for extra in ("2.png", "3.png", "4.png"):
        (directory / extra).write_bytes((directory / "1.png").read_bytes())
    assert len(load_set_images(tmp_path / "cardnews", "2026-08-09", set_id)) == 4

    calls = {"ai": 0}
    monkeypatch.setattr(
        "news_summary.cardnews_service.build_card_copy",
        lambda *args, **kwargs: calls.__setitem__("ai", calls["ai"] + 1),
    )

    response = client.post(f"/card-news/{set_id}/redraw", follow_redirects=True)

    assert response.status_code == 200
    assert len(load_set_images(tmp_path / "cardnews", "2026-08-09", set_id)) == 1, "옛 이미지가 남아 있다"
    assert json.loads(store.card_news_set(set_id)["cards"]) == original_cards, "문안이 바뀌었다"
    assert calls["ai"] == 0, "다시 그리기가 AI를 불렀다"
    # DB 장수와 실제 파일이 어긋나면, 그 값을 믿는 화면이 생기는 순간 틀린 배지가 붙는다.
    assert store.card_news_set(set_id)["image_count"] == 1, "DB 장수가 실제와 어긋난다"


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
        # 필드명에 인덱스를 붙여 소제목·본문이 위치가 아니라 번호로 짝지어진다.
        data={
            "cover": "사람이 고친 표지",
            "heading-0": "사람이 고친 소제목",
            "body-0": "사람이 손질한 첫째 카드 본문입니다. 규격에 맞게 충분히 씁니다.",
            "heading-1": "둘째 카드 소제목",
            "body-1": "사람이 손질한 둘째 카드 본문입니다. 규격에 맞게 충분히 씁니다.",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    row = store.card_news_set(set_id)
    assert row["cover"] == "사람이 고친 표지"
    # 소제목과 본문이 짝을 유지한 채 저장돼야 다시 그린 카드가 어긋나지 않는다.
    assert json.loads(row["cards"]) == [
        {"heading": "사람이 고친 소제목", "body": "사람이 손질한 첫째 카드 본문입니다. 규격에 맞게 충분히 씁니다."},
        {"heading": "둘째 카드 소제목", "body": "사람이 손질한 둘째 카드 본문입니다. 규격에 맞게 충분히 씁니다."},
    ]
    assert calls["ai"] == 0, "문안 손질에 AI를 다시 부르면 고친 글자가 덮인다"

    from news_summary.cardnews_service import load_set_images

    assert len(load_set_images(tmp_path / "cardnews", "2026-08-09", set_id)) == 1


def test_editing_copy_rejects_empty_input(monkeypatch, tmp_path):
    store, draft_id = prepare(monkeypatch, tmp_path)
    stub_generation(monkeypatch)
    client = make_client()
    build_one(client, draft_id)
    set_id = store.card_news_set_by_draft(draft_id)["id"]

    client.post(
        f"/card-news/{set_id}/copy",
        data={"cover": "   ", "heading-0": "", "body-0": ""},
        follow_redirects=True,
    )

    assert store.card_news_set(set_id)["cover"] == "담양 무더위쉼터 전면 점검", "빈 입력으로 덮이면 안 된다"


def test_editing_copy_enforces_the_same_limits_as_ai(monkeypatch, tmp_path):
    """AI 출력에만 규격을 걸고 사람 입력은 무검증이면 글자가 카드 밖으로 샌다.

    2026-08-09 검토에서 적발 — 폼 maxlength 안쪽 입력도 카드 높이를 넘길 수 있었다.
    """
    from news_summary.cardcopy import MAX_CARD_CHARS, MAX_HEADING_CHARS

    store, draft_id = prepare(monkeypatch, tmp_path)
    stub_generation(monkeypatch)
    client = make_client()
    build_one(client, draft_id)
    set_id = store.card_news_set_by_draft(draft_id)["id"]
    original = store.card_news_set(set_id)["cards"]

    response = client.post(
        f"/card-news/{set_id}/copy",
        data={
            "cover": "규격 확인 표지",
            "heading-0": "가" * (MAX_HEADING_CHARS + 1),
            "body-0": "나" * 40,
            "heading-1": "정상 소제목",
            "body-1": "다" * 40,
        },
        follow_redirects=True,
    )

    assert "소제목은" in response.data.decode("utf-8"), "왜 거절됐는지 알려 줘야 한다"
    assert store.card_news_set(set_id)["cards"] == original, "규격 위반이 저장되면 안 된다"

    too_long_body = client.post(
        f"/card-news/{set_id}/copy",
        data={"cover": "규격 확인 표지", "heading-0": "정상 소제목", "body-0": "라" * (MAX_CARD_CHARS + 1)},
        follow_redirects=True,
    )
    assert "본문은" in too_long_body.data.decode("utf-8")
    assert store.card_news_set(set_id)["cards"] == original


def test_editing_copy_pairs_by_index_even_with_a_gap_in_the_middle():
    """가운데 카드가 통째로 빠져도 짝이 안 밀린다 — 관측 계약을 고정한다.

    **주의: 이 테스트는 위치 zip 구현도 통과한다**(2026-08-11 실증). 비대칭 입력
    (본문만·소제목만)은 아래 테스트가 보이듯 검증에서 거절되므로, 받아들여지는
    입력에서는 두 구현이 같은 결과를 낸다. 즉 **짝짓기를 안전하게 만드는 것은
    인덱스 읽기가 아니라 거절이다.** 인덱스 읽기는 그 위의 이중 방어다.

    종전 테스트는 거절되는 입력을 넣고 단언을 `if` 안에 두어 **한 번도 실행되지
    않았다.** 그 자리를 이 둘로 나눴다.
    """
    from news_summary.web import _card_news_form_copy

    form = {
        "cover": "짝짓기 확인 표지",
        "heading-0": "첫째 소제목",
        "body-0": "첫째 카드 본문입니다. 규격에 맞게 충분히 씁니다.",
        # 1번은 통째로 없다.
        "heading-2": "셋째 소제목",
        "body-2": "셋째 카드 본문입니다. 규격에 맞게 충분히 씁니다.",
    }

    cover, cards, problem = _card_news_form_copy(form, expected=3)

    assert problem == ""
    assert cover == "짝짓기 확인 표지"
    assert cards == [
        {"heading": "첫째 소제목", "body": "첫째 카드 본문입니다. 규격에 맞게 충분히 씁니다."},
        {"heading": "셋째 소제목", "body": "셋째 카드 본문입니다. 규격에 맞게 충분히 씁니다."},
    ], "가운데가 빠지면서 짝이 밀렸다"


def test_editing_copy_rejects_a_body_without_its_heading_and_saves_nothing(monkeypatch, tmp_path):
    """짝짓기가 안전한 이유는 **한쪽만 있는 입력을 거절**하기 때문이다.

    거절할 때 절반만 저장되면 그게 곧 짝이 어긋난 카드다 — 저장이 아예
    일어나지 않아야 한다.
    """
    store, draft_id = prepare(monkeypatch, tmp_path)
    stub_generation(monkeypatch)
    client = make_client()
    build_one(client, draft_id)
    set_id = store.card_news_set_by_draft(draft_id)["id"]
    before = store.card_news_set(set_id)["cards"]

    response = client.post(
        f"/card-news/{set_id}/copy",
        data={
            "cover": "짝짓기 확인 표지",
            "body-0": "첫째 카드 본문입니다. 규격에 맞게 충분히 씁니다.",
            "heading-1": "둘째 소제목",
            "body-1": "둘째 카드 본문입니다. 규격에 맞게 충분히 씁니다.",
        },
        follow_redirects=True,
    )

    assert "소제목은" in response.get_data(as_text=True), "거절 사유가 화면에 안 뜬다"
    assert store.card_news_set(set_id)["cards"] == before, "거절했는데 문안이 바뀌었다"


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
    # CSRF를 끄지 않으면 토큰 없는 POST가 **인증 검사 앞에서** 400으로 끊긴다
    # (protect_state_changing_requests가 require_admin_login보다 먼저 등록된다).
    # 그러면 `in {302, 400}`은 400만 보고 통과해, 인증을 통째로 지워도 초록이었다
    # — 2026-08-10 감사에서 적발하고 재현했다.
    monkeypatch.setenv("NEWS_SUMMARY_CSRF_DISABLED", "1")
    client = make_client(app_testing=False)

    assert client.get("/card-news").status_code == 200, "열람은 로그인 없이 열려야 한다"
    for path in ("/card-news/manage",):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 302, f"{path}는 로그인이 필요하다"
        assert "/login" in response.headers["Location"], f"{path}가 로그인으로 안 보낸다"
    for path in (
        "/card-news/build",
        "/card-news/1/publish",
        "/card-news/1/copy",
        "/card-news/1/redraw",
        "/card-news/1/delete",
    ):
        response = client.post(path, follow_redirects=False)
        assert response.status_code == 302, f"{path}는 로그인이 필요하다"
        assert "/login" in response.headers["Location"], f"{path}가 로그인으로 안 보낸다"


def test_nav_links_to_card_news(monkeypatch, tmp_path):
    prepare(monkeypatch, tmp_path)
    client = make_client()

    html = client.get("/").data.decode("utf-8")

    assert 'href="/card-news"' in html


def _build_one_set(client, draft_id):
    return client.post(
        "/card-news/build",
        data={"draft_id": str(draft_id), "publish_date": "2026-08-09"},
        follow_redirects=True,
    )


def test_manage_page_warns_when_copy_came_from_the_fallback_model(monkeypatch, tmp_path):
    """예비 모델 문안은 문체·분량이 눈에 띄게 나쁘다 — 운영자가 알아야 손볼 수 있다."""
    _, draft_id = prepare(monkeypatch, tmp_path)
    stub_generation(monkeypatch)
    monkeypatch.setattr(
        "news_summary.cardnews_service.build_card_copy",
        lambda request, api_key: CardCopy(
            cover="담양 무더위쉼터 전면 점검",
            cards=[CardSlide(heading="야외 근로자 점검", body="야외 근로자 안전 점검에 나섰습니다.")],
            source_label=request.source_label,
            date_label=request.date_label,
            tags=["담양"],
            model="gemini-3.1-flash-lite",
        ),
    )
    client = make_client()
    _build_one_set(client, draft_id)

    html = client.get("/card-news/manage?date=2026-08-09").data.decode("utf-8")

    assert "Gemini Lite" in html, "어느 모델이 쓴 문안인지 표시되지 않았다"
    assert "예비 모델이 쓴 문안입니다" in html


def test_manage_page_does_not_warn_for_primary_model_copy(monkeypatch, tmp_path):
    """주 모델로 만든 문안에까지 경고가 뜨면 경고가 의미를 잃는다."""
    _, draft_id = prepare(monkeypatch, tmp_path)
    stub_generation(monkeypatch)
    monkeypatch.setattr(
        "news_summary.cardnews_service.build_card_copy",
        lambda request, api_key: CardCopy(
            cover="담양 무더위쉼터 전면 점검",
            cards=[CardSlide(heading="야외 근로자 점검", body="야외 근로자 안전 점검에 나섰습니다.")],
            source_label=request.source_label,
            date_label=request.date_label,
            tags=["담양"],
            model="gemini-3.5-flash",
        ),
    )
    client = make_client()
    _build_one_set(client, draft_id)

    html = client.get("/card-news/manage?date=2026-08-09").data.decode("utf-8")

    assert "Gemini Flash" in html
    assert "예비 모델이 쓴 문안입니다" not in html


def test_redraw_of_a_published_set_sends_it_back_to_review(monkeypatch, tmp_path):
    """다시 그리면 주민이 보던 그림이 바뀐다 — 사람이 한 번 보고 다시 발행해야 한다.

    옛 규격 문안은 한 장에 다 안 들어가 뒤 요점(보통 접수처·전화번호)이 통째로
    빠진다. 종전에는 그 카드가 **발행 상태 그대로** 주민에게 나갔고, 막는 것은
    flash 문구뿐이었다. 문안을 고치는 /copy 는 이미 검수 대기로 내리고 있었다.
    """
    store, draft_id = prepare(monkeypatch, tmp_path)
    stub_generation(monkeypatch)
    client = make_client()
    _build_one_set(client, draft_id)

    set_id = int(store.card_news_set_by_draft(draft_id)["id"])
    store.set_card_news_status(set_id, "published")
    assert store.card_news_set(set_id)["status"] == "published"

    client.post(f"/card-news/{set_id}/redraw", follow_redirects=True)

    assert store.card_news_set(set_id)["status"] == "draft", "발행 상태로 남았다"
    assert store.card_news_set(set_id)["published_at"] == ""


def test_redraw_of_a_draft_set_stays_a_draft(monkeypatch, tmp_path):
    """검수 대기 세트를 다시 그린다고 상태가 이상하게 바뀌면 안 된다."""
    store, draft_id = prepare(monkeypatch, tmp_path)
    stub_generation(monkeypatch)
    client = make_client()
    _build_one_set(client, draft_id)

    set_id = int(store.card_news_set_by_draft(draft_id)["id"])

    client.post(f"/card-news/{set_id}/redraw", follow_redirects=True)

    assert store.card_news_set(set_id)["status"] == "draft"


def test_public_page_hides_a_published_set_whose_images_vanished(monkeypatch, tmp_path):
    """그림이 사라진 발행 세트는 주민에게 **제목만** 보이는 상태가 된다.

    보관 정리가 그림만 걷거나 합성이 중간에 죽으면 그렇게 된다. 템플릿의 빈 상태
    안내는 세트가 0개일 때만 뜨므로, 세트가 남아 있으면 안내도 안 뜨고 카드도
    없는 화면이 나간다(2026-08-11 재현). 관리 화면은 걸러내면 안 된다 —
    운영자는 깨진 세트를 봐야 고칠 수 있다.
    """
    import shutil

    from news_summary.cardnews_service import set_directory

    store, draft_id = prepare(monkeypatch, tmp_path)
    stub_generation(monkeypatch)
    client = make_client()
    _build_one_set(client, draft_id)
    set_id = int(store.card_news_set_by_draft(draft_id)["id"])
    store.set_card_news_status(set_id, "published")

    before = client.get("/card-news?date=2026-08-09").get_data(as_text=True)
    assert "담양 무더위쉼터" in before

    shutil.rmtree(set_directory(tmp_path / "cardnews", "2026-08-09", set_id))

    after = client.get("/card-news?date=2026-08-09").get_data(as_text=True)
    assert "담양 무더위쉼터" not in after, "그림 없는 세트가 주민 화면에 남았다"

    with client.session_transaction() as session:
        session["admin_authenticated"] = True
    manage = client.get("/card-news/manage?date=2026-08-09").get_data(as_text=True)
    assert "담양 무더위쉼터" in manage, "관리 화면에서까지 사라지면 고칠 수가 없다"


def test_only_one_card_render_runs_at_a_time(monkeypatch, tmp_path):
    """카드 합성은 한 건이 100MB 안팎을 쥔다 — 512MB에서 겹치면 죽는다.

    한 건에 15~40초가 걸려 반응이 없어 보이므로 운영자가 버튼을 여러 번 누르기
    쉽다. 기다리게 하지 않고 "이미 만들고 있다"고 알려 주고 돌려보낸다.
    """
    from news_summary import web as web_module

    store, draft_id = prepare(monkeypatch, tmp_path)
    stub_generation(monkeypatch)
    client = make_client()

    # 다른 요청이 이미 자리를 잡고 있는 상황을 만든다.
    assert web_module._card_render_limit.acquire(blocking=False)
    try:
        response = _build_one_set(client, draft_id)
    finally:
        web_module._card_render_limit.release()

    assert "이미 만들고 있습니다" in response.get_data(as_text=True)
    assert store.card_news_set_by_draft(draft_id) is None, "자리를 못 잡았는데 세트가 생겼다"


def test_card_render_slot_is_released_after_a_failure(monkeypatch, tmp_path):
    """실패해도 자리를 놓아야 한다 — 안 놓으면 그 뒤로 영영 못 만든다."""
    from news_summary import web as web_module

    _, draft_id = prepare(monkeypatch, tmp_path)
    stub_generation(monkeypatch)
    monkeypatch.setattr(
        "news_summary.web.build_set_for_draft",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("합성 실패")),
    )
    client = make_client()

    _build_one_set(client, draft_id)

    assert web_module._card_render_limit.acquire(blocking=False), "실패 뒤 자리가 잠긴 채 남았다"
    web_module._card_render_limit.release()


def test_build_tells_the_operator_why_the_card_has_no_photo(monkeypatch, tmp_path):
    """카드에 사진이 없는 것은 화면에서 보이지만 **이유는 안 보인다**.

    첨부가 없어서인지, 못 받아서인지, 오늘 넣은 문턱(해상도·배너·화소)에 걸려서인지
    구별이 안 되면 운영자가 손쓸 방법이 없다.
    """
    _, draft_id = prepare(monkeypatch, tmp_path)
    stub_generation(monkeypatch)
    # 첨부는 있는데 문턱에 걸리는 크기로 돌려준다(폭 800 미만).
    small = io.BytesIO()
    Image.new("RGB", (400, 300), (90, 120, 150)).save(small, format="JPEG", quality=85)
    monkeypatch.setattr("news_summary.web._download_card_photo", lambda asset: small.getvalue())
    client = make_client()

    html = _build_one_set(client, draft_id).get_data(as_text=True)

    assert "내려받아 본 사진 1장이 모두 쓰이지 못했습니다" in html


def test_build_says_when_the_article_simply_had_no_attachment(monkeypatch, tmp_path):
    """첨부가 아예 없는 것과 문턱에 걸린 것은 다른 이야기다."""
    db_path = Path(f"data/.test_nophoto_{uuid4().hex}.sqlite").resolve()
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
            title="첨부 없는 기사",
            url=f"https://example.com/{uuid4().hex}",
            content="본문입니다.",
            published_at="2026-08-09",
            assets=[],
        )
    )
    draft_id = store.add_article_draft(
        ArticleDraft(
            press_release_id=release_id,
            title="첨부 없는 기사",
            body="담양군은 현장을 점검했습니다.",
            review_note="",
            model="gemini-3.5-flash",
        )
    )
    stub_generation(monkeypatch)
    client = make_client()

    html = _build_one_set(client, draft_id).get_data(as_text=True)

    assert "쓸 수 있는 사진 첨부가 없어" in html


def _publish_set(store, tmp_path, day: str, title: str) -> int:
    """그 날짜에 그림까지 갖춘 발행 세트를 하나 만든다."""
    release_id = store.add_press_release(
        PressRelease(
            source_id="damyang-county",
            source_name="담양군청 보도자료",
            region="전남 담양",
            title=title,
            url=f"https://example.com/{uuid4().hex}",
            content="본문입니다.",
            published_at=day,
            assets=[],
        )
    )
    draft_id = store.add_article_draft(
        ArticleDraft(
            press_release_id=release_id,
            title=title,
            body="담양군은 현장을 점검했습니다.",
            review_note="",
            model="gemini-3.5-flash",
        )
    )
    set_id = store.save_card_news_set(
        draft_id=draft_id,
        press_release_id=release_id,
        publish_date=day,
        cover=f"{title} 표지",
        cards=[{"heading": "소제목", "body": "본문입니다."}],
        tags=[],
        source_label="담양군청 보도자료",
        image_count=1,
    )
    store.set_card_news_status(set_id, "published")
    directory = tmp_path / "cardnews" / day / str(set_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "1.png").write_bytes(b"x")
    return set_id


def test_landing_falls_through_to_a_date_that_still_has_cards(monkeypatch, tmp_path):
    """날짜 목록은 **행** 기준인데 화면은 **그림** 기준이라 어긋날 수 있다.

    최신 날짜의 그림만 사라지면(부분 실패·수동 삭제) 바로 전날에 멀쩡한 카드가
    있는데도 첫 화면이 빈 채로 나간다(2026-08-11 재현).
    """
    import shutil

    store, _ = prepare(monkeypatch, tmp_path)
    _publish_set(store, tmp_path, "2026-08-08", "옛날 기사")
    _publish_set(store, tmp_path, "2026-08-09", "최신 기사")
    client = make_client()

    assert "최신 기사 표지" in client.get("/card-news").get_data(as_text=True)

    shutil.rmtree(tmp_path / "cardnews" / "2026-08-09")

    html = client.get("/card-news").get_data(as_text=True)

    assert "옛날 기사 표지" in html, "그림 있는 날짜로 안 내려갔다 — 주민이 빈 화면을 본다"


def test_explicitly_requested_date_still_shows_its_own_empty_state(monkeypatch, tmp_path):
    """날짜를 직접 고른 방문자에게 다른 날짜를 슬쩍 보여 주면 안 된다."""
    import shutil

    store, _ = prepare(monkeypatch, tmp_path)
    _publish_set(store, tmp_path, "2026-08-08", "옛날 기사")
    _publish_set(store, tmp_path, "2026-08-09", "최신 기사")
    client = make_client()
    shutil.rmtree(tmp_path / "cardnews" / "2026-08-09")

    html = client.get("/card-news?date=2026-08-09").get_data(as_text=True)

    assert "옛날 기사 표지" not in html, "요청하지 않은 날짜 카드를 보여 줬다"


def test_shared_link_never_shows_a_different_day_after_unpublish(monkeypatch, tmp_path):
    """카톡·밴드로 퍼진 링크가 엉뚱한 날 뉴스를 보여 주면 안 된다.

    발행을 내리는 순간(예: 다시 그리기) 그 날짜가 목록에서 빠지고, 종전에는
    `target not in published`라 조용히 최신 날짜로 갈아 끼웠다(2026-08-11 재현).
    """
    store, _ = prepare(monkeypatch, tmp_path)
    _publish_set(store, tmp_path, "2026-08-08", "옛날 기사")
    newest = _publish_set(store, tmp_path, "2026-08-09", "최신 기사")
    client = make_client()

    store.set_card_news_status(newest, "draft")

    html = client.get("/card-news?date=2026-08-09").get_data(as_text=True)

    assert "옛날 기사 표지" not in html, "공유 링크가 다른 날 카드를 보여 줬다"
    assert "최신 기사 표지" not in html, "발행 내린 카드가 여전히 보인다"
    # 기본 랜딩은 여전히 볼 것을 찾아 준다.
    assert "옛날 기사 표지" in client.get("/card-news").get_data(as_text=True)


def test_unknown_date_does_not_crash_the_public_page(monkeypatch, tmp_path):
    """요청 날짜를 그대로 쓰게 됐으므로 아무 값이나 들어와도 죽으면 안 된다."""
    store, _ = prepare(monkeypatch, tmp_path)
    _publish_set(store, tmp_path, "2026-08-08", "옛날 기사")
    client = make_client()

    for value in ("zzz", "2026/08/09", "../backups", ""):
        assert client.get(f"/card-news?date={value}").status_code == 200
