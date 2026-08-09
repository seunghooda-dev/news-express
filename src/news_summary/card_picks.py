# 하루치 초안 중 카드뉴스로 만들 만한 후보를 골라 주는 모듈 — 제안일 뿐 자동 발행하지 않는다
from __future__ import annotations

import re
from dataclasses import dataclass

# 하루 244건을 사람이 다 볼 수 없다. 8건으로 줄이고 사람이 3~5건을 고른다.
DEFAULT_SHORTLIST = 8
MAX_PER_SOURCE = 2
# 기관을 흩어도 주제가 같으면 소용없다. 실측에서 폭염 주간에 8건 중 7건이 폭염
# 기사로 채워졌다 — 주민이 보기엔 같은 소식 일곱 번이다.
MAX_PER_TOPIC = 2
# 상한 때문에 명단이 너무 짧아지면 사람이 고를 게 없다.
MIN_SHORTLIST = 5

# 주민이 당장 행동해야 하는 소식이 먼저다.
ACTION_TOKENS = (
    "신청", "접수", "모집", "공모", "마감", "지원금", "보조금", "무료",
    "개방", "운영", "실시", "시행", "혜택", "할인", "지급", "선정",
)
SAFETY_TOKENS = ("폭염", "한파", "호우", "태풍", "화재", "안전", "대피", "감염", "주의보", "경보", "단수", "정전")
DATE_TOKENS = ("까지", "부터", "이내", "당일", "이달", "다음 달")
# 주민 생활과 거리가 먼 행정 소식은 뒤로 민다.
ADMIN_TOKENS = ("인사", "임명", "위촉", "간담회", "방문", "표창", "수상", "협약", "이임", "취임", "회의")


@dataclass
class Candidate:
    draft_id: int
    title: str
    source_id: str
    source_name: str
    score: int
    reasons: list[str]
    topic: str = ""
    has_set: bool = False


def rank_candidates(rows, existing_draft_ids=(), limit: int = DEFAULT_SHORTLIST) -> list[Candidate]:
    """초안 목록에서 카드뉴스 후보를 점수순으로 추린다.

    같은 기관이 명단을 독차지하지 않도록 기관당 최대 2건으로 제한한다 — 지역
    소식지가 한 기관 홍보물처럼 보이면 주민이 안 본다.
    """
    existing = set(existing_draft_ids)
    scored: list[Candidate] = []
    for row in rows:
        draft_id = int(row["id"])
        title = str(row["title"] or "")
        body = str(row["original_content"] if "original_content" in row.keys() else "") or ""
        score, reasons = _score(title, body, bool(row["asset_count"] if "asset_count" in row.keys() else 0))
        scored.append(
            Candidate(
                draft_id=draft_id,
                title=title,
                source_id=str(row["source_id"] or ""),
                source_name=str(row["source_name"] or ""),
                score=score,
                reasons=reasons,
                topic=_topic(f"{title} {body}"),
                has_set=draft_id in existing,
            )
        )

    scored.sort(key=lambda c: (-c.score, c.draft_id))
    picked: list[Candidate] = []
    chosen_ids: set[int] = set()
    per_source: dict[str, int] = {}
    per_topic: dict[str, int] = {}

    def take(candidate: Candidate) -> None:
        chosen_ids.add(candidate.draft_id)
        per_source[candidate.source_id] = per_source.get(candidate.source_id, 0) + 1
        if candidate.topic:
            per_topic[candidate.topic] = per_topic.get(candidate.topic, 0) + 1
        picked.append(candidate)

    for candidate in scored:
        if len(picked) >= limit:
            break
        if per_source.get(candidate.source_id, 0) >= MAX_PER_SOURCE:
            continue
        if candidate.topic and per_topic.get(candidate.topic, 0) >= MAX_PER_TOPIC:
            continue
        take(candidate)

    # 주제가 한쪽으로 쏠린 날은 명단이 짧아진다. 사람이 3~5건을 고를 수 있을 만큼만
    # 상한을 풀어 채운다 — 비슷한 기사로 8칸을 억지로 메우면 고르는 데 방해만 된다.
    if len(picked) < min(limit, MIN_SHORTLIST):
        for candidate in scored:
            if len(picked) >= min(limit, MIN_SHORTLIST):
                break
            if candidate.draft_id in chosen_ids:
                continue
            if per_source.get(candidate.source_id, 0) >= MAX_PER_SOURCE:
                continue
            take(candidate)
    return picked


def _topic(text: str) -> str:
    """대충의 주제 열쇠. 정확한 분류가 아니라 같은 소식이 몰리는 것만 막으면 된다."""
    for token in SAFETY_TOKENS:
        if token in text:
            return token
    for token in ACTION_TOKENS:
        if token in text:
            return token
    return ""


def _score(title: str, body: str, has_photo: bool) -> tuple[int, list[str]]:
    text = f"{title} {body}"
    score = 0
    reasons: list[str] = []

    action_hits = [token for token in ACTION_TOKENS if token in text]
    if action_hits:
        score += 3 * min(len(action_hits), 3)
        reasons.append(f"주민 행동({'·'.join(action_hits[:3])})")

    safety_hits = [token for token in SAFETY_TOKENS if token in text]
    if safety_hits:
        score += 4
        reasons.append(f"안전({'·'.join(safety_hits[:2])})")

    if any(token in text for token in DATE_TOKENS) or re.search(r"\d{1,2}월\s?\d{1,2}일", text):
        score += 3
        reasons.append("기한 있음")

    if re.search(r"\d[\d,]*\s?(원|명|가구|개소|대)", text):
        score += 2
        reasons.append("구체 수치")

    if has_photo:
        score += 2
        reasons.append("사진 있음")

    admin_hits = [token for token in ADMIN_TOKENS if token in title]
    if admin_hits:
        score -= 4
        reasons.append(f"행정 소식({admin_hits[0]})")

    return score, reasons
