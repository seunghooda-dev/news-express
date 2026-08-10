import io
import json
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image

from news_summary.cardnews import CardCopy, CardSlide
from news_summary.cardnews_service import (
    CardNewsServiceError,
    build_set_for_draft,
    decode_cards,
    delete_set_images,
    load_set_images,
    prune_old_dates,
    rebuild_images_from_copy,
    set_directory,
)
from news_summary.models import ArticleDraft, PressRelease, PressReleaseAsset
from news_summary.storage import Store


def photo_bytes(color=(90, 130, 170)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2000, 1500), color).save(buffer, format="JPEG", quality=88)
    return buffer.getvalue()


def make_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / f"cardnews_{uuid4().hex}.sqlite")
    store.init_db()
    return store


def seed(store: Store, *, asset_urls=("https://example.com/a.jpg",), title="담양군, 폭염 현장 점검"):
    release_id = store.add_press_release(
        PressRelease(
            source_id="damyang-county",
            source_name="담양군청 보도자료",
            region="전남 담양",
            title=title,
            url=f"https://example.com/{uuid4().hex}",
            content="담양군은 폭염 취약 현장을 점검했다고 밝혔습니다.",
            published_at="2026-08-09",
            assets=[
                PressReleaseAsset(
                    url=url,
                    title="첨부 사진",
                    filename="",
                    content_type="image/jpeg",
                    asset_type="image",
                    is_image=True,
                )
                for url in asset_urls
            ],
        )
    )
    draft_id = store.add_article_draft(
        ArticleDraft(
            press_release_id=release_id,
            title=title,
            body="담양군은 야외 근로자 안전을 위해 현장을 점검했습니다.",
            review_note="",
            model="gemini-3.5-flash",
        )
    )
    return release_id, draft_id


def fake_copy_builder(request, api_key):
    return CardCopy(
        cover="담양 무더위쉼터 전면 점검",
        cards=[
            CardSlide(heading="야외 근로자 점검", body="야외 근로자 안전 점검에 나섰습니다."),
            CardSlide(heading="냉방기 상태 확인", body="냉방기 가동 상태를 확인했습니다."),
        ],
        source_label=request.source_label,
        date_label=request.date_label,
        tags=["담양"],
    )


def test_builds_set_and_writes_images(tmp_path):
    store = make_store(tmp_path)
    _, draft_id = seed(store)
    out = tmp_path / "cardnews"

    result = build_set_for_draft(
        store,
        draft_id,
        "key",
        out,
        publish_date="2026-08-09",
        downloader=lambda asset: photo_bytes(),
        copy_builder=fake_copy_builder,
    )

    assert result.set_id > 0
    assert len(result.image_paths) == 1, "기사 한 건 = 카드 한 장"
    assert all(path.exists() for path in result.image_paths)
    assert set_directory(out, "2026-08-09", result.set_id).is_dir()


def test_only_downloads_photos_from_its_own_release(tmp_path):
    """다른 기사 사진이 섞이면 신뢰 사고다 — 자기 원문 첨부만 내려받아야 한다.

    구현 중 대시보드에서 사진을 긁어와 화재 현장 사진에 공공예식장 문구가
    붙은 카드가 실제로 나왔다.
    """
    store = make_store(tmp_path)
    seed(store, asset_urls=("https://other.example/fire.jpg",), title="다른 기사")
    _, draft_id = seed(store, asset_urls=("https://mine.example/wedding.jpg",), title="내 기사")

    requested: list[str] = []

    def downloader(asset):
        requested.append(str(asset["url"]))
        return photo_bytes()

    build_set_for_draft(
        store,
        draft_id,
        "key",
        tmp_path / "cardnews",
        publish_date="2026-08-09",
        downloader=downloader,
        copy_builder=fake_copy_builder,
    )

    assert requested == ["https://mine.example/wedding.jpg"]
    assert not any("other.example" in url for url in requested)


