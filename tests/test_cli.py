import sys

import pytest
from werkzeug.security import check_password_hash


def test_admin_password_hash_command_outputs_usable_hash_and_argument_warning(monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        ["news-summary", "admin-password-hash", "PressRoom-47-Delta"],
    )

    from news_summary.cli import main

    main()

    captured = capsys.readouterr()
    output = captured.out.strip()
    assert output.startswith("NEWS_SUMMARY_ADMIN_PASSWORD_HASH=")
    generated_hash = output.split("=", 1)[1]
    assert check_password_hash(generated_hash, "PressRoom-47-Delta")
    assert "PressRoom-47-Delta" not in output
    assert "셸 기록에 남을 수 있습니다" in captured.err
    assert "PressRoom-47-Delta" not in captured.err


def test_admin_password_hash_command_interactive_mode_does_not_warn(monkeypatch, capsys):
    responses = iter(["PressRoom-47-Delta", "PressRoom-47-Delta"])
    monkeypatch.setattr(sys, "argv", ["news-summary", "admin-password-hash"])
    monkeypatch.setattr("getpass.getpass", lambda _prompt: next(responses))

    from news_summary.cli import main

    main()

    captured = capsys.readouterr()
    output = captured.out.strip()
    assert output.startswith("NEWS_SUMMARY_ADMIN_PASSWORD_HASH=")
    assert check_password_hash(output.split("=", 1)[1], "PressRoom-47-Delta")
    assert captured.err == ""


def test_admin_password_hash_command_rejects_weak_password(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["news-summary", "admin-password-hash", "news1234"])

    from news_summary.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert "상용 운영 기준에 약합니다" in str(exc_info.value)
    assert "12자 미만" in str(exc_info.value)
    assert "--allow-weak" in str(exc_info.value)
