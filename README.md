# News Express

광주·전남 지자체 홈페이지의 보도자료를 수집하고, 방송 뉴스 단신 형태의 기사 초안을 생성하는 프로젝트입니다.

## 목표

1. 지자체 보도자료 원문을 자동 수집합니다.
2. 원문 주소, 제목, 본문, 출처를 데이터베이스에 보존합니다.
3. 인공지능으로 기사 초안을 생성하되, 검수 전 발행하지 않습니다.
4. 관리자 화면에서 `원문 -> 초안 -> 기자 검수 -> 발행 후보` 흐름을 유지합니다.

## 빠른 실행

```powershell
cd "C:\Users\seung\news summary"
.\scripts\setup.ps1
.\scripts\run_review_app.ps1
```

브라우저에서 `http://127.0.0.1:5000`을 열면 검수 화면을 볼 수 있습니다.
바탕화면 바로가기는 `scripts\start_news_express.ps1`을 사용하며, 서버가 꺼져 있으면 자동으로 다시 실행합니다.

`.env`에 `GEMINI_API_KEY`를 넣으면 Gemini가 기사 초안을 생성합니다. 키가 없으면 규칙 기반 방송 단신 초안으로 동작합니다.
검수 화면을 실행하면 자동 수집이 기본으로 켜져 매시간 정각마다 전체 기관 보도자료를 확인하고, 새 원문은 Gemini 기사 초안으로 만들어 검수 대기에 추가합니다.

## 설정

기본 설정에는 아래 공식 보도자료 소스가 켜져 있습니다.

- 광주광역시청 보도자료
- 전라남도청 보도자료
- 전남 17개 군청 보도자료/군정소식
  - 담양, 곡성, 구례, 고흥, 보성, 화순, 장흥, 강진, 해남, 영암, 무안, 함평, 영광, 장성, 완도, 진도, 신안

`config/municipalities.sample.yaml`을 참고해 `config/municipalities.yaml`의 시·군·구청 보도자료 주소와 선택자를 조정할 수 있습니다.

구독 피드가 있으면 `type: rss`를 우선 사용하고, 없으면 `type: html_board`를 사용합니다. 담양군처럼 목록을 자료 형태로 내려주는 곳은 `type: json_board`를 사용합니다.

## 사용

```powershell
news-summary init-db
news-summary show-sources
news-summary collect --limit 10
news-summary draft --limit 5
news-summary show-drafts
news-summary export
```

수집과 초안 생성을 한 번에 실행하려면:

```powershell
news-summary run --limit 10
```

PowerShell 스크립트로 수집과 초안 생성을 한 번에 실행하려면:

```powershell
.\scripts\collect_and_draft.ps1
```

## 자동 수집

`.\scripts\run_review_app.ps1`로 검수 화면을 켜두면 자동 수집기가 함께 실행됩니다.

- 기본 주기: 매시간 정각마다 1회
- 수집 범위: 설정에서 켜진 전체 기관
- 기본 처리: 기관별 최근 30건 확인, 새 원문은 Gemini 초안으로 생성
- Gemini 실패 시: 규칙 기반 초안을 만들지 않고 원문을 보류해 다음 주기에 다시 시도
- 수동 재수집: 홈 화면의 `수동 재수집` 버튼 하나로 전체 기관 확인과 Gemini 초안 생성을 함께 실행

필요하면 `.env`에서 아래 값을 조정할 수 있습니다.

```text
NEWS_SUMMARY_AUTO_COLLECT=1
NEWS_SUMMARY_AUTO_COLLECT_LIMIT=30
NEWS_SUMMARY_AUTO_DRAFT_LIMIT=250
NEWS_SUMMARY_AUTO_REQUIRE_GEMINI=1
NEWS_SUMMARY_GEMINI_LITE_UNTIL=
NEWS_SUMMARY_ADMIN_PASSWORD=
NEWS_SUMMARY_AUTH_REQUIRED=0
NEWS_SUMMARY_BACKUP_DIR=data/backups
NEWS_SUMMARY_LOG_DIR=data/logs
```

`NEWS_SUMMARY_GEMINI_LITE_UNTIL=YYYY-MM-DD`를 설정하면 해당 날짜까지 `gemini-3.5-flash` 실패 시 `gemini-3.1-flash-lite`를 함께 시도합니다. 날짜가 지나면 자동으로 3.5 Flash만 사용합니다.

`NEWS_SUMMARY_ADMIN_PASSWORD` 값을 설정하면 관리자 로그인이 강제됩니다. 운영 로그는 홈 화면에 표시하지 않고 `/ops-logs` 경로에서 확인합니다.

## 백업과 자동 실행

```powershell
.\scripts\backup_news_express.ps1
.\scripts\restore_news_express.ps1 -BackupPath data\backups\백업파일.zip -DryRun
.\scripts\restore_news_express.ps1 -BackupPath data\backups\백업파일.zip
.\scripts\install_startup_task.ps1
```

- 백업 대상: SQLite DB, 기사 설정, 수집 설정, `.env`, `exports/`
- `install_startup_task.ps1`은 Windows 작업 스케줄러에 5분마다 서버 생존 확인 작업을 등록합니다.

## 검수 화면

검수 화면에서 할 수 있는 일:

- 19개 기관별 최근 수집 상태와 수집량 확인
- Gemini 로컬 성공 호출 수와 Google AI Studio 사용량 페이지 확인
- 주의 필요, 오늘 기사, 신청·모집, 행사·교육, 지원·예산, 사진·카드뉴스, 게시일 확인 필터로 검수 우선순위 정리
- 원문과 기사 초안을 좌우로 비교
- 제목, 본문, 검수 메모 수정
- 본문 총 글자수와 초안 수정 이력 확인
- 승인 전 체크리스트로 날짜, 형식, 원문 길이, 신청 정보 반영 여부 확인
- 검수 대기, 승인, 반려 상태 변경
- 승인된 기사 마크다운/표 파일 내보내기

## 구조

- `config/`: 지자체별 수집 설정
- `templates/`: 기사 생성 지시문
- `src/news_summary/`: 수집, 저장, 기사 생성, 명령줄 코드
- `data/`: 데이터베이스와 개발용 데이터
- `exports/`: 승인 기사 내보내기 결과
- `docs/`: 기획 및 아키텍처 문서
- `tests/`: 테스트 코드

## 다음 개발 단계

1. 광주 5개 구청과 전남 시청 보도자료까지 수집 범위 확대
2. 발행 시스템 연동
3. 예약 실행 또는 작업 스케줄러 등록
4. 기사 스타일 지시문 세분화
