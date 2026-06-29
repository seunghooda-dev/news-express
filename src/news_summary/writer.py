from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path

from .models import ArticleDraft, PressRelease
from .ops_logging import get_logger
from .writing_settings import custom_prompt_section


PROMPT_PATH = Path(__file__).resolve().parents[2] / "templates" / "broadcast_shortform_prompt.md"
GEMINI_PRIMARY_MODELS = ("gemini-3.5-flash",)
GEMINI_LITE_MODELS = ("gemini-3.1-flash-lite",)
GEMINI_FLASH_MODELS = GEMINI_PRIMARY_MODELS
DEFAULT_GEMINI_MODELS = GEMINI_PRIMARY_MODELS + GEMINI_LITE_MODELS
GEMINI_SOURCE_MAX_CHARS_ENV = "NEWS_SUMMARY_GEMINI_SOURCE_MAX_CHARS"
DEFAULT_GEMINI_SOURCE_MAX_CHARS = 2400
MIN_GEMINI_SOURCE_MAX_CHARS = 1200
MAX_GEMINI_SOURCE_MAX_CHARS = 6000
logger = get_logger("writer")


GEMINI_SOURCE_PRIORITY_TOKENS = (
    "대상",
    "기간",
    "일시",
    "장소",
    "금액",
    "예산",
    "규모",
    "신청",
    "접수",
    "모집",
    "지원",
    "환급",
    "선정",
    "운영",
    "추진",
    "개최",
    "교육",
    "행사",
    "사업",
    "협약",
    "개선",
    "확대",
    "계획",
    "예정",
    "부터",
    "까지",
    "만원",
    "억원",
    "명",
    "가구",
    "개소",
)

GEMINI_SOURCE_NOISE_TOKENS = (
    "다운로드",
    "미리보기",
    "첨부파일",
    "첨부 파일",
    "파일명",
    "바로보기",
    "목록",
    "이전글",
    "다음글",
    "공유하기",
    "인쇄",
    "저작권",
    "무단전재",
    "copyright",
)


class GeminiRefineError(RuntimeError):
    def __init__(self, message: str, attempted_models: list[str] | None = None) -> None:
        super().__init__(message)
        self.attempted_models = attempted_models or []


class GeminiDraftError(RuntimeError):
    def __init__(self, message: str, attempted_models: list[str] | None = None) -> None:
        super().__init__(message)
        self.attempted_models = attempted_models or []


def generate_draft(
    item_id: int,
    item: PressRelease,
    model: str | None = None,
    require_gemini: bool = False,
) -> ArticleDraft:
    gemini_api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if gemini_api_key:
        last_error: Exception | None = None
        gemini_models = _gemini_draft_model_candidates(item, model)
        attempted: list[str] = []
        for gemini_model in gemini_models:
            attempted.append(gemini_model)
            try:
                return _generate_gemini_draft(item_id, item, gemini_api_key, gemini_model)
            except Exception as exc:  # noqa: BLE001 - try the next configured Gemini model.
                last_error = exc
                logger.warning(
                    "gemini draft model failed press_release_id=%s model=%s error_type=%s error=%s",
                    item_id,
                    gemini_model,
                    type(exc).__name__,
                    _shorten(str(exc), 240),
                )
                if _is_gemini_quota_error(exc) and gemini_model == gemini_models[-1]:
                    break
                continue
        if require_gemini:
            raise GeminiDraftError(_summarize_gemini_error(last_error), attempted)
        failed_model = attempted[0] if attempted else gemini_models[0]
        error_name = type(last_error).__name__ if last_error else "UnknownError"
        return _fallback_draft(item_id, item, f"{failed_model}:gemini-error:{error_name}")

    if require_gemini:
        raise GeminiDraftError("Gemini API 키가 설정되어 있지 않아 자동 초안 생성을 보류했습니다.", [])

    openai_model = model or os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return _fallback_draft(item_id, item, openai_model)

    try:
        from openai import OpenAI
    except ModuleNotFoundError:
        return _fallback_draft(item_id, item, openai_model)

    client = OpenAI(api_key=api_key)
    system_prompt = build_system_prompt()
    response = client.responses.create(
        model=openai_model,
        input=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": _user_prompt(item),
            },
        ],
        temperature=0.3,
    )
    return _parse_model_output(item_id, response.output_text, openai_model)


