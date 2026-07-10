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
- 보관 기준: 주말과 공휴일을 제외한 최근 3운영일 범위의 원문과 초안만 유지
- Gemini 실패 시: 규칙 기반 초안을 만들지 않고 원문을 보류해 다음 주기에 다시 시도
- 수동 재수집: 홈 화면의 `수동 재수집` 버튼 하나로 전체 기관 확인과 Gemini 초안 생성을 함께 실행

필요하면 `.env`에서 아래 값을 조정할 수 있습니다.

```text
NEWS_SUMMARY_AUTO_COLLECT=1
NEWS_SUMMARY_AUTO_COLLECT_LIMIT=30
NEWS_SUMMARY_AUTO_DRAFT_LIMIT=250
NEWS_SUMMARY_AUTO_REQUIRE_GEMINI=1
NEWS_SUMMARY_GEMINI_SOURCE_MAX_CHARS=2400
NEWS_SUMMARY_ADMIN_PASSWORD=
NEWS_SUMMARY_AUTH_REQUIRED=0
NEWS_SUMMARY_BACKUP_DIR=data/backups
NEWS_SUMMARY_LOG_DIR=data/logs
NEWS_SUMMARY_RETENTION_DAYS=3
NEWS_SUMMARY_RETENTION_HOLIDAYS=
DATABASE_URL=
```

초안 생성 모델은 원문 난이도에 따라 자동으로 고릅니다. 지원사업·신청·금액·기간 정보가 많은 원문은 `gemini-3.5-flash`, 단순 행사·캠페인성 원문은 `gemini-3.1-flash-lite`를 우선 사용합니다. 수동 다듬기는 품질 유지를 위해 `gemini-3.5-flash`를 사용합니다.

`NEWS_SUMMARY_GEMINI_SOURCE_MAX_CHARS`는 Gemini에 보내는 원문 본문 발췌 길이입니다. 기본값은 2400자이며, 품질 저하를 막기 위해 1200자 미만으로는 내려가지 않습니다.

`NEWS_SUMMARY_RETENTION_HOLIDAYS`에는 추가 공휴일을 `YYYY-MM-DD,YYYY-MM-DD` 형식으로 넣을 수 있습니다. 공휴일과 주말은 최근 3일 계산에서 제외되어 그만큼 보관 범위가 늘어납니다.

`NEWS_SUMMARY_ADMIN_PASSWORD` 또는 `NEWS_SUMMARY_ADMIN_PASSWORD_HASH` 값을 설정하면 관리자 로그인이 강제됩니다. 관리자 비밀번호는 12자 이상, 영문/숫자/기호를 섞은 예측 어려운 값으로 설정합니다. 운영 환경에서는 평문 비밀번호보다 아래 명령으로 만든 해시 값을 `NEWS_SUMMARY_ADMIN_PASSWORD_HASH`에 넣는 방식을 권장합니다. 운영 로그는 홈 화면에 표시하지 않고 `/ops-logs` 경로에서 확인합니다.

```powershell
.\.venv\Scripts\python.exe -m news_summary.cli admin-password-hash
```

운영용 해시는 위처럼 비밀번호 인자 없이 실행해 화면에 표시되지 않게 입력합니다. 비밀번호를 명령어 뒤에 직접 붙이면 PowerShell 기록에 남을 수 있습니다.

## PostgreSQL 전환

기본값은 기존처럼 SQLite 파일(`NEWS_SUMMARY_DB`)입니다. 클라우드 PostgreSQL을 사용하려면 `.env`에 `DATABASE_URL`을 추가합니다.

```text
DATABASE_URL=postgresql://kbcnews:비밀번호@발급받은호스트:5432/kbcnews?sslmode=require
```

`DATABASE_URL`이 있으면 웹앱과 CLI는 PostgreSQL을 우선 사용합니다. URL에는 비밀번호가 들어가므로 GitHub에 올리면 안 됩니다.

기존 SQLite 데이터를 PostgreSQL로 옮기려면 먼저 SQLite 백업을 만든 뒤 아래 명령을 실행합니다.

```powershell
news-summary migrate-sqlite-to-postgres --sqlite-db data/news_summary.sqlite --database-url "postgresql://kbcnews:비밀번호@발급받은호스트:5432/kbcnews?sslmode=require"
```

대상 PostgreSQL DB에 이미 데이터가 있으면 명령은 중단됩니다. 기존 데이터를 비우고 다시 넣어야 할 때만 `--replace`를 추가합니다.

PostgreSQL 모드에서는 앱 백업 ZIP 안에 `data/postgres_export.json` 논리 덤프가 포함됩니다. 운영 복구 안정성을 위해 이 앱 백업과 함께 클라우드 DB 제공자의 백업/스냅샷도 유지합니다.

## Render Starter 배포

2~3명이 외부에서 테스트하고 PC를 계속 켜두지 않으려면 Render Starter 웹 서비스를 사용할 수 있습니다.

프로젝트 루트의 `render.yaml`은 아래 조건으로 준비되어 있습니다.

- 서비스 이름: `news-express`
- 플랜: Starter
- 실행 방식: `gunicorn` 단일 워커, 8스레드
- 헬스체크: `/healthz`
- DB: Render가 아닌 외부 PostgreSQL, 예: Neon `DATABASE_URL`
- 자동 수집: 웹 서비스 프로세스 안에서 매시간 정각 실행

배포 순서:

