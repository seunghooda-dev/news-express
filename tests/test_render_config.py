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


def test_operations_deployment_report_reads_render_auto_deploy_config():
    from news_summary.web import _render_deploy_config_report

    report = _render_deploy_config_report()

    assert report["auto_deploy_trigger"] == "commit"
    assert report["auto_deploy_label"] == "커밋 시 자동 배포"
    assert report["auto_deploy_level"] == "ok"
