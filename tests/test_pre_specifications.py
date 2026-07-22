import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import requests
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.data.models import Base
from app.g2b.pre_specifications.client import (
    PreSpecificationApiClient,
    PreSpecificationApiConfigurationError,
    PreSpecificationApiConfig,
    PreSpecificationApiError,
    normalize_source_item,
)
from app.g2b.pre_specifications.models import (
    PreSpecificationCollectionRunModel,
    PreSpecificationModel,
    PreSpecificationSnapshotModel,
)
from app.g2b.pre_specifications.router import (
    collect_pre_specification_data,
    fetch_pre_specification_detail,
    fetch_pre_specifications,
    router,
)
from app.g2b.pre_specifications.schemas import (
    CollectPreSpecificationsRequest,
    PreSpecificationListQuery,
)
from app.g2b.pre_specifications.service import (
    collect_pre_specifications,
    deadline_status,
    list_pre_specifications,
    upsert_pre_specifications,
)
from app.services.auth_service import require_organization_auth


def api_payload(items, total_count):
    return {
        "response": {
            "header": {"resultCode": "00", "resultMsg": "OK"},
            "body": {
                "items": {"item": items},
                "totalCount": str(total_count),
            },
        }
    }


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, *, params, headers, timeout):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return FakeResponse(self.payloads.pop(0))


class FailingSession:
    def get(self, url, *, params, headers, timeout):
        raise requests.Timeout("timeout")


class StubClient:
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error

    def collect(self, start_date, end_date):
        if self.error:
            raise self.error
        return self.rows


class PreSpecificationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_client_collects_single_and_list_item_pages(self):
        session = FakeSession(
            [
                api_payload({"bfSpecRgstNo": "R001"}, 2),
                api_payload([{"bfSpecRgstNo": "R002"}], 2),
            ]
        )
        client = PreSpecificationApiClient(
            PreSpecificationApiConfig(
                base_url="https://example.test",
                service_key="encoded%2Bkey",
                page_size=1,
            ),
            session=session,
        )

        rows = client.collect(date(2026, 7, 20), date(2026, 7, 21))

        self.assertEqual([row["bfSpecRgstNo"] for row in rows], ["R001", "R002"])
        self.assertEqual([call["params"]["pageNo"] for call in session.calls], [1, 2])
        self.assertEqual(session.calls[0]["params"]["serviceKey"], "encoded+key")

    def test_client_wraps_transport_error(self):
        client = PreSpecificationApiClient(
            PreSpecificationApiConfig(
                base_url="https://example.test",
                service_key="key",
            ),
            session=FailingSession(),
        )

        with self.assertRaisesRegex(PreSpecificationApiError, "호출에 실패"):
            client.collect(date(2026, 7, 20), date(2026, 7, 20))

    def test_client_rejects_api_error_response(self):
        session = FakeSession(
            [
                {
                    "response": {
                        "header": {
                            "resultCode": "03",
                            "resultMsg": "NODATA_ERROR",
                        },
                        "body": {},
                    }
                }
            ]
        )
        client = PreSpecificationApiClient(
            PreSpecificationApiConfig(
                base_url="https://example.test",
                service_key="key",
            ),
            session=session,
        )

        with self.assertRaisesRegex(PreSpecificationApiError, "NODATA_ERROR"):
            client.collect(date(2026, 7, 20), date(2026, 7, 20))

    def test_source_payload_maps_to_stable_contract(self):
        item = normalize_source_item(
            {
                "bfSpecRgstNo": " R001 ",
                "bidNtceNo": "20260720-1",
                "bidNtceOrd": "00",
                "prdctClsfcNoNm": "AI 교육",
                "asignBdgtAmt": "1,000",
                "rgstDt": "202607201030",
                "specDocFileUrl1": "https://example.test/file",
            }
        )

        self.assertEqual(item["bf_spec_rgst_no"], "R001")
        self.assertEqual(item["bid_notice_no"], "20260720-1")
        self.assertEqual(item["allocated_budget"], Decimal("1000"))
        self.assertEqual(len(item["attachments"]), 1)

    def test_upsert_preserves_identity_and_deduplicates_snapshot(self):
        first = {
            "bf_spec_rgst_no": "R001",
            "business_name": "클라우드 교육",
            "registered_at": "202607201030",
            "raw": {"bfSpecRgstNo": "R001", "version": 1},
        }
        self.assertEqual(upsert_pre_specifications(self.db, [first]), (1, 0))
        self.assertEqual(upsert_pre_specifications(self.db, [first]), (0, 1))
        self.assertEqual(
            self.db.scalar(select(func.count(PreSpecificationSnapshotModel.id))),
            1,
        )

        changed = {**first, "raw": {"bfSpecRgstNo": "R001", "version": 2}}
        self.assertEqual(upsert_pre_specifications(self.db, [changed]), (0, 1))
        self.assertEqual(
            self.db.scalar(select(func.count(PreSpecificationSnapshotModel.id))),
            2,
        )

    def test_list_supports_keywords_exclusion_budget_and_attachment(self):
        upsert_pre_specifications(
            self.db,
            [
                {
                    "bf_spec_rgst_no": "R001",
                    "business_name": "AI 교육 연수",
                    "demand_agency_name": "A교육청",
                    "allocated_budget": 2000,
                    "attachments": [{"url": "https://example.test/1"}],
                },
                {
                    "bf_spec_rgst_no": "R002",
                    "business_name": "AI 연수구 홍보",
                    "demand_agency_name": "B구청",
                    "allocated_budget": 3000,
                },
                {
                    "bf_spec_rgst_no": "R003",
                    "business_name": "클라우드 전환",
                    "demand_agency_name": "C공사",
                    "allocated_budget": 500,
                },
            ],
        )
        query = PreSpecificationListQuery(
            keywords=["AI", "교육"],
            keyword_mode="AND",
            excluded_keywords=["연수구"],
            min_budget=1000,
            attachment="HAS",
        )

        rows, total = list_pre_specifications(self.db, query)

        self.assertEqual(total, 1)
        self.assertEqual(rows[0].bf_spec_rgst_no, "R001")

    def test_exclusion_only_keeps_non_matching_rows(self):
        upsert_pre_specifications(
            self.db,
            [
                {"bf_spec_rgst_no": "R001", "business_name": "AI 교육"},
                {"bf_spec_rgst_no": "R002", "business_name": "연수구 홍보"},
            ],
        )

        rows, total = list_pre_specifications(
            self.db,
            PreSpecificationListQuery(excluded_keywords=["연수구"]),
        )

        self.assertEqual(total, 1)
        self.assertEqual(rows[0].bf_spec_rgst_no, "R001")

    def test_collection_records_success_and_failure(self):
        success = collect_pre_specifications(
            self.db,
            date(2026, 7, 20),
            date(2026, 7, 20),
            client=StubClient(
                [
                    {"bfSpecRgstNo": "R001", "prdctClsfcNoNm": "AI 교육"},
                    {"bfSpecRgstNo": "R001", "prdctClsfcNoNm": "AI 교육 변경"},
                    {"prdctClsfcNoNm": "식별자 없음"},
                ]
            ),
        )
        self.assertEqual(success["fetched_count"], 1)
        self.assertEqual(success["inserted_count"], 1)

        with self.assertRaisesRegex(RuntimeError, "수집 실패"):
            collect_pre_specifications(
                self.db,
                date(2026, 7, 21),
                date(2026, 7, 21),
                client=StubClient(error=RuntimeError("수집 실패")),
            )

        runs = self.db.scalars(
            select(PreSpecificationCollectionRunModel).order_by(
                PreSpecificationCollectionRunModel.id
            )
        ).all()
        self.assertEqual([run.status for run in runs], ["SUCCESS", "FAILED"])

    def test_deadline_status_uses_kst_date(self):
        now = datetime(2026, 7, 20, 15, 30, tzinfo=timezone.utc)
        same_kst_day = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)

        self.assertEqual(deadline_status(None, now), "UNKNOWN")
        self.assertEqual(deadline_status(same_kst_day, now), "TODAY")

    def test_api_routes_use_current_organization_auth(self):
        protected_paths = {
            "/api/v1/pre-specifications",
            "/api/v1/pre-specifications/collect",
            "/api/v1/pre-specifications/{bf_spec_rgst_no}",
        }
        routes = [route for route in router.routes if route.path in protected_paths]

        self.assertEqual({route.path for route in routes}, protected_paths)
        for route in routes:
            dependency_calls = {
                dependency.call for dependency in route.dependant.dependencies
            }
            self.assertIn(require_organization_auth, dependency_calls)

    def test_api_lists_and_fetches_detail_with_current_organization_auth(self):
        upsert_pre_specifications(
            self.db,
            [
                {
                    "bf_spec_rgst_no": "R001",
                    "business_name": "AI 교육",
                    "demand_agency_name": "교육청",
                    "attachments": [{"url": "https://example.test/spec"}],
                },
                {
                    "bf_spec_rgst_no": "R002",
                    "business_name": "연수구 홍보",
                },
            ],
        )
        auth = {
            "user_id": 1,
            "role": "viewer",
            "organization_id": 10,
            "organization_role": "member",
        }
        response = fetch_pre_specifications(
            q=None,
            keywords=["AI"],
            keyword_mode="OR",
            excluded_keywords=["연수구"],
            registered_from=None,
            registered_to=None,
            demand_agency=None,
            min_budget=None,
            max_budget=None,
            attachment="ALL",
            deadline_status="ALL",
            page=1,
            page_size=30,
            _=auth,
            db=self.db,
        )
        detail = fetch_pre_specification_detail("R001", _=auth, db=self.db)

        self.assertEqual(response.total, 1)
        self.assertEqual(response.items[0].bf_spec_rgst_no, "R001")
        self.assertEqual(detail.attachments[0]["url"], "https://example.test/spec")
        with self.assertRaises(HTTPException) as raised:
            fetch_pre_specification_detail("UNKNOWN", _=auth, db=self.db)
        self.assertEqual(raised.exception.status_code, 404)

    def test_api_manual_collection_requires_system_admin(self):
        request = CollectPreSpecificationsRequest(
            start_date=date(2026, 7, 20),
            end_date=date(2026, 7, 20),
        )
        with self.assertRaises(HTTPException) as raised:
            collect_pre_specification_data(
                request,
                auth={
                    "user_id": 1,
                    "role": "viewer",
                    "organization_id": 10,
                    "organization_role": "admin",
                },
                db=self.db,
            )
        self.assertEqual(raised.exception.status_code, 403)

        admin_auth = {
            "user_id": 1,
            "role": "admin",
            "organization_id": 10,
            "organization_role": "admin",
        }
        with patch(
            "app.g2b.pre_specifications.router.collect_pre_specifications",
            return_value={
                "run_key": "manual:test",
                "fetched_count": 2,
                "inserted_count": 2,
                "updated_count": 0,
            },
        ):
            allowed = collect_pre_specification_data(
                request,
                auth=admin_auth,
                db=self.db,
            )

        self.assertEqual(allowed.inserted_count, 2)

    def test_api_maps_collection_configuration_and_source_errors(self):
        request = CollectPreSpecificationsRequest(
            start_date=date(2026, 7, 20),
            end_date=date(2026, 7, 20),
        )
        auth = {
            "user_id": 1,
            "role": "admin",
            "organization_id": 10,
            "organization_role": "admin",
        }
        cases = (
            (PreSpecificationApiConfigurationError("키 없음"), 503),
            (PreSpecificationApiError("원본 오류"), 502),
        )

        for error, expected_status in cases:
            with self.subTest(expected_status=expected_status):
                with patch(
                    "app.g2b.pre_specifications.router.collect_pre_specifications",
                    side_effect=error,
                ):
                    with self.assertRaises(HTTPException) as raised:
                        collect_pre_specification_data(request, auth=auth, db=self.db)
                self.assertEqual(raised.exception.status_code, expected_status)

    def test_api_rejects_inverted_budget_range(self):
        with self.assertRaises(HTTPException) as raised:
            fetch_pre_specifications(
                q=None,
                keywords=[],
                keyword_mode="OR",
                excluded_keywords=[],
                registered_from=None,
                registered_to=None,
                demand_agency=None,
                min_budget=Decimal("2000"),
                max_budget=Decimal("1000"),
                attachment="ALL",
                deadline_status="ALL",
                page=1,
                page_size=30,
                _={"organization_id": 10},
                db=self.db,
            )

        self.assertEqual(raised.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
