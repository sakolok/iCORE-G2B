# iCore 현재 API 참조

이 문서는 현재 FastAPI 애플리케이션에 등록된 인증, 상태 확인, 입찰공고 수집, 개찰결과 API를 요약한다. 모든 경로는 같은 API 호스트를 기준으로 한다.

## 인증 방식

| 구분 | 전달 방식 | 사용 범위 |
| --- | --- | --- |
| 공개 | 인증 헤더 없음 | 상태 확인 및 로그인 |
| 사용자 | `Authorization: Bearer <access_token>` | 사용자·조직 데이터 조회 및 변경 |
| 내부 수집 | `X-Scraper-Internal-Token` | 수집 워커의 중복 제거·실행 보고 |
| Cloud Scheduler | 내부 수집 토큰과 OIDC Bearer 토큰을 함께 확인 | 운영 환경의 12시간 개찰결과 수집 |

로그인 응답의 `access_token`을 이후 사용자 API의 Bearer 토큰으로 사용한다. 토큰, Google credential, 내부 토큰, 서비스계정 키는 문서나 프론트 코드에 기록하지 않는다.

## Health

| 메서드·경로 | 인증 | 목적 |
| --- | --- | --- |
| `GET /api/health` | 공개 | API 프로세스의 기본 상태를 `{"status":"ok"}`로 확인한다. |

## Auth

| 메서드·경로 | 인증 | 요청 핵심 필드 | 목적 |
| --- | --- | --- | --- |
| `POST /api/auth/google` | 공개 | `credential` | Google ID credential을 검증하고 허용된 Workspace 사용자에게 세션 토큰을 발급한다. |
| `POST /api/auth/login` | 공개 | `username`, `password` | 설정에서 레거시 비밀번호 로그인이 활성화된 경우에만 세션 토큰을 발급한다. |
| `POST /api/auth/single-user` | 공개, 로컬 전용 | 없음 | `local` 또는 `test` 환경의 loopback 접속에서만 설정된 단일 사용자의 세션 토큰을 발급한다. |
| `GET /api/auth/me` | 사용자 | 없음 | 현재 사용자와 활성 조직·조직 역할을 반환한다. |

로그인 성공 응답에는 `access_token`, 사용자 식별 정보, 시스템 역할, 조직 식별 정보와 조직 역할이 포함된다.

## Scraper: 입찰공고 수집

`/api/scraper`는 입찰공고 수집 모듈이다. `/api/v1/results`의 개찰결과 수집·선택 반영 흐름과 별개다.

### 사용자 API

| 메서드·경로 | 인증 | 요청 또는 쿼리 | 목적 |
| --- | --- | --- | --- |
| `GET /api/scraper/config` | 사용자 | 없음 | 실행 여부, 알림 시각, 키워드·제외 키워드, 수신자, Sheet 대상과 최근 실행 이력을 조회한다. |
| `PUT /api/scraper/config` | 사용자 | `ScraperConfig` | 수집 설정을 저장하고 Cloud Scheduler 설정을 동기화한다. 이 요청 자체는 Sheet에 쓰지 않는다. |
| `POST /api/scraper/trigger` | 사용자 | `run_now`, `reason` | 구성된 Cloud Scheduler의 즉시 실행을 요청하거나 실행 요청 ID를 발급한다. 실제 워커 실행은 설정에 따라 Sheet 기록을 수행할 수 있다. |
| `GET /api/scraper/runs` | 사용자 | `limit` (기본 20, 서비스에서 최대 100으로 제한) | 최근 입찰공고 수집 실행 결과를 조회한다. |
| `POST /api/scraper/execute` | 사용자 | `run_now`, `reason` | API 프로세스에서 입찰공고 수집 파이프라인을 즉시 실행한다. |

`POST /api/scraper/execute`는 신규 공고가 있고 Sheet 대상이 설정되어 있으면 Google Sheets에 직접 append한다. 이 레거시 입찰공고 실행 경로에는 개찰결과의 `dry_run` 미리보기 절차가 없다.

### 내부 워커 API

| 메서드·경로 | 인증 | 요청 핵심 필드 | 목적 |
| --- | --- | --- | --- |
| `GET /api/scraper/internal/last-run` | 내부 수집 | 없음 | 마지막 완료 실행 시각을 조회한다. |
| `POST /api/scraper/internal/dedup` | 내부 수집 | `run_id`, `since_notified_at`, `notices` | 이전에 처리한 공고를 제외하고 새 공고만 반환한다. |
| `POST /api/scraper/runs` | 내부 수집 | 실행 건수·상태·공고 목록 | 워커 실행 결과와 수집 공고를 저장한다. |

## Opening results: 개찰결과

개찰결과는 공통 원본을 수집한 뒤, 로그인한 사용자의 키워드 프로필에 맞는 결과만 검토 목록으로 제공한다. 목록 조회나 상세 조회만으로 Google Sheets에 기록되지 않는다.

### 원본 수집

| 메서드·경로 | 인증 | 요청 핵심 필드 | 목적 |
| --- | --- | --- | --- |
| `POST /api/v1/results/collect` | 사용자 + 시스템 `admin` 역할 | `start_at`, `end_at`, `business_type`, `include_entries` | 지정 기간의 개찰결과 공통 원본과 업체별 순위·점수를 수집한다. 한 요청의 기간은 최대 14일이다. |
| `POST /api/v1/results/internal/collect` | 내부 수집 + 운영 환경 Scheduler OIDC | 없음 | 12시간 슬롯 단위의 예약 수집을 실행하며 같은 슬롯의 중복 실행을 방지한다. |

