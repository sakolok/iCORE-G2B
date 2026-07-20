# 사전규격 모듈

`g2b/pre_specifications`는 기존 개찰결과 도메인과 분리된 사전규격 수집·조회 모듈입니다.

- 원본 키: `bfSpecRgstNo`
- 연결 공고 키: `bid_notice_no` + `bid_notice_ord` (둘 다 확인된 경우에만 저장)
- 시간: API의 naive 시간은 KST로 해석
- 미확인 값: 추정하지 않고 `null`
- 원본: `g2b_pre_specifications`에 보존하고 raw snapshot은 변경 버전만 추가
- 사용자 상태: `user_pre_specification_states`에서 `EXPORTED` 등을 관리하므로 Sheet 반영 후에도 원본은 삭제하지 않음

프론트는 목록 요청에서 `keywords`를 반복 쿼리 파라미터로 보내고 `keyword_mode=AND|OR`, `excluded_keywords`를 함께 보냅니다. 예: `?keywords=AI&keywords=교육&keyword_mode=AND&excluded_keywords=연수구`.

Sheet 목적지는 기존 `sheet_destinations`를 사용합니다. 사전규격 탭의 A:L 헤더는 `sheet_export.SHEET_HEADERS` 순서를 유지해야 합니다.
