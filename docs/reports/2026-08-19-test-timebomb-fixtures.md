# 시한폭탄 픽스처 리포트 (2026-08-19)

- **대상**: KBC NEWS EXPRESS (`news-express-1.onrender.com`) 테스트 스위트
- **범위**: 사용자 지시 "사전-실패 테스트 1건 수정" + "앞으로 이런 증상이 안 나타나게 보완"
- **기준 커밋**: `6317706` → `7ac1a65` (테스트 1건 수정, `[skip render]` — 프로덕션 코드 무변경)
- **방법**: 실패 재현 → 근본 원인 규명 → `now()` 접촉 테스트 6파일 전수 감사 → 상대 날짜로 수정. 감사는 읽기 전용, 수정은 한 곳.

## 1. 증상 — 코드 변경 0인데 어느 날 빨개진다

`test_scheduler_recovery_loop.py::test_recovered_releases_are_actually_saved`가 auth 변경과 무관하게 **항상** 실패했다(`saved`가 빈 집합). 어제까지 초록이던 테스트가, 소스에 손대지 않았는데 오늘 빨개진다 — 이게 **시한폭탄 픽스처**다. 고정 날짜를 박아 두면 실시간이 흐르며 그 날짜가 `now()` 기준 창 밖으로 밀린다.

## 2. 근본 원인 — 고정 게시일이 보관창 밖으로 밀렸다

진단이 처음엔 config 소스 맵을 지목했으나 그건 틀렸다. `two_sources` 픽스처가 `_source_recovery_candidates`를 몽키패치해 후보를 **직접** 주므로 `_collectable_source_map(load_sources)` 경로는 애초에 타지 않는다. 후보 산출과 `collect_source_with_fallback`는 정상이다.

진짜 원인은 **보관 필터**였다. `make_release`가 게시일을 `"2026-08-11"`로 고정했는데, 복구 저장 경로(`_recover_failed_sources_once`)는 내부에서 `collection_retention_cutoff_date()`를 **실시간 now()로** 계산한다(영업일 3일, `DEFAULT_RETENTION_DAYS=3`). 2026-08-18 기준 컷오프는 `2026-08-13`이라, `filter_releases_by_retention`이 2026-08-11 기사를 `add_press_release` **전에** 걸러낸다 → 저장 0건 → 단언 실패. 작성 시점(2026-08-12)엔 창 안이었으나 시계가 지나며 창 밖으로 밀렸다. 나머지 4개 형제 테스트는 저장 내용을 보지 않아 통과했다 — 그래서 이 1건만 빨개졌다.

## 3. 수정 — 시간 의존 제거 (단언 유지)

픽스처 게시일을 오늘(KST)로 바꿔 시계 독립으로 만들었다. 단언과 원 의도(복구가 두 소스 기사를 실제로 저장)는 그대로다.

```python
def _within_retention_date() -> str:
    """보관 기간(영업일 3일) 밖 날짜는 복구가 저장 전에 걸러낸다 — 오늘(KST)로 시간 의존을 없앤다."""
    return datetime.now(timezone(timedelta(hours=9))).date().isoformat()
```

## 4. 전수 감사 — 터진 건 하나뿐

`now()`에 닿는 테스트 6파일을 전부 추적했다. 유일한 폭탄은 복구 루프 테스트였고 나머지는 이미 안전하다.

| 파일 | 판정 | 근거 |
|---|---|---|
| `test_scheduler_recovery_loop.py` | 수정함 | 유일한 폭탄. 고정 게시일 → 오늘(KST) |
| `test_service.py` | 안전 | **같은 복구 경로**를 `FixedDatetime` + `collection_retention_cutoff_date` stub으로 올바르게 시험. 나머지도 `today=`/`now=` 명시 |
| `test_scheduler_pruning.py` | 안전 | `now`를 잡아 코드에 그대로 넘김 / 개수 기준 / 라벨 폴더 |
| `test_retention_filter.py` | 안전 | 명시적 `CUTOFF = date(2026, 8, 1)` |
| `test_web.py` | 안전 | `FixedDatetime` / `today=`·`now=` 명시 / `now − timedelta` 상대 오프셋 / 과거 날짜는 표시용 데이터 |
| `test_cardnews_service.py`·`test_exporter.py` | 안전 | 명시적 컷오프 문자열 / 발행일은 내보내기와 무관한 데이터 |

두 가지가 결론을 떠받친다. 하나, `test_service.py`가 **동일한 복구 경로를 이미 올바르게** 시험하므로 복구 루프 테스트는 규약을 안 지킨 외톨이였다. 둘, 과거 날짜(`2026-05`, `2026-07`)를 쓰면서 어제까지 초록이던 테스트는 정의상 real now()에 안 물린 것이다 — 안 그랬으면 이미 빨개졌을 테니까.

## 5. 권장 시험 패턴 — 새 픽스처는 반드시 이 중 하나

`now()` 기준 창(보관·나이·지연·쿨다운)에 닿는 픽스처는 **절대 고정 날짜를 시계 고정 없이 박지 않는다.** 이미 이 저장소가 쓰는 네 가지 안전 패턴이다.

**① 명시적 참조 주입 (가장 권장)** — 코드가 `today=`/`now=`를 받으면 테스트가 그 값을 준다.
```python
assert collection_retention_cutoff_date(today=date(2026, 6, 29)) == date(2026, 6, 25)
now = datetime.now(timezone.utc)
collector._prune_old_operation_events_once(now)   # 잡은 now를 그대로 넘긴다
```

**② 고정 시계** — 코드가 참조를 안 받으면 모듈의 `datetime`을 바꿔치기한다.
```python
class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 7, 6, 10, 0, tzinfo=tz or timezone.utc)
monkeypatch.setattr("news_summary.scheduler.datetime", FixedDatetime)
monkeypatch.setattr(
    "news_summary.scheduler.collection_retention_cutoff_date", lambda today=None: date(2026, 7, 2)
)
```

**③ 상대 오프셋** — "31분 전", "10분 후"는 시계가 흘러도 참이다.
```python
stale_at = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
next_run_at = (datetime.now(LOCAL_TZ) + timedelta(minutes=10)).isoformat()
```

**④ 상대 날짜** — 보관창 **안**에 들어야 하는 기사면 오늘(KST)로 만든다(§3의 헬퍼).

**안티패턴** — 절대 최근 날짜 + 시계 고정 없는 real now() 판정. 이번에 터진 바로 그 모양이다.
```python
published_at="2026-08-11"   # ← 며칠 뒤 보관창 밖으로 밀려 조용히 빨개진다
```

## 6. 검증

- 대상 파일 **5/5**, 전체 스위트 **602 passed**(2026-08-18). 종료코드 0.
- 날짜가 바뀐 **2026-08-19에도 602 passed, exit 0** — 롤오버로 아무것도 안 터졌고, 수정이 시계 독립임이 실측됐다.
- 배포는 `scripts/safe_deploy.py`로만. 수집 회차 진행 중이라 회차 종료까지 기다렸다 push. `[skip render]`라 러닝 서비스는 `07b2d4d` 그대로 — 재빌드·회차 방해 없음(테스트 전용 변경이라 프로덕션 무관).
