# 운영 방법

## 처음 실행

```powershell
cd "C:\Users\seung\news summary"
.\scripts\setup.ps1
.\scripts\run_review_app.ps1
```

브라우저에서 `http://127.0.0.1:5000`을 엽니다.

## 매일 쓰는 흐름

1. `원문 수집`을 누릅니다.
2. `초안 생성`을 누릅니다.
3. 각 초안을 열어 원문과 비교합니다.
4. 필요하면 제목과 본문을 수정합니다.
5. 문제가 없는 초안은 `승인`으로 바꿉니다.
6. `승인 기사 내보내기`를 누릅니다.

## 명령줄 흐름

```powershell
.\scripts\collect_and_draft.ps1
.\.venv\Scripts\python.exe -m news_summary.cli show-drafts
.\.venv\Scripts\python.exe -m news_summary.cli export
```

## 인공지능 API

`.env`에 `GEMINI_API_KEY`를 넣으면 Gemini 기사 초안을 사용할 수 있습니다.

키가 없어도 규칙 기반 초안을 만들기 때문에 수집과 검수 흐름은 그대로 확인할 수 있습니다.
