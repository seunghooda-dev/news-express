# 기사 초안을 카드뉴스 문안(표지 문구 + 본문 카드)으로 다시 쓰는 모듈
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .cardnews import CardCopy, CardSlide
from .ops_logging import get_logger
from .writer import DEFAULT_GEMINI_MODELS, _dedupe_models, _is_gemini_quota_error, _summarize_gemini_error

logger = get_logger("cardcopy")

# 기사 한 건이 카드 **한 장**에 다 들어간다(2026-08-10 사용자 지시). 제목과 요점
# 전부가 사진 아래 660px 남짓을 나눠 쓰므로, 여러 장으로 넘기던 때보다 훨씬 짧다 —
# 본문 상한을 110자 그대로 두면 요점 네 개가 카드 높이를 두 배로 넘긴다.
MIN_COVER_CHARS = 6
MAX_COVER_CHARS = 36
MIN_HEADING_CHARS = 4
MAX_HEADING_CHARS = 16
MIN_CARD_CHARS = 20
# 상한은 배치가 실제로 못 견디는 선이어야 한다. 실측(2026-08-10): 요점이 2~3개면
# 120자까지도 안 잘리고, 4개일 때 100자가 한계다. 55로 뒀더니 57자 문안이 통째로
# 거절돼 세트가 안 나왔다 — 길이 취향은 프롬프트의 목표치(30~45자)가 잡는다.
MAX_CARD_CHARS = 70
MIN_CARDS = 2
MAX_CARDS = 4
MAX_TAGS = 5

