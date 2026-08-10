# 기사 초안과 첨부 사진을 카드뉴스 이미지로 합성하는 모듈 — 웹·DB에 의존하지 않는다
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable

from PIL import Image, ImageDraw, ImageFont

from .memory import release_free_heap

logger = logging.getLogger(__name__)

# 4:5 — 모바일 세로 화면을 가장 크게 쓴다. 인스타로 확장할 때도 그대로 쓸 수 있다.
CARD_WIDTH = 1080
CARD_HEIGHT = 1350

# 이보다 좁은 사진은 쓰지 않는다 — 많이 확대하면 화질 저하가 그대로 보인다.
# 판정 축은 **폭**이다. 밴드에 넣을 때 폭을 기준으로 배율을 잡으므로 긴 변으로
# 재면 700x2000 같은 세로 사진이 확대돼 버린다(2026-08-09 검토에서 적발).
#
# 처음에는 카드 폭(1080)을 그대로 문턱으로 삼아 확대를 0으로 막았다. 그런데
# **사진이 붙은 초안의 43%가 글자만 남았다** — 2026-08-07 금요일 전량 실측에서
# 사진 있는 110건 중 47건이 그랬고, 점수 상위 3건이 전부 여기 걸렸다. 지자체
# CMS가 1000·980·780px로 줄여 내보내는 탓이다(가장 흔한 폭이 1000px).
# 사진이 아예 없는 카드가 손해가 더 크므로 1.35배까지는 확대를 허용한다.
# 문턱 800px이면 110건 중 90건(82%)에 사진이 실린다.
MIN_PHOTO_EDGE = 800

# 카드 폭이 1080이라 원본을 그대로 들고 있을 이유가 없다. 25MB JPEG가 1억 화소면
# RGB로 펼쳐 240MB인데 세트당 4장을 동시에 쥔다 — 이 서비스는 512MB에서 돈다.
MAX_WORKING_EDGE = CARD_WIDTH * 2

# 펼치기 전에 거르는 화소 상한. RGB 한 벌이 W×H×3이고 convert가 사본을 하나 더
# 만드니 12MP면 약 72MB다. 카드는 2160px까지만 쓰므로 그 위는 어차피 줄인다 —
# 지자체 첨부는 대개 1MP 남짓이고, 큰 DSLR 사진은 JPEG라 draft가 먼저 줄인다.
MAX_DECODE_PIXELS = 12_000_000

# 이보다 납작하면 사진이 아니라 배너다. 밴드(폭:높이 최대 2.35:1)에 넣어 봐야
# 위아래가 검정으로 남는다.
MAX_PHOTO_ASPECT = 3.0

# 사진이 위, 요점이 아래. 글이 짧은 기사는 사진을 키워 빈자리를 없앤다.
MIN_PHOTO_BAND = int(CARD_HEIGHT * 0.34)
MAX_PHOTO_BAND = int(CARD_HEIGHT * 0.52)
PHOTO_TEXT_GAP = 30
# 출처·날짜가 앉을 자리. 구분선을 CARD_HEIGHT-150에 긋고 그 위로 40px을 비워 둔다 —
# 130으로 뒀더니 마지막 줄이 구분선을 뚫고 지나갔다(2026-08-10 시안에서 확인).
SINGLE_FOOTER_HEIGHT = 190
FADE_HEIGHT = 200
MARGIN = 65

COVER_BG = "#111111"
COVER_TEXT = "#FFFFFF"
COVER_META = "#9A9A9A"
BODY_HEADING = "#FFFFFF"
BODY_TEXT = "#C7C7C7"

MAX_COVER_LINES = 3
# (제목, 소제목, 본문) 글자 크기 사다리. 큰 것부터 넣어 보고 안 들어가면 줄인다 —
# 요점이 2개인 기사와 4개인 기사가 같은 카드 높이를 나눠 써야 하기 때문이다.
SINGLE_SIZE_STEPS = ((58, 38, 32), (52, 35, 30), (46, 32, 28), (42, 30, 26), (38, 28, 24), (34, 26, 22))
# 요점과 요점 사이, 그리고 소제목과 그 본문 사이의 간격.
ITEM_GAP = 30
HEADING_GAP = 8
TITLE_GAP = 34
# 줄 끝에서 혼자 넘어가면 안 되는 글자들.
TRAILING_PUNCTUATION = ".,!?)]}』」”’%"

