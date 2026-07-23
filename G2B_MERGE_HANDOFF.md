# G2B 입찰공고 수집기 병합 인수인계

이 브랜치는 나라장터 **입찰공고 조회 · 상세/첨부파일 분석 · 선택 공고 Google Sheets 저장** 기능을 다른 팀의 통합 코드에 옮기기 위한 작업 브랜치다.

이 브랜치는 `back`을 기준으로 만들었으며, 프론트 기능 폴더도 함께 포함한다. `main`, `front`, `back`에는 이 변경을 병합하거나 푸시하지 않았다.

## 포함한 코드

| 경로 | 내용 |
| --- | --- |
| `app/features/g2b_bid_notice/` | 목록 API 조회, 상세/첨부 분석, 분류, Sheets 저장, 공통 저장 계약, 회귀 테스트 |
| `src/features/g2b-team-profile/` | 검색 조건, 결과 미리보기, 선택 저장 UI 및 해당 화면 스타일 |
| `requirements.txt` | PDF/HWP/XLS 분석에 필요한 `pypdf`, `olefile`, `xlrd` 추가 |
| `.env.example` | 나라장터·Google Sheets 설정값의 빈 예시 |

`src/features/g2b-team-profile/preview.jsx`는 로컬 Vite 미리보기 전용 진입점이다. 통합 앱에서는 `G2BCollectionSettingsPreview`를 기존 메뉴/라우트에서 import해 렌더링하며, 기존 `index.html`, `App.jsx`, `main.jsx`는 덮어쓰지 않는다.

## 전달하거나 커밋하지 않는 파일

- `.env`, `.secrets/`, Google 서비스 계정 JSON, 나라장터 API 키
- `node_modules/`, `dist/`, `__pycache__/`, `.local/` 첨부파일 캐시·조회 이력
- 공용 DB 모델, 마이그레이션, 기존 수집기와 기존 화면 파일

## 통합 시 반드시 할 일

### 프론트 API 연결

`bidNoticePreviewApi.js`는 현재 로컬 서버 `http://127.0.0.1:8000`을 사용한다. 통합 환경에서는 요청 본문과 아래 경로는 유지하고, 주소만 공용 API 클라이언트/환경변수로 바꾼다.

```text
POST /api/bid-notice-search/preview
POST /api/bid-notice-search/sheets/selected
```

통합 백엔드가 인증을 사용하므로 기존 Bearer 토큰도 함께 보낸다.

### 백엔드 라우터 연결

`router.py`의 미리보기 라우터를 통합 FastAPI `main.py`에 등록한다.

```python
from app.features.g2b_bid_notice.router import router as bid_notice_search_router

app.include_router(bid_notice_search_router)
```

**주의:** `router.py`에는 `/preview`만 있다. Sheets 저장 엔드포인트는 로컬 전용 `local_preview.py`에 있으므로, 통합 환경에서 Sheets 저장도 유지하려면 `save_selected_notices()` 구현을 인증된 공용 라우터로 옮겨야 한다. 이를 하지 않으면 조회는 되지만 Sheets 저장 요청은 404가 된다.

## 유지해야 하는 동작

1. 테스트 조회는 10건만 표시하지만, 표시한 10건 모두 상세 페이지와 첨부파일까지 분석한다.
2. 업종제한(기관코드)이 허용 목록과 불일치하면 다른 조건과 관계없이 제외한다.
3. 첨부파일 열은 PC 경로가 아니라 나라장터 원본 다운로드 링크를 저장한다.
4. Sheets 저장은 사용자가 선택한 공고만 수행한다. 같은 공고번호가 Sheet에 있으면 중복 저장하지 않고, 새 저장분은 헤더 바로 아래에 삽입한다.
5. 공용 DB 모델·마이그레이션은 이 기능에서 만들거나 변경하지 않는다.

## 공통 DB 저장 계약

`contracts.py`의 `BidNoticeStorageRecord`를 공통 저장 함수에 전달한다.

- 연결 키: `bid_notice_no` + `bid_notice_ord`
- 두 값은 문자열 보존, 비교 시 `00`과 `000`은 같은 차수
- 금액은 숫자, 시간은 KST, 확인 불가 값은 추정하지 않고 `null`

전체 필드 정의는 `app/features/g2b_bid_notice/CONTRACT.md`를 따른다.

## 환경변수

실제 값은 Git에 저장하지 않는다.

```dotenv
G2B_SERVICE_KEY=
G2B_API_BASE_URL=https://apis.data.go.kr/1230000/ad/BidPublicInfoService
G2B_LOOKBACK_DAYS=14

GSHEET_SERVICE_ACCOUNT_FILE=.secrets/google-service-account.json
GSHEET_ID=
GSHEET_TAB_NAME=나라장터 공고 수집 목록
```

서비스 계정 JSON은 대상 Sheet에 편집자 권한을 받은 계정의 파일이어야 한다.

## 검증 명령

```powershell
python -m unittest `
  app.features.g2b_bid_notice.test_enrichment `
  app.features.g2b_bid_notice.test_classifier `
  app.features.g2b_bid_notice.test_service_pagination `
  app.features.g2b_bid_notice.test_sheets `
  app.features.g2b_bid_notice.test_contracts
```

프론트는 프론트 프로젝트 루트에서 `npm.cmd run build`로 확인한다.

스캔 PDF OCR까지 운영하려면 서버에 `tesseract`와 `pdftoppm`(Poppler)이 필요하다. 없으면 일반 텍스트 PDF·HWP·HWPX·DOCX·XLS 분석은 계속 가능하지만, 스캔 PDF는 확인 필요 상태가 될 수 있다.