def refine_draft_with_gemini(
    draft,
    instruction: str,
    current_title: str,
    current_body: str,
    current_review_note: str,
    model: str | None = None,
) -> ArticleDraft:
    gemini_api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not gemini_api_key:
        raise RuntimeError("Gemini API 키가 설정되어 있지 않습니다.")
    attempted = []
    last_error: Exception | None = None
    gemini_models = _gemini_refine_model_candidates(model)
    for gemini_model in gemini_models:
        attempted.append(gemini_model)
        try:
            return _generate_gemini_refinement(
                draft,
                instruction,
                current_title,
                current_body,
                current_review_note,
                gemini_api_key,
                gemini_model,
            )
        except Exception as exc:  # noqa: BLE001 - try the next configured Gemini model.
            last_error = exc
            logger.warning(
                "gemini refine model failed press_release_id=%s model=%s error_type=%s error=%s",
                _draft_value(draft, "press_release_id"),
                gemini_model,
                type(exc).__name__,
                _shorten(str(exc), 240),
            )
            if _is_gemini_quota_error(exc) and gemini_model == gemini_models[-1]:
                break
            continue
    raise GeminiRefineError(_summarize_gemini_error(last_error), attempted)


def _generate_gemini_draft(
    item_id: int,
    item: PressRelease,
    api_key: str,
    model_name: str,
) -> ArticleDraft:
    try:
        from google import genai
        from google.genai import types
    except ModuleNotFoundError as exc:
        raise RuntimeError("Gemini 라이브러리가 설치되어 있지 않습니다.") from exc

    client = genai.Client(api_key=api_key)
    system_prompt = build_system_prompt()
    response = client.models.generate_content(
        model=model_name,
        contents=_user_prompt(item),
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
        ),
    )
    return _parse_model_output(item_id, response.text or "", f"{model_name}:gemini")


def _generate_gemini_refinement(
    draft,
    instruction: str,
    current_title: str,
    current_body: str,
    current_review_note: str,
    api_key: str,
    model_name: str,
) -> ArticleDraft:
    try:
        from google import genai
        from google.genai import types
    except ModuleNotFoundError as exc:
        raise RuntimeError("Gemini 라이브러리가 설치되어 있지 않습니다.") from exc

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model_name,
        contents=_refine_user_prompt(draft, instruction, current_title, current_body, current_review_note),
        config=types.GenerateContentConfig(
            system_instruction=build_system_prompt(),
        ),
    )
    return _parse_model_output(_draft_value(draft, "press_release_id"), response.text or "", f"{model_name}:gemini-refine")


def _user_prompt(item: PressRelease) -> str:
    source_excerpt = _gemini_source_excerpt(item.content, item.title)
    return (
        f"출처: {item.source_name}\n"
        f"지역: {item.region}\n"
        f"원문 제목: {item.title}\n"
        f"원문 URL: {item.url}\n"
        f"게시일: {item.published_at or '미상'}\n\n"
        "원문 본문(기사 작성에 필요한 핵심 문단만 발췌):\n"
        f"{source_excerpt}"
    )


