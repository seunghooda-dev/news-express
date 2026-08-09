# 기사 초안과 첨부 사진을 카드뉴스 이미지로 합성하는 모듈 — 웹·DB에 의존하지 않는다
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageDraw, ImageFont

from .memory import release_free_heap

logger = logging.getLogger(__name__)

# 4:5 — 모바일 세로 화면을 가장 크게 쓴다. 인스타로 확장할 때도 그대로 쓸 수 있다.
CARD_WIDTH = 1080
CARD_HEIGHT = 1350

# 이보다 좁은 사진은 확대하지 않는다 — 확대하면 화질 저하가 그대로 보인다.
# 실측상 원본의 약 40%가 여기에 걸린다(980x735, 600x400 등).
# 판정 축은 **폭**이다. 밴드에 넣을 때 폭을 기준으로 배율을 잡으므로 긴 변으로
# 재면 800x2000 같은 세로 사진이 1.35배로 확대돼 버린다(2026-08-09 검토에서 적발).
MIN_PHOTO_EDGE = 1080

# 카드 폭이 1080이라 원본을 그대로 들고 있을 이유가 없다. 25MB JPEG가 1억 화소면
# RGB로 펼쳐 240MB인데 세트당 4장을 동시에 쥔다 — 이 서비스는 512MB에서 돈다.
MAX_WORKING_EDGE = CARD_WIDTH * 2

COVER_PHOTO_RATIO = 0.58
BODY_PHOTO_RATIO = 0.42
FADE_HEIGHT = 200
MARGIN = 65

COVER_BG = "#111111"
COVER_TEXT = "#FFFFFF"
COVER_META = "#9A9A9A"
# 표지와 본문의 바탕색을 맞춘다 — 넘길 때 흰 배경이 끼면 산만하다(2026-08-09 사용자 지시).
BODY_BG = COVER_BG
BODY_HEADING = "#FFFFFF"
BODY_TEXT = "#C7C7C7"
BODY_META = "#7A7A7A"

MAX_COVER_LINES = 4
MAX_HEADING_LINES = 3
MAX_BODY_LINES = 7
HEADING_GAP = 26

FONT_PATH = Path(__file__).resolve().parent / "static" / "fonts" / "PretendardVariable.woff2"


@dataclass
class CardSlide:
    """본문 카드 한 장 — 소제목과 본문을 함께 담는다.

    처음에는 문장 하나만 넣었는데 카드가 휑하고 무슨 얘긴지 한눈에 안 들어왔다
    (2026-08-09 사용자 지적: "내용이 조금 빈약해"). 소제목이 있으면 넘기면서도
    무슨 내용인지 잡힌다.
    """

    heading: str
    body: str


@dataclass
class CardCopy:
    """카드 한 세트의 문안. 문안 생성 단계(AI)의 산출물이자 합성의 입력이다."""

    cover: str
    cards: list[CardSlide]
    source_label: str = ""
    date_label: str = ""
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # 옛 형태(문자열)와 저장 형태(dict) 모두 받아 준다. 무엇이든 str()로 감싸면
        # dict의 repr이 카드에 그대로 인쇄되므로 형태별로 나눠 읽는다.
        self.cards = [_as_slide(slide) for slide in self.cards]


def _as_slide(value: object) -> CardSlide:
    if isinstance(value, CardSlide):
        return value
    if isinstance(value, dict):
        return CardSlide(heading=str(value.get("heading") or ""), body=str(value.get("body") or ""))
    return CardSlide(heading="", body=str(value))


class CardNewsError(RuntimeError):
    """합성을 시작할 수 없는 입력일 때. 잘라내지 않고 거절한다."""


def build_card_images(copy: CardCopy, photos: Sequence[bytes] = ()) -> list[bytes]:
    """표지 1장 + 본문 N장의 PNG 바이트를 만든다.

    사진이 없거나 해상도가 모자라면 텍스트 중심 배치로 자동 전환한다 — 호출하는
    쪽에서 분기할 필요가 없다.
    """
    if not copy.cover.strip():
        raise CardNewsError("표지 문구가 비어 있습니다.")
    slides = [slide for slide in copy.cards if slide.heading.strip() or slide.body.strip()]
    if not slides:
        raise CardNewsError("본문 카드 문구가 하나도 없습니다.")

    usable = _usable_photos(photos)
    images: list[bytes] = []
    try:
        cover_photo = usable[0] if usable else None
        images.append(_encode(_render_cover(copy, cover_photo)))
        total = len(slides) + 1
        for index, slide in enumerate(slides, start=2):
            # 표지에 쓴 사진은 본문에서 다시 쓰지 않는다 — 같은 사진이 연달아 나오면 지루하다.
            photo = usable[index - 1] if index - 1 < len(usable) else None
            images.append(_encode(_render_body(slide, index, total, photo)))
    finally:
        for photo in usable:
            photo.close()
        # 원본을 펼친 메모리는 여기서 free됐지만 glibc가 쥐고 있다 — 돌려준다.
        release_free_heap()
    return images


