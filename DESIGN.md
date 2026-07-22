# iCore 개찰결과 디자인 시스템

## 제품 맥락

- **제품:** 나라장터 개찰결과를 사용자 키워드로 선별해 검토하는 내부 웹 앱
- **사용자:** 회사 내부 개찰결과 담당자. 현재 로컬 개발에서는 기본 사용자 한 명으로 자동 진입한다.
- **핵심 흐름:** 최근 14일 결과 검토 → 필요한 결과 선택 → 반영 전 미리보기 → 개인 Google Sheets 반영
- **기억할 문장:** 필요한 개찰결과만 골라서 기록해요.

## 참고 기준

- Apps in Toss 디자인 개요: https://developers-apps-in-toss.toss.im/design/overview.html
- Apps in Toss UI/UX 가이드: https://developers-apps-in-toss.toss.im/design/consumer-ux-guide.html
- Apps in Toss UX 라이팅: https://developers-apps-in-toss.toss.im/design/ux-writing.html
- 로컬 분석 문서: `DESIGN-notion.md`

TDS 컴포넌트와 그래픽은 Apps in Toss 범위에 사용 권한이 제한된다. iCore는 해당 코드와 자산을 복제하지 않고 정보 구조, 한 가지 행동색, 명확한 문구, 접근성 원칙만 자체 UI로 구현한다.

## 디자인 방향

`Calm Decision Workspace`를 기준으로 한다.

- Notion 분석 문서의 따뜻한 문서형 배경과 흰 작업 표면을 사용한다.
- Toss의 큰 제목, 명확한 정보 계층, 한 가지 파란 행동색을 적용한다.
- 대시보드용 작은 카드 나열보다 검색·필터·표·선택 작업바의 한 흐름을 강조한다.
- 장식보다 사용자가 다음 행동을 예측할 수 있는 문구와 상태를 우선한다.
- 행마다 카드를 만들지 않고 표와 얇은 구분선을 사용한다.

## 색상

- Canvas: `#F6F5F4`
- Canvas subtle: `#F2F4F6`
- Surface: `#FFFFFF`
- Surface muted: `#F7F8FA`
- Text primary: `#191F28`
- Text secondary: `#4E5968`
- Text muted: `#8B95A1`
- Border: `#E5E8EB`
- Border strong: `#D1D6DB`
- Primary: `#3182F6`
- Primary hover: `#1B64DA`
- Primary pressed: `#1957C2`
- Primary soft: `#EDF6FF`
- Selected row: `#EEF6FF`
- Success: `#20A162`, background `#EAF8F1`
- Warning: `#C47F00`, background `#FFF6DF`
- Error: `#E5484D`, background `#FFF0F0`

파란색은 주요 행동, 선택, 링크, 포커스에만 사용한다. 성공·경고·오류색은 상태 배지와 피드백에만 제한한다. 색상만으로 상태를 전달하지 않는다.

## 타이포그래피

- 기본: `Pretendard Variable`, `Pretendard`, `Noto Sans KR`, sans-serif
- 페이지 제목: 30–40px / 750 / `letter-spacing: -0.045em`
- 섹션 제목: 18–20px / 700
- 본문: 15–18px / 400–500
- 표: 14px / 400, 사업명만 700
- 표 헤더·배지·메타데이터: 11–13px / 600–700
- 공고번호, 금액, 점수: `font-variant-numeric: tabular-nums`

영문 고정폭 글꼴을 화면 성격으로 사용하지 않는다. 숫자 정렬이 필요한 값에만 tabular numerals를 적용한다.

## 간격과 형태

- 기본 단위: 4px, 주요 리듬은 8px
- 화면 여백: 데스크톱 28–40px, 모바일 16–28px
- 섹션 사이: 16–32px
- 패널 내부: 18–24px
- 표 셀: 세로 13–15px, 가로 16px
- 입력·버튼 기본 높이: 40px
- 주요 행동과 모바일 터치 영역: 최소 44px
- 입력·버튼: 10px radius
- 패널: 14–18px radius
- 모달: 20–24px radius
- 배지·아바타: full radius

기본 패널은 그림자 없이 경계선으로 구분한다. 하단 선택 작업바처럼 실제로 떠 있어야 하는 표면만 약한 그림자를 사용한다.

## 화면 구조