def test_persists_copy_for_later_editing(tmp_path):
    store = make_store(tmp_path)
    _, draft_id = seed(store)

    result = build_set_for_draft(
        store, draft_id, "key", tmp_path / "cardnews",
        publish_date="2026-08-09", downloader=lambda asset: photo_bytes(),
        copy_builder=fake_copy_builder,
    )

    row = store.card_news_set(result.set_id)
    assert row["cover"] == "담양 무더위쉼터 전면 점검"
    # 소제목·본문이 각각 남아야 사람이 고칠 때 둘을 따로 손볼 수 있다.
    assert json.loads(row["cards"]) == [
        {"heading": slide.heading, "body": slide.body} for slide in result.copy.cards
    ]
    assert row["status"] == "draft", "사람이 확인하기 전에는 발행 상태가 아니어야 한다"

    restored = decode_cards(row)
    assert restored.cover == result.copy.cover
    assert restored.cards == result.copy.cards
    assert restored.date_label == "2026.08.09"


def test_legacy_string_cards_are_read_and_redrawn(tmp_path):
    """옛 형태(문자열 리스트)로 저장된 세트도 읽고 다시 그릴 수 있어야 한다.

    구판이 저장해 둔 세트가 새 구조 때문에 열리지 않으면 이미 만든 카드가 통째로 죽는다.
    """
    store = make_store(tmp_path)
    _, draft_id = seed(store)
    out = tmp_path / "cardnews"
    result = build_set_for_draft(
        store, draft_id, "key", out, publish_date="2026-08-09",
        downloader=lambda asset: photo_bytes(), copy_builder=fake_copy_builder,
    )
    # 구판이 쓰던 형태 그대로 덮어쓴다.
    store.update_card_news_copy(
        result.set_id,
        "담양 무더위쉼터 전면 점검",
        ["야외 근로자 안전 점검에 나섰습니다.", "냉방기 가동 상태를 확인했습니다."],
    )

    restored = decode_cards(store.card_news_set(result.set_id))

    assert restored.cards == [
        CardSlide(heading="", body="야외 근로자 안전 점검에 나섰습니다."),
        CardSlide(heading="", body="냉방기 가동 상태를 확인했습니다."),
    ]
    paths = rebuild_images_from_copy(store, result.set_id, out, downloader=lambda asset: photo_bytes())
    assert len(paths) == 1, "기사 한 건 = 카드 한 장"


def test_rebuilding_same_draft_replaces_instead_of_duplicating(tmp_path):
    store = make_store(tmp_path)
    _, draft_id = seed(store)
    out = tmp_path / "cardnews"

    first = build_set_for_draft(
        store, draft_id, "key", out, publish_date="2026-08-09",
        downloader=lambda asset: photo_bytes(), copy_builder=fake_copy_builder,
    )
    second = build_set_for_draft(
        store, draft_id, "key", out, publish_date="2026-08-09",
        downloader=lambda asset: photo_bytes(), copy_builder=fake_copy_builder,
    )

    assert first.set_id == second.set_id
    assert len(store.card_news_sets_for_date("2026-08-09")) == 1
    # 재생성 시 옛 이미지가 남아 섞이면 안 된다.
    assert len(load_set_images(out, "2026-08-09", second.set_id)) == 1


def test_photo_download_failure_still_produces_cards(tmp_path):
    store = make_store(tmp_path)
    _, draft_id = seed(store)

    def broken(asset):
        raise TimeoutError("image host down")

    result = build_set_for_draft(
        store, draft_id, "key", tmp_path / "cardnews", publish_date="2026-08-09",
        downloader=broken, copy_builder=fake_copy_builder,
    )

    assert len(result.image_paths) == 1, "사진이 없어도 텍스트 카드는 나와야 한다"


def test_missing_draft_is_rejected(tmp_path):
    store = make_store(tmp_path)

    with pytest.raises(CardNewsServiceError, match="찾을 수 없습니다"):
        build_set_for_draft(
            store, 9999, "key", tmp_path / "cardnews",
            downloader=lambda asset: photo_bytes(), copy_builder=fake_copy_builder,
        )