def _usable_photos(photos: Sequence[bytes]) -> list[Image.Image]:
    """열리고 해상도가 충분한 사진만 남긴다. 깨진 첨부는 조용히 건너뛴다."""
    usable: list[Image.Image] = []
    for raw in photos:
        if not raw:
            continue
        try:
            image = Image.open(io.BytesIO(raw))
            # JPEG는 디코딩 단계에서 미리 줄여 펼치는 메모리 자체를 아낀다.
            image.draft("RGB", (MAX_WORKING_EDGE, MAX_WORKING_EDGE))
            image.load()
            if image.width < MIN_PHOTO_EDGE:
                logger.info("card news photo too narrow size=%sx%s", image.width, image.height)
                image.close()
                continue
            image = image.convert("RGB")
            if max(image.size) > MAX_WORKING_EDGE:
                image.thumbnail((MAX_WORKING_EDGE, MAX_WORKING_EDGE), Image.LANCZOS)
        except Exception as exc:  # noqa: BLE001 - 첨부 하나가 깨져도 카드는 나와야 한다.
            logger.info("card news photo skipped error=%s", exc)
            continue
        usable.append(image)
    return usable


def _font(size: int, weight: int = 700) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(FONT_PATH), size)
    try:
        font.set_variation_by_axes([weight])
    except Exception:  # noqa: BLE001 - 가변 축을 못 쓰면 기본 굵기로 그린다.
        pass
    return font


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """한글은 단어 경계가 넓어 글자 단위로 접어야 오른쪽이 들쭉날쭉하지 않다."""
    lines: list[str] = []
    current = ""
    for char in text:
        if char == "\n":
            lines.append(current)
            current = ""
            continue
        candidate = current + char
        if draw.textlength(candidate, font=font) > max_width and current:
            lines.append(current)
            current = char
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _fit_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    max_lines: int,
    sizes: Sequence[int],
    weight: int,
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """줄 수에 들어갈 때까지 글자를 줄인다. 그래도 넘치면 마지막 줄을 말줄임한다."""
    font = _font(sizes[-1], weight)
    lines: list[str] = []
    for size in sizes:
        font = _font(size, weight)
        lines = _wrap(draw, text, font, max_width)
        if len(lines) <= max_lines:
            return font, lines
    lines = lines[:max_lines]
    if lines:
        lines[-1] = lines[-1][:-1] + "…"
    return font, lines


