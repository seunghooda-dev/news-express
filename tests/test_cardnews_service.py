import io
import json
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image

from news_summary.cardnews import CardCopy
from news_summary.cardnews_service import (
    CardNewsServiceError,
    build_set_for_draft,
    decode_cards,
    delete_set_images,
    load_set_images,
    prune_old_dates,
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
        cards=["야외 근로자 안전 점검에 나섰습니다.", "냉방기 가동 상태를 확인했습니다."],
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
    assert len(result.image_paths) == 3, "표지 1장 + 본문 2장"
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
    assert json.loads(row["cards"]) == result.copy.cards
    assert row["status"] == "draft", "사람이 확인하기 전에는 발행 상태가 아니어야 한다"

    restored = decode_cards(row)
    assert restored.cover == result.copy.cover
    assert restored.cards == result.copy.cards
    assert restored.date_label == "2026.08.09"


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
    assert len(load_set_images(out, "2026-08-09", second.set_id)) == 3


def test_photo_download_failure_still_produces_cards(tmp_path):
    store = make_store(tmp_path)
    _, draft_id = seed(store)

    def broken(asset):
        raise TimeoutError("image host down")

    result = build_set_for_draft(
        store, draft_id, "key", tmp_path / "cardnews", publish_date="2026-08-09",
        downloader=broken, copy_builder=fake_copy_builder,
    )

    assert len(result.image_paths) == 3, "사진이 없어도 텍스트 카드는 나와야 한다"


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
