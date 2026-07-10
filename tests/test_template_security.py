import re
from pathlib import Path


TEMPLATE_DIR = Path("src/news_summary/templates")
POST_FORM_RE = re.compile(r"<form\b(?P<attrs>[^>]*)>(?P<body>.*?)</form>", re.IGNORECASE | re.DOTALL)


def test_every_post_form_includes_csrf_token():
    missing_tokens: list[str] = []

    for template_path in sorted(TEMPLATE_DIR.glob("*.html")):
        content = template_path.read_text(encoding="utf-8")
        for match in POST_FORM_RE.finditer(content):
            attrs = match.group("attrs")
            if not re.search(r"\bmethod\s*=\s*['\"]post['\"]", attrs, re.IGNORECASE):
                continue
            body = match.group("body")
            if "csrf_token()" not in body or 'name="_csrf_token"' not in body:
                missing_tokens.append(f"{template_path}:{content[: match.start()].count(chr(10)) + 1}")

    assert missing_tokens == []
