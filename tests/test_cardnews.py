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


def test_builds_exactly_one_card_at_fixed_size():
    """기사 한 건은 카드 한 장이다(2026-08-10 사용자 지시).

    여러 장으로 넘기면 첫 장 제목만 보고 지나가 신청 기한·대상이 안 읽혔다.
    """
    images = build_card_images(sample_copy(), [photo_bytes(3500, 2625)])

    assert len(images) == 1, "기사 한 건 = 카드 한 장"
    card = open_card(images[0])
    assert (card.width, card.height) == (CARD_WIDTH, CARD_HEIGHT)


def test_every_slide_is_drawn_on_the_one_card():
    """한 장뿐이므로 빠진 요점은 주민에게 전달될 방법이 없다."""
    three = build_card_images(sample_copy(), [])
    two = build_card_images(sample_copy(cards=sample_copy().cards[:2]), [])

    assert open_card(three[0]).tobytes() != open_card(two[0]).tobytes(), "세 번째 요점이 안 그려졌다"


def test_heading_is_drawn_together_with_body():
    """소제목이 있어야 넘기면서도 무슨 얘긴지 잡힌다(2026-08-09 사용자 지적: "내용이 조금 빈약해")."""
    with_heading = build_card_images(sample_copy(), [])
    without_heading = build_card_images(
        sample_copy(cards=[CardSlide(heading="", body=slide.body) for slide in sample_copy().cards]),
        [],
    )

    assert open_card(with_heading[0]).tobytes() != open_card(without_heading[0]).tobytes()


def test_heading_only_slide_is_kept():
    """본문이 비어도 소제목이 있으면 그 요점은 살린다 — 빈 것으로 취급해 버리면 사실이 사라진다."""
    images = build_card_images(sample_copy(cards=[CardSlide(heading="소제목만 있는 요점", body="")]), [])

    assert len(images) == 1
    assert (open_card(images[0]).width, open_card(images[0]).height) == (CARD_WIDTH, CARD_HEIGHT)


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

    assert len(images) == 1
    card = open_card(images[0])
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

    assert len(images) == 1
    assert (open_card(images[0]).width, open_card(images[0]).height) == (CARD_WIDTH, CARD_HEIGHT)


def test_broken_attachment_is_skipped_not_fatal():
    """첨부 하나가 깨져도 카드는 나와야 한다."""
    images = build_card_images(sample_copy(), [b"not-an-image", photo_bytes(2000, 1500)])

    assert len(images) == 1


def test_very_long_headline_is_truncated_not_overflowed():
    images = build_card_images(sample_copy(cover="가" * 300), [photo_bytes(2000, 1500)])

    assert (open_card(images[0]).width, open_card(images[0]).height) == (CARD_WIDTH, CARD_HEIGHT)


def test_very_long_body_text_stays_inside_card():
    images = build_card_images(sample_copy(cards=[CardSlide(heading="긴 본문 카드", body="나" * 600)]), [])

    assert len(images) == 1
    assert (open_card(images[0]).width, open_card(images[0]).height) == (CARD_WIDTH, CARD_HEIGHT)


def test_empty_cover_is_rejected_rather_than_guessed():
    with pytest.raises(CardNewsError):
        build_card_images(sample_copy(cover="   "), [photo_bytes(2000, 1500)])


def test_blank_body_cards_are_rejected():
    blank = [CardSlide(heading="", body=""), CardSlide(heading="   ", body="   ")]

    with pytest.raises(CardNewsError):
        build_card_images(sample_copy(cards=blank), [photo_bytes(2000, 1500)])


def test_first_photo_is_the_one_that_appears():
    """카드가 한 장이라 사진도 한 장만 쓴다 — 첫 첨부가 그 자리를 갖는다."""
    first_only = build_card_images(sample_copy(), [photo_bytes(2000, 1500, (200, 60, 60))])
    with_spares = build_card_images(
        sample_copy(),
        [photo_bytes(2000, 1500, (200, 60, 60)), photo_bytes(2000, 1500, (60, 200, 60))],
    )

    assert open_card(first_only[0]).tobytes() == open_card(with_spares[0]).tobytes(), "뒤 첨부가 끼어들었다"


def test_trailing_period_never_orphans_onto_its_own_line():
    """마침표 하나만 남은 줄은 오식처럼 보인다(2026-08-10 시안에서 확인).

    한 장 카드는 글자 크기를 분량에 맞춰 줄이므로 줄바꿈 지점이 매번 달라진다 —
    특정 폭 하나가 아니라 넓은 구간을 훑어야 재발을 잡는다.
    """
    from PIL import ImageDraw

    from news_summary.cardnews import _font, _wrap

    draw = ImageDraw.Draw(Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT)))
    text = "농업용수 부족에 대비해 가뭄대책반을 병행 운영하며 농작물 피해 예방에 나섭니다."
    for size in (32, 28, 24):
        font = _font(size, 500)
        for width in range(360, 960, 4):
            lines = _wrap(draw, text, font, width)
            assert lines[-1].strip() != ".", f"글자 {size} · 폭 {width}에서 마침표가 혼자 남았다"


