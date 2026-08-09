import json

import pytest

from news_summary.cardcopy import (
    MAX_CARD_CHARS,
    MAX_COVER_CHARS,
    MAX_HEADING_CHARS,
    MIN_HEADING_CHARS,
    CardCopyError,
    CardCopyRequest,
    build_card_copy,
)
from news_summary.cardnews import CardSlide


def sample_request() -> CardCopyRequest:
    return CardCopyRequest(
        title="담양군, 폭염 취약 현장 긴급 점검",
        body="담양군은 야외 근로자와 취약계층 안전을 위해 현장을 점검했다고 밝혔습니다. "
        "무더위쉼터 운영 상태와 냉방기 가동 여부도 함께 확인했습니다.",
        source_label="담양군청 보도자료",
        region="전남 담양",
        date_label="2026.08.09",
    )


def good_payload() -> dict:
    return {
        "cover": "담양 무더위쉼터 전면 점검",
        "cards": [
            {
                "heading": "야외 근로자 안전 점검",
                "body": "담양군이 야외 근로자와 취약계층 안전을 위해 폭염 취약 현장을 직접 점검했습니다.",
            },
            {
                "heading": "무더위쉼터 냉방기 확인",
                "body": "무더위쉼터 냉방기 가동 상태와 운영 시간을 전수 확인해 이용 불편이 없도록 했습니다.",
            },
            {
                "heading": "폭염특보 해제까지 지속",
                "body": "군은 폭염특보가 해제될 때까지 취약 현장 점검을 계속 이어 갈 계획이라고 밝혔습니다.",
            },
        ],
        "tags": ["담양", "폭염"],
    }


def responder(payload, calls=None):
    def _generate(request, api_key, model_name):
        if calls is not None:
            calls.append(model_name)
        return payload(model_name) if callable(payload) else payload
    return _generate


def test_builds_copy_from_valid_json():
    copy = build_card_copy(sample_request(), "key", generator=responder(json.dumps(good_payload())))

    assert copy.cover == "담양 무더위쉼터 전면 점검"
    assert len(copy.cards) == 3
    assert copy.cards[0] == CardSlide(
        heading="야외 근로자 안전 점검",
        body="담양군이 야외 근로자와 취약계층 안전을 위해 폭염 취약 현장을 직접 점검했습니다.",
    )
    assert copy.source_label == "담양군청 보도자료"
    assert copy.tags == ["담양", "폭염"]


def test_accepts_json_wrapped_in_code_fence():
    """모델이 ```json 울타리를 붙이는 일이 잦다."""
    raw = "```json\n" + json.dumps(good_payload()) + "\n```"
    copy = build_card_copy(sample_request(), "key", generator=responder(raw))

    assert len(copy.cards) == 3


def test_accepts_json_with_surrounding_chatter():
    raw = "네, 카드 문안입니다.\n" + json.dumps(good_payload()) + "\n도움이 되셨길 바랍니다."
    copy = build_card_copy(sample_request(), "key", generator=responder(raw))

    assert copy.cover


def test_strips_hash_from_tags():
    payload = good_payload()
    payload["tags"] = ["#담양", "#폭염대비"]
    copy = build_card_copy(sample_request(), "key", generator=responder(json.dumps(payload)))

    assert copy.tags == ["담양", "폭염대비"]


def test_rejects_overlong_card_instead_of_truncating():
    """잘린 문장이 카드에 박히면 사람이 고치기 더 번거롭다 — 자르지 말고 거절한다."""
    payload = good_payload()
    payload["cards"][0]["body"] = "가" * (MAX_CARD_CHARS + 1)

    with pytest.raises(CardCopyError, match="본문 카드 길이"):
        build_card_copy(sample_request(), "key", models=("m1",), generator=responder(json.dumps(payload)))


def test_rejects_too_short_heading():
    """한두 글자짜리 소제목은 그 장에서 무슨 얘기를 하는지 알려 주지 못한다."""
    payload = good_payload()
    payload["cards"][0]["heading"] = "가" * (MIN_HEADING_CHARS - 1)

    with pytest.raises(CardCopyError, match="소제목 길이"):
        build_card_copy(sample_request(), "key", models=("m1",), generator=responder(json.dumps(payload)))


def test_rejects_overlong_heading():
    """소제목이 길면 카드 위쪽을 다 잡아먹는다 — 본문과 마찬가지로 자르지 말고 거절한다."""
    payload = good_payload()
    payload["cards"][0]["heading"] = "가" * (MAX_HEADING_CHARS + 1)

    with pytest.raises(CardCopyError, match="소제목 길이"):
        build_card_copy(sample_request(), "key", models=("m1",), generator=responder(json.dumps(payload)))


def test_plain_string_card_is_read_as_body_without_heading():
    """모델이 {소제목, 본문} 대신 문자열 하나를 줘도 버리지 않는다.

    버렸다면 장수 미달('장수')로 걸릴 텐데, 소제목 규격에서 걸리는 것이
    문자열이 본문으로 읽혔다는 뜻이다.
    """
    payload = good_payload()
    payload["cards"] = [slide["body"] for slide in payload["cards"]]

    with pytest.raises(CardCopyError, match="소제목 길이"):
        build_card_copy(sample_request(), "key", models=("m1",), generator=responder(json.dumps(payload)))