def test_publish_status_transition(tmp_path):
    store = make_store(tmp_path)
    _, draft_id = seed(store)
    result = build_set_for_draft(
        store, draft_id, "key", tmp_path / "cardnews", publish_date="2026-08-09",
        downloader=lambda asset: photo_bytes(), copy_builder=fake_copy_builder,
    )

    assert store.card_news_sets_for_date("2026-08-09", status="published") == []
    store.set_card_news_status(result.set_id, "published")

    published = store.card_news_sets_for_date("2026-08-09", status="published")
    assert len(published) == 1
    assert store.card_news_published_dates() == ["2026-08-09"]


def test_delete_removes_images_and_row(tmp_path):
    store = make_store(tmp_path)
    _, draft_id = seed(store)
    out = tmp_path / "cardnews"
    result = build_set_for_draft(
        store, draft_id, "key", out, publish_date="2026-08-09",
        downloader=lambda asset: photo_bytes(), copy_builder=fake_copy_builder,
    )

    delete_set_images(out, "2026-08-09", result.set_id)
    store.delete_card_news_set(result.set_id)

    assert load_set_images(out, "2026-08-09", result.set_id) == []
    assert store.card_news_set(result.set_id) is None


def test_prune_keeps_only_recent_dates(tmp_path):
    root = tmp_path / "cardnews"
    for day in ("2026-08-01", "2026-08-05", "2026-08-09"):
        (root / day / "1").mkdir(parents=True)
        (root / day / "1" / "1.png").write_bytes(b"x")

    removed = prune_old_dates(root, keep_days=2)

    assert removed == 1
    assert not (root / "2026-08-01").exists()
    assert (root / "2026-08-09").exists()


def test_prune_is_noop_without_directory(tmp_path):
    assert prune_old_dates(tmp_path / "missing", keep_days=3) == 0


def test_stores_which_model_wrote_the_copy(tmp_path):
    """예비 모델로 넘어간 세트를 관리 화면이 표시하려면 DB에 남아 있어야 한다."""
    store = make_store(tmp_path)
    _, draft_id = seed(store)

    def lite_builder(request, api_key):
        copy = fake_copy_builder(request, api_key)
        copy.model = "gemini-3.1-flash-lite"
        return copy

    result = build_set_for_draft(
        store,
        draft_id,
        "key",
        tmp_path / "cardnews",
        publish_date="2026-08-09",
        downloader=lambda asset: photo_bytes(),
        copy_builder=lite_builder,
    )

    row = store.card_news_set(result.set_id)
    assert row["copy_model"] == "gemini-3.1-flash-lite"


def test_editing_copy_keeps_the_recorded_model(tmp_path):
    """사람이 글자를 고쳐도 그 문안을 **처음 쓴** 모델은 그대로여야 한다."""
    store = make_store(tmp_path)
    _, draft_id = seed(store)

    def lite_builder(request, api_key):
        copy = fake_copy_builder(request, api_key)
        copy.model = "gemini-3.1-flash-lite"
        return copy

    result = build_set_for_draft(
        store,
        draft_id,
        "key",
        tmp_path / "cardnews",
        publish_date="2026-08-09",
        downloader=lambda asset: photo_bytes(),
        copy_builder=lite_builder,
    )
    store.update_card_news_copy(
        result.set_id, cover="사람이 고친 표지", cards=[{"heading": "손질", "body": "사람이 고친 본문입니다."}]
    )

    assert store.card_news_set(result.set_id)["copy_model"] == "gemini-3.1-flash-lite"


