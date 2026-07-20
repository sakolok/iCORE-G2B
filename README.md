# iCore API

개찰결과 공통 원본을 수집·정규화하고 사용자별 키워드 매칭, 검토 상태, 개인 Google Sheet 반영을 제공하는 FastAPI 서버입니다.

## 구조

```text
app/
  core/                         # 환경 설정
  data/                         # 공통 SQLAlchemy 모델·DB 초기화
  g2b/
    bid_notice.py               # 공식 입찰공고 정규화 헬퍼
    bid_notices/service.py      # 입찰공고 수집 설정·중복방지·실행 이력
    opening_results/            # 개찰결과 수집·매칭·Sheet 반영
  routers/                      # 인증·상태·입찰공고 API
  services/                     # 인증·Cloud Scheduler 연동
cloudrun/g2b_worker/            # 입찰공고·사전규격 수집 워커
tests/                          # 단위·회귀 테스트
main.py                         # FastAPI 진입점
```

## 데이터 처리 경계

- 08·11·14·17시 정기 수집은 공통 DB 원본과 사용자별 매칭을 갱신합니다.
- 목록 조회와 Sheet 미리보기는 Google Sheet를 변경하지 않습니다.
- 사용자가 미리보기 결과를 최종 확인한 요청에서만 선택된 결과를 반영합니다.
- 성공적으로 반영했거나 제외한 결과는 사용자 검토함에 다시 노출하지 않습니다.

## 로컬 실행

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/uvicorn main:app --reload --env-file .env
```

예제는 로컬 전용 단일 사용자 인증을 사용하며 프론트의
`VITE_SINGLE_USER_MODE_ENABLED=true`와 함께 실행합니다. 운영에서는 단일 사용자 모드를 끄고
동일한 Google OAuth Client ID를 프론트와 백엔드에 설정합니다. 비밀값과 서비스계정 JSON은
`.secrets/` 또는 로컬 환경변수로만 관리하고 이미지나 Git에 포함하지 않습니다.

## 검증

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
docker build -t icore-back:local .
docker build -f Dockerfile.cloudrun-scraper -t icore-scraper:local .
```

GitHub Actions의 GCP 배포는 저장소 변수 `GCP_DEPLOY_ENABLED=true`와 필요한 GCP
secrets를 모두 설정한 경우에만 실행됩니다. 활성화하면 Artifact Registry의 `icore`
저장소에 API·워커 이미지를 올리고 기존 Cloud Run `icore-api`, `icore-g2b-worker`를
갱신합니다. VM 배포는 사용하지 않습니다. 변수가 없으면 테스트·이미지 빌드 검증까지만
수행합니다.
