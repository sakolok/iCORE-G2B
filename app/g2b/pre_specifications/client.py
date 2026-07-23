from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import unquote

import requests

from app.core.config import settings


class PreSpecificationApiError(RuntimeError):
    pass


class PreSpecificationApiConfigurationError(PreSpecificationApiError):
    pass


@dataclass(frozen=True)
class PreSpecificationApiConfig:
    base_url: str
    service_key: str
    page_size: int = 100
    timeout_seconds: int = 20

    @classmethod
    def from_env(cls) -> "PreSpecificationApiConfig":
        service_key = settings.g2b_pre_spec_service_key.strip()
        if not service_key:
            raise PreSpecificationApiConfigurationError(
                "G2B_PRE_SPEC_SERVICE_KEY 또는 G2B_SERVICE_KEY가 필요합니다."
            )
        return cls(
            base_url=(
                "https://apis.data.go.kr/1230000/ao/"
                "HrcspSsstndrdInfoService"
            ),
            service_key=service_key,
        )


class PreSpecificationApiClient:
    PATH = "/getPublicPrcureThngInfoServc"

    def __init__(
        self,
        config: PreSpecificationApiConfig,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config
        self.session = session or requests.Session()

    @property
    def service_key(self) -> str:
        if "%" in self.config.service_key:
            return unquote(self.config.service_key)
        return self.config.service_key

    @staticmethod
    def _items(payload: Any) -> tuple[int, list[dict[str, Any]]]:
        root = payload.get("response", payload) if isinstance(payload, dict) else {}
        raw_header = root.get("header", {}) if isinstance(root, dict) else {}
        raw_body = root.get("body", {}) if isinstance(root, dict) else {}
        header = raw_header if isinstance(raw_header, dict) else {}
        body = raw_body if isinstance(raw_body, dict) else {}
        if str(header.get("resultCode") or "") != "00":
            raise PreSpecificationApiError(
                str(header.get("resultMsg") or "사전규격 API 오류")
            )

        items = body.get("items", {}) if isinstance(body, dict) else {}
        if isinstance(items, dict):
            items = items.get("item", [])
        if isinstance(items, dict):
            items = [items]
        rows = [item for item in (items or []) if isinstance(item, dict)]
        try:
            total_count = int(str(body.get("totalCount") or 0))
        except (TypeError, ValueError) as error:
            raise PreSpecificationApiError(
                "사전규격 API의 전체 건수가 올바르지 않습니다."
            ) from error
        return total_count, rows

    def collect(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page = 1
        total_count: int | None = None
        while total_count is None or len(rows) < total_count:
            try:
                response = self.session.get(
                    f"{self.config.base_url}{self.PATH}",
                    params={
                        "serviceKey": self.service_key,
                        "type": "json",
                        "inqryDiv": "1",
                        "inqryBgnDt": start_date.strftime("%Y%m%d0000"),
                        "inqryEndDt": end_date.strftime("%Y%m%d2359"),
                        "pageNo": page,
                        "numOfRows": self.config.page_size,
                    },
                    headers={"Accept": "application/json"},
                    timeout=self.config.timeout_seconds,
                )
                response.raise_for_status()
                current_total, page_rows = self._items(response.json())
            except requests.RequestException as error:
                raise PreSpecificationApiError(
                    "사전규격 API 호출에 실패했습니다."
                ) from error
            except ValueError as error:
                raise PreSpecificationApiError(
                    "사전규격 API가 JSON 응답을 반환하지 않았습니다."
                ) from error

            total_count = current_total
            rows.extend(page_rows)
            if not page_rows:
                break
            page += 1
        return rows


def normalize_source_item(item: dict[str, Any]) -> dict[str, Any]:
    def clean(value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    def amount(value: Any) -> Decimal | None:
        try:
            return Decimal(str(value).replace(",", ""))
        except (InvalidOperation, TypeError, ValueError):
            return None

    external_id = clean(item.get("bfSpecRgstNo")) or ""
    attachments = [
        {
            "key": f"{external_id}-{index}",
            "label": f"규격서 {index}",
            "url": url,
        }
        for index in range(1, 6)
        if (url := clean(item.get(f"specDocFileUrl{index}")))
    ]
    return {
        "bf_spec_rgst_no": external_id,
        "bid_notice_no": clean(item.get("bidNtceNo")),
        "bid_notice_ord": clean(item.get("bidNtceOrd")),
        "reference_no": clean(item.get("refNo")),
        "business_name": clean(item.get("prdctClsfcNoNm")),
        "business_type": clean(item.get("bsnsDivNm")),
        "demand_agency_name": clean(
            item.get("rlDminsttNm") or item.get("orderInsttNm")
        ),
        "ordering_agency_name": clean(item.get("orderInsttNm")),
        "allocated_budget": amount(item.get("asignBdgtAmt")),
        "registered_at": item.get("rgstDt") or item.get("rcptDt"),
        "opinion_deadline": item.get("opninRgstClseDt"),
        "delivery_deadline": item.get("dlvrTmlmtDt"),
        "contact_name": clean(item.get("ofclNm")),
        "contact_phone": clean(item.get("ofclTelNo")),
        "attachments": attachments,
        "raw": item,
    }
