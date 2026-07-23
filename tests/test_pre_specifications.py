import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock, patch

import requests
from fastapi import HTTPException
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import Session

from app.data.bootstrap import ensure_schema_compatibility
from app.data.models import Base, OrganizationModel, UserModel
from app.g2b.opening_results.models import SheetDestinationModel
from app.g2b.opening_results.sheet_export import SheetUpsertResult
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
    PreSpecificationSheetExportModel,
    PreSpecificationSnapshotModel,
    UserPreSpecificationStateModel,
    UserPreSpecificationProfileModel,
)
from app.g2b.pre_specifications.router import (
    collect_pre_specification_data,
    collect_pre_specification_data_on_schedule,
    delete_pre_specification_from_inbox,
    export_pre_specifications_sheet,
    fetch_archived_pre_specification_detail,
    fetch_archived_pre_specifications,
    fetch_pre_specification_detail,
    fetch_pre_specifications,
    fetch_pre_specification_settings,
    restore_pre_specification_to_inbox,
    save_pre_specification_profile,
    save_pre_specification_sheet_destination,
    router,
)
from app.g2b.pre_specifications.schemas import (
    CollectPreSpecificationsRequest,
    ExportPreSpecificationsSheetRequest,
    PreSpecificationListQuery,
    PreSpecificationProfileUpdateRequest,
    PreSpecificationSheetDestinationUpsertRequest,
)
from app.g2b.pre_specifications.sheet_export import (
    SHEET_HEADERS,
    PreSpecificationSheetWriter,
    build_sheet_rows,
)
from app.g2b.pre_specifications.service import (
    collect_pre_specifications,
    deadline_status,
    list_archived_pre_specifications,
    list_pre_specifications,
    run_scheduled_pre_specifications,
    upsert_pre_specifications,
)
from app.services.auth_service import (
    require_organization_auth,
    verify_cloud_scheduler_oidc_token,
    verify_scraper_internal_token,
)


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


class PreSpecificationSchemaCompatibilityTest(unittest.TestCase):
    def test_adds_missing_sheet_export_attempt_count(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE g2b_pre_specification_sheet_exports "
                    "(id INTEGER PRIMARY KEY)"
                )
            )

        ensure_schema_compatibility(engine)

        columns = {
            column["name"]: column
            for column in inspect(engine).get_columns(
                "g2b_pre_specification_sheet_exports"
            )
        }
        self.assertIn("attempt_count", columns)
        self.assertFalse(columns["attempt_count"]["nullable"])


class StubClient:
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error

    def collect(self, start_date, end_date):
        if self.error:
            raise self.error
        return self.rows


class FakeSheetRequest:
    def __init__(self, result=None, callback=None):
        self.result = result or {}
        self.callback = callback

    def execute(self):
        if self.callback:
            self.callback()
        return self.result


class FakePreSpecificationSheetService:
    def __init__(self, *, tab_exists=True):
        self.tab_exists = tab_exists
        self.header = list(SHEET_HEADERS)
        self.existing_rows = [["R001", "기존 값"]]
        self.write_data = []
        self.added_tabs = []

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, *, spreadsheetId, fields=None, range=None):
        if fields:
            return FakeSheetRequest(
                {
                    "sheets": (
                        [{"properties": {"title": "사전규격"}}]
                        if self.tab_exists
                        else []
                    )
                }
            )
        if range.endswith("A1:L1"):
            return FakeSheetRequest({"values": [self.header]})
        return FakeSheetRequest({"values": self.existing_rows})

    def update(self, **kwargs):
        return FakeSheetRequest()

    def batchUpdate(self, *, spreadsheetId, body):
        if "data" in body:
            self.write_data.extend(body["data"])
        else:
            requests = body.get("requests") or []
            self.added_tabs.extend(
                request["addSheet"]["properties"]["title"]
                for request in requests
                if "addSheet" in request
            )
        return FakeSheetRequest()


class PreSpecificationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.organization = OrganizationModel(name="테스트 조직", slug="pre-spec-test")
        self.user = UserModel(
            username="pre-spec-user",
            password_salt="salt",
            password_hash="hash",
            role="admin",
            is_active=True,
        )
        self.db.add_all([self.organization, self.user])
        self.db.flush()
        self.destination = SheetDestinationModel(
            organization_id=self.organization.id,
            owner_user_id=self.user.id,
            label="내 프로젝트 Sheet",
            spreadsheet_id="sheet-id",
            tab_name="개찰결과",
            is_default=True,
            is_active=True,
        )
        self.db.add(self.destination)
        self.db.commit()
        self.auth = {
            "user_id": self.user.id,
            "role": "admin",
            "organization_id": self.organization.id,
            "organization_role": "admin",
        }

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

        rows, total = list_pre_specifications(
            self.db,
            query,
            organization_id=self.organization.id,
            user_id=self.user.id,
        )

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
            organization_id=self.organization.id,
            user_id=self.user.id,
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

    def test_scheduled_collection_uses_current_kst_date(self):
        client = StubClient(
            [{"bfSpecRgstNo": "R001", "prdctClsfcNoNm": "AI 교육"}]
        )

        result = run_scheduled_pre_specifications(
            self.db,
            now=datetime(2026, 7, 20, 15, 30, tzinfo=timezone.utc),
            client=client,
        )

        self.assertEqual(result["fetched_count"], 1)
        run = self.db.scalar(select(PreSpecificationCollectionRunModel))
        self.assertEqual(run.window_start.date(), date(2026, 7, 21))
        self.assertEqual(run.window_end.date(), date(2026, 7, 21))

    def test_deadline_status_uses_kst_date(self):
        now = datetime(2026, 7, 20, 15, 30, tzinfo=timezone.utc)
        same_kst_day = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)

        self.assertEqual(deadline_status(None, now), "UNKNOWN")
        self.assertEqual(deadline_status(same_kst_day, now), "TODAY")

    def test_api_routes_use_current_organization_auth(self):
        protected_paths = {
            "/api/v1/pre-specifications",
            "/api/v1/pre-specifications/archive",
            "/api/v1/pre-specifications/archive/{bf_spec_rgst_no}",
            "/api/v1/pre-specifications/collect",
            "/api/v1/pre-specifications/export/sheet",
            "/api/v1/pre-specifications/{bf_spec_rgst_no}",
            "/api/v1/pre-specifications/{bf_spec_rgst_no}/restore",
        }
        routes = [route for route in router.routes if route.path in protected_paths]

        self.assertEqual({route.path for route in routes}, protected_paths)
        for route in routes:
            dependency_calls = {
                dependency.call for dependency in route.dependant.dependencies
            }
            self.assertIn(require_organization_auth, dependency_calls)

    def test_scheduled_route_requires_internal_token_and_scheduler_oidc(self):
        route = next(
            route
            for route in router.routes
            if route.path == "/api/v1/pre-specifications/internal/collect"
        )
        dependency_calls = {
            dependency.call for dependency in route.dependant.dependencies
        }

        self.assertIn(verify_scraper_internal_token, dependency_calls)
        self.assertIn(verify_cloud_scheduler_oidc_token, dependency_calls)

        with patch(
            "app.g2b.pre_specifications.router.run_scheduled_pre_specifications",
            return_value={
                "run_key": "scheduled:test",
                "fetched_count": 1,
                "inserted_count": 1,
                "updated_count": 0,
            },
        ):
            response = collect_pre_specification_data_on_schedule(db=self.db)

        self.assertEqual(response.run_key, "scheduled:test")

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
            auth=self.auth,
            db=self.db,
        )
        detail = fetch_pre_specification_detail(
            "R001",
            auth=self.auth,
            db=self.db,
        )

        self.assertEqual(response.total, 1)
        self.assertEqual(response.items[0].bf_spec_rgst_no, "R001")
        self.assertEqual(detail.attachments[0]["url"], "https://example.test/spec")
        with self.assertRaises(HTTPException) as raised:
            fetch_pre_specification_detail("UNKNOWN", auth=self.auth, db=self.db)
        self.assertEqual(raised.exception.status_code, 404)

    def test_personal_conditions_and_sheet_destinations_are_scoped_to_owner(self):
        other_user = UserModel(
            username="pre-spec-other-user",
            password_salt="salt",
            password_hash="hash",
            role="viewer",
            is_active=True,
        )
        self.db.add(other_user)
        self.db.flush()
        self.db.add(
            SheetDestinationModel(
                organization_id=self.organization.id,
                owner_user_id=other_user.id,
                label="다른 사용자 Sheet",
                spreadsheet_id="other-sheet-id",
                tab_name="사전규격",
                is_default=True,
                is_active=True,
            )
        )
        upsert_pre_specifications(
            self.db,
            [
                {"bf_spec_rgst_no": "R001", "business_name": "AI 교육"},
                {"bf_spec_rgst_no": "R002", "business_name": "AI 제외 사업"},
                {"bf_spec_rgst_no": "R003", "business_name": "클라우드 사업"},
            ],
        )

        profile = save_pre_specification_profile(
            PreSpecificationProfileUpdateRequest(
                enabled=True,
                keywords=["AI"],
                excluded_keywords=["제외"],
            ),
            auth=self.auth,
            db=self.db,
        )
        settings = fetch_pre_specification_settings(auth=self.auth, db=self.db)
        response = fetch_pre_specifications(
            q=None,
            keywords=[],
            keyword_mode="OR",
            excluded_keywords=[],
            registered_from=None,
            registered_to=None,
            demand_agency=None,
            min_budget=None,
            max_budget=None,
            attachment="ALL",
            deadline_status="ALL",
            page=1,
            page_size=30,
            auth=self.auth,
            db=self.db,
        )
        destination = save_pre_specification_sheet_destination(
            PreSpecificationSheetDestinationUpsertRequest(
                label="내 사전규격 Sheet",
                spreadsheet_id="my-pre-spec-sheet",
                tab_name="사전규격",
            ),
            auth=self.auth,
            db=self.db,
        )

        self.assertTrue(profile.enabled)
        self.assertEqual(profile.keywords, ["AI"])
        self.assertEqual(profile.excluded_keywords, ["제외"])
        self.assertEqual([row.bf_spec_rgst_no for row in response.items], ["R001"])
        self.assertEqual(
            [item.label for item in settings.sheet_destinations],
            ["내 프로젝트 Sheet"],
        )
        self.assertEqual(destination.scope, "PERSONAL")
        self.assertEqual(
            self.db.get(UserPreSpecificationProfileModel, 1).user_id,
            self.user.id,
        )

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
                auth=self.auth,
                db=self.db,
            )

        self.assertEqual(raised.exception.status_code, 422)

    def test_dismissed_item_stays_in_archive_for_fourteen_days_and_restores(self):
        upsert_pre_specifications(
            self.db,
            [
                {"bf_spec_rgst_no": "R001", "business_name": "AI 교육"},
                {"bf_spec_rgst_no": "R002", "business_name": "클라우드 전환"},
            ],
        )

        dismissed = delete_pre_specification_from_inbox(
            "R001",
            auth=self.auth,
            db=self.db,
        )
        inbox = fetch_pre_specifications(
            q=None,
            keywords=[],
            keyword_mode="OR",
            excluded_keywords=[],
            registered_from=None,
            registered_to=None,
            demand_agency=None,
            min_budget=None,
            max_budget=None,
            attachment="ALL",
            deadline_status="ALL",
            page=1,
            page_size=30,
            auth=self.auth,
            db=self.db,
        )
        archive = fetch_archived_pre_specifications(
            page=1,
            page_size=30,
            auth=self.auth,
            db=self.db,
        )
        detail = fetch_archived_pre_specification_detail(
            "R001",
            auth=self.auth,
            db=self.db,
        )

        self.assertEqual(dismissed.state, "DISMISSED")
        self.assertEqual([item.bf_spec_rgst_no for item in inbox.items], ["R002"])
        self.assertEqual(archive.total, 1)
        self.assertTrue(archive.items[0].can_restore)
        self.assertEqual(
            archive.items[0].expires_at - archive.items[0].handled_at,
            timedelta(days=14),
        )
        self.assertEqual(detail.bf_spec_rgst_no, "R001")

        restored = restore_pre_specification_to_inbox(
            "R001",
            auth=self.auth,
            db=self.db,
        )
        self.assertTrue(restored.visible)
        self.assertEqual(
            fetch_archived_pre_specifications(
                page=1,
                page_size=30,
                auth=self.auth,
                db=self.db,
            ).total,
            0,
        )

    def test_expired_archive_item_is_hidden_and_cannot_be_restored(self):
        upsert_pre_specifications(
            self.db,
            [{"bf_spec_rgst_no": "R001", "business_name": "AI 교육"}],
        )
        delete_pre_specification_from_inbox("R001", auth=self.auth, db=self.db)
        state = self.db.scalar(select(UserPreSpecificationStateModel))
        state.acted_at = datetime.now(timezone.utc) - timedelta(days=15)
        self.db.commit()

        rows, total = list_archived_pre_specifications(
            self.db,
            organization_id=self.organization.id,
            user_id=self.user.id,
        )

        self.assertEqual((rows, total), ([], 0))
        with self.assertRaises(HTTPException) as raised:
            restore_pre_specification_to_inbox(
                "R001",
                auth=self.auth,
                db=self.db,
            )
        self.assertEqual(raised.exception.status_code, 404)
        _, inbox_total = list_pre_specifications(
            self.db,
            PreSpecificationListQuery(),
            organization_id=self.organization.id,
            user_id=self.user.id,
        )
        self.assertEqual(inbox_total, 0)

    def test_sheet_preview_and_write_use_existing_connection_and_archive_result(self):
        upsert_pre_specifications(
            self.db,
            [
                {
                    "bf_spec_rgst_no": "R001",
                    "business_name": "AI 교육",
                    "demand_agency_name": "교육청",
                    "allocated_budget": 1000,
                    "attachments": [{"url": "https://example.test/spec"}],
                }
            ],
        )
        preview = export_pre_specifications_sheet(
            ExportPreSpecificationsSheetRequest(
                bf_spec_rgst_nos=["R001"],
                destination_id=self.destination.id,
            ),
            auth=self.auth,
            db=self.db,
        )
        writer = Mock()
        writer.upsert.return_value = SheetUpsertResult(
            inserted_count=1,
            updated_count=0,
        )
        with patch(
            "app.g2b.pre_specifications.router.PreSpecificationSheetWriter.from_env",
            return_value=writer,
        ) as writer_factory:
            written = export_pre_specifications_sheet(
                ExportPreSpecificationsSheetRequest(
                    bf_spec_rgst_nos=["R001"],
                    destination_id=self.destination.id,
                    dry_run=False,
                    expected_preview_token=preview.preview_token,
                ),
                auth=self.auth,
                db=self.db,
            )

        writer_factory.assert_called_once_with("sheet-id", "개찰결과")
        self.assertEqual(preview.destination_tab_name, "개찰결과")
        self.assertEqual(len(preview.headers), 12)
        self.assertEqual(preview.preview_rows[0][0], "R001")
        self.assertTrue(written.written)
        self.assertEqual(written.inserted_count, 1)
        self.assertEqual(
            self.db.scalar(select(PreSpecificationSheetExportModel.status)),
            "SUCCEEDED",
        )
        self.assertEqual(
            self.db.scalar(select(UserPreSpecificationStateModel.state)),
            "EXPORTED",
        )
        archive = fetch_archived_pre_specifications(
            page=1,
            page_size=30,
            auth=self.auth,
            db=self.db,
        )
        self.assertEqual(archive.items[0].handled_state, "EXPORTED")
        self.assertFalse(archive.items[0].can_restore)

    def test_sheet_failure_releases_lock_and_keeps_item_visible(self):
        upsert_pre_specifications(
            self.db,
            [{"bf_spec_rgst_no": "R001", "business_name": "AI 교육"}],
        )
        preview = export_pre_specifications_sheet(
            ExportPreSpecificationsSheetRequest(
                bf_spec_rgst_nos=["R001"],
                destination_id=self.destination.id,
            ),
            auth=self.auth,
            db=self.db,
        )
        writer = Mock()
        writer.upsert.side_effect = RuntimeError("sheet unavailable")

        with patch(
            "app.g2b.pre_specifications.router.PreSpecificationSheetWriter.from_env",
            return_value=writer,
        ):
            with self.assertRaises(HTTPException) as raised:
                export_pre_specifications_sheet(
                    ExportPreSpecificationsSheetRequest(
                        bf_spec_rgst_nos=["R001"],
                        destination_id=self.destination.id,
                        dry_run=False,
                        expected_preview_token=preview.preview_token,
                    ),
                    auth=self.auth,
                    db=self.db,
                )

        self.assertEqual(raised.exception.status_code, 502)
        self.db.refresh(self.destination)
        self.assertIsNone(self.destination.export_lock_token)
        self.assertEqual(
            self.db.scalar(select(PreSpecificationSheetExportModel.status)),
            "FAILED",
        )
        self.assertIsNone(self.db.scalar(select(UserPreSpecificationStateModel.id)))
        _, total = list_pre_specifications(
            self.db,
            PreSpecificationListQuery(),
            organization_id=self.organization.id,
            user_id=self.user.id,
        )
        self.assertEqual(total, 1)

    def test_personal_sheet_export_hides_item_only_for_owner(self):
        teammate = UserModel(
            username="pre-spec-teammate",
            password_salt="salt",
            password_hash="hash",
            role="viewer",
            is_active=True,
        )
        self.db.add(teammate)
        self.db.commit()
        upsert_pre_specifications(
            self.db,
            [{"bf_spec_rgst_no": "R001", "business_name": "AI 교육"}],
        )
        member_auth = {
            "user_id": teammate.id,
            "role": "viewer",
            "organization_id": self.organization.id,
            "organization_role": "member",
        }
        with self.assertRaises(HTTPException) as raised:
            export_pre_specifications_sheet(
                ExportPreSpecificationsSheetRequest(
                    bf_spec_rgst_nos=["R001"],
                    destination_id=self.destination.id,
                ),
                auth=member_auth,
                db=self.db,
            )
        self.assertEqual(raised.exception.status_code, 409)

        preview = export_pre_specifications_sheet(
            ExportPreSpecificationsSheetRequest(
                bf_spec_rgst_nos=["R001"],
                destination_id=self.destination.id,
            ),
            auth=self.auth,
            db=self.db,
        )
        writer = Mock()
        writer.upsert.return_value = SheetUpsertResult(
            inserted_count=1,
            updated_count=0,
        )
        with patch(
            "app.g2b.pre_specifications.router.PreSpecificationSheetWriter.from_env",
            return_value=writer,
        ):
            export_pre_specifications_sheet(
                ExportPreSpecificationsSheetRequest(
                    bf_spec_rgst_nos=["R001"],
                    destination_id=self.destination.id,
                    dry_run=False,
                    expected_preview_token=preview.preview_token,
                ),
                auth=self.auth,
                db=self.db,
            )

        _, teammate_total = list_pre_specifications(
            self.db,
            PreSpecificationListQuery(),
            organization_id=self.organization.id,
            user_id=teammate.id,
        )
        teammate_archive = fetch_archived_pre_specifications(
            page=1,
            page_size=30,
            auth=member_auth,
            db=self.db,
        )
        self.assertEqual(teammate_total, 1)
        self.assertEqual(teammate_archive.total, 0)

    def test_sheet_writer_updates_existing_row_and_inserts_new_row(self):
        upsert_pre_specifications(
            self.db,
            [
                {"bf_spec_rgst_no": "R001", "business_name": "변경 값"},
                {"bf_spec_rgst_no": "R002", "business_name": "신규 값"},
            ],
        )
        rows = self.db.scalars(
            select(PreSpecificationModel).order_by(
                PreSpecificationModel.bf_spec_rgst_no
            )
        ).all()
        service = FakePreSpecificationSheetService()
        writer = PreSpecificationSheetWriter("sheet-id", service)

        result = writer.upsert(build_sheet_rows(rows))

        self.assertEqual(result.inserted_count, 1)
        self.assertEqual(result.updated_count, 1)
        self.assertEqual(
            [item["range"] for item in service.write_data],
            ["'사전규격'!A2:L2", "'사전규격'!A3:L3"],
        )

    def test_sheet_writer_creates_dedicated_tab_when_missing(self):
        service = FakePreSpecificationSheetService(tab_exists=False)
        writer = PreSpecificationSheetWriter("sheet-id", service)

        writer.upsert([])

        self.assertEqual(service.added_tabs, ["사전규격"])

    def test_sheet_writer_uses_saved_destination_tab_name(self):
        service = FakePreSpecificationSheetService(tab_exists=False)
        writer = PreSpecificationSheetWriter("sheet-id", service, "내 사전규격")

        writer.upsert([])

        self.assertEqual(service.added_tabs, ["내 사전규격"])


if __name__ == "__main__":
    unittest.main()