SYSTEM_PROMPT = """너는 지역 뉴스 카드뉴스 편집자다. 기사 초안을 주민이 휴대폰에서
한눈에 읽는 **카드 한 장**의 문안으로 바꾼다.

## 카드는 한 장뿐이다
사진 한 장 아래에 제목과 요점이 전부 들어간다. 넘길 다음 장이 없으므로
**여기서 빠진 사실은 주민에게 전달되지 않는다.** 요점만 읽고도 기사를 다 읽은
셈이 되어야 한다 — 누가, 무엇을, 언제까지, 어디서 하는지가 남아야 한다.

## 길이 — 목표치를 맞추고, 상한은 절대 넘지 않는다
글자 수는 공백과 문장부호를 포함해 센다.

- 제목: **18~26자를 목표**로. 어떤 경우에도 %(cover_max)d자를 넘지 않는다.
- 소제목: **8~14자를 목표**로. 어떤 경우에도 %(head_max)d자를 넘지 않는다.
- 본문: **30~45자로 쓴다. 50자를 넘으면 실패다.** %(card_max)d자는 검사기가
  거절하는 선일 뿐 목표가 아니다. 한 문장으로 끝내라.

상한은 "채워야 할 칸"이 아니라 "넘으면 안 되는 선"이다. 한 장에 다 넣어야 하므로
길게 쓰면 글자가 작아져 읽히지 않는다.

## 구성
- 요점은 %(min_cards)d~%(max_cards)d개. **각 요점은 소제목과 본문을 함께 담는다.**
- 개수는 **세어서** 정한다. 원문에 있는 것만 `slots`에 적고 그 개수로 정한다.
  **3개 이상이면 4개, 1~2개면 3개, 하나도 없고 문단이 2개 이하면 2개.**
  샐 항목은 넷이다 — `자격`, `일정`, `비용`, `접수처`.
- 요점끼리 같은 말을 반복하지 마라. 한 장에서는 중복이 바로 눈에 띈다.
- 제목은 기사 제목을 베끼지 말고 주민에게 무엇이 달라지는지 말한다.
- **제목에는 직함·기관 정식명칭을 넣지 않는다** — 이름만 쓰거나 통째로 뺀다.
  글자 수를 맞추려고 '전남광주통합특별시장'을 '시장'으로 줄이는 일이 없게 한다.

## 소제목은 보도자료 제목투를 피한다
'~점검', '~추진', '~논의'처럼 명사로 끝나는 관공서 제목투로만 채우지 마라.
한 카드 안에서 형태를 섞는다.

- 숫자·금액·기한을 앞세운 것 — "신청은 8월 21일까지"
- 주민에게 말 거는 것 — "수강료도 재료비도 없습니다"
- 상황을 그리는 짧은 문장 — "쉼터 600곳이 문을 열었습니다"

**형태를 섞어라.** 문장으로 끝나는 소제목은 **한 개 또는 두 개까지만** 쓰고,
나머지는 숫자나 명사로 끝낸다. 전부 같은 형태면 실패다.

위 예시는 **형태만 참고**한다 — 예시에 쓰인 낱말을 원문 대신 가져다 쓰지 마라
("교육비"를 "수강료"로 바꾸는 식).

## 사실을 바꾸지 않는다
- 원문에 없는 사실을 지어내지 않는다.
- **'여', '약', '내외' 같은 어림 표현을 지우지 않는다** — "600여 곳"을 "600곳"으로 줄이지 마라.
- **기관·부서·직위 명칭은 원문 표기 그대로** 쓴다. **줄이지도 늘이지도 않는다** —
  원문이 '군수'면 '신안군수'로 늘리지 말고, '광양시청 시민홀'을 '시청 시민홀'로
  줄이지 마라. 자리가 모자라면 그 항목을 통째로 뺀다.
- **소제목에서 자격 한정을 떨어뜨리지 마라.** 원문이 "○○시민이라면 누구나"면
  소제목도 "○○시민 누구나"다 — "누구나 신청 가능합니다"로 줄이면 타지역 주민이
  오해한다. 자격을 다 못 담으면 소제목에서 자격 얘기를 빼고 다른 것을 말한다.
- **'선착순', '추첨', '조기 마감', '누구나'는 원문에 그 낱말이 있을 때만 쓴다.**
  원문에 접수 방식이 없으면 **"방문 또는 전화로 신청하면 됩니다"처럼 원문에 적힌
  경로만** 쓰고 끝낸다. 정원만 적혀 있으면 정원만 쓴다.
- **원문이 '논의했다·검토한다·공감했다'면 그 단계를 넘기지 않는다.** 제목에서도
  '시작됩니다', '확정됐습니다', '달라집니다'로 승격하지 마라.
- **원문의 '방침·예정·계획·검토' 어미를 문안에도 남긴다** — "넓혀갈 방침입니다"를
  "넓힙니다"로 단정하지 마라. 날짜 조사도 그대로다("11일까지"를 "11일에"로 바꾸지 마라).
- **날짜에 없는 월·연도를 채워 넣지 않는다.** 원문이 "오는 28일"이면 문안도 "오는 28일"이다.
- 누가 한 일인지 원문에 있으면 **최소 한 요점에는 주체를 남긴다**(이름과 직함, 없으면 기관명).
  주어 없이 '점검했습니다', '확인했습니다'로 끝내지 마라.

## 그 밖에
- 신청 기한, 장소, 대상, 금액처럼 주민이 행동할 때 필요한 정보를 우선한다.
- 문장은 '~합니다', '~됩니다'처럼 평서형으로 끝낸다.
- 해시태그는 최대 %(max_tags)d개, 지역명과 주제 중심으로.

JSON만 출력한다. 다른 설명을 붙이지 않는다.
{"slots": ["자격", "일정"], "cover": "...",
 "cards": [{"heading": "...", "body": "..."}, ...], "tags": ["...", "..."]}
""" % {
    "cover_max": MAX_COVER_CHARS,
    "head_max": MAX_HEADING_CHARS,
    "min_cards": MIN_CARDS,
    "max_cards": MAX_CARDS,
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
    candidates = _dedupe_models(list(models))
    attempted: list[str] = []
    last_error: Exception | None = None
    for index, model_name in enumerate(candidates):
        # 좋은 모델을 일시 장애 한 번으로 버리면 규격을 잘 못 맞추는 예비 모델만 남는다.
        # 다만 **쿼터 소진에는 재시도가 해롭다** — 한도만 더 빨리 깎는다(2026-08-09 실측:
        # flash 일일 한도 소진 상태에서 재시도가 429를 7번 더 불렀다).
        for attempt in range(2):
            attempted.append(model_name)
            try:
                raw = generate(request, api_key, model_name)
                return _validate(raw, request)
            except CardCopyError as exc:
                logger.warning("card copy rejected model=%s reason=%s", model_name, exc)
                last_error = exc
                continue
            except Exception as exc:  # noqa: BLE001 - 다음 시도나 다음 모델로 넘긴다.
                logger.warning("card copy failed model=%s error=%s", model_name, exc)
                last_error = exc
                if _is_gemini_quota_error(exc):
                    break
                continue
        if _is_gemini_quota_error(last_error) and index == len(candidates) - 1:
            break

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
    """모델 응답을 슬라이드로 읽는다.

    문자열 하나만 온 경우도 본문으로 받아 두지만, 소제목이 비어 있으므로
    바로 뒤 규격 검사에서 거절된다 — 그 편이 "구 스키마로 답했다"는 원인이
    메시지에 드러난다.
    """
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
