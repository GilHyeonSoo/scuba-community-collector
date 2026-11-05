# 구현 구조

## 모듈

| 경로 | 역할 |
|---|---|
| `collector/adapters/` | 플랫폼별 요청·페이지 탐색·커서 생성 |
| `collector/http.py` | 제한 시간, 호스트별 속도, 5xx 재시도, 429 대기, robots·챌린지 확인 |
| `collector/engine.py` | 대상별 작업 실행, 원본 적재 → 정규화, 오류 격리, 가져오기·재처리 |
| `collector/normalize.py` | 원본 형식 → 공통 Record 변환 |
| `collector/storage.py` | SQLite 트랜잭션, 원본 gzip, upsert, 검색·스트리밍 내보내기·보관 정리 |
| `collector/ui.py` | Tk/ttk 한국어 데스크톱 화면과 앱 내 스케줄러 |
| `collector/config.py` | 설정 검증, 데이터 위치, 원자적 설정 저장 |
| `collector/secrets.py` | Windows DPAPI 인증 보관, 환경 변수 우선 적용 |
| `collector/lock.py` | GUI/CLI 동시 커서 변경 방지 |
| `collector/__main__.py` | GUI/CLI 진입점 |

## 공통 레코드

| 필드 | 저장 형식 / 의미 |
|---|---|
| `id` | 플랫폼 접두사 + 충돌하지 않는 범위의 native ID, PK |
| `native_id` | 플랫폼 원래 ID |
| `platform` | reddit / google_play / app_store / dcinside / naver_cafe / scubaboard |
| `content_type` | post / comment / review |
| `title` | 제목, 댓글·일부 리뷰는 null |
| `body` | 텍스트. Reddit 본문은 원본 Markdown 유지 |
| `author` | 기본 HMAC 가명값 또는 null |
| `created_at` | UTC ISO 8601 또는 미제공 null |
| `source_url` | 원문/리뷰 위치 |
| `parent_id` | 부모 레코드 또는 스레드 참조, 선택적 |
| `extra_metadata` | SQLite JSON 텍스트(내보내기 시 JSON 객체) |
| `first_seen_at` | 최초 적재 UTC |
| `fetched_at` | 최종 적재 UTC |
| `raw_path` | 마지막 원본 봉투 위치, 로컬 전용 |
| `fingerprint` | 변경 감지 SHA-256, 내부 전용 |

PostgreSQL의 JSONB 대신 SQLite JSON 텍스트를 사용합니다. ScubaBoard 댓글의 `parent_id=scubaboard_thread_<thread_id>`는 외부 스레드 참조이며, 첫 글 레코드 ID에 대한 FK가 아닙니다. 네이버 검색 링크가 URL을 달리 표현하면 동일 글의 ID가 달라질 수 있습니다.

## 원본과 정합성

원본 봉투에는 `schema_version`, 수집 대상 설정의 스냅샷, `kind`, 원본 `payload`, 페이지 등 `context`, `loaded_at`이 들어갑니다. 인증 헤더·쿠키는 봉투에 넣지 않습니다. Reddit은 API 객체에서 콘텐츠와 필요한 공개 메타데이터를 추출하여 저장하며, 전체 HTTP 응답 봉투를 보관하지 않습니다.

변환·upsert·배치 완료 상태·실행 건수는 배치마다 하나의 DB 트랜잭션으로 반영됩니다. 실패한 배치의 레코드 일부만 반영하지 않습니다. 원본 파일과 DB 사이에는 분산 트랜잭션이 없으므로, 파일 저장 직후 프로세스가 강제 종료되면 DB에 등록되지 않은 원본 파일이 남을 수 있습니다. 정상적으로 `pending/error`에 등록된 배치는 UI/CLI에서 재처리할 수 있습니다.

소스 전체가 완료되거나 정상적인 한도 종료에 도달한 후 커서를 저장합니다. 실패/취소는 이전 커서를 유지합니다. 따라서 재수집으로 인한 중복 요청은 발생할 수 있지만 PK upsert로 중복 적재를 막습니다. 재처리는 해당 배치만 복구하며 실패한 네트워크 탐색 전체의 커서를 앞당기지 않습니다.

데이터 삭제·오래된 글 수정·중간 페이지 변동의 전수 동기화는 별도 제품 범위입니다. 이 프로그램은 제공되는 최신 목록과 이어받기 탐색을 중심으로 하는 조사용 수집기입니다.

## 확장

새 Collector는 `collect()`에서 `RawBatch`를 yield하고, 정상 종료 시 `state`, `partial`, `message`를 설정합니다. 변환은 `TRANSFORMS`, 실행 어댑터는 `COLLECTORS`에 등록합니다. 새 어댑터는 정상·한도 종료·에러·토큰 반복·재처리 경로를 검증해야 합니다.

현재 스레드 작업은 예외를 대상별로 격리하지만 프로세스 장애나 라이브러리의 무한 블로킹까지 격리하지는 않습니다. 대규모 운영에서 컨테이너 워커나 PostgreSQL을 도입하려면 `Engine`과 `Store` 경계를 바탕으로 작업 큐 및 저장소 구현을 확장해야 합니다.