def _refine_user_prompt(
    draft,
    instruction: str,
    current_title: str,
    current_body: str,
    current_review_note: str,
) -> str:
    original_content = _gemini_source_excerpt(
        str(_draft_value(draft, "original_content") or ""),
        str(_draft_value(draft, "original_title") or ""),
    )
    return (
        "아래 원문과 현재 기사 초안을 바탕으로, 사용자가 적은 방향에 맞게 초안을 다시 다듬어라.\n"
        "사용자 지시는 문장 흐름, 제목 방향, 강조점 조정에만 반영하고 원문에 없는 사실은 추가하지 않는다.\n"
        "출력 형식은 반드시 기존 기사 작성 규칙의 '제목/본문/검수 메모' 형식을 따른다.\n\n"
        f"사용자 다듬기 방향:\n{instruction[:900].strip()}\n\n"
        f"출처: {_draft_value(draft, 'source_name')}\n"
        f"지역: {_draft_value(draft, 'region')}\n"
        f"원문 제목: {_draft_value(draft, 'original_title')}\n"
        f"원문 URL: {_draft_value(draft, 'url')}\n"
        f"게시일: {_draft_value(draft, 'published_at') or '미상'}\n\n"
        "원문 본문(기사 작성에 필요한 핵심 문단만 발췌):\n"
        f"{original_content}\n\n"
        f"현재 제목:\n{current_title}\n\n"
        f"현재 본문:\n{current_body}\n\n"
        f"현재 검수 메모:\n{current_review_note}"
    )


def _draft_value(draft, key: str):
    if isinstance(draft, dict):
        return draft.get(key, "")
    try:
        return draft[key]
    except (KeyError, IndexError, TypeError):
        return ""


def _gemini_draft_model_candidates(item: PressRelease, model: str | None = None) -> list[str]:
    enabled_models = current_gemini_models()
    if model and model in enabled_models:
        return [model]
    if _needs_flash_for_draft(item):
        return list(GEMINI_PRIMARY_MODELS)
    return _dedupe_models([*GEMINI_LITE_MODELS, *GEMINI_PRIMARY_MODELS])


def _gemini_refine_model_candidates(model: str | None = None) -> list[str]:
    enabled_models = current_gemini_models()
    if model and model in enabled_models:
        return [model]
    return list(GEMINI_PRIMARY_MODELS)


def _dedupe_models(models: list[str]) -> list[str]:
    deduped = []
    for model in models:
        if model not in deduped:
            deduped.append(model)
    return deduped


def _needs_flash_for_draft(item: PressRelease) -> bool:
    return _draft_complexity_score(item) >= 12


def _draft_complexity_score(item: PressRelease) -> int:
    text = f"{item.title}\n{_clean_gemini_source_content(item.content, item.title)}"
    score = 0
    if len(text) >= 1800:
        score += 3
    if len(text) >= 3200:
        score += 3
    score += min(len(re.findall(r"\d", text)), 18) // 3

    complex_tokens = (
        "신청",
        "접수",
        "모집",
        "공모",
        "대상",
        "자격",
        "조건",
        "지원",
        "환급",
        "보조",
        "사업비",
        "예산",
        "금액",
        "만원",
        "억원",
        "기간",
        "부터",
        "까지",
        "선정",
        "심사",
    )
    matched_tokens = {token for token in complex_tokens if token in text}
    score += len(matched_tokens) * 2
    if len(matched_tokens) >= 4:
        score += 4
    if re.search(r"\d+\s*(?:만|억)?\s*원|\d{1,2}월\s*\d{1,2}일|20\d{2}[./-]\d{1,2}[./-]\d{1,2}", text):
        score += 3
    return score


def current_gemini_models(today: date | None = None) -> list[str]:
    return _dedupe_models(list(DEFAULT_GEMINI_MODELS))


def gemini_source_max_chars() -> int:
    try:
        parsed = int(os.getenv(GEMINI_SOURCE_MAX_CHARS_ENV, str(DEFAULT_GEMINI_SOURCE_MAX_CHARS)))
    except ValueError:
        return DEFAULT_GEMINI_SOURCE_MAX_CHARS
    return min(MAX_GEMINI_SOURCE_MAX_CHARS, max(MIN_GEMINI_SOURCE_MAX_CHARS, parsed))


