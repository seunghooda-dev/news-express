from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections.abc import Iterable

from news_summary.settings import env_path, load_environment
from news_summary.storage import Store
from news_summary.writer import build_system_prompt, _ensure_news_brief_title, _normalize_three_paragraph_body


DEFAULT_MODELS = (
    "gemini-3.5-flash",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
    "gemini-pro-latest",
)


def main() -> None:
    load_environment()
    parser = argparse.ArgumentParser(description="규칙 기반 기사 초안을 Gemini로 일괄 재작성합니다.")
    parser.add_argument("--batch-size", type=int, default=5, help="Gemini 요청 1회에 묶을 초안 수")
    parser.add_argument("--limit", type=int, default=0, help="최대 처리 건수, 0이면 전체")
    parser.add_argument("--sleep", type=float, default=1.0, help="요청 사이 대기 초")
    parser.add_argument("--models", default="", help="쉼표로 구분한 Gemini 모델명 목록")
    args = parser.parse_args()

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY 또는 GOOGLE_API_KEY가 설정되어 있지 않습니다.")

    try:
        from google import genai
        from google.genai import types
    except ModuleNotFoundError as exc:
        raise SystemExit("google-genai 패키지가 설치되어 있지 않습니다.") from exc

    store = Store(env_path("NEWS_SUMMARY_DB", "data/news_summary.sqlite"))
    client = genai.Client(api_key=api_key)
    models = _model_list(args.models)
    total_updated = 0
    exhausted: list[str] = []

    for model_name in models:
        print(f"MODEL_START {model_name}", flush=True)
        while True:
            remaining_allowed = None if args.limit <= 0 else max(args.limit - total_updated, 0)
            if remaining_allowed == 0:
                break
            batch_limit = min(args.batch_size, remaining_allowed) if remaining_allowed else args.batch_size
            rows = _fetch_rule_based_rows(store, batch_limit)
            if not rows:
                print("DONE all rule-based drafts regenerated", flush=True)
                _print_counts(store)
                return

            try:
                results = _request_batch(client, types, model_name, rows)
            except Exception as exc:  # noqa: BLE001 - API errors should be logged and model-switched.
                message = str(exc)
                if _should_switch_model(message):
                    print(f"MODEL_SWITCH {model_name} {message[:220]}", flush=True)
                    exhausted.append(model_name)
                    break
                if len(rows) > 1:
                    print(f"BATCH_RETRY_SINGLE {model_name} {type(exc).__name__}: {message[:220]}", flush=True)
                    single_updated, quota_exhausted = _retry_single_rows(store, client, types, model_name, rows)
                    total_updated += single_updated
                    if quota_exhausted:
                        exhausted.append(model_name)
                        break
                    time.sleep(args.sleep)
                    continue
                print(f"FAILED draft_id={rows[0]['draft_id']} model={model_name} {type(exc).__name__}: {message[:500]}", flush=True)
                raise

            updated = _save_results(store, model_name, rows, results)
            total_updated += updated
            print(f"BATCH_OK model={model_name} updated={updated} total_updated={total_updated}", flush=True)
            _print_counts(store)
            time.sleep(args.sleep)

    print("STOP models exhausted or limit reached", flush=True)
    if exhausted:
        print("EXHAUSTED " + ", ".join(exhausted), flush=True)
    _print_counts(store)


def _model_list(raw: str) -> list[str]:
    names = [name.strip() for name in raw.split(",") if name.strip()] if raw else []
    env_model = os.getenv("GEMINI_MODEL")
    if env_model and not names:
        names.insert(0, env_model)
    names.extend(DEFAULT_MODELS)
    deduped = []
    for name in names:
        if name not in deduped:
            deduped.append(name)
    return deduped


