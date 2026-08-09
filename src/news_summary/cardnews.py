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

# 이보다 작은 사진은 확대하지 않는다 — 확대하면 화질 저하가 그대로 보인다.
# 실측상 원본의 약 40%가 여기에 걸린다(980x735, 600x400 등).
MIN_PHOTO_EDGE = 1080

COVER_PHOTO_RATIO = 0.58
BODY_PHOTO_RATIO = 0.42
FADE_HEIGHT = 200
MARGIN = 65

COVER_BG = "#111111"
COVER_TEXT = "#FFFFFF"
COVER_META = "#9A9A9A"
BODY_BG = "#F5F5F3"
BODY_TEXT = "#141414"
BODY_META = "#8A8A8A"

MAX_COVER_LINES = 4
MAX_BODY_LINES = 8

FONT_PATH = Path(__file__).resolve().parent / "static" / "fonts" / "PretendardVariable.woff2"


@dataclass
class CardCopy:
    """카드 한 세트의 문안. 문안 생성 단계(AI)의 산출물이자 합성의 입력이다."""

    cover: str
    cards: list[str]
    source_label: str = ""
    date_label: str = ""
    tags: list[str] = field(default_factory=list)


class CardNewsError(RuntimeError):
    """합성을 시작할 수 없는 입력일 때. 잘라내지 않고 거절한다."""


def build_card_images(copy: CardCopy, photos: Sequence[bytes] = ()) -> list[bytes]:
    """표지 1장 + 본문 N장의 PNG 바이트를 만든다.

    사진이 없거나 해상도가 모자라면 텍스트 중심 배치로 자동 전환한다 — 호출하는
    쪽에서 분기할 필요가 없다.
    """
    if not copy.cover.strip():
        raise CardNewsError("표지 문구가 비어 있습니다.")
    body_texts = [text for text in copy.cards if text and text.strip()]
    if not body_texts:
        raise CardNewsError("본문 카드 문구가 하나도 없습니다.")

    usable = _usable_photos(photos)
    images: list[bytes] = []
    try:
        cover_photo = usable[0] if usable else None
        images.append(_encode(_render_cover(copy, cover_photo)))
        total = len(body_texts) + 1
        for index, text in enumerate(body_texts, start=2):
            # 표지에 쓴 사진은 본문에서 다시 쓰지 않는다 — 같은 사진이 연달아 나오면 지루하다.
            photo = usable[index - 1] if index - 1 < len(usable) else None
            images.append(_encode(_render_body(text, index, total, photo)))
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
            image.load()
            image = image.convert("RGB")
        except Exception as exc:  # noqa: BLE001 - 첨부 하나가 깨져도 카드는 나와야 한다.
            logger.info("card news photo skipped error=%s", exc)
            continue
        if max(image.size) < MIN_PHOTO_EDGE:
            logger.info("card news photo too small size=%sx%s", image.width, image.height)
            image.close()
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


def _cover_photo_band(photo: Image.Image, height: int) -> Image.Image:
    """가로 사진을 자르지 않고 폭에 맞춘 뒤 위에서부터 필요한 만큼만 쓴다."""
    scale = max(CARD_WIDTH / photo.width, height / photo.height)
    resized = photo.resize((max(1, int(photo.width * scale)), max(1, int(photo.height * scale))), Image.LANCZOS)
    left = max(0, (resized.width - CARD_WIDTH) // 2)
    top = max(0, (resized.height - height) // 2)
    band = resized.crop((left, top, left + CARD_WIDTH, top + height))
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
        band = _cover_photo_band(photo, band_height)
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


def _render_body(text: str, index: int, total: int, photo: Image.Image | None) -> Image.Image:
    canvas = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), BODY_BG)
    draw = ImageDraw.Draw(canvas)
    max_width = CARD_WIDTH - MARGIN * 2

    if photo is not None:
        band_height = int(CARD_HEIGHT * BODY_PHOTO_RATIO)
        band = _cover_photo_band(photo, band_height)
        canvas.paste(band, (0, 0))
        band.close()
        text_area_top = band_height
        sizes = (52, 46, 40)
    else:
        text_area_top = 0
        sizes = (60, 54, 48)

    font, lines = _fit_lines(draw, text, max_width, MAX_BODY_LINES, sizes, 600)
    line_height = int(font.size * 1.5)
    # 남은 공간 가운데에 둔다 — 아래가 휑하게 비지 않는다.
    block_height = line_height * len(lines)
    available = CARD_HEIGHT - text_area_top - 120
    y = text_area_top + max(60, (available - block_height) // 2)
    for line in lines:
        draw.text((MARGIN, y), line, font=font, fill=BODY_TEXT)
        y += line_height

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