def _gemini_source_excerpt(content: str, title: str = "", max_chars: int | None = None) -> str:
    max_chars = max_chars or gemini_source_max_chars()
    cleaned = _clean_gemini_source_content(content, title)
    if len(cleaned) <= max_chars:
        return cleaned

    sentences = _dedupe_sentences(_split_sentences(cleaned))
    if not sentences:
        return _trim_text_to_boundary(cleaned, max_chars)

    selected = set(range(min(3, len(sentences))))
    scored = sorted(
        (
            (_gemini_sentence_score(sentence, index), index)
            for index, sentence in enumerate(sentences)
            if index not in selected
        ),
        reverse=True,
    )
    for score, index in scored:
        if score <= 0 and len(selected) >= 6:
            break
        candidate = selected | {index}
        candidate_text = _join_sentences([sentences[item] for item in sorted(candidate)])
        if len(candidate_text) <= max_chars:
            selected.add(index)

    excerpt = _join_sentences([sentences[index] for index in sorted(selected)])
    return _trim_text_to_boundary(excerpt, max_chars)


def _clean_gemini_source_content(content: str, title: str) -> str:
    lines = []
    for raw_line in str(content or "").replace("\r\n", "\n").splitlines():
        line = " ".join(raw_line.split())
        if not line or _is_gemini_noise_line(line):
            continue
        lines.append(line)
    text = "\n".join(lines) if lines else str(content or "")
    return _strip_press_release_noise(text, title)


def _is_gemini_noise_line(line: str) -> bool:
    lowered = line.lower()
    if any(token in lowered for token in GEMINI_SOURCE_NOISE_TOKENS):
        return True
    has_contact = bool(re.search(r"\d{2,4}-\d{3,4}-?\d{0,4}|[\w.+-]+@[\w.-]+", line))
    if has_contact and len(line) <= 180:
        return True
    if re.match(r"^(담당|문의|자료제공|제공부서|작성자|전화|연락처)\s*[:：]", line):
        return True
    return False


def _gemini_sentence_score(sentence: str, index: int) -> int:
    score = max(0, 24 - index)
    score += sum(8 for token in GEMINI_SOURCE_PRIORITY_TOKENS if token in sentence)
    score += min(len(re.findall(r"\d", sentence)), 10) * 3
    if re.search(r"[“”\"']", sentence):
        score += 8
    if "밝혔다" in sentence or "말했다" in sentence or "전했다" in sentence:
        score += 4
    if _is_gemini_noise_line(sentence):
        score -= 80
    if "군 관계자" in sentence or "시 관계자" in sentence or "구 관계자" in sentence:
        score -= 8
    return score


