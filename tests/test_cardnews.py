import io

import pytest
from PIL import Image

from news_summary.cardnews import (
    CARD_HEIGHT,
    CARD_WIDTH,
    MIN_PHOTO_EDGE,
    CardCopy,
    CardNewsError,
    CardSlide,
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
            CardSlide(
                heading="야외 근로자 안전 점검",
                body="담양군이 야외 근로자와 취약계층 안전을 위해 현장을 점검했습니다.",
            ),
            CardSlide(
                heading="무더위쉼터 냉방기 확인",
                body="무더위쉼터 운영 상태와 냉방기 가동 여부를 함께 확인했습니다.",
            ),
            CardSlide(
                heading="폭염특보 해제까지 지속",
                body="군은 폭염특보가 해제될 때까지 점검을 이어갈 계획입니다.",
            ),
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


def test_heading_is_drawn_together_with_body():
    """소제목이 있어야 넘기면서도 무슨 얘긴지 잡힌다(2026-08-09 사용자 지적: "내용이 조금 빈약해")."""
    with_heading = build_card_images(sample_copy(), [])
    without_heading = build_card_images(
        sample_copy(cards=[CardSlide(heading="", body=slide.body) for slide in sample_copy().cards]),
        [],
    )

    # 첫 본문 카드가 달라야 소제목이 실제로 그려진 것이다.
    assert open_card(with_heading[1]).tobytes() != open_card(without_heading[1]).tobytes()


def test_heading_only_slide_is_kept():
    """본문이 비어도 소제목이 있으면 그 카드는 살린다 — 빈 카드로 취급해 버리면 장수가 어긋난다."""
    images = build_card_images(sample_copy(cards=[CardSlide(heading="소제목만 있는 카드", body="")]), [])

    assert len(images) == 2
    assert (open_card(images[1]).width, open_card(images[1]).height) == (CARD_WIDTH, CARD_HEIGHT)


def test_legacy_string_cards_still_render():
    """옛 형태(문자열 리스트)로 만든 세트도 그대로 그려져야 한다 — 이미 저장된 세트가 깨지면 안 된다."""
    legacy = sample_copy(
        cards=["야외 근로자 안전 점검에 나섰습니다.", "냉방기 가동 상태를 확인했습니다."]
    )

    assert legacy.cards == [
        CardSlide(heading="", body="야외 근로자 안전 점검에 나섰습니다."),
        CardSlide(heading="", body="냉방기 가동 상태를 확인했습니다."),
    ]

    images = build_card_images(legacy, [])

    assert len(images) == 3, "표지 1장 + 본문 2장"
    for raw in images:
        card = open_card(raw)
        assert (card.width, card.height) == (CARD_WIDTH, CARD_HEIGHT)


def test_low_resolution_photo_is_not_upscaled():
    """작은 사진을 억지로 키우면 화질 저하가 그대로 보인다 — 안 쓰는 게 낫다.

    문턱을 800px로 내린 뒤에도 이 선 아래는 여전히 버린다. 실측에서 10px짜리
    추적용 이미지가 13건 섞여 있었다(2026-08-07 금요일 첨부 247건).
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
    images = build_card_images(sample_copy(cards=[CardSlide(heading="긴 본문 카드", body="나" * 600)]), [])

    assert len(images) == 2
    assert (open_card(images[1]).width, open_card(images[1]).height) == (CARD_WIDTH, CARD_HEIGHT)


def test_empty_cover_is_rejected_rather_than_guessed():
    with pytest.raises(CardNewsError):
        build_card_images(sample_copy(cover="   "), [photo_bytes(2000, 1500)])


def test_blank_body_cards_are_rejected():
    blank = [CardSlide(heading="", body=""), CardSlide(heading="   ", body="   ")]

    with pytest.raises(CardNewsError):
        build_card_images(sample_copy(cards=blank), [photo_bytes(2000, 1500)])


def test_cover_photo_is_not_reused_on_first_body_card():
    """같은 사진이 연달아 나오면 카드뉴스가 지루해진다."""
    two_photos = [photo_bytes(2000, 1500, (200, 60, 60)), photo_bytes(2000, 1500, (60, 200, 60))]
    images = build_card_images(sample_copy(), two_photos)

    cover_top = open_card(images[0]).crop((0, 0, 200, 200)).tobytes()
    body_top = open_card(images[1]).crop((0, 0, 200, 200)).tobytes()
    assert cover_top != body_top


def wide_photo_with_edge_markers() -> bytes:
    """좌우 끝에 표식을 둔 아주 넓은 사진 — 잘리면 표식이 사라진다."""
    image = Image.new("RGB", (2400, 1250), (40, 40, 40))
    for x in range(0, 60):
        for y in range(0, 1250):
            image.putpixel((x, y), (255, 0, 0))
            image.putpixel((2400 - 1 - x, y), (0, 0, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_wide_photo_keeps_both_edges():
    """가로 사진의 좌우를 자르면 현장과 인물이 날아간다.

    2026-08-09 갭 분석에서 적발 — max 배율로 밴드를 채우느라 1.379보다 넓은
    사진의 좌우가 잘리고 있었다. 지자체 사진은 표본이 1.33~1.92다.
    """
    images = build_card_images(sample_copy(), [wide_photo_with_edge_markers()])
    cover = open_card(images[0]).convert("RGB")

    # 사진 밴드 한가운데 높이에서 좌우 끝 색을 본다.
    y = int(CARD_HEIGHT * 0.58) // 2
    left = cover.getpixel((2, y))
    right = cover.getpixel((CARD_WIDTH - 3, y))

    assert left[0] > 150 and left[1] < 90, f"왼쪽 끝이 잘렸다: {left}"
    assert right[2] > 150 and right[1] < 90, f"오른쪽 끝이 잘렸다: {right}"


def test_tall_photo_is_cropped_vertically_not_horizontally():
    """세로로 긴 사진은 위아래를 잘라도 좌우는 온전해야 한다."""
    tall = Image.new("RGB", (1200, 2400), (10, 200, 10))
    for x in range(0, 40):
        for y in range(0, 2400):
            tall.putpixel((x, y), (255, 0, 0))
    buffer = io.BytesIO()
    tall.save(buffer, format="PNG")

    images = build_card_images(sample_copy(), [buffer.getvalue()])
    cover = open_card(images[0]).convert("RGB")

    left = cover.getpixel((2, 100))
    assert left[0] > 150 and left[1] < 90, f"왼쪽 끝이 잘렸다: {left}"


def test_long_human_edited_text_never_spills_past_the_card():
    """사람이 고친 문안에는 AI 규격이 안 걸린다 — 길어도 카드 밖으로 새면 안 된다.

    2026-08-09 검토에서 적발 — 줄 수는 통과하는데 높이가 넘쳐 장수 표시와 겹치는
    대역이 있었다. 폼 maxlength(소제목 30·본문 160) 안쪽에서도 일어났다.
    """
    images = build_card_images(
        sample_copy(cards=[CardSlide(heading="가" * 30, body="나" * 160)]),
        [photo_bytes(2000, 1500)],
    )
    card = open_card(images[1]).convert("RGB")

    # 맨 아랫줄이 배경색 그대로여야 글자가 안 샌 것이다.
    bottom = [card.getpixel((x, CARD_HEIGHT - 3)) for x in range(60, CARD_WIDTH - 60, 40)]
    assert all(abs(p[0] - 17) < 12 and abs(p[1] - 17) < 12 for p in bottom), f"글자가 카드 밖으로 샜다: {bottom[:4]}"


def test_narrow_tall_photo_is_not_upscaled():
    """판정 축이 긴 변이면 700x2000 같은 사진이 1.54배로 확대된다.

    밴드 배율은 폭 기준이므로 폭으로 재야 주석("많이 확대하지 않는다")과 코드가 맞는다.
    """
    narrow = build_card_images(sample_copy(), [photo_bytes(700, 2000)])
    text_only = build_card_images(sample_copy(), [])

    assert open_card(narrow[0]).tobytes() == open_card(text_only[0]).tobytes(), "폭이 좁은 사진이 쓰였다"


def test_common_cms_width_photo_is_used():
    """지자체 CMS가 가장 많이 뱉는 폭(1000px)이 버려지면 카드에 사진이 안 실린다.

    2026-08-07 금요일 전량 실측: 첨부 247건 중 1000px가 24건으로 최다였고,
    문턱이 1080px이던 동안 사진 있는 초안 110건 중 47건이 글자만 나왔다.
    점수 상위 3건이 전부 여기 걸려 실제로 사진 없는 카드가 나왔다.
    """
    common = build_card_images(sample_copy(), [photo_bytes(1000, 668)])
    text_only = build_card_images(sample_copy(), [])

    assert open_card(common[0]).tobytes() != open_card(text_only[0]).tobytes(), "1000px 사진이 버려졌다"


def test_huge_photo_is_downscaled_before_holding():
    """원본을 그대로 4장 쥐면 512MB에서 터진다 — 열자마자 줄여서 들고 있어야 한다."""
    from news_summary.cardnews import MAX_WORKING_EDGE, _usable_photos

    photos = _usable_photos([photo_bytes(6000, 4000)])

    assert photos, "쓸 수 있는 사진이어야 한다"
    assert max(photos[0].size) <= MAX_WORKING_EDGE, f"원본 크기 그대로 들고 있다: {photos[0].size}"
    for photo in photos:
        photo.close()


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