FONT_PATH = Path(__file__).resolve().parent / "static" / "fonts" / "PretendardVariable.woff2"


@dataclass
class CardSlide:
    """카드에 실리는 요점 하나 — 소제목과 본문을 함께 담는다.

    처음에는 문장 하나만 넣었는데 카드가 휑하고 무슨 얘긴지 한눈에 안 들어왔다
    (2026-08-09 사용자 지적: "내용이 조금 빈약해"). 소제목이 있으면 훑기만 해도
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
    # 어느 모델이 쓴 문안인지. 합성에는 안 쓰이지만 **이 단계의 산출물**이라 여기 붙인다 —
    # 예비 모델로 넘어간 것을 운영자가 알아야 한다(2026-08-10 실측: lite는 본문의 52%가
    # 50자를 넘고 소제목 71%가 명사로 끝난다).
    model: str = ""

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


def build_card_images(
    copy: CardCopy,
    photos: Iterable[bytes] = (),
    *,
    on_photo: Callable[[bool], None] | None = None,
) -> list[bytes]:
    """기사 한 건을 **카드 한 장**으로 만든다.

    전에는 표지 1장 + 본문 N장으로 넘겨 봤는데, 넘기지 않으면 첫 장의 제목만 남고
    정작 신청 기한·대상이 안 읽혔다(2026-08-10 사용자 지시: "사진 한 장에 기사를
    전부 축약해서 넣어"). 한 장이면 사진과 요점이 같은 화면에 남는다.

    반환은 목록을 유지한다 — 저장·표시 경로가 이미 목록을 받아 쓴다.
    사진이 없거나 해상도가 모자라면 글자 중심 배치로 자동 전환한다.
    """
    if not copy.cover.strip():
        raise CardNewsError("표지 문구가 비어 있습니다.")
    slides = [slide for slide in copy.cards if slide.heading.strip() or slide.body.strip()]
    if not slides:
        raise CardNewsError("본문 카드 문구가 하나도 없습니다.")

    # 카드가 한 장이니 사진도 한 장이면 된다. photos가 지연 생성이면 여기서
    # 멈추는 만큼 내려받기도 멈춘다.
    usable = _usable_photos(photos, limit=1)
    if on_photo is not None:
        # 결과를 밖으로 알린다. 반환형(목록)을 바꾸지 않으려고 콜백을 쓴다 —
        # 부르는 쪽 전부와 테스트가 이미 목록을 받아 쓰고 있다.
        on_photo(bool(usable))
    try:
        return [_encode(_render_single(copy, slides, usable[0] if usable else None))]
    finally:
        for photo in usable:
            photo.close()
        # 원본을 펼친 메모리는 여기서 free됐지만 glibc가 쥐고 있다 — 돌려준다.
        release_free_heap()


def draft_target(size: tuple[int, int], max_edge: int) -> tuple[int, int]:
    """`draft()`에 넘길 **가로세로비를 지킨** 목표 크기(썸네일 경로와 공유한다)."""
    width, height = size
    if width <= 0 or height <= 0:
        return (max_edge, max_edge)
    longest = max(width, height)
    if longest <= max_edge:
        return (width, height)
    scale = max_edge / longest
    return (max(1, int(width * scale)), max(1, int(height * scale)))


def _draft_target(size: tuple[int, int]) -> tuple[int, int]:
    """`draft()`에 넘길 **가로세로비를 지킨** 목표 크기.

    정사각 상자를 넘기면 축소가 통째로 무산된다. `draft`는 "요청 크기보다 작아지지
    않는" 최대 축소만 고르는데, 6000x4000에 (2160, 2160)을 주면 1/2인 3000x2000의
    세로가 2160보다 작아 **1/1이 선택된다** — 24MP가 그대로 펼쳐진다(2026-08-11
    적발, 기존 회귀 테스트가 잡아 줬다). 긴 변만 맞춘 상자를 주면 1/2가 골라진다.
    """
    return draft_target(size, MAX_WORKING_EDGE)


def _usable_photos(photos: Iterable[bytes], limit: int | None = None) -> list[Image.Image]:
    """열리고 쓸 만한 사진만 남긴다. 깨진 첨부는 조용히 건너뛴다.

    limit을 주면 그만큼 찾는 즉시 멈춘다. photos가 지연 생성이면 **뒤 첨부는
    내려받지도 않는다** — 카드가 한 장이라 사진도 한 장이면 충분한데, 4장을 받아
    4장 다 펼치고 있었다(2026-08-10 검토에서 적발, 요청당 최대 165MB).
    """
    usable: list[Image.Image] = []
    for raw in photos:
        if not raw:
            continue
        try:
            image = Image.open(io.BytesIO(raw))
            # JPEG는 디코딩 단계에서 미리 줄여 펼치는 메모리 자체를 아낀다.
            image.draft("RGB", _draft_target(image.size))
            # draft는 **JPEG에만** 구현돼 있다(PNG·WEBP·TIFF는 무동작). 그래서 여기서
            # 재는 크기는 JPEG면 이미 줄어든 값, 그 밖이면 원본 그대로다 — 위험한
            # 경우만 정확히 걸린다. 25MP 실측: JPEG +70.9MB / PNG **+199.2MB**
            # (파일 75.5MB · 피크 291.2MB). 이 서비스는 512MB에서 돈다.
            if image.width * image.height > MAX_DECODE_PIXELS:
                logger.info(
                    "card news photo too many pixels size=%sx%s format=%s",
                    image.width,
                    image.height,
                    image.format,
                )
                image.close()
                continue
            image.load()
            image = image.convert("RGB")
            if max(image.size) > MAX_WORKING_EDGE:
                # 축소를 **폭 검사보다 먼저** 한다. 뒤에 두면 긴 변 기준 축소가
                # 폭을 문턱 아래로 되돌려, 막아 둔 확대가 되살아난다
                # (900x2600 → 747x2160 → 1.45배, 2026-08-10 검토에서 적발).
                image.thumbnail((MAX_WORKING_EDGE, MAX_WORKING_EDGE), Image.LANCZOS)
            if image.width < MIN_PHOTO_EDGE:
                logger.info("card news photo too narrow size=%sx%s", image.width, image.height)
                image.close()
                continue
            if image.width > image.height * MAX_PHOTO_ASPECT:
                # 기관 배너(1000x120 같은)가 첨부 1번에 붙는 일이 잦다. 밴드에 넣으면
                # 위아래가 검정으로 남아 사진 자리를 통째로 버린다.
                logger.info("card news photo too wide size=%sx%s", image.width, image.height)
                image.close()
                continue
        except Exception as exc:  # noqa: BLE001 - 첨부 하나가 깨져도 카드는 나와야 한다.
            logger.info("card news photo skipped error=%s", exc)
            continue
        usable.append(image)
        if limit is not None and len(usable) >= limit:
            break
    return usable


@lru_cache(maxsize=64)
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
            # 마침표 하나만 다음 줄로 떨어지면 오식처럼 보인다 — 문장부호는 그 줄에 붙인다.
            # 다만 여백(65px) 안에서만 봐준다. 한도 없이 붙이면 "신청하세요!!!!!"처럼
            # 부호가 연달아 올 때 글자가 카드 밖으로 잘려 나간다(2026-08-10 검토에서 적발).
            if char in TRAILING_PUNCTUATION and draw.textlength(candidate, font=font) <= max_width + MARGIN:
                current = candidate
                continue
            lines.append(current)
            current = char
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


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


Block = tuple[ImageFont.FreeTypeFont, list[str], str, int, int]
"""(글꼴, 줄들, 색, 줄 간격, 위쪽 여백) — 한 덩어리를 그리는 데 필요한 전부."""


def _render_single(copy: CardCopy, slides: list[CardSlide], photo: Image.Image | None) -> Image.Image:
    """사진 한 장 아래에 제목과 요점 전부를 얹는다."""
    canvas = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), COVER_BG)
    draw = ImageDraw.Draw(canvas)
    max_width = CARD_WIDTH - MARGIN * 2

    if photo is not None:
        # 먼저 사진을 가장 작게 잡고 글을 앉혀 실제 높이를 잰 뒤, 남는 자리를 사진에 준다.
        # 요점이 둘뿐인 기사에서 아래쪽이 휑하게 비는 것을 막는다.
        room = CARD_HEIGHT - (MIN_PHOTO_BAND + PHOTO_TEXT_GAP) - SINGLE_FOOTER_HEIGHT
        blocks = _single_blocks(draw, copy.cover, slides, max_width, room)
        slack = room - _blocks_height(blocks)
        band_height = min(MAX_PHOTO_BAND, MIN_PHOTO_BAND + max(0, slack))
        band = _photo_band(photo, band_height, COVER_BG)
        _paste_with_fade(canvas, band, COVER_BG)
        band.close()
        text_top = band_height + PHOTO_TEXT_GAP
    else:
        # 사진이 없으면 그 자리를 글이 쓴다 — 확대한 사진보다 낫다.
        text_top = 110
        blocks = _single_blocks(draw, copy.cover, slides, max_width, CARD_HEIGHT - text_top - SINGLE_FOOTER_HEIGHT)

    y = text_top
    for font, lines, colour, step, gap in blocks:
        y += gap
        for line in lines:
            draw.text((MARGIN, y), line, font=font, fill=colour)
            y += step

    meta = " · ".join(part for part in (copy.source_label, copy.date_label) if part)
    if meta:
        draw.rectangle([MARGIN, CARD_HEIGHT - 150, MARGIN + 90, CARD_HEIGHT - 144], fill=COVER_TEXT)
        draw.text((MARGIN, CARD_HEIGHT - 110), meta, font=_font(30, 500), fill=COVER_META)
    return canvas


def _single_blocks(
    draw: ImageDraw.ImageDraw,
    title: str,
    slides: list[CardSlide],
    max_width: int,
    available: int,
) -> list[Block]:
    """제목과 요점 전부가 available 안에 들어가는 배치를 고른다.

    큰 글자부터 넣어 보고 넘치면 다음 단계로 줄인다. 가장 작은 단계로도 안 되면
    뒤 요점부터 덜어낸다 — **어떤 경우에도 카드 밖으로 글자를 흘리지 않는다.**
    """
    groups: list[list[Block]] = []
    for title_size, head_size, body_size in SINGLE_SIZE_STEPS:
        title_font = _font(title_size, 800)
        title_lines = _wrap(draw, title, title_font, max_width)
        if len(title_lines) > MAX_COVER_LINES:
            # 딱 잘라 두면 낱말이 끊긴 채 끝나 오식처럼 보인다.
            title_lines = title_lines[:MAX_COVER_LINES]
            title_lines[-1] = title_lines[-1][:-1] + "…"
        groups = [[(title_font, title_lines, COVER_TEXT, int(title_size * 1.26), 0)]]

        for index, slide in enumerate(slides):
            # 소제목과 본문을 **한 덩어리로 묶는다.** 따로 두면 아래 덜어내기가
            # 본문만 지워 소제목이 고아로 남는다(2026-08-10 검토에서 적발).
            group: list[Block] = []
            gap = TITLE_GAP if not index else ITEM_GAP
            if slide.heading.strip():
                font = _font(head_size, 800)
                group.append((font, _wrap(draw, slide.heading, font, max_width), BODY_HEADING, int(head_size * 1.3), gap))
                gap = HEADING_GAP
            if slide.body.strip():
                font = _font(body_size, 500)
                group.append((font, _wrap(draw, slide.body, font, max_width), BODY_TEXT, int(body_size * 1.5), gap))
            if group:
                groups.append(group)

        if _groups_height(groups) <= available:
            return [block for group in groups for block in group]

    # 가장 작은 글자로도 안 들어간다 — redraw가 옛 규격(본문 110자) 문안을 그대로
    # 넣는 경로에서 실제로 생긴다. **요점 단위로** 덜어내야 반쪽짜리가 안 남는다.
    while len(groups) > 1 and _groups_height(groups) > available:
        dropped = groups.pop()
        logger.warning(
            "card news slide dropped to fit lines=%s",
            [line for _, lines, _, _, _ in dropped for line in lines][:2],
        )
    return [block for group in groups for block in group]


def _groups_height(groups: list[list[Block]]) -> int:
    return sum(_blocks_height(group) for group in groups)


def _blocks_height(blocks: list[Block]) -> int:
    return sum(gap + len(lines) * step for _, lines, _, step, gap in blocks)


def _encode(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    image.close()
    return buffer.getvalue()
