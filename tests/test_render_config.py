from pathlib import Path

import yaml


def test_render_auto_deploy_is_enabled_for_github_commits():
    config = yaml.safe_load(Path("render.yaml").read_text(encoding="utf-8"))
    services = config["services"]
    web_service = next(service for service in services if service["name"] == "news-express")

    assert web_service["autoDeployTrigger"] == "commit"
    assert web_service["buildFilter"]["paths"] == [
        "src/**",
        "config/**",
        "scripts/**",
        "templates/**",
        "pyproject.toml",
        "render.yaml",
    ]


def test_render_auth_defaults_are_production_safe():
    config = yaml.safe_load(Path("render.yaml").read_text(encoding="utf-8"))
    services = config["services"]
    web_service = next(service for service in services if service["name"] == "news-express")
    env = {item["key"]: item for item in web_service["envVars"]}

    assert env["NEWS_SUMMARY_AUTH_REQUIRED"]["value"] == "1"
    assert env["NEWS_SUMMARY_AUTH_DISABLED"]["value"] == "0"
    assert env["NEWS_SUMMARY_ADMIN_PASSWORD_HASH"]["sync"] is False
    assert env["NEWS_SUMMARY_SECRET_KEY"]["generateValue"] is True
    assert "NEWS_SUMMARY_ADMIN_PASSWORD" not in env
    assert "NEWS_SUMMARY_CSRF_DISABLED" not in env
    assert "NEWS_SUMMARY_AUTH_RATE_LIMIT_DISABLED" not in env
    assert "NEWS_SUMMARY_PUBLIC_HEALTH_DETAILS" not in env


def test_render_deploy_fallback_only_runs_for_runtime_paths():
    workflow = yaml.safe_load(Path(".github/workflows/render-deploy.yml").read_text(encoding="utf-8"))

    assert workflow["on"]["push"]["branches"] == ["codex/news-express"]
    assert workflow["on"]["push"]["paths"] == [
        "src/**",
        "config/**",
        "scripts/**",
        "templates/**",
        "pyproject.toml",
        "render.yaml",
    ]


def test_render_deploy_fallback_fails_loudly_when_hook_secret_missing():
    # 시크릿이 없을 때 조용히 skip하면 배포가 멈춰도 초록불이라 알아채기 어렵다.
    # 이 경우 워크플로가 명시적으로 실패(exit 1)하도록 강제한다.
    workflow = yaml.safe_load(Path(".github/workflows/render-deploy.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["deploy"]["steps"]
    guard = next(
        step for step in steps if step.get("if") == "env.RENDER_DEPLOY_HOOK_URL == ''"
    )
    assert "exit 1" in guard["run"]


def test_operations_deployment_report_reads_render_auto_deploy_config():
    from news_summary.web import _render_deploy_config_report

    report = _render_deploy_config_report()

    assert report["auto_deploy_trigger"] == "commit"
    assert report["auto_deploy_label"] == "커밋 시 자동 배포"
    assert report["auto_deploy_level"] == "ok"