def test_dropping_a_slide_never_leaves_a_heading_without_its_body():
    """자리가 모자라 요점을 덜어낼 때 소제목만 남으면 안 된다.

    남으면 "접수는 이렇게 하시면 됩니다"만 찍히고 접수처·전화번호가 사라진
    카드가 **발행 상태 그대로** 주민에게 나간다. 2026-08-10 검토에서 적발 —
    redraw가 옛 규격(본문 110자) 문안을 무검증으로 넣는 경로에서 재현됐다.
    """
    from PIL import ImageDraw

    from news_summary.cardnews import (
        BODY_HEADING,
        MARGIN,
        MIN_PHOTO_BAND,
        PHOTO_TEXT_GAP,
        SINGLE_FOOTER_HEIGHT,
        _single_blocks,
    )

    draw = ImageDraw.Draw(Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT)))
    room = CARD_HEIGHT - (MIN_PHOTO_BAND + PHOTO_TEXT_GAP) - SINGLE_FOOTER_HEIGHT
    legacy = [CardSlide(heading="가" * 20, body="나" * 105) for _ in range(4)]

    blocks = _single_blocks(draw, "다" * 34, legacy, CARD_WIDTH - MARGIN * 2, room)

    assert blocks, "블록이 통째로 비었다"
    assert blocks[-1][2] != BODY_HEADING, "소제목만 남고 본문이 사라졌다"


def test_only_one_photo_is_fetched_even_when_more_are_attached():
    """카드가 한 장이라 사진도 한 장이면 된다.

    2026-08-10 검토에서 적발 — 4장을 내려받아 4장 다 펼치고 1장만 썼다.
    장당 원본 25MB·펼친 것 14MB라 요청 하나가 512MB에서 위험했다.
    """
    consumed: list[int] = []

    def lazy_photos():
        for index in range(4):
            consumed.append(index)
            yield photo_bytes(2000, 1500)

    build_card_images(sample_copy(), lazy_photos())

    assert consumed == [0], f"쓰지도 않을 사진을 {len(consumed)}장 가져왔다"


def test_tall_photo_narrowed_by_downscaling_is_rejected():
    """긴 변 기준 축소가 폭을 문턱 아래로 되돌린다 — 900x2600은 747x2160이 된다.

    폭 검사를 축소보다 먼저 하면 747을 1080으로, 즉 1.45배 확대하게 된다.
    """
    narrowed = build_card_images(sample_copy(), [photo_bytes(900, 2600)])
    text_only = build_card_images(sample_copy(), [])

    assert open_card(narrowed[0]).tobytes() == open_card(text_only[0]).tobytes(), "축소 뒤 좁아진 사진이 쓰였다"


def test_wide_banner_is_not_used_as_a_photo():
    """기관 배너(1000x120 같은)가 첨부 1번에 붙는 일이 잦다.

    밴드에 넣으면 위아래가 검정으로 남아 사진 자리를 통째로 버린다.
    """
    banner = build_card_images(sample_copy(), [photo_bytes(1000, 120)])
    text_only = build_card_images(sample_copy(), [])

    assert open_card(banner[0]).tobytes() == open_card(text_only[0]).tobytes(), "배너가 사진으로 쓰였다"


def test_repeated_punctuation_does_not_run_past_the_card():
    """문장부호를 줄 끝에 붙이는 규칙에 한도가 없으면 연속 부호가 카드 밖으로 나간다."""
    images = build_card_images(
        sample_copy(cards=[CardSlide(heading="느낌표 시험", body="지금 바로 신청하세요" + "!" * 12)]),
        [],
    )
    card = open_card(images[0]).convert("RGB")

    edge = [card.getpixel((CARD_WIDTH - 4, y)) for y in range(150, CARD_HEIGHT - 150, 10)]
    assert all(abs(p[0] - 17) < 14 and abs(p[1] - 17) < 14 for p in edge), "글자가 오른쪽 끝까지 밀렸다"


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

    # 밴드 높이는 글 분량에 따라 변한다 — 어떤 경우에도 밴드 안인 위쪽에서 잰다.
    y = 100
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
    card = open_card(images[0]).convert("RGB")

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
        "news_summary.cardnews._render_single",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("boom")),
    )

    with pytest.raises(ValueError):
        build_card_images(sample_copy(), [photo_bytes(2000, 1500)])

    assert calls["count"] == 1
