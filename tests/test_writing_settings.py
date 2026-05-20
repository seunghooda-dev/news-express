from pathlib import Path

from news_summary.web import create_app
from news_summary.writing_settings import custom_prompt_section, load_writing_settings, save_writing_settings


def test_writing_settings_round_trip():
    path = Path("data/.test_writing_settings_round_trip.json")
    path.unlink(missing_ok=True)

    saved = save_writing_settings(
        {
            "tone": "차분하지만 단호한 어조",
            "sentence_style": "짧은 문장",
            "title_style": "핵심 숫자를 제목 앞에 배치",
            "focus_points": "예산, 대상, 신청 기간",
            "avoid_phrases": "총력, 박차",
            "extra_instruction": "주민 생활 영향이 있으면 먼저 쓴다.",
            "style_example": "예시 제목\n\n예시 본문",
        },
        path,
    )
    loaded = load_writing_settings(path)

    assert loaded == saved
    assert "차분하지만 단호한 어조" in custom_prompt_section(loaded)
    assert "주민 생활 영향" in custom_prompt_section(loaded)
    assert "예시는 문장 길이" in custom_prompt_section(loaded)
    assert "\n\n예시 본문" in loaded["style_example"]
    path.unlink(missing_ok=True)


def test_writing_settings_page_saves_custom_values(monkeypatch):
    settings_path = Path("data/.test_writing_settings_page.json").resolve()
    settings_path.unlink(missing_ok=True)
    monkeypatch.setenv("NEWS_SUMMARY_WRITING_SETTINGS", str(settings_path))
    app = create_app()
    app.testing = True
    client = app.test_client()

    response = client.post(
        "/writing-settings",
        data={
            "tone": "담백한 라디오 뉴스 어조",
            "sentence_style": "문장을 더 짧게",
            "title_style": "지역명을 앞세우기",
            "focus_points": "날짜와 신청 방법",
            "avoid_phrases": "기대된다",
            "extra_instruction": "인용문은 꼭 필요한 경우만 쓴다.",
            "style_example": "테스트 예시\n본문입니다.",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "담백한 라디오 뉴스 어조" in response.data.decode("utf-8")
    assert "참고 기사 예시" in response.data.decode("utf-8")
    assert load_writing_settings(settings_path)["title_style"] == "지역명을 앞세우기"
    settings_path.unlink(missing_ok=True)
