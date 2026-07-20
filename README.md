# iCore Web

개찰결과를 사용자별 키워드로 검토하고, 선택한 결과만 개인 Google Sheet에 반영하는 React 웹 앱입니다.

## 주요 흐름

1. 서버 DB에 수집된 최근 14일 개찰결과를 조회합니다.
2. 현재 사용자의 포함·제외 키워드에 매칭된 결과만 표시합니다.
3. 사용자가 반영할 결과를 선택하고 서버가 만든 17개 열을 미리 확인합니다.
4. 최종 확인 요청에서만 Google Sheet 쓰기를 실행합니다.

목록 조회, 검색, 체크박스 선택과 미리보기는 Google Sheet를 변경하지 않습니다.

## 구조

```text
src/
  api/client.js                 # 인증·개찰결과 API 클라이언트
  components/LayoutShell.jsx   # 공통 웹 레이아웃
  pages/LoginPage.jsx          # Google 로그인
  pages/OpeningResultsPage.jsx # 검토·키워드·Sheet 반영 화면
```

## 로컬 실행

```bash
npm ci
cp .env.example .env.local
npm run dev
```

기본 주소는 `http://localhost:5173`입니다. 환경변수의 의미는 [`.env.example`](.env.example)을 참고하세요.
예제는 로컬 전용 단일 사용자 모드를 사용하므로 백엔드도 `SINGLE_USER_MODE_ENABLED=true`로
실행해야 합니다. 운영에서는 양쪽 단일 사용자 모드를 끄고, 같은 Google OAuth Client ID를
`VITE_GOOGLE_CLIENT_ID`와 백엔드 `GOOGLE_OAUTH_CLIENT_ID`에 설정합니다.

## 검증

```bash
npm run build
```

`dist/`는 빌드 산출물이므로 Git에서 추적하지 않습니다.

GitHub Actions의 GCP 배포는 저장소 변수 `GCP_DEPLOY_ENABLED=true`와 필요한 GCP
secrets를 모두 설정한 경우에만 실행됩니다. 활성화하면 빌드 결과를
`iceu-kolok91-icore-client-web/admin`에 반영합니다. 변수가 없으면 설치·빌드 검증까지만
수행합니다.
