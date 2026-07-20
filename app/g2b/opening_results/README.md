# G2B 개찰결과 모듈

이 모듈은 다른 수집 모듈과 독립적으로 다음 범위를 담당한다.

1. 개찰결과 목록 수집
2. 개찰완료 건의 업체별 순위·투찰금액·평가점수 수집
3. 최종 낙찰업체 연결
4. 복수 차수·재입찰 회차를 덮어쓰지 않는 저장
5. 공통 원본 1건을 활성 사용자별 조건으로 매칭한 최근 14일 받은 목록
6. 사용자 선택 기반 Google Sheet 출력과 재노출 방지 이력

공식 데이터는 [조달청 나라장터 낙찰정보서비스](https://www.data.go.kr/data/15129397/openapi.do)를 사용한다.

## 다른 모듈과 공유할 연결 키

```text
입찰공고 연결: bid_notice_no + bid_notice_ord
개찰 회차 식별: bid_notice_no + bid_notice_ord + bid_class_no + rebid_no
```

입찰공고 모듈이 아직 합쳐지지 않아도 개찰결과는 위 키로 먼저 저장한다.
API에 받은 차수·분류·재입찰번호 문자열은 그대로 보존하되, 모듈 연결 키를 비교할 때는
`00`과 `000`처럼 값은 같고 0 채움만 다른 코드를 같은 값으로 취급한다.

Google Sheet 출력에 필요한 다음 9개 값은 입찰공고 모듈이 `scraper_notices`의 공식 연결
필드에 저장한다.

```text
bid_notice_no
bid_notice_ord
business_name
demand_agency_name
base_amount
prearranged_price_decision_method
proposal_deadline
region_restriction
is_two_stage_bid
```

개찰결과 모듈은 선택된 `result_ids`의 공고번호·차수로 이 필드를 서버에서 일괄 조회한다.
`POST /api/v1/results/export/sheet`의 기존 `notice_contexts`는 구버전 요청 호환을 위해
수용하지만 값은 완전히 무시한다. 프론트가 보낸 사업명·기관명·금액은 공식 Sheet 값의
대체값이나 fallback으로 사용하지 않는다.

`proposal_deadline`이 timezone 없이 저장되면 KST 시각으로 해석한다. 입찰공고 모듈은
`estimated_price`를 `base_amount`로 복사하지 않고 각각의 공식 필드를 구분해야 한다.
`bid_notice_no`와 `bid_notice_ord`는 앞뒤 공백을 제거해 저장한다.

현재 입찰공고 워커의 공식 필드 매핑은 다음과 같다.

| 저장 필드 | 나라장터 공식 원천 |
| --- | --- |
| `bid_notice_no` | `bidNtceNo` |
| `bid_notice_ord` | `bidNtceOrd` |
| `business_name` | `bidNtceNm` |
| `demand_agency_name` | `dminsttNm` |
| `base_amount` | 용역기초금액조회 `bssamt` |
| `prearranged_price_decision_method` | `prearngPrceDcsnMthdNm`; `비예가`일 때만 기초금액 공란 허용 |
| `proposal_deadline` | `bidClseDt` (KST) |
| `region_restriction` | 참가가능지역조회 `prtcptPsblRgnNm`; 정상 빈 응답은 `없음` |
| `is_two_stage_bid` | 검증된 공식 입찰·계약·낙찰방법명에 `2단계` 포함 여부 |

기초금액과 참가가능지역 URL은 `G2B_SOURCE_URL`의 서비스 경로에서 자동으로 파생한다.
별도 게이트웨이를 쓰는 환경은 `G2B_BASE_AMOUNT_SOURCE_URL`, `G2B_REGION_SOURCE_URL`로
각 오퍼레이션 URL을 덮어쓸 수 있다. 보강 API 실패나 공식값 미공개 상태는 추정값으로
채우지 않고 `NULL`로 유지하여 Sheet 실제 반영을 차단한다. `전자입찰`처럼 2단계 여부를
확정할 수 없는 방법명도 임의로 `N` 처리하지 않는다. 공식 컨텍스트가 미완성인 실행은
성공 체크포인트를 전진시키지 않고 최근 14일 범위에서 다음 12시간 실행 때 다시 보강한다.

## 개발·병합 순서

1. 세 모듈이 공고번호와 차수를 문자열로 보존하도록 공통 키를 먼저 고정한다.
2. 세 모듈이 `app/g2b/keyword_policy.py`의 포함 OR·제외 우선 판정 규칙을 공유한다.
3. 개찰결과 담당자는 이 디렉터리 안에서 수집·저장·조회·Sheet 변환을 완성하고 테스트한다.
4. 입찰공고 담당자는 `scraper_notices`의 위 9개 공식 연결 필드를 채운다.
5. 기준 파일에 합칠 때 통합 담당자 한 명만 `main.py`, CI, 배포 환경변수를 연결한다.
6. 프론트에서 최근 14일 결과를 확인하고 Sheet에 보낼 행만 체크한다.
7. 선택 ID로 `dry_run=true` 미리보기를 확인한 뒤 `dry_run=false`로 반영한다.

| 모듈 | 소유 범위 | 다른 모듈이 지켜야 할 경계 |
| --- | --- | --- |
| 사전규격 | 사전규격 원본과 자체 매칭 | `scraper_notices`와 개찰결과 원본을 직접 수정하지 않는다. |
| 입찰공고 | `scraper_notices`의 공식 공고 컨텍스트 9개 필드 | 공고번호·차수를 문자열로 보존하고 공식값이 없으면 추정하지 않는다. |
| 개찰결과 | 개찰 원본·순위·점수·사용자 매칭·선택 Sheet 반영 | 입찰공고 수집기를 호출하지 않고 공고 컨텍스트를 읽기만 한다. |
| 통합 담당자 | `main.py`, DB 호환 보강, CI, 배포 환경변수 | 각 모듈 변경을 합친 뒤 전체 계약 테스트를 한 번에 실행한다. |

병합 시 규칙을 다시 구현하지 않고 다음 코드 위치를 단일 소스로 사용한다.

- 공고번호·차수 정규화: `app/g2b/bid_notice.py::canonical_bid_notice_identity`
- Sheet 17개 열·점수 표기: `app/g2b/opening_results/sheet_export.py::SHEET_HEADERS`, `_sheet_score_breakdown`
- 사용자별 노출·조직 공용 Sheet 억제: `app/g2b/opening_results/matching.py::visible_result_predicates`, `complete_sheet_exports`

개찰결과 수집은 입찰공고 수집과 독립적으로 실행한다. Sheet 변환 시점에만
`notice_context_repository.py`가 입찰공고 DB를 읽으며, 입찰공고 수집기나 저장 로직을
호출하지 않는다. 공식 공고가 없거나 필수 필드가 비어 있거나 같은 공고번호·차수의 행이
여러 건이면 Google API 호출 전에 전체 쓰기를 409로 차단한다.

## 공통 원본과 사용자별 매칭

12시간 수집은 사용자 수와 관계없이 한 번만 실행하며 `g2b_opening_rounds`와
`g2b_opening_entries`에는 사용자 ID를 넣지 않는다. 개찰 요약 원본은 모두 저장하고,
업체별 순위·점수 상세 API는 활성 사용자 조건 중 하나라도 매칭된 결과에만 호출한다. 저장이 끝나면
`user_result_profiles`로 제목을 판정하고 `user_opening_result_matches`에 사용자별 받은 목록을 만든다.
여러 사용자가 같은 결과에 매칭되어도 상세 API는 한 번만 호출하고 공통 업체 상세를 공유한다.

`keywords`는 하나라도 제목에 포함되면 통과하는 OR 조건이고, `excluded_keywords`는 하나라도
제목에 포함되면 우선 제외하는 조건이다. 예를 들어 포함값이 `AI, 클라우드, 연수`이고 제외값이
`연수구, 연수원`이면 `교원 직무연수`는 매칭하지만 `AI 기반 연수원 시설`은 제외한다.
기관명과 업체명은 이 판정에 사용하지 않는다.

인증된 사용자는 조직 역할과 관계없이 본인의 조건만 수정할 수 있다. 조건을 바꾸면 외부 API를
호출하지 않고 DB에 저장된 최근 14일 공통 원본을 해당 사용자에 대해 동기 재매칭한다. 원본 수집
전에 한 사용자의 키워드로 데이터를 버리지 않으므로 다른 사용자가 원하는 결과가 누락되지 않는다.
과거 요약이 새 조건으로 매칭됐지만 업체 상세가 아직 없으면 `상세 수집 대기`로 표시하고 Sheet
반영을 차단한다. 다음 정기 수집은 저장된 원문으로 상세를 보충한다. 상세 API의 빈 응답은 완료로
보지 않으며 기존 업체행도 지우지 않는다.

신규 사용자는 비활성·빈 키워드 프로필로 시작한다. 사용자별 프로필로 전환할 때 이미 활성 상태인
사용자는 기존 조직 프로필을 한 번 복사하며, 이후에는 서로 독립적으로 관리한다. 조직 ID와 역할은
기존 데이터 호환 및 조직 공용 Sheet 권한에만 유지한다.

## 목록 제외와 재노출 방지

- `DELETE /api/v1/results/{id}`는 공통 원본을 지우지 않고 현재 사용자의 상태만 `DISMISSED`로 남긴다.
- 개인 Sheet에 성공적으로 반영하면 해당 사용자에게만 `EXPORTED`로 처리한다.
- 조직 공용 Sheet에 성공적으로 반영하면 같은 조직 구성원 모두의 받은 목록에서 제외한다.
- Google API가 실패하면 `sheet_exports`를 `FAILED`로 남기고 받은 목록은 유지해 재시도할 수 있다.
- 억제 키는 숫자 ID가 아니라 `g2b_opening_rounds.external_key`이므로 원본을 삭제 후 다시 수집해도
  이미 제외하거나 반영한 결과는 재노출되지 않는다.

## 키워드 판정 규칙

영문·숫자 키워드는 `AI`가 `RAIL` 안에서 우연히 일치하지 않도록 단어 경계를 적용한다.
한글 키워드는 부분 일치를 사용하므로 `연수`의 오탐은 제외 키워드 `연수구`, `연수원`처럼
명시적으로 차단한다.

## 점수 계산

```text
종합점수 = 입찰가격점수(bidPrceEvlVal) + 기술평가점수(techEvlVal)
```

두 값이 모두 있을 때만 종합점수를 만든다. 한쪽이 없으면 빈 값으로 출력한다.
공식 응답의 `totalEvlAmtVal`은 비교·감사용 `official_total_score`로 별도 보존한다.
Sheet에는 `19.5+75=94.50`처럼 가격점수와 기술점수 계산식 및 소수 둘째 자리까지
반올림한 합계를 함께 기록한다.

## API

```text
POST /api/v1/results/collect
POST /api/v1/results/internal/collect
GET  /api/v1/results
GET  /api/v1/results/settings
PUT  /api/v1/results/settings/profile
GET  /api/v1/results/sheet-destinations
POST /api/v1/results/sheet-destinations/verify
POST /api/v1/results/sheet-destinations
DELETE /api/v1/results/sheet-destinations/{id}
GET  /api/v1/results/{id}
DELETE /api/v1/results/{id}
POST /api/v1/results/{id}/restore
POST /api/v1/results/export/sheet
```

목록 API는 별도 기간을 지정하지 않으면 최근 14일만 반환한다. 이 제한은 프론트 표시 범위이며
DB의 과거 결과를 삭제하지 않는다. 목록과 상세는 입찰공고 DB의 공식 사업명·수요기관·기초금액·
가격결정방법·제안마감·지역제한·2단계 입찰 여부를 함께 반환한다. 각 행의
`sheet_export_status`, `sheet_exportable`, `sheet_block_reasons`로 상세 수집 대기·공고정보 누락·
공고정보 중복을 Sheet 미리보기 전에 확인할 수 있다.

`GET /settings`는 서비스계정 키나 경로를 노출하지 않고 Sheet 공유에 필요한
`sheet_service_account_email`만 반환한다. `POST /sheet-destinations/verify`는 전체 Google Sheet
URL 또는 ID를 받아 접근권한, 탭 존재, A:Q 헤더가 `MATCH`·`EMPTY`·`MISMATCH`인지 읽기 전용으로
확인한다. 빈 탭은 연결할 수 있지만 다른 헤더가 있는 탭은 실제 반영 전에 차단한다.
`PUT /settings/profile`은 인증된 사용자의 본인 포함·제외 키워드와 활성 여부만 변경하고, 저장된
최근 14일 원본을 즉시 재매칭한다. 이 작업은 나라장터 API나 Google API를 호출하지 않는다.

Sheet 내보내기는 필수 `result_ids`와 서버에 등록된 `destination_id`로 사용자가 선택한 행만
처리하며 기본값은 `dry_run=true`다. 실제 기록은 미리보기 응답의 `preview_token`을
`expected_preview_token`으로 되돌려 보내면서 `dry_run=false`를 명시해야 한다. 미리보기 이후
결과나 목적지가 바뀌면 409로 다시 확인시킨다. 기존 탭 전체를 교체하지 않고,
같은 공고번호가 있으면 해당 A:Q 행만 갱신하고 없으면 새 행을 추가한다. 선택하지 않은 기존
행은 변경하거나 삭제하지 않는다.

Sheet ID와 탭 이름은 미리 등록된 조직 공용 또는 본인 소유 목적지에서만 읽으며, 요청에 임의의
Sheet ID를 넣을 수 없다. 조직 공용 목적지는 조직 관리자만 조회·검증·반영·관리하고 개인 목적지는
본인만 조회·사용한다. 같은 Sheet ID와 탭은 전체 시스템에서 한 조직에만 등록할 수 있으며, 등록 후 Sheet ID·탭·
개인/조직 범위는 바꾸지 않는다. 잘못 등록한 목적지는 비활성화한 뒤 같은 소유 범위로 재연결한다.
선택 결과가 없거나 입찰공고 컨텍스트의 기초금액·제안마감·지역제한여부·2단계 입찰 여부가
누락되면 Sheet를 변경하지 않는다. 기존행 갱신과 신규행 추가는 한 번의 배치 요청으로 처리한다.
`sheet_exports(destination_id, result_external_key)` 고유 이력과 공고번호 기준 Sheet upsert를 함께
사용해 중복 요청과 외부 호출 후 응답 유실 시에도 중복 행을 추가하지 않는다.

`DELETE /{result_id}`로 제외한 직후에는 현재 사용자 본인의 `DISMISSED` 상태만
`POST /{result_id}/restore`로 실행취소할 수 있다. 실행취소하지 않은 tombstone은 다음 수집에도
유지되며, 다른 사용자의 제외 상태나 `EXPORTED` 상태는 복원하지 않는다.

## 12시간 수집 주기

개찰 수집과 Sheet 반영은 별도 흐름이다. Cloud Scheduler는 아래 내부 API를 매일
`00:17`, `12:17` KST에 호출하며 Sheet에는 기록하지 않고 DB만 갱신한다.

```text
cron: 17 0,12 * * *
POST /api/v1/results/internal/collect
X-Scraper-Internal-Token: ...
Authorization: Bearer <Cloud Scheduler OIDC token>
```

각 실행은 고정된 12시간 슬롯을 사용한다. 이전 성공 이후 놓친 슬롯이 있으면 최대 최근 14일까지
현재 실행에서 자동 보충한다. 동일 슬롯의 재호출은
`g2b_opening_collection_runs.run_key`로 건너뛰고, 서로 다른 슬롯에서 같은 결과가 다시
응답되어도 라운드·업체·원문 스냅샷 고유키로 중복 저장하지 않는다. 45분 임대와 `claim_token`
fencing으로 만료된 이전 작업이 새 작업의 원본·매칭·상태를 덮어쓰지 못하게 한다.
수동 수집과 서로 다른 슬롯의 수집도 `business_type`별 공통 임대로 직렬화한다.
운영 환경의 내부 수집 API는 고정 내부 토큰에 더해 Scheduler 호출 서비스계정, 대상 URL audience,
Google 서명을 검증한다. 같은 슬롯이 아직 `RUNNING`이면 409를 반환해 Scheduler의 제한된 지수
백오프 재시도가 계속되며, 이미 `SUCCESS`인 슬롯만 200으로 건너뛴다.

첫 배포 때만 관리자용 `POST /api/v1/results/collect`로 최근 14일을 한 번 백필하고, 이후에는
12시간 스케줄을 사용한다. 개찰 목록은 입력일시, 최종 낙찰 목록은 등록일시를 기준으로
증분 조회하므로 개찰일과 최종 낙찰일이 달라도 후속 낙찰 응답을 기존 라운드에 연결한다.
현재 정기 수집의 1차 범위는 `SERVICE`이며, `GOODS` 정기 수집은 물품 모듈 연결 시 추가한다.

## 환경변수

```text
G2B_AWARD_SERVICE_KEY       # 없으면 G2B_SERVICE_KEY 사용
G2B_AWARD_SOURCE_URL        # 선택
G2B_AWARD_PAGE_SIZE         # 기본 100
G2B_AWARD_TIMEOUT_SECONDS   # 기본 20
G2B_AWARD_SCHEDULER_TARGET_URL # HTTPS /api/v1/results/internal/collect 전체 URL
G2B_AWARD_SCHEDULER_OIDC_AUDIENCE # Cloud Run 서비스 기본 URL(경로 제외)
SCRAPER_INTERNAL_TOKEN      # Scheduler 내부 헤더용 운영 비밀값, 32자 이상
CLOUD_SCHEDULER_INVOKER_SERVICE_ACCOUNT # Scheduler OIDC 발급 서비스계정
GSHEET_OPENING_RESULT_ID    # 없으면 GSHEET_ID 사용
GSHEET_SERVICE_ACCOUNT_JSON # 없으면 ADC 사용
GSHEET_SERVICE_ACCOUNT_EMAIL # ADC 사용 시 Sheet 공유 안내를 위해 실행 SA 이메일 필수
DEFAULT_ORGANIZATION_NAME   # 기존 사용자를 연결할 기본 조직명
DEFAULT_ORGANIZATION_SLUG   # 기본 icore
AUTH_TOKEN_TTL_HOURS        # 기본 8, 기존 무기한 토큰은 재로그인 필요
AUTH_SECRET_KEY             # production 필수, 32자 이상
DEFAULT_ADMIN_PASSWORD      # production 필수, 12자 이상; 과거 기본 비밀번호는 시작 시 회전
LEGACY_PASSWORD_LOGIN_ENABLED # 기본 false; 긴급 복구 시에만 명시적으로 true
DEFAULT_ADMIN_EMAIL         # 기존 관리자 계정에 연결할 Google Workspace 이메일
GOOGLE_OAUTH_CLIENT_ID      # Google Identity Services 웹 클라이언트 ID
ALLOWED_LOGIN_DOMAINS       # 기본 iceu.kr,iceu.co.kr
GOOGLE_LOGIN_ALLOWED_EMAILS # 신규 계정으로 자동 등록할 승인 이메일 목록, 쉼표 구분
```

`POST /api/auth/google`은 Google ID 토큰을 서버에서 검증한 뒤 `email_verified`와
`hd`가 허용 도메인인지 확인한다. 기존 `users.email`과 일치하는 활성 사용자는 로그인할 수 있고,
아직 등록되지 않은 이메일은 `GOOGLE_LOGIN_ALLOWED_EMAILS`에 있을 때만 최초 로그인에서
일반 사용자, 기본 조직 멤버십, 비활성·빈 개인 키워드 프로필을 한 번 생성한다.

GitHub Actions에는 내부 수집 API의 전체 URL을
`G2B_AWARD_SCHEDULER_TARGET_URL` secret으로 등록한다.
ADC로 Sheet를 쓰는 배포는 Cloud Run 또는 VM 실행 서비스계정 이메일을
`GSHEET_SERVICE_ACCOUNT_EMAIL`에도 설정해야 사용자가 연결 전에 해당 이메일로 Sheet를 공유할 수 있다.
