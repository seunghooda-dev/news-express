# 운영 하드닝·자동배포 복구 리포트 (2026-07-14)

- **프로젝트**: news-express (뉴스 요약/보도자료 초안 웹앱, Flask + PostgreSQL, Render 배포)
- **DB**: 작성 당시는 Neon — 이후 Supabase로 옮겼다. 아래 본문의 Neon 언급은 작성 당시 기록이며, 현재 DB는 Supabase(Session pooler)다.
- **저장소/브랜치**: `seunghooda-dev/news-express` · `codex/news-express`
- **라이브**: https://news-express-5uq0.onrender.com (작성 당시 주소 — 2026-07-16 싱가포르 이전 후 현재 주소는 https://news-express-1.onrender.com)

## 1. 개요

로컬 소스 검토에서 시작해 운영 보안·배포·버그·UI를 순차적으로 개선했다.

## 2. 수행 내용 (커밋)

| 커밋 | 내용 |
|------|------|
| `45931e5` | 운영관리 전용 비밀번호 게이트(항상 입력) + RSS 파싱 defusedxml + SSL 검증 자체점검 |
| `7c990be` | 운영 쓰기작업(백업·복원·자동수집) 잠금해제도 운영 비밀번호로 통일 |
| `231ce94` | 배포 훅 시크릿 미설정 시 워크플로가 조용히 통과하던 문제 → 실패로 노출 |
| `b6b12e9` | 자동배포 복구 후 불필요해진 fallback 워크플로 제거 |
| `e11bb81` | 운영 관리 비밀번호를 화면에서 변경(DB 저장, 약한 비번 허용) |
| `f445fc2` | 자동 수집 UniqueViolation 수정(원자적 upsert) |
| `889f5b2` | 대시보드 split 카드 세로 높이 정렬 |
| `017518e` | Render 리전을 싱가포르로 명시(강진 수집용, 새 서비스에 적용) |
| (이번) | 페이지별 브라우저 제목 + CSP script-src nonce 강화 |

## 3. 핵심 이슈 원인·해결

### 자동배포 5일 정지
GitHub→Render webhook 끊김 + fallback 워크플로가 시크릿 없이 조용히 skip(초록불). Git 재연결로 webhook 복구 → native 자동배포 정상화. 워크플로는 제거, 실패 가시화 로직은 반영 후 정리.

### 자동 수집 UniqueViolation
동시 수집 시 `SELECT-후-INSERT` 경합으로 PostgreSQL 유니크 제약 위반. `INSERT ... ON CONFLICT (url) DO NOTHING RETURNING id` 원자적 upsert로 해결.

### 강진군 수집 실패 (미해결 · 계획됨)
Render가 US(Oregon) 리전이라 강진(gangjin.go.kr)이 미국 IP를 차단 → `ConnectTimeout`. 로컬(한국 IP)·다른 지자체는 정상. Render는 리전 변경 불가라 **싱가포르로 새 서비스 재생성 필요**. 비용(중복 $7) 때문에 **2026년 7월 말 이전 예정**. `render.yaml`에 `region: singapore` 준비됨.

## 4. 백업 내구성 점검 결과

- 백업은 PostgreSQL에서도 정상 동작(`_add_postgres_export`로 JSON 내보내기), 운영관리에서 **다운로드 가능**.
- 단, 자동 저장본은 Render `/tmp`라 **재시작 시 소멸** — 오프박스 보관 아님. 자체 점검이 "임시 경로"로 경고 중.
- **실제 DB 내구성은 Neon(관리형 Postgres)이 담당**하므로, 앱 백업은 보조 수단. 결론: 현 구조로 충분하며, 장기 보관이 필요하면 운영관리에서 수동 다운로드하거나 Neon 백업/PITR을 이용.

## 5. UI/기타 개선

- **대시보드 카드 높이**: `.split` 컬럼을 flex로, `.list`가 컬럼을 채우게 해 두 카드 높이 일치(라이브 실측 0px 차이).
- **페이지별 제목**: 모든 페이지가 `<title>News Express</title>`로 같던 것을 페이지별(`대시보드 · News Express` 등)로. 뷰가 넘기는 `page_title`이 있으면 그 값 사용, 없으면 엔드포인트 매핑.
- **CSP 강화**: `script-src`에서 `'unsafe-inline'` 제거하고 요청별 **nonce** 적용. 인라인 스크립트 4곳에 nonce 부여로 XSS 방어 강화.

## 6. 검증

- 자동화 테스트 전체 통과(신규: 운영 비번 게이트/통일/변경, 중복 URL 멱등, 배포 워크플로 실패, 페이지 제목, CSP nonce 등).
- 라이브 end-to-end: 운영관리 게이트·비번, 자동배포, 카드 높이 픽셀 일치 확인.

## 7. 남은 것

- **강진**: 7월 말 싱가포르 리전 이전 시 해결 예정(계획 확정).
- 운영 비밀번호: 현재 skwmakzl1(관리자 폴백), 운영관리 화면에서 DB 전용 비번으로 변경 가능.
