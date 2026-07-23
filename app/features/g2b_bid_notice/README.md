# G2B 입찰공고 검색 백엔드 feature

이 폴더는 기존 `scraper.py`, `platform_service.py`, Cloud Scheduler, Cloud Run 워커를 수정하지 않는다.

병합 담당자는 `main.py`에 아래 두 줄만 추가한다.

```python
from app.features.g2b_bid_notice.router import router as bid_notice_search_router
app.include_router(bid_notice_search_router)
```

제공 API는 `POST /api/bid-notice-search/preview`다. 기존 `SCRAPER_PRIVATE_API_BASE`를 이용해 **입찰공고 검색 미리보기만** 수행한다. 이메일, Google Sheet, 실행 이력, 정기 수집 설정을 변경하지 않는다.

정기 수집에도 같은 조건을 적용하려면, 워커 병합 시 이 feature의 분류 함수를 호출하는 별도 작업이 필요하다.
