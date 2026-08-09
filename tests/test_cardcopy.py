import json

import pytest

from news_summary.cardcopy import (
    MAX_CARD_CHARS,
    MAX_COVER_CHARS,
    CardCopyError,
    CardCopyRequest,
    build_card_copy,
)


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
            "담양군이 야외 근로자와 취약계층 안전 점검에 나섰습니다.",
            "무더위쉼터 냉방기 가동 상태를 전수 확인했습니다.",
            "폭염특보가 해제될 때까지 점검이 이어집니다.",
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
    payload["cards"][0] = "가" * (MAX_CARD_CHARS + 1)

    with pytest.raises(CardCopyError, match="본문 카드 길이"):
        build_card_copy(sample_request(), "key", models=("m1",), generator=responder(json.dumps(payload)))


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

    assert calls == ["first", "second"]
    assert copy.cover == "담양 무더위쉼터 전면 점검"


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
