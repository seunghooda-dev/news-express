from __future__ import annotations

import csv
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .storage import Store

LOCAL_TZ = timezone(timedelta(hours=9))


def export_approved(
    store: Store,
    output_dir: Path,
    unexported_only: bool = True,
    approved_on: date | None = None,
) -> tuple[Path, Path, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = list(store.approved_drafts(unexported_only=unexported_only))
    if approved_on:
        rows = [row for row in rows if _approved_local_date(row) == approved_on]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    markdown_path = output_dir / f"승인기사_{stamp}.md"
    csv_path = output_dir / f"승인기사_{stamp}.csv"

    with markdown_path.open("w", encoding="utf-8", newline="\n") as file:
        file.write("# 승인 기사\n\n")
        for row in rows:
            file.write(f"## {row['title']}\n\n")
            file.write(f"- 출처: {row['source_name']}\n")
            file.write(f"- 원문: {row['url']}\n")
            if row["published_at"]:
                file.write(f"- 게시일: {row['published_at']}\n")
            file.write("\n")
            file.write(row["body"].strip())
            file.write("\n\n")
            if row["review_note"]:
                file.write("검수 메모:\n")
                file.write(row["review_note"].strip())
                file.write("\n\n")

    with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["번호", "제목", "본문", "검수 메모", "출처", "원문 주소", "게시일"])
        for row in rows:
            writer.writerow(
                [
                    row["id"],
                    row["title"],
                    row["body"],
                    row["review_note"],
                    row["source_name"],
                    row["url"],
                    row["published_at"],
                ]
            )

    store.mark_exported([int(row["id"]) for row in rows])
    return markdown_path, csv_path, len(rows)


def _approved_local_date(row) -> date | None:
    value = row["approved_time"] or row["updated_at"] or row["created_at"]
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo:
        parsed = parsed.astimezone(LOCAL_TZ)
    else:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return parsed.date()