`business_type`은 `SERVICE`, `GOODS`, `CONSTRUCTION`, `FOREIGN` 중 하나이며 기본값은 `SERVICE`다. 내부 예약 수집은 운영 환경에서 내부 토큰과 지정 서비스계정의 OIDC 토큰을 모두 요구한다.

### 사용자 검토 목록

| 메서드·경로 | 인증 | 요청 또는 쿼리 | 목적 |
| --- | --- | --- | --- |
| `GET /api/v1/results` | 사용자+조직 | `q`, `status`, `opened_from`, `opened_to`, `sheet_export_status`, `page`, `page_size` | 현재 사용자의 키워드와 제외 정책에 매칭된 개찰결과를 조회한다. `page_size`는 1~100이다. |
| `GET /api/v1/results/{result_id}` | 사용자+조직 | 경로의 `result_id` | 사용자가 볼 수 있는 결과의 공고 정보와 업체별 순위·점수 상세를 조회한다. |
| `DELETE /api/v1/results/{result_id}` | 사용자+조직 | 경로의 `result_id` | 해당 결과를 현재 사용자의 검토 목록에서 제외한다. 원본 공통 데이터는 삭제하지 않는다. |
| `POST /api/v1/results/{result_id}/restore` | 사용자+조직 | 경로의 `result_id` | 제외했던 결과를 사용자 검토 목록으로 복구한다. |

`status`는 `OPENED`, `AWARDED`, `FAILED`, `REBID`, `CANCELLED`, `UNKNOWN` 중 하나다. `sheet_export_status`는 `READY`, `DETAIL_PENDING`, `NOTICE_CONTEXT_MISSING`, `NOTICE_CONTEXT_AMBIGUOUS`, `BLOCKED`를 지원한다.

### 개인 키워드 설정

| 메서드·경로 | 인증 | 요청 핵심 필드 | 목적 |
| --- | --- | --- | --- |
| `GET /api/v1/results/settings` | 사용자+조직 | 없음 | 현재 사용자의 키워드 프로필, 접근 가능한 Sheet 목적지, Sheet 서비스계정 이메일을 조회한다. |
| `PUT /api/v1/results/settings/profile` | 사용자+조직 | `enabled`, `keywords`, `excluded_keywords` | 사용자별 포함·제외 키워드를 저장한다. 프로필을 활성화할 때 포함 키워드가 한 개 이상 필요하다. |

### Google Sheet 목적지

| 메서드·경로 | 인증 | 요청 핵심 필드 | 목적 및 쓰기 여부 |
| --- | --- | --- | --- |
| `GET /api/v1/results/sheet-destinations` | 사용자+조직 | 없음 | 접근 가능한 개인 목적지와, 권한이 있는 경우 조직 목적지를 조회한다. 쓰지 않는다. |
| `POST /api/v1/results/sheet-destinations/verify` | 사용자+조직 | `spreadsheet_id`, `tab_name` | 서비스계정의 접근 가능 여부, 탭 존재 여부와 헤더 상태를 읽어 확인한다. 쓰지 않는다. |
| `POST /api/v1/results/sheet-destinations` | 사용자+조직 | `destination_id`, `label`, `spreadsheet_id`, `tab_name`, `scope`, `is_default` | 개인 또는 조직 Sheet 목적지를 등록·수정한다. Sheet 본문에는 쓰지 않는다. 조직 목적지는 관리자만 관리한다. |
| `DELETE /api/v1/results/sheet-destinations/{destination_id}` | 사용자+조직 | 경로의 `destination_id` | 등록된 목적지를 비활성화한다. Sheet 문서는 삭제하거나 수정하지 않는다. |

`scope`의 기본값은 `PERSONAL`, 기본 탭 이름은 `개찰결과`다.

### 선택 결과 Sheet 반영

`POST /api/v1/results/export/sheet`만 검토 목록의 개찰결과를 Google Sheets에 반영한다. 요청의 `result_ids`는 1~100개이며, 현재 사용자가 볼 수 있는 선택 결과만 처리한다.

1. 미리보기 요청: `dry_run: true`(기본값), `result_ids`, 선택적 `destination_id`를 보낸다.
2. 서버는 `preview_rows`, 누락 정보, 대상 Sheet 정보와 `preview_token`을 반환한다. 이 단계에서는 Sheet에 쓰지 않으며 `written`은 `false`다.
3. 사용자가 내용을 확인한 뒤 같은 선택과 목적지로 `dry_run: false`, `expected_preview_token: <preview_token>`을 보낸다.
4. 서버가 현재 데이터로 계산한 토큰과 일치할 때만 고정 헤더와 행을 upsert하고 `inserted_count`, `updated_count`, `written`을 반환한다.

최종 반영은 다음 경우 차단된다.

- 업체별 순위·점수 상세 수집이 완료되지 않은 결과
- 연결된 입찰공고의 공식 필수 정보가 누락되거나 공고 연결이 모호한 결과
- 한 요청에 같은 공고번호의 여러 개찰 회차가 포함된 경우
- 미리보기 토큰이 없거나, 미리보기 이후 선택 결과 또는 목적지가 변경된 경우
- 이미 다른 반영 작업에서 처리 중이거나 반영 완료된 결과

개인 목적지는 본인만 사용한다. 조직 공용 목적지의 실제 반영은 조직 관리자 또는 시스템 관리자만 수행할 수 있다.
