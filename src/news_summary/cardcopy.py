# 기사 초안을 카드뉴스 문안(표지 문구 + 본문 카드)으로 다시 쓰는 모듈
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .cardnews import CardCopy, CardSlide
from .ops_logging import get_logger
from .writer import DEFAULT_GEMINI_MODELS, _dedupe_models, _is_gemini_quota_error, _summarize_gemini_error

logger = get_logger("cardcopy")

# 카드 한 장이 넘어가면 카드뉴스가 아니라 그냥 기사다. 시안에서 4줄이 넘어가는 것을 보고 정했다.
MIN_COVER_CHARS = 6
MAX_COVER_CHARS = 30
MIN_HEADING_CHARS = 4
MAX_HEADING_CHARS = 22
MIN_CARD_CHARS = 25
MAX_CARD_CHARS = 110
MIN_CARDS = 2
MAX_CARDS = 4
MAX_TAGS = 5

SYSTEM_PROMPT = """너는 지역 뉴스 카드뉴스 편집자다. 기사 초안을 주민이 휴대폰에서
훑어보는 카드뉴스 문안으로 바꾼다.

지켜야 할 것:
- 표지 문구는 %(cover_min)d~%(cover_max)d자. 핵심 하나만. 제목을 그대로 베끼지 말고 주민에게
  무엇이 달라지는지 말한다.
- 본문 카드는 %(min_cards)d~%(max_cards)d장. **각 장은 소제목과 본문을 함께 담는다.**
  - 소제목: %(head_min)d~%(head_max)d자. 그 장에서 말하려는 것 한 줄.
  - 본문: %(card_min)d~%(card_max)d자. 소제목을 풀어 설명하되 한 장에 한 가지만.
- 신청 기한, 장소, 대상, 금액처럼 주민이 행동할 때 필요한 정보를 우선한다.
- 원문에 없는 사실을 지어내지 않는다. 숫자와 날짜는 원문 그대로 쓴다.
- 문장은 '~합니다', '~됩니다'처럼 평서형으로 끝낸다.
- 해시태그는 최대 %(max_tags)d개, 지역명과 주제 중심으로.

JSON만 출력한다. 다른 설명을 붙이지 않는다.
{"cover": "...", "cards": [{"heading": "...", "body": "..."}, ...], "tags": ["...", "..."]}
""" % {
    "cover_min": MIN_COVER_CHARS,
    "cover_max": MAX_COVER_CHARS,
    "head_min": MIN_HEADING_CHARS,
    "head_max": MAX_HEADING_CHARS,
    "min_cards": MIN_CARDS,
    "max_cards": MAX_CARDS,
    "card_min": MIN_CARD_CHARS,
    "card_max": MAX_CARD_CHARS,
    "max_tags": MAX_TAGS,
}


class CardCopyError(RuntimeError):
    """문안을 쓸 수 없거나 규격을 못 맞췄을 때. 잘라내지 않고 거절한다."""


@dataclass
class CardCopyRequest:
    title: str
    body: str
    source_label: str = ""
    region: str = ""
    date_label: str = ""


def build_card_copy(
    request: CardCopyRequest,
    api_key: str,
    models: tuple[str, ...] = DEFAULT_GEMINI_MODELS,
    generator=None,
) -> CardCopy:
    """초안을 카드 문안으로 바꾼다. 규격을 못 맞추면 다음 모델로 넘어가고, 끝내 못 맞추면 거절한다."""
    if not request.title.strip() or not request.body.strip():
        raise CardCopyError("제목과 본문이 모두 있어야 문안을 만들 수 있습니다.")

    generate = generator or _generate_with_gemini
    attempted: list[str] = []
    last_error: Exception | None = None
    for model_name in _dedupe_models(list(models)):
        attempted.append(model_name)
        try:
            raw = generate(request, api_key, model_name)
            return _validate(raw, request)
        except CardCopyError as exc:
            # 규격 미달은 모델을 바꾸면 통과하는 일이 잦다 — 다음 모델로 넘어간다.
            logger.warning("card copy rejected model=%s reason=%s", model_name, exc)
            last_error = exc
            continue
        except Exception as exc:  # noqa: BLE001 - 쿼터·네트워크 오류는 다음 모델로 넘긴다.
            logger.warning("card copy failed model=%s error=%s", model_name, exc)
            last_error = exc
            if _is_gemini_quota_error(exc) and model_name == attempted[-1]:
                break
            continue

    detail = str(last_error) if isinstance(last_error, CardCopyError) else _summarize_gemini_error(last_error)
    raise CardCopyError(f"카드 문안 생성 실패 ({', '.join(attempted)}): {detail}")