def test_prune_removes_the_rows_with_the_images(tmp_path):
    """그림만 걷고 행을 남기면 주민 화면에 제목만 있는 기사가 남는다."""
    store = make_store(tmp_path)
    root = tmp_path / "cardnews"
    kept, pruned = "2026-08-09", "2026-07-01"
    ids = {}
    # 세트는 draft_id로 덮어쓰기(업서트)라 날짜마다 다른 초안이어야 한다.
    for day in (kept, pruned):
        release_id, draft_id = seed(store, title=f"{day} 기사")
        (root / day / "1").mkdir(parents=True)
        (root / day / "1" / "1.png").write_bytes(b"x")
        ids[day] = store.save_card_news_set(
            draft_id=draft_id,
            press_release_id=release_id,
            publish_date=day,
            cover=f"{day} 표지",
            cards=[{"heading": "소제목", "body": "본문입니다."}],
            tags=[],
            source_label="담양군청 보도자료",
            image_count=1,
        )

    removed = prune_old_dates(root, keep_days=1, store=store)

    assert removed == 1
    assert not (root / pruned).exists()
    assert store.card_news_set(ids[pruned]) is None, "그림은 지웠는데 행이 남았다"
    assert store.card_news_set(ids[kept]) is not None, "보관 기간 안의 세트를 지웠다"


def test_prune_without_a_store_leaves_rows_alone(tmp_path):
    """store를 안 주면 종전대로 폴더만 정리한다 — 기존 호출부를 깨지 않는다."""
    store = make_store(tmp_path)
    release_id, draft_id = seed(store)
    root = tmp_path / "cardnews"
    (root / "2026-07-01" / "1").mkdir(parents=True)
    set_id = store.save_card_news_set(
        draft_id=draft_id,
        press_release_id=release_id,
        publish_date="2026-07-01",
        cover="표지",
        cards=[{"heading": "소제목", "body": "본문입니다."}],
        tags=[],
        source_label="담양군청 보도자료",
        image_count=1,
    )

    assert prune_old_dates(root, keep_days=0) == 0
    assert store.card_news_set(set_id) is not None


def test_set_directory_rejects_a_date_that_escapes_the_card_root(tmp_path):
    """이 값이 곧 경로 조각이고, 그 경로에 shutil.rmtree가 걸린다.

    2026-08-11 재현: `../backups`가 카드뉴스 루트를 벗어났다. 로그인·CSRF 뒤라
    공개 취약점은 아니지만, 더 잦을 형태는 오타다 — `2026/08/09`는 3단 중첩
    폴더를 만들고 prune_old_dates가 그것을 하루로 세지 못해 영영 안 지운다.
    """
    root = tmp_path / "cardnews"

    assert set_directory(root, "2026-08-09", 7) == root / "2026-08-09" / "7"

    for bad in ("../backups", "..\backups", "2026/08/09", "", "   ", "2026-8-9"):
        with pytest.raises(CardNewsServiceError, match="날짜 형식"):
            set_directory(root, bad, 7)


def test_build_refuses_a_malformed_publish_date(tmp_path):
    """합성 진입점에서도 막혀야 한다 — 폴더를 만들기 전에 걸린다."""
    store = make_store(tmp_path)
    _, draft_id = seed(store)

    with pytest.raises(CardNewsServiceError, match="날짜 형식"):
        build_set_for_draft(
            store,
            draft_id,
            "key",
            tmp_path / "cardnews",
            publish_date="../backups",
            downloader=lambda asset: photo_bytes(),
            copy_builder=fake_copy_builder,
        )


def test_reading_a_set_with_a_broken_date_does_not_crash(tmp_path):
    """검증이 생기기 전에 저장된 행이 있을 수 있다 — 읽기는 관대해야 한다.

    쓰기는 그대로 거절하므로 새로 이상한 폴더가 생기지는 않는다. 그런데 읽기까지
    막으면 그 행 하나 때문에 관리 화면이 통째로 500이 된다.
    """
    root = tmp_path / "cardnews"

    assert load_set_images(root, "2026/08/09", 7) == []
    delete_set_images(root, "../backups", 7)  # 예외 없이 아무것도 안 한다
