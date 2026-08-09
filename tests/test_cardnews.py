import io

import pytest
from PIL import Image

from news_summary.cardnews import (
    CARD_HEIGHT,
    CARD_WIDTH,
    MIN_PHOTO_EDGE,
    CardCopy,
    CardNewsError,
    build_card_images,
)


def photo_bytes(width: int, height: int, color=(80, 120, 160)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def sample_copy(**overrides) -> CardCopy:
    data = {
        "cover": "담양군, 폭염 취약 현장 긴급 점검",
        "cards": [
            "담양군이 야외 근로자와 취약계층 안전을 위해 현장을 점검했습니다.",
            "무더위쉼터 운영 상태와 냉방기 가동 여부를 함께 확인했습니다.",
            "군은 폭염특보가 해제될 때까지 점검을 이어갈 계획입니다.",
        ],
        "source_label": "담양군청 보도자료",
        "date_label": "2026.08.09",
    }
    data.update(overrides)
    return CardCopy(**data)


def open_card(raw: bytes) -> Image.Image:
    return Image.open(io.BytesIO(raw))


def test_builds_cover_and_body_cards_at_fixed_size():
    images = build_card_images(sample_copy(), [photo_bytes(3500, 2625)])

    assert len(images) == 4, "표지 1장 + 본문 3장"
    for raw in images:
        card = open_card(raw)
        assert (card.width, card.height) == (CARD_WIDTH, CARD_HEIGHT)


def test_low_resolution_photo_is_not_upscaled():
    """작은 사진을 억지로 키우면 화질 저하가 그대로 보인다 — 안 쓰는 게 낫다.

    실측상 원본의 약 40%가 1080px에 못 미친다(980x735, 600x400 등).
    """
    small = photo_bytes(600, 400)
    with_small = build_card_images(sample_copy(), [small])
    text_only = build_card_images(sample_copy(), [])

    # 작은 사진은 버려지므로 사진 없는 경우와 같은 배치가 나와야 한다.
    assert open_card(with_small[0]).tobytes() == open_card(text_only[0]).tobytes()


def test_photo_exactly_at_threshold_is_used():
    at_threshold = build_card_images(sample_copy(), [photo_bytes(MIN_PHOTO_EDGE, 800)])
    text_only = build_card_images(sample_copy(), [])

    assert open_card(at_threshold[0]).tobytes() != open_card(text_only[0]).tobytes()


def test_works_without_any_photo():
    images = build_card_images(sample_copy(), [])

    assert len(images) == 4
    assert (open_card(images[0]).width, open_card(images[0]).height) == (CARD_WIDTH, CARD_HEIGHT)


def test_broken_attachment_is_skipped_not_fatal():
    """첨부 하나가 깨져도 카드는 나와야 한다."""
    images = build_card_images(sample_copy(), [b"not-an-image", photo_bytes(2000, 1500)])

    assert len(images) == 4


def test_very_long_headline_is_truncated_not_overflowed():
    images = build_card_images(sample_copy(cover="가" * 300), [photo_bytes(2000, 1500)])

    assert (open_card(images[0]).width, open_card(images[0]).height) == (CARD_WIDTH, CARD_HEIGHT)


def test_very_long_body_text_stays_inside_card():
    images = build_card_images(sample_copy(cards=["나" * 600]), [])

    assert len(images) == 2
    assert (open_card(images[1]).width, open_card(images[1]).height) == (CARD_WIDTH, CARD_HEIGHT)


def test_empty_cover_is_rejected_rather_than_guessed():
    with pytest.raises(CardNewsError):
        build_card_images(sample_copy(cover="   "), [photo_bytes(2000, 1500)])


def test_blank_body_cards_are_rejected():
    with pytest.raises(CardNewsError):
        build_card_images(sample_copy(cards=["", "   "]), [photo_bytes(2000, 1500)])


def test_cover_photo_is_not_reused_on_first_body_card():
    """같은 사진이 연달아 나오면 카드뉴스가 지루해진다."""
    two_photos = [photo_bytes(2000, 1500, (200, 60, 60)), photo_bytes(2000, 1500, (60, 200, 60))]
    images = build_card_images(sample_copy(), two_photos)

    cover_top = open_card(images[0]).crop((0, 0, 200, 200)).tobytes()
    body_top = open_card(images[1]).crop((0, 0, 200, 200)).tobytes()
    assert cover_top != body_top


def test_releases_free_heap_after_building(monkeypatch):
    """1080 합성은 썸네일보다 무겁다 — 끝나면 반드시 힙을 돌려준다."""
    calls = {"count": 0}
    monkeypatch.setattr(
        "news_summary.cardnews.release_free_heap",
        lambda: calls.__setitem__("count", calls["count"] + 1) or True,
    )

    build_card_images(sample_copy(), [photo_bytes(2000, 1500)])

    assert calls["count"] == 1


def test_releases_free_heap_even_when_rendering_fails(monkeypatch):
    calls = {"count": 0}
    monkeypatch.setattr(
        "news_summary.cardnews.release_free_heap",
        lambda: calls.__setitem__("count", calls["count"] + 1) or True,
    )
    monkeypatch.setattr(
        "news_summary.cardnews._render_cover",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("boom")),
    )

    with pytest.raises(ValueError):
        build_card_images(sample_copy(), [photo_bytes(2000, 1500)])

    assert calls["count"] == 1
