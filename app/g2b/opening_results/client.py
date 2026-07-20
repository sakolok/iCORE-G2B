import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

from app.g2b.opening_results.schemas import BusinessType


KST = ZoneInfo("Asia/Seoul")


class OpeningResultApiError(RuntimeError):
    pass


class OpeningResultApiConfigurationError(OpeningResultApiError):
    pass


@dataclass(frozen=True)
class OpeningResultApiConfig:
    base_url: str
    service_key: str
    page_size: int = 100
    timeout_seconds: int = 20

    @classmethod
    def from_env(cls) -> "OpeningResultApiConfig":
        service_key = os.getenv("G2B_AWARD_SERVICE_KEY", os.getenv("G2B_SERVICE_KEY", "")).strip()
        if not service_key:
            raise OpeningResultApiConfigurationError(
                "G2B_AWARD_SERVICE_KEY 또는 G2B_SERVICE_KEY가 필요합니다."
            )
        return cls(
            base_url=os.getenv(
                "G2B_AWARD_SOURCE_URL",
                "https://apis.data.go.kr/1230000/as/ScsbidInfoService",
            ).rstrip("/"),
            service_key=service_key,
            page_size=max(1, min(int(os.getenv("G2B_AWARD_PAGE_SIZE", "100")), 999)),
            timeout_seconds=max(1, int(os.getenv("G2B_AWARD_TIMEOUT_SECONDS", "20"))),
        )


class OpeningResultApiClient:
    SUMMARY_PATHS = {
        BusinessType.SERVICE: "/getOpengResultListInfoServc",
        BusinessType.GOODS: "/getOpengResultListInfoThng",
        BusinessType.CONSTRUCTION: "/getOpengResultListInfoCnstwk",
        BusinessType.FOREIGN: "/getOpengResultListInfoFrgcpt",
    }
    WINNER_PATHS = {
        BusinessType.SERVICE: "/getScsbidListSttusServc",
        BusinessType.GOODS: "/getScsbidListSttusThng",
        BusinessType.CONSTRUCTION: "/getScsbidListSttusCnstwk",
        BusinessType.FOREIGN: "/getScsbidListSttusFrgcpt",
    }
    ENTRY_PATH = "/getOpengResultListInfoOpengCompt"

    def __init__(
        self,
        config: OpeningResultApiConfig,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config
        self.session = session or requests.Session()

    @staticmethod
    def _format_query_datetime(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=KST)
        return value.astimezone(KST).strftime("%Y%m%d%H%M")

    @staticmethod
    def _extract_body(payload: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(payload, dict):
            raise OpeningResultApiError("낙찰정보 API 응답이 객체가 아닙니다.")
        if "response" in payload:
            response = payload.get("response")
            if not isinstance(response, dict):
                raise OpeningResultApiError("낙찰정보 API response 형식이 올바르지 않습니다.")
        elif "header" in payload and "body" in payload:
            response = payload
        else:
            raise OpeningResultApiError("낙찰정보 API 성공 응답 형식이 없습니다.")
        header = response.get("header")
        body = response.get("body")
        if not isinstance(header, dict) or not isinstance(body, dict):
            raise OpeningResultApiError("낙찰정보 API header/body 형식이 올바르지 않습니다.")
        result_code = str(header.get("resultCode") or "").strip()
        if result_code != "00":
            result_message = str(header.get("resultMsg") or "").strip()
            raise OpeningResultApiError(
                f"낙찰정보 API 오류: code={result_code}, message={result_message}"
            )
        return header, body

    @staticmethod
    def _extract_items(body: dict[str, Any]) -> list[dict[str, Any]]:
        items = body.get("items")
        if isinstance(items, dict):
            items = items.get("item")
        if items is None:
            return []
        if isinstance(items, dict):
            return [items]
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
        return []

    def _fetch_all(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        page = 1
        rows: list[dict[str, Any]] = []
        seen_page_signatures: set[str] = set()
        while True:
            page_params = {
                "serviceKey": self.config.service_key,
                "pageNo": page,
                "numOfRows": self.config.page_size,
                "type": "json",
                **params,
            }
            try:
                response = self.session.get(
                    f"{self.config.base_url}{path}",
                    params=page_params,
                    headers={"Accept": "application/json"},
                    timeout=self.config.timeout_seconds,
                )
                response.raise_for_status()
                payload = response.json()
            except requests.RequestException as error:
                raise OpeningResultApiError(
                    f"낙찰정보 API 호출에 실패했습니다: path={path}, page={page}"
                ) from error
            except ValueError as error:
                raise OpeningResultApiError(
                    f"낙찰정보 API가 JSON이 아닌 응답을 반환했습니다: path={path}, page={page}"
                ) from error
            _, body = self._extract_body(payload)
            page_rows = self._extract_items(body)
            page_signature = repr(page_rows)
            if page_signature in seen_page_signatures:
                raise OpeningResultApiError(
                    f"낙찰정보 API가 같은 페이지를 반복 반환했습니다: path={path}, page={page}"
                )
            seen_page_signatures.add(page_signature)
            rows.extend(page_rows)

            try:
                total_count = int(str(body.get("totalCount") or "0"))
            except ValueError:
                total_count = 0
            if not page_rows or (total_count and len(rows) >= total_count):
                break
            if not total_count and len(page_rows) < self.config.page_size:
                break
            page += 1
        return rows

    def search_rounds(
        self,
        business_type: BusinessType,
        start_at: datetime,
        end_at: datetime,
    ) -> list[dict[str, Any]]:
        return self._fetch_all(
            self.SUMMARY_PATHS[business_type],
            {
                "inqryDiv": "1",
                "inqryBgnDt": self._format_query_datetime(start_at),
                "inqryEndDt": self._format_query_datetime(end_at),
            },
        )

    def search_winners(
        self,
        business_type: BusinessType,
        start_at: datetime,
        end_at: datetime,
    ) -> list[dict[str, Any]]:
        return self._fetch_all(
            self.WINNER_PATHS[business_type],
            {
                "inqryDiv": "1",
                "inqryBgnDt": self._format_query_datetime(start_at),
                "inqryEndDt": self._format_query_datetime(end_at),
            },
        )

    def fetch_entries(self, summary: dict[str, Any]) -> list[dict[str, Any]]:
        bid_notice_no = str(summary.get("bidNtceNo") or "").strip()
        if not bid_notice_no:
            return []
        params: dict[str, Any] = {"bidNtceNo": bid_notice_no}
        for name in ("bidNtceOrd", "bidClsfcNo", "rbidNo"):
            value = str(summary.get(name) or "").strip()
            if value:
                params[name] = value
        return self._fetch_all(self.ENTRY_PATH, params)