def _photo_band(photo: Image.Image, height: int, background: str) -> Image.Image:
    """사진을 **폭에 맞춰** 밴드에 넣는다. 좌우는 어떤 경우에도 자르지 않는다.

    지자체 사진은 표본 8장이 전부 가로형(1.33~1.92)이라 좌우를 자르면 현장과
    인물이 날아간다. 폭을 먼저 맞추고,

    - 사진이 밴드보다 높으면 위아래만 가운데 기준으로 잘라낸다(1.33 사진 기준 29px).
    - 사진이 밴드보다 낮으면(1.92처럼 아주 넓은 사진) 남는 위아래를 배경으로 채운다.

    처음에는 max 배율로 밴드를 꽉 채웠는데, 그러면 넓은 사진의 좌우가 잘려
    "자르지 않는다"는 설계가 거짓이 됐다(2026-08-09 갭 분석에서 적발).
    """
    scale = CARD_WIDTH / photo.width
    new_height = max(1, int(photo.height * scale))
    resized = photo.resize((CARD_WIDTH, new_height), Image.LANCZOS)
    band = Image.new("RGB", (CARD_WIDTH, height), background)
    if new_height >= height:
        top = (new_height - height) // 2
        band.paste(resized.crop((0, top, CARD_WIDTH, top + height)), (0, 0))
    else:
        band.paste(resized, (0, (height - new_height) // 2))
    resized.close()
    return band


def _paste_with_fade(canvas: Image.Image, band: Image.Image, bg: str) -> None:
    """사진 아래쪽을 배경색으로 서서히 녹여 글자가 사진 위로 얹힌 티가 안 나게 한다."""
    canvas.paste(band, (0, 0))
    gradient = Image.new("L", (1, FADE_HEIGHT))
    for y in range(FADE_HEIGHT):
        gradient.putpixel((0, y), int(255 * y / FADE_HEIGHT))
    mask = gradient.resize((CARD_WIDTH, FADE_HEIGHT))
    overlay = Image.new("RGB", (CARD_WIDTH, FADE_HEIGHT), bg)
    canvas.paste(overlay, (0, band.height - FADE_HEIGHT), mask)
    gradient.close()
    mask.close()
    overlay.close()


def _render_cover(copy: CardCopy, photo: Image.Image | None) -> Image.Image:
    canvas = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), COVER_BG)
    draw = ImageDraw.Draw(canvas)
    max_width = CARD_WIDTH - MARGIN * 2

    if photo is not None:
        band_height = int(CARD_HEIGHT * COVER_PHOTO_RATIO)
        band = _photo_band(photo, band_height, COVER_BG)
        _paste_with_fade(canvas, band, COVER_BG)
        band.close()
        text_top = band_height + 60
        sizes = (72, 64, 56)
    else:
        # 사진이 없거나 작으면 글자를 키워 화면을 채운다 — 확대한 사진보다 낫다.
        text_top = 300
        sizes = (92, 84, 76)

    font, lines = _fit_lines(draw, copy.cover, max_width, MAX_COVER_LINES, sizes, 800)
    line_height = int(font.size * 1.28)
    y = text_top
    for line in lines:
        draw.text((MARGIN, y), line, font=font, fill=COVER_TEXT)
        y += line_height

    meta = " · ".join(part for part in (copy.source_label, copy.date_label) if part)
    if meta:
        draw.rectangle([MARGIN, CARD_HEIGHT - 150, MARGIN + 90, CARD_HEIGHT - 144], fill=COVER_TEXT)
        draw.text((MARGIN, CARD_HEIGHT - 110), meta, font=_font(30, 500), fill=COVER_META)
    return canvas


def _render_body(slide: CardSlide, index: int, total: int, photo: Image.Image | None) -> Image.Image:
    """본문 카드. 표지와 같은 검은 바탕을 쓴다 — 넘길 때 배경이 바뀌면 산만하다."""
    canvas = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), BODY_BG)
    draw = ImageDraw.Draw(canvas)
    max_width = CARD_WIDTH - MARGIN * 2

    if photo is not None:
        band_height = int(CARD_HEIGHT * BODY_PHOTO_RATIO)
        band = _photo_band(photo, band_height, BODY_BG)
        _paste_with_fade(canvas, band, BODY_BG)
        band.close()
        text_area_top = band_height + 20
        heading_sizes = (58, 52, 46)
        body_sizes = (44, 40, 36)
    else:
        text_area_top = 0
        heading_sizes = (68, 60, 54)
        body_sizes = (50, 46, 42)

    blocks: list[tuple[ImageFont.FreeTypeFont, list[str], str, int]] = []
    if slide.heading.strip():
        font, lines = _fit_lines(draw, slide.heading, max_width, MAX_HEADING_LINES, heading_sizes, 800)
        blocks.append((font, lines, BODY_HEADING, int(font.size * 1.3)))
    if slide.body.strip():
        font, lines = _fit_lines(draw, slide.body, max_width, MAX_BODY_LINES, body_sizes, 500)
        blocks.append((font, lines, BODY_TEXT, int(font.size * 1.6)))

    block_height = sum(len(lines) * step for _, lines, _, step in blocks)
    block_height += HEADING_GAP * (len(blocks) - 1) if len(blocks) > 1 else 0
    available = CARD_HEIGHT - text_area_top - 130
    # 하한만 두면 블록이 available보다 클 때 글자가 카드 밖으로 흘러 장수 표시와 겹친다
    # (2026-08-09 검토에서 적발 — 줄 수는 통과하는데 높이가 넘치는 대역이 있다).
    y = text_area_top + min(max(60, (available - block_height) // 2), max(0, available - block_height))
    for order, (font, lines, colour, step) in enumerate(blocks):
        if order:
            y += HEADING_GAP
        for line in lines:
            draw.text((MARGIN, y), line, font=font, fill=colour)
            y += step

    label = f"{index} / {total}"
    label_font = _font(28, 600)
    draw.text(
        (CARD_WIDTH - MARGIN - draw.textlength(label, font=label_font), CARD_HEIGHT - 90),
        label,
        font=label_font,
        fill=BODY_META,
    )
    return canvas


def _encode(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    image.close()
    return buffer.getvalue()