### 자동 진입 · 임시 단일 사용자 모드

- Google 로그인 화면은 현재 사용하지 않는다.
- 프론트는 기존 서버 토큰을 복원하고, 없거나 만료됐으면 로컬 백엔드에서 기본 사용자 세션을 발급받는다.
- 단일 사용자 모드는 `local` 또는 `test` 환경의 loopback 접속에서만 사용할 수 있다.
- 개인 키워드, 제외 이력, Sheet 목적지는 기본 사용자 ID에 계속 귀속된다.

### 개찰결과

- 고정 상단바: 브랜드, 현재 영역, 현재 기본 사용자
- 페이지 제목: 최근 14일 범위와 제품 목적을 한 문장으로 설명
- 정보 안내: 목록 조회와 Google Sheets 쓰기가 분리됐음을 안내
- 결과 찾기: 검색, 상태, 반영 가능 여부, 기간, 키워드 조건을 한 패널에 배치
- 결과 표: 사업명·기관을 중심으로 상태와 Sheet 반영 가능 여부를 비교
- 14일 보관함: 제외·Sheet 반영 결과를 열람하고 제외 항목을 기간 안에 복구
- 선택 작업바: 선택 건수, 목적지, `선택한 N건 검토` 행동을 고정
- 상세 Drawer: 공고정보와 상위 5개 업체 점수를 연속된 문서 구조로 제공
- 반영 모달: 미리보기 후 `Google Sheets에 N건 반영`으로 결과를 예측할 수 있게 표현

## 상태

- Hover: `#F7FAFF`
- Selected: `#EEF6FF` + 실제 체크박스
- Disabled: muted surface와 텍스트 + 원인 tooltip/설명
- Loading: 기존 너비를 유지한 spinner + 현재 행동 문구
- Success/Warning/Error: 아이콘, 색, 구체적인 텍스트를 함께 제공
- Dismissed: 제외 완료 알림, 10초 되돌리기, 14일 보관함 복구 유지

## UX 라이팅

- 해요체와 능동형을 기본으로 한다.
- 버튼은 결과를 예측할 수 있게 쓴다.
  - `확인` → `선택한 3건 검토`
  - `최종 반영` → `Google Sheets에 3건 반영`
- 오류에는 원인과 다음 행동을 함께 쓴다.
- 다이얼로그의 보조 행동은 가능하면 `닫기`를 사용한다.
- 사용할 수 없는 기능은 숨기지 않고 이유를 알려준다.

## 접근성 및 모션

- 키보드 포커스: 2px Primary 외곽선
- 체크박스, 버튼, 링크는 실제 의미 요소를 유지한다.
- 선택과 상태를 색으로만 구분하지 않는다.
- 상태 전환은 120–180ms로 제한한다.
- `prefers-reduced-motion`에서는 장식성 전환을 제거한다.
- 로딩·오류·완료 메시지는 Ant Design의 접근성 상태 컴포넌트를 유지한다.

## 보존해야 할 기능 계약

- 토큰 기반 세션 복원과 로컬 기본 사용자 자동 진입
- 사용자별 포함·제외 키워드
- 최근 14일 결과와 DB 새로고침
- 페이지를 이동해도 유지되는 최대 100건 선택
- Sheet 읽기 전용 연결 검증
- 선택 결과만 dry-run 미리보기
- preview token을 이용한 최종 반영
- 반영·제외 결과 재노출 방지, 10초 제외 취소, 14일 보관함 열람·복구
- `가격점수+기술점수=합계` 형식과 합계 둘째 자리 반올림

## 결정 기록

- 2026-07-19: 기존 `Procurement Ledger`의 산업적·고정폭 중심 표현을 폐기했다.
- 2026-07-19: 따뜻한 문서 배경과 Toss식 정보 계층을 결합한 `Calm Decision Workspace`로 변경했다.
- 2026-07-19: 구조색을 파란색 하나로 제한하고, 결과 표와 선택 작업바를 화면의 중심으로 유지했다.
- 2026-07-19: TDS 코드·자산은 사용하지 않고 공식 UX 원칙만 iCore 자체 컴포넌트에 적용했다.
- 2026-07-19: Google 로그인 화면을 임시 제거하고 로컬 전용 단일 사용자 자동 진입으로 전환했다.