def _generate_with_gemini(request: CardCopyRequest, api_key: str, model_name: str) -> object:
    try:
        from google import genai
        from google.genai import types
    except ModuleNotFoundError as exc:
        raise RuntimeError("Gemini 라이브러리가 설치되어 있지 않습니다.") from exc

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model_name,
        contents=_user_prompt(request),
        config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
    )
    return response.text or ""


def _user_prompt(request: CardCopyRequest) -> str:
    return (
        f"출처: {request.source_label}\n"
        f"지역: {request.region}\n"
        f"게시일: {request.date_label or '미상'}\n\n"
        f"기사 제목:\n{request.title}\n\n"
        f"기사 본문:\n{request.body}\n"
    )


def _validate(raw: object, request: CardCopyRequest) -> CardCopy:
    """모델 반환값을 믿지 않는다 — 규격을 다시 재고 어긋나면 거절한다.

    자르지 않는 이유는, 잘린 문장이 카드에 박히면 사람이 고치기 더 번거롭기 때문이다.
    """
    payload = _parse_payload(raw)

    cover = _clean(payload.get("cover"))
    if not MIN_COVER_CHARS <= len(cover) <= MAX_COVER_CHARS:
        raise CardCopyError(f"표지 문구 길이가 규격을 벗어났습니다({len(cover)}자).")

    cards_raw = payload.get("cards")
    if not isinstance(cards_raw, list):
        raise CardCopyError("본문 카드가 목록이 아닙니다.")
    cards = [slide for slide in (_slide(item) for item in cards_raw) if slide is not None]
    if not MIN_CARDS <= len(cards) <= MAX_CARDS:
        raise CardCopyError(f"본문 카드 장수가 규격을 벗어났습니다({len(cards)}장).")
    for slide in cards:
        if not MIN_HEADING_CHARS <= len(slide.heading) <= MAX_HEADING_CHARS:
            raise CardCopyError(f"카드 소제목 길이가 규격을 벗어났습니다({len(slide.heading)}자).")
        if not MIN_CARD_CHARS <= len(slide.body) <= MAX_CARD_CHARS:
            raise CardCopyError(f"본문 카드 길이가 규격을 벗어났습니다({len(slide.body)}자).")

    tags_raw = payload.get("tags")
    tags = [_clean(tag).lstrip("#") for tag in tags_raw] if isinstance(tags_raw, list) else []
    tags = [tag for tag in tags if tag][:MAX_TAGS]

    return CardCopy(
        cover=cover,
        cards=cards,
        source_label=request.source_label,
        date_label=request.date_label,
        tags=tags,
    )


def _slide(item: object) -> CardSlide | None:
    """모델이 {소제목, 본문} 대신 문자열 하나를 줄 때도 받아 준다."""
    if isinstance(item, dict):
        heading = _clean(item.get("heading") or item.get("title"))
        body = _clean(item.get("body") or item.get("text"))
    else:
        heading, body = "", _clean(item)
    if not heading and not body:
        return None
    return CardSlide(heading=heading, body=body)


def _parse_payload(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        raise CardCopyError("모델이 빈 응답을 돌려줬습니다.")
    # 모델이 ```json 울타리를 붙이는 일이 잦다.
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    else:
        brace = re.search(r"\{.*\}", text, re.S)
        if brace:
            text = brace.group(0)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CardCopyError("모델 응답을 JSON으로 읽을 수 없습니다.") from exc
    if not isinstance(payload, dict):
        raise CardCopyError("모델 응답이 객체가 아닙니다.")
    return payload


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()
