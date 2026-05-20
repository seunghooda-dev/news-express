from news_summary.writer import GeminiDraftError, GeminiRefineError, generate_draft, refine_draft_with_gemini, _parse_model_output
from news_summary.models import PressRelease
from news_summary.writer import _fallback_draft, _rule_based_broadcast_body, _strip_press_release_noise


def test_parse_model_output_extracts_sections():
    text = """제목: 광주시, 청년 지원 사업 확대

본문:
광주시가 청년 지원 사업을 확대했습니다.
대상자는 다음 달부터 신청할 수 있습니다.

검수 메모:
- 신청 기간 원문 재확인 필요
"""

    draft = _parse_model_output(1, text, "test-model")

    assert draft.title == "광주시, 청년 지원 사업 확대"
    assert "청년 지원 사업" in draft.body
    assert "신청 기간" in draft.review_note


def test_rule_based_broadcast_body_polishes_common_endings():
    item = PressRelease(
        source_id="test",
        source_name="테스트",
        region="광주",
        title="광주시, 사업 추진",
        url="https://example.com",
        content=(
            "광주시, 사업 추진 작성일 : 2026-05-20 "
            "광주시는 새 사업을 추진한다고 밝혔다. "
            "사업 대상은 시민이다. "
            "시는 다음 달부터 접수할 예정이다."
        ),
    )

    body = _rule_based_broadcast_body(item)

    assert len(body.split("\n\n")) == 3
    assert "밝혔습니다" in body
    assert "시민입니다" in body
    assert "예정입니다" in body
    assert "됩니다입니다" not in body


def test_strip_press_release_noise_keeps_first_article_sentence_after_subtitles():
    title = "전남도, 2027년 국고 건의사업 대응전략 논의"
    content = (
        "전남도, 2027년 국고 건의사업 대응전략 논의 "
        "- 중간보고회 열어 중앙부처의 예산요구서 반영여부 점검 - "
        "- 6월 본격화할 기획처 심의 단계 대비해 사업 논리 보강 - "
        "【예산담당관 제갈래원 286-2510, 국고팀장 박주선 286-2530】 "
        "(국고 건의사업 중앙부처 반영 중간 보고회 사진 2장 첨부) "
        "전라남도가 2027년 국고 확보를 위해 건의사업의 중앙부처 반영 상황을 점검하고, "
        "기획예산처 심의 단계 대응 전략 마련에 나섰다."
    )

    cleaned = _strip_press_release_noise(content, title)

    assert cleaned.startswith("전라남도가 2027년 국고 확보를 위해")


def test_rule_based_broadcast_body_writes_three_paragraph_news_brief():
    item = PressRelease(
        source_id="haenam",
        source_name="해남군청 보도자료",
        region="전남 해남",
        title="“이틀만에 마감 땅끝해남 반값여행”오는 26일 2차 접수",
        url="https://example.com",
        content=(
            "해남군은 여행 경비를 지원하는‘땅끝해남 반값여행’2차 접수를 오는 26일부터 시작한다. "
            "땅끝해남 반값여행은 해남을 방문하는 관광객에게 여행 경비의 50% 이상을 모바일 해남사랑상품권으로 환급해 주게 된다. "
            "개인은 5만원 이상, 2인 이상 팀은 10만원 이상 소비할 경우 혜택을 받을 수 있으며, 환급 한도는 개인 최대 10만원, 팀 최대 20만원이다. "
            "특히 청년 신청자에게는 환급률을 70%까지 확대 적용해 청년 개인은 최대 14만원, 2인 이상 팀은 최대 28만원까지 환급받을 수 있다. "
            "앞서 진행된 1차 접수에는 2일 만에 2,200여팀, 4,858명이 신청해 인기리에 조기마감된 바 있다. "
            "이번 2차 여행 기간은 5월 27일부터 6월 29일까지로, 5월 26일 오전 9시부터 신청을 접수한다."
        ),
    )

    body = _rule_based_broadcast_body(item)
    paragraphs = body.split("\n\n")

    assert len(paragraphs) == 3
    assert paragraphs[0].startswith("해남군은 여행 경비를 지원하는")
    assert "최대 28만원" in paragraphs[1]
    assert "5월 26일" in paragraphs[2]
    assert "군 관계자는" not in paragraphs[2]


def test_fallback_draft_titles_haenam_half_price_trip_like_news_brief():
    item = PressRelease(
        source_id="haenam",
        source_name="해남군청 보도자료",
        region="전남 해남",
        title="“이틀만에 마감 땅끝해남 반값여행”오는 26일 2차 접수",
        url="https://example.com",
        content=(
            "해남군은 여행 경비를 지원하는‘땅끝해남 반값여행’2차 접수를 오는 26일부터 시작한다. "
            "청년 개인은 최대 14만원, 2인 이상 팀은 최대 28만원까지 환급받을 수 있다."
        ),
    )

    draft = _fallback_draft(1, item, "test-model")

    assert draft.title == "해남군, '반값여행' 2차 접수 시작…최대 28만 원 환급"


def test_generate_draft_prefers_gemini_when_key_exists(monkeypatch):
    item = PressRelease(
        source_id="test",
        source_name="테스트",
        region="광주",
        title="광주시, 사업 추진",
        url="https://example.com",
        content="광주시는 새 사업을 추진한다고 밝혔다.",
    )

    def fake_gemini(item_id, press_release, api_key, model_name):
        return _parse_model_output(
            item_id,
            "제목: [뉴스 단신] 광주시, 사업 추진\n\n본문:\n광주시가 새 사업을 추진합니다.\n\n대상은 시민입니다.\n\n시는 다음 달부터 접수합니다.\n\n검수 메모:\n- Gemini 테스트",
            f"{model_name}:gemini",
        )

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test")
    monkeypatch.delenv("GEMINI_MODELS", raising=False)
    monkeypatch.setattr("news_summary.writer._generate_gemini_draft", fake_gemini)

    draft = generate_draft(1, item)

    assert draft.model == "gemini-3.5-flash:gemini"
    assert draft.title == "광주시, 사업 추진"
    assert len(draft.body.split("\n\n")) == 3


