# iCORE-G2B

나라장터 개찰결과를 공통 원본으로 수집하고 사용자별 키워드로 검토한 뒤, 사용자가 선택한 결과만 개인 Google Sheet에 반영하는 내부 웹 서비스입니다.

## 제품 원칙

- 개찰결과 원본과 업체 상세는 사용자 수와 관계없이 한 번만 저장합니다.
- 각 사용자는 본인의 포함·제외 키워드에 맞는 최근 14일 결과만 검토합니다.
- 목록 조회, 검색, 선택과 미리보기는 Google Sheet를 변경하지 않습니다.
- 최종 확인한 결과만 Sheet에 반영하며, 반영·제외한 결과는 다시 노출하지 않습니다.
- 공통 수집은 12시간 주기로 실행합니다.

## 저장소 브랜치

| 브랜치 | 역할 |
| --- | --- |
| `main` | 제품·아키텍처·API 문서 |
| `front` | React 기반 개찰결과 검토 웹 앱 |
| `back` | FastAPI API, 수집·매칭·Sheet 반영, Cloud Run 워커 |

`front`와 `back` 브랜치는 각 애플리케이션과 배포 워크플로를 관리하고, `main`은 공통 문서를 관리합니다. 로컬의 `icore-front/`, `icore-back/` 디렉터리는 연결된 Git worktree이며 `main` 브랜치에 포함하지 않습니다.

## 문서

- [G2B 사업기회 관리 PRD](docs/product/g2b-opportunity-management-prd.md)
- [개찰결과 검토 UX 명세](docs/product/opening-results-ux-spec.md)
- [시스템 아키텍처](docs/architecture/system-architecture.md)
- [데이터베이스 구조](docs/architecture/database-schema.md)
- [API 참조](docs/reference/api.md)

## 노코드 랜딩 기능 정리 상태

랜딩 빌더·사이트 관리 프론트와 백엔드 API는 제거됐습니다. 기존 DB 테이블이나 클라우드 자산은 코드 배포 과정에서 자동 삭제하지 않으며, 별도 백업·승인 절차를 거쳐 폐기합니다.