def _trim_text_to_boundary(text: str, max_chars: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= max_chars:
        return text
    clipped = text[:max_chars].rstrip()
    boundaries = [(clipped.rfind(token), len(token)) for token in (".", "다.", "요.", "음.")]
    boundary, token_length = max(boundaries, key=lambda item: item[0])
    if boundary >= int(max_chars * 0.72):
        clipped = clipped[: boundary + token_length].rstrip()
    return clipped.rstrip(" ,;:-") + "..."


def _summarize_gemini_error(exc: Exception | None) -> str:
    if exc is None:
        return "Gemini 호출에 실패했습니다."
    message = str(exc)
    lowered = message.lower()
    if "429" in message or "resource_exhausted" in lowered or "quota" in lowered:
        return "Gemini 요청 한도가 찼습니다. 원문 난이도에 맞는 모델을 시도했지만 초안 생성을 보류했습니다."
    if "503" in message or "unavailable" in lowered:
        return "Gemini 모델이 일시적으로 과부하 상태입니다. 잠시 뒤 다시 시도하세요."
    if "api key" in lowered or "401" in message or "unauthorized" in lowered:
        return "Gemini API 키 인증에 문제가 있습니다."
    if "403" in message or "permission" in lowered:
        return "Gemini API 키 권한 또는 결제/프로젝트 설정에 문제가 있습니다."
    if "invalid_argument" in lowered or "400" in message:
        return "Gemini 모델명이나 요청 형식에 문제가 있습니다."
    return f"Gemini 호출 중 오류가 발생했습니다. ({type(exc).__name__})"


def _is_gemini_quota_error(exc: Exception | None) -> bool:
    if exc is None:
        return False
    message = str(exc)
    lowered = message.lower()
    return "429" in message or "resource_exhausted" in lowered or "quota" in lowered


def build_system_prompt() -> str:
    return f"{PROMPT_PATH.read_text(encoding='utf-8').strip()}\n\n{custom_prompt_section()}"


def _fallback_draft(item_id: int, item: PressRelease, model_name: str) -> ArticleDraft:
    body = _rule_based_broadcast_body(item)
    return ArticleDraft(
        press_release_id=item_id,
        title=_rule_based_title(item),
        body=body,
        review_note=_fallback_note(model_name),
        model=f"{model_name}:rule-based",
    )


def _fallback_note(model_name: str) -> str:
    if "gemini-sdk-missing" in model_name:
        return "Gemini 라이브러리가 설치되지 않아 규칙 기반 초안으로 저장했습니다. 의존성 설치 후 다시 생성하세요."
    if "gemini-error" in model_name:
        return "Gemini 호출 중 오류가 발생해 규칙 기반 초안으로 저장했습니다. API 키, 모델명, 사용량 제한을 확인하세요."
    return "인공지능 API 키가 없어 규칙 기반 초안으로 저장했습니다. 원문과 대조해 수치, 날짜, 기관명을 확인하세요."


def _parse_model_output(item_id: int, text: str, model_name: str) -> ArticleDraft:
    title = ""
    body_lines: list[str] = []
    note_lines: list[str] = []
    section = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("제목:"):
            title = line.removeprefix("제목:").strip()
            section = None
        elif line == "본문:":
            section = "body"
        elif line == "검수 메모:":
            section = "note"
        elif section == "body":
            body_lines.append(line)
        elif section == "note" and line:
            note_lines.append(line)

    title = _clean_article_title(title or "제목 검수 필요")
    body_text = _clean_body_text("\n".join(body_lines).strip() or text.strip(), title)
    return ArticleDraft(
        press_release_id=item_id,
        title=title,
        body=_normalize_three_paragraph_body(body_text),
        review_note="\n".join(note_lines).strip() or "특이사항 없음",
        model=model_name,
    )


def _shorten(text: str, max_chars: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= max_chars else text[: max_chars - 1].rstrip() + "..."


def _rule_based_broadcast_body(item: PressRelease) -> str:
    cleaned = _strip_press_release_noise(item.content, item.title)
    sentences = _split_sentences(cleaned)
    if not sentences:
        return _normalize_three_paragraph_body(_shorten(item.content, max_chars=450))
    styled_sentences = [_to_broadcast_style(sentence) for sentence in sentences]
    return _build_three_paragraph_body(styled_sentences)


def _strip_press_release_noise(content: str, title: str) -> str:
    text = " ".join(content.split())
    if title and text.startswith(title):
        text = text[len(title) :].strip()
    text = re.sub(r"^(?:-\s*[^-.。]{5,180}\s*-\s*)+", " ", text)
    text = re.sub(r"^【[^】]{0,160}】\s*", " ", text)
    text = re.sub(r"^.*?〔[^〕]{0,220}(?:☎|\d{2,4}-\d{3,4})[^〕]{0,220}〕\s*", " ", text)
    text = re.sub(r"^\([^)]*사진\s*\d*장\s*첨부[^)]*\)\s*", " ", text)
    text = re.sub(r"작성일\s*[:：]?\s*20\d{2}[./-]\d{1,2}[./-]\d{1,2}(?:\s+\d{1,2}:\d{2})?", " ", text)
    text = re.sub(r"^\d+\s+", " ", text)
    if title and title in text[:180]:
        text = text.split(title, 1)[1].strip()
    text = re.sub(r"^\d+\s+[^.]{0,260}(?:제공|사진|기념촬영)\s+", " ", text)
    text = re.sub(r"【[^】]{0,80}\d{2,4}-\d{3,4}[^】]*】", " ", text)
    text = re.sub(r"\([^)]*사진\s*\d*장\s*첨부[^)]*\)", " ", text)
    text = re.sub(r"\([^)]*첨부[^)]*\)", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -")


def _split_sentences(text: str) -> list[str]:
    pieces = re.split(r"(?<=[.!?。])\s+|(?<=다\.)\s+|(?<=요\.)\s+|(?<=음\.)\s+", text)
    sentences = []
    for piece in pieces:
        piece = piece.strip(" -")
        if len(piece) < 10:
            continue
        if any(token in piece for token in ("다운로드", "미리보기", "바로듣기", "담당부서")):
            continue
        sentences.append(piece)
    if sentences:
        return sentences
    return [text[:450].rstrip()]


def _clean_article_title(title: str) -> str:
    title = " ".join(title.split()).strip()
    title = re.sub(r"^\[?\s*뉴스\s*단신\s*\]?\s*[:：-]?\s*", "", title).strip()
    if not title:
        title = "제목 검수 필요"
    return title


def _clean_body_text(text: str, title: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    cleaned_lines = []
    title_key = _normalize_compare_text(title)
    skipping_leading_noise = True

    for line in lines:
        if not line:
            cleaned_lines.append(line)
            continue

        line_without_label = _remove_body_title_label(line)
        line_key = _normalize_compare_text(line_without_label)
        is_title_line = title_key and line_key == title_key
        is_section_label = line in {"본문:", "본문", "제목:", "제목"}

        if skipping_leading_noise and (is_section_label or is_title_line or not line_without_label):
            continue

        skipping_leading_noise = False
        cleaned_lines.append(line_without_label)

    cleaned = "\n".join(cleaned_lines).strip()
    cleaned = re.sub(r"^\s*\[?\s*뉴스\s*단신\s*\]?\s*[:：-]?\s*", "", cleaned)
    return cleaned or text.strip()


def _remove_body_title_label(line: str) -> str:
    line = re.sub(r"^\s*제목\s*[:：]\s*", "", line).strip()
    line = re.sub(r"^\[?\s*뉴스\s*단신\s*\]?\s*[:：-]?\s*", "", line).strip()
    return line


def _normalize_compare_text(text: str) -> str:
    return re.sub(r"[\W_]+", "", text).casefold()


def _rule_based_title(item: PressRelease) -> str:
    if "반값여행" in item.content and "2차" in item.content:
        max_amounts = [int(value) for value in re.findall(r"최대\s*(\d+)\s*만원", item.content)]
        max_amount = max(max_amounts) if max_amounts else None
        suffix = f"…최대 {max_amount}만 원 환급" if max_amount else ""
        return _clean_article_title(f"해남군, '반값여행' 2차 접수 시작{suffix}")
    return _clean_article_title(item.title)


def _normalize_three_paragraph_body(text: str) -> str:
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", text.strip()) if paragraph.strip()]
    if len(paragraphs) == 3:
        return "\n\n".join(paragraphs)
    if len(paragraphs) > 3:
        return "\n\n".join(paragraphs[:3])

    sentences = [_to_broadcast_style(sentence) for sentence in _split_sentences(text)]
    return _build_three_paragraph_body(sentences)


def _build_three_paragraph_body(sentences: list[str]) -> str:
    sentences = _dedupe_sentences(sentences)
    if len(sentences) <= 3:
        return "\n\n".join(sentences)

    used = {0}
    lead = sentences[0]
    detail, detail_indices = _pick_sentence_group(
        sentences,
        used,
        tokens=("대상", "지원", "환급", "최대", "혜택", "한도", "청년", "선정", "사업", "참여", "운영", "규모", "예산"),
        prefer_late=False,
    )
    used.update(detail_indices)
    closing, closing_indices = _pick_sentence_group(
        sentences,
        used,
        tokens=("신청", "기간", "오는", "부터", "까지", "계획", "예정", "방침", "조기", "접수", "개최", "진행"),
        prefer_late=True,
    )
    used.update(closing_indices)

    paragraphs = [lead, detail, closing]
    for index, sentence in enumerate(sentences):
        if len([paragraph for paragraph in paragraphs if paragraph]) >= 3:
            break
        if index not in used:
            paragraphs.append(sentence)
    return "\n\n".join(paragraph for paragraph in paragraphs if paragraph)[:1400]


def _pick_sentence_group(
    sentences: list[str],
    used: set[int],
    tokens: tuple[str, ...],
    prefer_late: bool,
) -> tuple[str, set[int]]:
    candidates = []
    for index, sentence in enumerate(sentences):
        if index in used:
            continue
        score = _sentence_importance(sentence, tokens)
        order_bonus = index / 100 if prefer_late else -index / 100
        candidates.append((score + order_bonus, index))
    if not candidates:
        return "", set()

    _, selected = max(candidates)
    indices = {selected}
    neighbors = [index for index in (selected - 1, selected + 1) if 0 <= index < len(sentences)]
    neighbors.sort(key=lambda index: _sentence_importance(sentences[index], tokens), reverse=True)
    for neighbor in neighbors:
        if neighbor in used or neighbor < 0 or neighbor >= len(sentences):
            continue
        candidate_text = _join_sentences([sentences[index] for index in sorted(indices | {neighbor})])
        if _sentence_importance(sentences[neighbor], tokens) > 0 and len(candidate_text) <= 260:
            indices.add(neighbor)
    return _join_sentences([sentences[index] for index in sorted(indices)]), indices


def _sentence_importance(sentence: str, tokens: tuple[str, ...]) -> int:
    score = sum(2 for token in tokens if token in sentence)
    score += min(len(re.findall(r"\d", sentence)), 6)
    if "말했습니다" in sentence or "당부했습니다" in sentence:
        score -= 6
    return score


def _join_sentences(sentences: list[str]) -> str:
    return " ".join(sentence.strip() for sentence in sentences if sentence.strip())


def _dedupe_sentences(sentences: list[str]) -> list[str]:
    seen = set()
    unique = []
    for sentence in sentences:
        key = re.sub(r"\W+", "", sentence)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(sentence)
    return unique


def _to_broadcast_style(sentence: str) -> str:
    sentence = sentence.strip()
    sentence = sentence.rstrip(".")
    replacements = [
        ("밝혔다", "밝혔습니다"),
        ("전했다", "전했습니다"),
        ("말했다", "말했습니다"),
        ("설명했다", "설명했습니다"),
        ("강조했다", "강조했습니다"),
        ("선정했다", "선정했습니다"),
        ("결정했다", "결정했습니다"),
        ("실시했다", "실시했습니다"),
        ("완료했다", "완료했습니다"),
        ("요청했다", "요청했습니다"),
        ("참여했다", "참여했습니다"),
        ("제공했다", "제공했습니다"),
        ("확보했다", "확보했습니다"),
        ("마련했다", "마련했습니다"),
        ("확정했다", "확정했습니다"),
        ("했다", "했습니다"),
        ("열었다", "열었습니다"),
        ("나섰다", "나섰습니다"),
        ("떨어졌다", "떨어졌습니다"),
        ("졌다", "졌습니다"),
        ("됐다", "됐습니다"),
        ("되었다", "됐습니다"),
        ("왔다", "왔습니다"),
        ("한다", "합니다"),
        ("된다", "됩니다"),
        ("나선다", "나섭니다"),
        ("높인다", "높입니다"),
        ("늘린다", "늘립니다"),
        ("지원한다", "지원합니다"),
        ("운영한다", "운영합니다"),
        ("추진한다", "추진합니다"),
        ("개최한다", "개최합니다"),
        ("진행한다", "진행합니다"),
        ("가능하다", "가능합니다"),
        ("필요하다", "필요합니다"),
        ("예정이다", "예정입니다"),
        ("계획이다", "계획입니다"),
        ("것이다", "것입니다"),
        ("이었다", "이었습니다"),
        ("였다", "였습니다"),
        ("이다", "입니다"),
        ("있다", "있습니다"),
    ]
    for source, target in replacements:
        if sentence.endswith(source):
            sentence = sentence[: -len(source)] + target
            break
    if not sentence.endswith(("습니다", "입니다", "합니다", "했습니다", "밝혔습니다", "전했습니다", "됩니다")):
        sentence += "입니다"
    if not sentence.endswith((".", "!", "?")):
        sentence += "."
    return sentence