def test_parse_model_output_removes_title_from_body():
    text = """제목: [뉴스 단신] 광주시, 청년 지원 사업 확대

본문:
[뉴스 단신] 광주시, 청년 지원 사업 확대

광주시가 청년 지원 사업을 확대했습니다.

대상자는 다음 달부터 신청할 수 있습니다.

시는 접수 일정을 별도로 안내할 계획입니다.
"""

    draft = _parse_model_output(1, text, "test-model")

    assert draft.title == "광주시, 청년 지원 사업 확대"
    assert "[뉴스 단신]" not in draft.title
    assert "[뉴스 단신]" not in draft.body
    assert "청년 지원 사업 확대\n\n광주시가" not in draft.body


def test_generate_draft_can_require_gemini(monkeypatch):
    item = PressRelease(
        source_id="test",
        source_name="테스트",
        region="광주",
        title="광주시, 사업 추진",
        url="https://example.com",
        content="광주시는 새 사업을 추진한다고 밝혔다.",
    )

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    try:
        generate_draft(1, item, require_gemini=True)
    except GeminiDraftError as exc:
        assert "자동 초안 생성을 보류" in str(exc)
    else:
        raise AssertionError("GeminiDraftError가 발생해야 합니다.")


def test_refine_draft_with_gemini_passes_reporter_instruction(monkeypatch):
    draft_row = {
        "press_release_id": 7,
        "source_name": "신안군청 보도자료",
        "region": "전남 신안",
        "original_title": "미술관 교육 운영",
        "url": "https://example.com",
        "published_at": "2026-05-20",
        "original_content": "신안군 저녁노을미술관이 교육 프로그램을 운영한다.",
    }

    def fake_refinement(draft, instruction, current_title, current_body, current_review_note, api_key, model_name):
        assert "신청 방법 중심" in instruction
        assert current_title == "기존 제목"
        assert "기존 본문" in current_body
        assert api_key == "test-key"
        return _parse_model_output(
            draft["press_release_id"],
            "제목: [뉴스 단신] 신안군, 교육 프로그램 운영\n\n본문:\n신안군이 교육 프로그램을 운영합니다.\n\n참가자는 실습 중심 교육을 받습니다.\n\n신청 방법은 원문 기준으로 확인해야 합니다.\n\n검수 메모:\n- 지시 반영",
            f"{model_name}:gemini-refine",
        )

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test")
    monkeypatch.delenv("GEMINI_MODELS", raising=False)
    monkeypatch.setattr("news_summary.writer._generate_gemini_refinement", fake_refinement)

    refined = refine_draft_with_gemini(
        draft_row,
        "신청 방법 중심으로 정리",
        "기존 제목",
        "기존 본문",
        "기존 메모",
    )

    assert refined.model == "gemini-3.5-flash:gemini-refine"
    assert refined.title == "신안군, 교육 프로그램 운영"


def test_refine_draft_with_gemini_uses_only_non_lite_flash_models(monkeypatch):
    draft_row = {
        "press_release_id": 7,
        "source_name": "신안군청 보도자료",
        "region": "전남 신안",
        "original_title": "미술관 교육 운영",
        "url": "https://example.com",
        "published_at": "2026-05-20",
        "original_content": "신안군 저녁노을미술관이 교육 프로그램을 운영한다.",
    }
    calls = []

    def fake_refinement(draft, instruction, current_title, current_body, current_review_note, api_key, model_name):
        calls.append(model_name)
        if model_name == "gemini-3.5-flash":
            raise RuntimeError("503 overloaded")
        return _parse_model_output(
            draft["press_release_id"],
            "제목: [뉴스 단신] Gemini 3 Flash 성공\n\n본문:\n첫 문단입니다.\n\n둘째 문단입니다.\n\n셋째 문단입니다.\n\n검수 메모:\n- non-lite 모델",
            f"{model_name}:gemini-refine",
        )

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", "quota-model")
    monkeypatch.delenv("GEMINI_MODELS", raising=False)
    monkeypatch.setattr("news_summary.writer._generate_gemini_refinement", fake_refinement)

    refined = refine_draft_with_gemini(draft_row, "다듬기", "제목", "본문", "메모")

    assert calls == ["gemini-3.5-flash", "gemini-3-flash-preview"]
    assert refined.model == "gemini-3-flash-preview:gemini-refine"


def test_refine_draft_with_gemini_reports_attempted_models(monkeypatch):
    draft_row = {
        "press_release_id": 7,
        "source_name": "신안군청 보도자료",
        "region": "전남 신안",
        "original_title": "미술관 교육 운영",
        "url": "https://example.com",
        "published_at": "2026-05-20",
        "original_content": "신안군 저녁노을미술관이 교육 프로그램을 운영한다.",
    }

    def always_fail(*args, **kwargs):
        raise RuntimeError("429 RESOURCE_EXHAUSTED quota exceeded")

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODELS", "quota-a,quota-b")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.setattr("news_summary.writer._generate_gemini_refinement", always_fail)

    try:
        refine_draft_with_gemini(draft_row, "다듬기", "제목", "본문", "메모")
    except GeminiRefineError as exc:
        assert "요청 한도" in str(exc)
        assert exc.attempted_models == ["gemini-3.5-flash", "gemini-3-flash-preview"]
    else:
        raise AssertionError("GeminiRefineError가 발생해야 합니다.")