def test_extra_keys_from_model_are_ignored():
    """장수를 세게 하려고 slots 같은 키를 출력에 넣었다 — 검증이 그것 때문에 깨지면 안 된다."""
    payload = good_payload()
    payload["slots"] = ["자격", "일정", "비용"]
    payload["card_count"] = 3

    copy = build_card_copy(sample_request(), "key", generator=responder(json.dumps(payload)))

    assert len(copy.cards) == 3
    assert copy.cover


def test_rejects_overlong_cover():
    payload = good_payload()
    payload["cover"] = "가" * (MAX_COVER_CHARS + 1)

    with pytest.raises(CardCopyError, match="표지 문구 길이"):
        build_card_copy(sample_request(), "key", models=("m1",), generator=responder(json.dumps(payload)))


def test_rejects_too_many_cards():
    payload = good_payload()
    payload["cards"] = payload["cards"] * 3

    with pytest.raises(CardCopyError, match="장수"):
        build_card_copy(sample_request(), "key", models=("m1",), generator=responder(json.dumps(payload)))


def test_rejects_single_card_set():
    payload = good_payload()
    payload["cards"] = payload["cards"][:1]

    with pytest.raises(CardCopyError, match="장수"):
        build_card_copy(sample_request(), "key", models=("m1",), generator=responder(json.dumps(payload)))


def test_rejects_non_json_response():
    with pytest.raises(CardCopyError):
        build_card_copy(sample_request(), "key", models=("m1",), generator=responder("문안을 만들 수 없습니다."))


def test_rejects_empty_response():
    with pytest.raises(CardCopyError):
        build_card_copy(sample_request(), "key", models=("m1",), generator=responder(""))


def test_rejects_cards_that_are_not_a_list():
    payload = good_payload()
    payload["cards"] = "카드1, 카드2"

    with pytest.raises(CardCopyError, match="목록"):
        build_card_copy(sample_request(), "key", models=("m1",), generator=responder(json.dumps(payload)))


def test_falls_back_to_next_model_when_first_breaks_spec():
    """규격 미달은 모델을 바꾸면 통과하는 일이 잦다."""
    bad = good_payload()
    bad["cover"] = "짧"
    calls: list[str] = []

    def payload_for(model_name):
        return json.dumps(bad if model_name == "first" else good_payload())

    copy = build_card_copy(
        sample_request(), "key", models=("first", "second"), generator=responder(payload_for, calls)
    )

    # 첫 모델은 일시 장애일 수 있어 한 번 더 준다 — 그래도 안 되면 다음 모델로 넘어간다.
    assert calls == ["first", "first", "second"]
    assert copy.cover == "담양 무더위쉼터 전면 점검"


def test_primary_model_gets_a_second_chance():
    """좋은 모델을 일시 장애 한 번으로 버리면 규격을 잘 못 맞추는 예비 모델만 남는다.

    실측(2026-08-09): flash 규격 통과 11/12, lite 1/2. 유일한 실패가 flash 503 뒤 lite였다.
    """
    calls: list[str] = []

    def payload_for(model_name):
        # 첫 호출만 망가뜨리고 두 번째부터는 정상 — 재시도가 없으면 lite로 넘어간다.
        if model_name == "flash" and calls.count("flash") == 1:
            raise RuntimeError("503 UNAVAILABLE")
        return json.dumps(good_payload())

    copy = build_card_copy(
        sample_request(), "key", models=("flash", "lite"), generator=responder(payload_for, calls)
    )

    assert calls[:2] == ["flash", "flash"], "첫 모델에 한 번 더 기회를 줘야 한다"
    assert "lite" not in calls, "재시도로 통과했으면 예비 모델까지 가지 않는다"
    assert copy.cover


def test_reports_every_attempted_model_when_all_fail():
    with pytest.raises(CardCopyError, match="first, second"):
        build_card_copy(
            sample_request(), "key", models=("first", "second"), generator=responder("망가진 응답")
        )


def test_requires_title_and_body():
    with pytest.raises(CardCopyError, match="제목과 본문"):
        build_card_copy(CardCopyRequest(title="", body="본문"), "key", generator=responder("{}"))

    with pytest.raises(CardCopyError, match="제목과 본문"):
        build_card_copy(CardCopyRequest(title="제목", body="   "), "key", generator=responder("{}"))


def test_result_feeds_card_builder_directly():
    """문안 생성의 산출물이 합성의 입력이어야 한다 — 사이에 변환이 끼면 안 된다."""
    from news_summary.cardnews import build_card_images

    copy = build_card_copy(sample_request(), "key", generator=responder(json.dumps(good_payload())))
    images = build_card_images(copy, [])

    assert len(images) == len(copy.cards) + 1