def _fetch_rule_based_rows(store: Store, limit: int):
    with store.connect() as conn:
        return conn.execute(
            """
            SELECT ad.id AS draft_id, ad.status, ad.model AS old_model,
                   pr.id AS press_release_id, pr.source_id, pr.source_name, pr.region,
                   pr.title, pr.url, pr.content, pr.published_at, pr.collected_at,
                   pr.validation_status, pr.validation_note
            FROM article_drafts ad
            JOIN press_releases pr ON pr.id = ad.press_release_id
            WHERE ad.model LIKE '%:rule-based%'
            ORDER BY ad.id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def _request_batch(client, types, model_name: str, rows) -> list[dict[str, object]]:
    response = client.models.generate_content(
        model=model_name,
        contents=_batch_prompt(rows),
        config=types.GenerateContentConfig(
            system_instruction=build_system_prompt(),
            response_mime_type="application/json",
            temperature=0.3,
        ),
    )
    return _parse_json_response(response.text or "")


def _batch_prompt(rows) -> str:
    parts = [
        "아래 여러 지자체 보도자료를 각각 방송 뉴스 단신으로 재작성하세요.",
        "반드시 JSON 배열만 반환하세요. 마크다운 코드블록은 쓰지 마세요.",
        "각 배열 항목 형식: {\"draft_id\": 숫자, \"title\": \"[뉴스 단신] ...\", \"body\": \"문단1\\n\\n문단2\\n\\n문단3\", \"review_note\": \"원문 대조 메모\"}",
        "본문은 항목마다 정확히 3문단이어야 합니다.",
        "원문에 없는 날짜, 금액, 인원, 지명, 기관명, 발언을 만들지 마세요.",
    ]
    for row in rows:
        parts.append(
            "\n".join(
                [
                    f"\n[초안ID: {row['draft_id']}]",
                    f"출처: {row['source_name']}",
                    f"지역: {row['region']}",
                    f"원문 제목: {row['title']}",
                    f"원문 URL: {row['url']}",
                    f"게시일: {row['published_at'] or '미상'}",
                    "원문 본문:",
                    row["content"],
                    f"[/초안ID: {row['draft_id']}]",
                ]
            )
        )
    return "\n\n".join(parts)


def _parse_json_response(text: str) -> list[dict[str, object]]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    if not cleaned.startswith("["):
        match = re.search(r"\[[\s\S]*\]", cleaned)
        if match:
            cleaned = match.group(0)
    data = json.loads(cleaned)
    if not isinstance(data, list):
        raise ValueError("Gemini 응답이 JSON 배열이 아닙니다.")
    return [item for item in data if isinstance(item, dict)]


def _save_results(store: Store, model_name: str, rows, results: Iterable[dict[str, object]]) -> int:
    by_id = {}
    for result in results:
        try:
            by_id[int(result["draft_id"])] = result
        except (KeyError, TypeError, ValueError):
            continue

    updated = 0
    for row in rows:
        result = by_id.get(int(row["draft_id"]))
        if not result:
            raise ValueError(f"Gemini 응답에 draft_id={row['draft_id']} 항목이 없습니다.")
        title = _ensure_news_brief_title(str(result.get("title") or row["title"]))
        body = _normalize_three_paragraph_body(str(result.get("body") or ""))
        review_note = str(result.get("review_note") or "Gemini 일괄 재작성 완료. 원문 대조가 필요합니다.").strip()
        store.update_draft(
            int(row["draft_id"]),
            title,
            body,
            review_note,
            row["status"],
            f"{model_name}:gemini",
        )
        updated += 1
        print(f"UPDATED draft_id={row['draft_id']} model={model_name}:gemini", flush=True)
    return updated


def _retry_single_rows(store: Store, client, types, model_name: str, rows) -> tuple[int, bool]:
    updated = 0
    for row in rows:
        try:
            results = _request_batch(client, types, model_name, [row])
            updated += _save_results(store, model_name, [row], results)
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if _should_switch_model(message):
                print(f"MODEL_SWITCH {model_name} {message[:220]}", flush=True)
                return updated, True
            print(f"SINGLE_FAILED draft_id={row['draft_id']} model={model_name} {type(exc).__name__}: {message[:300]}", flush=True)
            raise
    return updated, False


def _is_quota_error(message: str) -> bool:
    lowered = message.lower()
    return "429" in message or "resource_exhausted" in lowered or "quota" in lowered


def _should_switch_model(message: str) -> bool:
    lowered = message.lower()
    return _is_quota_error(message) or "503" in message or "unavailable" in lowered


def _print_counts(store: Store) -> None:
    with store.connect() as conn:
        remaining = conn.execute("SELECT COUNT(*) AS count FROM article_drafts WHERE model LIKE '%:rule-based%'").fetchone()[
            "count"
        ]
        gemini = conn.execute("SELECT COUNT(*) AS count FROM article_drafts WHERE model LIKE '%:gemini'").fetchone()[
            "count"
        ]
    print(f"COUNTS remaining_rule_based={remaining} gemini={gemini}", flush=True)


if __name__ == "__main__":
    main()