1. GitHub에 최신 코드가 푸시되어 있는지 확인합니다.
2. Render Dashboard에서 `New` -> `Blueprint`를 선택합니다.
3. GitHub 저장소 `seunghooda-dev/news-express`를 연결합니다.
4. Blueprint 파일은 기본값 `render.yaml`을 사용합니다.
5. 아래 비밀 환경변수를 Render 화면에서 직접 입력합니다.

```text
DATABASE_URL=Neon PostgreSQL 연결 문자열
GEMINI_API_KEY=Gemini API 키
NEWS_SUMMARY_ADMIN_PASSWORD_HASH=admin-password-hash 명령으로 생성한 해시
```

`NEWS_SUMMARY_SECRET_KEY`는 Render가 자동 생성합니다.

배포 후 Render가 제공하는 `https://news-express.onrender.com` 형태의 주소로 접속합니다. 기본 배포 설정은 로그인 보호가 켜진 상태입니다. 임시 테스트 중에만 비밀번호 없이 접속해야 하면 Render 환경변수에서 `NEWS_SUMMARY_AUTH_DISABLED=1`로 바꿀 수 있지만, 정식 공유 전에는 반드시 다시 `0`으로 되돌리고 `NEWS_SUMMARY_ADMIN_PASSWORD_HASH`를 설정합니다.

`/healthz`는 Render가 사용하는 공개 헬스체크입니다. 내부 운영 상태가 더 많이 담긴 `/healthz/details`는 관리자 로그인이 켜진 운영 환경에서는 기본적으로 로그인 뒤에만 볼 수 있습니다. 외부 모니터링 도구가 상세 상태까지 꼭 봐야 할 때만 `NEWS_SUMMARY_PUBLIC_HEALTH_DETAILS=1`을 설정합니다.

### Render 자동 배포 보완

Render의 `Auto-Deploy`가 `On Commit`인데도 GitHub 푸시가 자동 배포로 이어지지 않으면 GitHub Actions 보완 배포를 사용합니다. 저장소에는 `.github/workflows/render-deploy.yml`이 포함되어 있으며, `codex/news-express` 브랜치에서 운영 영향 경로가 바뀔 때만 Render Deploy Hook을 호출합니다.

배포 대상 경로:

- `src/**`
- `config/**`
- `scripts/**`
- `templates/**`
- `pyproject.toml`
- `render.yaml`

설정 순서:

1. Render Dashboard의 `news-express` 서비스 `Settings` -> `Deploy Hook`에서 hook URL을 복사합니다.
2. GitHub 저장소 `Settings` -> `Secrets and variables` -> `Actions`에서 새 Repository secret을 만듭니다.
3. Secret 이름은 `RENDER_DEPLOY_HOOK_URL`로 지정하고, 값에는 Render Deploy Hook URL을 넣습니다.
4. 이후 `codex/news-express` 브랜치의 운영 영향 경로가 바뀔 때 GitHub Actions가 Render 배포를 보완 실행합니다.

Deploy Hook URL은 비밀번호와 같은 민감 정보입니다. README, 코드, 이슈, 채팅에 직접 저장하지 말고 GitHub Secret 또는 Render 화면 안에서만 관리합니다.
Secret이 아직 없으면 워크플로는 실패하지 않고 보완 배포를 건너뜁니다.

## 외부 접속: Cloudflare Tunnel

회사 기자들과 함께 쓰려면 아래 순서로 설정합니다.

1. 로컬 앱 로그인 보호를 먼저 켭니다.
   - `.env`에 `NEWS_SUMMARY_AUTH_REQUIRED=1`을 설정합니다.
   - 로컬에서 `http://127.0.0.1:5000/admin/setup`에 접속해 관리자 비밀번호를 먼저 만듭니다.
2. Cloudflare 계정에 도메인을 연결합니다.
3. `cloudflared tunnel login`을 실행해 Cloudflare 계정과 도메인을 승인합니다.
4. 정식 호스트명을 정한 뒤 터널을 설치합니다.

```powershell
.\scripts\install_cloudflare_named_tunnel.ps1 -Hostname news.example.com
```

도메인을 아직 연결하지 않았거나 임시 테스트 링크만 필요하면 아래 명령을 사용할 수 있습니다.

```powershell
.\scripts\start_cloudflare_quick_tunnel.ps1
```

임시 링크가 살아 있는지 확인하려면 아래 점검 스크립트를 실행합니다.

```powershell
.\scripts\check_cloudflare_quick_tunnel.ps1
```

정식 공유 전에는 Cloudflare Zero Trust의 Access 애플리케이션에서 허용할 기자 이메일만 등록해야 합니다.

## 백업과 자동 실행

```powershell
.\scripts\backup_news_express.ps1
.\scripts\restore_news_express.ps1 -BackupPath data\backups\백업파일.zip -DryRun
.\scripts\restore_news_express.ps1 -BackupPath data\backups\백업파일.zip
.\scripts\install_startup_task.ps1
```

- 백업 대상: SQLite DB 또는 PostgreSQL JSON 덤프, 기사 설정, 수집 설정, `exports/`
- 기본 로컬 백업에는 `.env`가 포함됩니다. 운영 환경처럼 비밀값을 환경변수로 관리하는 경우 `NEWS_SUMMARY_BACKUP_INCLUDE_ENV=0`을 설정하면 `.env`를 백업에서 제외합니다.
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
