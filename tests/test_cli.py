import sys

import pytest
from werkzeug.security import check_password_hash


def test_admin_password_hash_command_outputs_usable_hash(monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        ["news-summary", "admin-password-hash", "PressRoom-47-Delta"],
    )

    from news_summary.cli import main

    main()

    output = capsys.readouterr().out.strip()
    assert output.startswith("NEWS_SUMMARY_ADMIN_PASSWORD_HASH=")
    generated_hash = output.split("=", 1)[1]
    assert check_password_hash(generated_hash, "PressRoom-47-Delta")
    assert "PressRoom-47-Delta" not in output


def test_admin_password_hash_command_rejects_weak_password(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["news-summary", "admin-password-hash", "news1234"])

    from news_summary.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert "상용 운영 기준에 약합니다" in str(exc_info.value)
    assert "12자 미만" in str(exc_info.value)
    assert "--allow-weak" in str(exc_info.value)
