# G2B 수집 결과 공통 저장 계약

이 기능 폴더에는 DB 테이블이나 마이그레이션이 없습니다. 통합 브랜치에서 제공하는 공통 저장 함수를 호출할 때만 아래 레코드를 전달합니다.

## 입찰공고

`BidNoticeStorageRecord`는 항상 아래 키를 제공합니다. 확인할 수 없는 값은 `null`입니다.

| 키 | G2B 원본값 | 규칙 |
| --- | --- | --- |
| `bid_notice_no` | `bidNtceNo` | 문자열 그대로 보존 |
| `bid_notice_ord` | `bidNtceOrd` | 문자열 그대로 보존 |
| `business_name` | `bidNtceNm` | 원본 공고명이 제공될 때만 전달 |
| `demand_agency_name` | `dminsttNm` | 원본 수요기관명이 제공될 때만 전달 |
| `base_amount` | `bsisAmount` | 숫자만 전달. 추정가격·배정예산으로 대체하지 않음 |
| `proposal_deadline` | 제안서 제출마감 전용 필드 | KST `datetime`. 입찰마감일로 대체하지 않음 |
| `region_restriction` | 지역제한 여부 전용 필드 | 명시적인 불리언 값만 전달 |
| `is_two_stage_bid` | 2단계입찰 여부 전용 필드 | 명시적인 불리언 값만 전달 |

중복 비교는 `bid_notice_dedup_key()`를 사용합니다. 저장값을 바꾸지 않으며, 숫자 차수만 비교용으로 정규화하므로 `00`과 `000`은 동일한 차수로 판정합니다.

## 사전규격

`PreSpecificationStorageRecord`의 고유 키는 `bfSpecRgstNo`입니다. 사전규격이 입찰공고와 연결된 경우에만 `bid_notice_no`, `bid_notice_ord`를 별도 nullable 필드로 전달합니다. 연결 정보를 확인할 수 없으면 `null`로 남깁니다.

## 통합 시점

통합 담당자가 공통 DB 모델·마이그레이션·저장 함수를 만들거나 관리합니다. 이 모듈은 `contracts.py`의 레코드를 만들어 그 함수에 전달하며, 공용 SQLAlchemy 모델을 import하거나 변경하지 않습니다.
