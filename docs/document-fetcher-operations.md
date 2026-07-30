# 입찰공고 문서 다운로드 운영

`icore-doc-fetcher`는 나라장터 첨부파일만 내려받는 비공개 Cloud Run
서비스다. 기존 `icore-api`는 VPC에 연결하지 않고, 문서분석 다운로드 요청만
서비스 계정 인증을 거쳐 전용 서비스로 전달한다.

## 단계적 활성화

1. `G2B_DOCUMENT_FETCHER_URL`과 `G2B_DOCUMENT_FETCHER_AUDIENCE`에 전용 서비스
   URL을 등록한다.
2. `G2B_DOCUMENT_FETCHER_NOTICE_IDS`에 검증할 공고 식별자를 쉼표로 구분해
   등록한다.
3. `G2B_DOCUMENT_FETCHER_ENABLED=true`로 전환한다.
4. 대상 공고의 문서분석 상태와 지역·업종 제한 반영값을 확인한다.
5. 검증이 끝나면 `G2B_DOCUMENT_FETCHER_NOTICE_IDS`를 비워 전체 문서분석에
   적용한다.

## 즉시 롤백

문서 다운로드 전용 경로에 문제가 생기면 `icore-api`의
`G2B_DOCUMENT_FETCHER_ENABLED`만 `false`로 바꾼다. 이 값이 `false`이면 기존
직접 다운로드 경로만 사용한다. 전용 VPC, NAT, 고정 IP, 전용 Cloud Run
서비스는 요청을 받지 않으므로 롤백 시 삭제하지 않는다.

```bash
gcloud run services update icore-api \
  --project=iceu-kolok91 \
  --region=asia-northeast3 \
  --update-env-vars=G2B_DOCUMENT_FETCHER_ENABLED=false
```

롤백 후 `/api/health` 응답과 일반 목록 조회가 정상인지 확인한다.
