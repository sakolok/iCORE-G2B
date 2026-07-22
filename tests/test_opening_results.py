import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock, patch

import requests
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session

from app.data.models import (
    OrganizationMemberModel,
    OrganizationModel,
    OrganizationResultProfileModel,
    ScraperConfigModel,
    ScraperNoticeModel,
    UserResultProfileModel,
    UserModel,
)
from app.data.models import Base
from app.g2b.opening_results.client import (
    OpeningResultApiClient,
    OpeningResultApiConfig,
    OpeningResultApiError,
)
from app.g2b.opening_results.models import (
    BidNoticeEnrichmentJobModel,
    BidOpeningCollectionRunModel,
    BidOpeningCollectionLeaseModel,
    BidOpeningEntryModel,
    BidOpeningRoundModel,
    BidResultSnapshotModel,
    OrganizationOpeningResultMatchModel,
    SheetDestinationModel,
    SheetExportModel,
    UserOpeningResultMatchModel,
    UserOpeningResultStateModel,
)
from app.g2b.opening_results.schemas import (
    BidNoticeSheetContext,
    BusinessType,
    CollectOpeningResultsRequest,
    ExportOpeningResultsSheetRequest,
    OpeningResultListQuery,
    OpeningResultProfileUpdateRequest,
    OpeningStatus,
    ScheduledCollectOpeningResultsResponse,
    SheetDestinationUpsertRequest,
    SheetDestinationVerifyRequest,
)
from app.g2b.opening_results.router import (
    collect_results,
    collect_results_on_schedule,
    export_results_sheet,
    fetch_archived_result_detail,
    fetch_archived_results,
    fetch_sheet_destinations,
    fetch_result_detail,
    fetch_result_settings,
    fetch_results,
    restore_result_to_inbox,
    save_result_profile,
    upsert_sheet_destination,
    verify_sheet_destination,
)
from app.g2b.opening_results.matching import (
    ResultAccessError,
    SheetDestinationAccessError,
    SheetExportConflictError,
    SheetDestinationConflictError,
    claim_sheet_exports,
    complete_sheet_exports,
    deactivate_sheet_destination,
    dismiss_result,
    ensure_sheet_target_access,
    fail_sheet_exports,
    list_archived_results,
    normalize_spreadsheet_id,
    save_sheet_destination,
    sync_user_matches,
    update_user_result_profile,
)
from app.g2b.opening_results.sheet_export import (
    GoogleSheetWriter,
    LEGACY_SHEET_HEADERS,
    SHEET_HEADERS,
    SheetExportConfigurationError,
    SheetUpsertResult,
    _sheet_score_breakdown,
    build_sheet_rows,
    get_sheet_service_account_email,
)
from app.g2b.opening_results.service import (
    build_round_external_key,
    build_scheduled_collection_window,
    collect_opening_results,
    get_opening_result,
    list_opening_results,
    normalize_status,
    OpeningResultCollectionLeaseLostError,
    OpeningResultCollectionConflictError,
    run_scheduled_opening_results,
)


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


class FakeSheetRequest:
    def __init__(self, calls, operation, arguments, response=None):
        self.calls = calls
        self.operation = operation
        self.arguments = arguments
        self.response = response or {}

    def execute(self):
        self.calls.append((self.operation, self.arguments))
        return self.response


class FakeSheetValues:
    def __init__(self, service):
        self.service = service

    def get(self, **arguments):
        response = (
            {"values": [self.service.header]}
            if arguments["range"].endswith("A1:Q1") and self.service.header
            else {"values": self.service.existing_rows}
        )
        return FakeSheetRequest(self.service.calls, "get", arguments, response)

    def update(self, **arguments):
        return FakeSheetRequest(self.service.calls, "update", arguments)

    def batchUpdate(self, **arguments):
        return FakeSheetRequest(self.service.calls, "batchUpdate", arguments)

class FakeSheetService:
    def __init__(
        self,
        *,
        header=None,
        existing_rows=None,
        spreadsheet_title="개찰결과 테스트",
        tab_names=None,
    ):
        self.calls = []
        self.header = SHEET_HEADERS if header is None else header
        self.existing_rows = existing_rows or []
        self.spreadsheet_title = spreadsheet_title
        self.tab_names = ["개찰결과"] if tab_names is None else tab_names
        self.tab_ids = {
            tab_name: index
            for index, tab_name in enumerate(self.tab_names, start=1)
        }
        self.values_api = FakeSheetValues(self)

    def spreadsheets(self):
        return self

    def values(self):
        return self.values_api

    def get(self, **arguments):
        response = {
            "properties": {"title": self.spreadsheet_title},
            "sheets": [
                {
                    "properties": {
                        "sheetId": self.tab_ids[tab_name],
                        "title": tab_name,
                    }
                }
                for tab_name in self.tab_names
            ],
        }
        return FakeSheetRequest(self.calls, "getSpreadsheet", arguments, response)

    def batchUpdate(self, **arguments):
        return FakeSheetRequest(
            self.calls,
            "formatBatchUpdate",
            arguments,
        )


def api_payload(items, total_count):
    return {
        "response": {
            "header": {"resultCode": "00", "resultMsg": "OK"},
            "body": {
                "items": {"item": items},
                "pageNo": "1",
                "numOfRows": "2",
                "totalCount": str(total_count),
            },
        }
    }


class OpeningResultClientTests(unittest.TestCase):
    def test_fetches_every_page(self):
        session = FakeSession(
            [
                api_payload([{"bidNtceNo": "A"}, {"bidNtceNo": "B"}], 3),
                api_payload({"bidNtceNo": "C"}, 3),
            ]
        )
        client = OpeningResultApiClient(
            OpeningResultApiConfig(
                base_url="https://example.test",
                service_key="key",
                page_size=2,
            ),
            session=session,
        )

        rows = client.search_rounds(
            BusinessType.SERVICE,
            datetime(2026, 7, 14, tzinfo=timezone.utc),
            datetime(2026, 7, 15, tzinfo=timezone.utc),
        )

        self.assertEqual([row["bidNtceNo"] for row in rows], ["A", "B", "C"])
        self.assertEqual([call["params"]["pageNo"] for call in session.calls], [1, 2])
        self.assertTrue(session.calls[0]["url"].endswith("/getOpengResultListInfoServc"))
        self.assertEqual(session.calls[0]["params"]["inqryDiv"], "1")

    def test_repeated_page_is_rejected(self):
        repeated = api_payload([{"bidNtceNo": "A"}], 3)
        client = OpeningResultApiClient(
            OpeningResultApiConfig(
                base_url="https://example.test",
                service_key="key",
                page_size=1,
            ),
            session=FakeSession([repeated, repeated]),
        )

        with self.assertRaises(OpeningResultApiError):
            client.search_rounds(
                BusinessType.SERVICE,
                datetime(2026, 7, 14, tzinfo=timezone.utc),
                datetime(2026, 7, 15, tzinfo=timezone.utc),
            )

    def test_network_error_is_exposed_as_opening_result_error(self):
        client = OpeningResultApiClient(
            OpeningResultApiConfig(
                base_url="https://example.test",
                service_key="key",
            ),
            session=FailingSession(),
        )

        with self.assertRaises(OpeningResultApiError):
            client.search_rounds(
                BusinessType.SERVICE,
                datetime(2026, 7, 14, tzinfo=timezone.utc),
                datetime(2026, 7, 15, tzinfo=timezone.utc),
            )

    def test_malformed_success_envelope_is_rejected(self):
        client = OpeningResultApiClient(
            OpeningResultApiConfig(
                base_url="https://example.test",
                service_key="key",
            ),
            session=FakeSession([{"error": "quota exceeded"}]),
        )

        with self.assertRaises(OpeningResultApiError):
            client.search_rounds(
                BusinessType.SERVICE,
                datetime(2026, 7, 14, tzinfo=timezone.utc),
                datetime(2026, 7, 15, tzinfo=timezone.utc),
            )


class StubOpeningResultClient:
    def __init__(self, summaries, winners=None, entries=None):
        self.summaries = summaries
        self.winners = winners or []
        self.entries = entries or {}
        self.search_round_call_count = 0
        self.fetch_entry_call_count = 0

    def search_rounds(self, business_type, start_at, end_at):
        self.search_round_call_count += 1
        return list(self.summaries)

    def search_winners(self, business_type, start_at, end_at):
        return list(self.winners)

    def fetch_entries(self, summary):
        self.fetch_entry_call_count += 1
        key = (
            summary.get("bidNtceNo"),
            summary.get("bidNtceOrd"),
            summary.get("rbidNo"),
        )
        return list(self.entries.get(key, []))


class OpeningResultServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.db.add(
            ScraperConfigModel(
                enabled=True,
                notify_times="09:00:00",
                gsheet_ids="",
                receiver_emails="admin@icore.local",
                keywords="AI,클라우드,연수",
                excluded_keywords="연수구,연수원",
            )
        )
        self.organization = OrganizationModel(name="테스트 조직", slug="test-org")
        self.user = UserModel(
            username="tester",
            password_salt="salt",
            password_hash="hash",
            role="admin",
            is_active=True,
        )
        self.db.add_all([self.organization, self.user])
        self.db.flush()
        self.db.add_all(
            [
                OrganizationMemberModel(
                    organization_id=self.organization.id,
                    user_id=self.user.id,
                    role="admin",
                    is_active=True,
                ),
                OrganizationResultProfileModel(
                    organization_id=self.organization.id,
                    enabled=True,
                    keywords="AI,클라우드,연수,선택",
                    excluded_keywords="연수구,연수원",
                ),
                UserResultProfileModel(
                    organization_id=self.organization.id,
                    user_id=self.user.id,
                    enabled=True,
                    keywords="AI,클라우드,연수,선택",
                    excluded_keywords="연수구,연수원",
                ),
            ]
        )
        self.destination = SheetDestinationModel(
            organization_id=self.organization.id,
            owner_user_id=None,
            label="테스트 공용 Sheet",
            spreadsheet_id="sheet-id",
            tab_name="개찰결과",
            is_default=True,
            is_active=True,
        )
        self.db.add(self.destination)
        self.db.commit()
        self.auth = {
            "user_id": self.user.id,
            "username": self.user.username,
            "role": self.user.role,
            "organization_id": self.organization.id,
            "organization_name": self.organization.name,
            "organization_role": "admin",
        }
        self.request = CollectOpeningResultsRequest(
            start_at=datetime(2026, 7, 14, tzinfo=timezone.utc),
            end_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
            business_type=BusinessType.SERVICE,
        )

    def add_bid_notice(
        self,
        bid_notice_no="R26BK00000001",
        bid_notice_ord="00",
        *,
        business_name="AI 교육 운영 용역",
        demand_agency_name="OO대학교",
        base_amount=Decimal("165000000"),
        prearranged_price_decision_method="복수예가",
        proposal_deadline=datetime(2026, 7, 20, 15, 0),
        region_restriction="서울특별시",
        region_restriction_api_status="API_VALUE",
        is_two_stage_bid=True,
        notice_url="https://www.g2b.go.kr/notice/detail",
        dedup_suffix="default",
    ):
        now = datetime.now(timezone.utc)
        row = ScraperNoticeModel(
            dedup_key=f"{bid_notice_no}|{bid_notice_ord}|{dedup_suffix}",
            notice_id=bid_notice_no,
            title=business_name or "입찰공고",
            bid_notice_no=bid_notice_no,
            bid_notice_ord=bid_notice_ord,
            business_name=business_name,
            demand_agency_name=demand_agency_name,
            base_amount=base_amount,
            prearranged_price_decision_method=prearranged_price_decision_method,
            proposal_deadline=proposal_deadline,
            region_restriction=region_restriction,
            region_restriction_api_status=region_restriction_api_status,
            is_two_stage_bid=is_two_stage_bid,
            notice_url=notice_url,
            first_seen_at=now,
            last_seen_at=now,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def completed_summary(rebid_no="0"):
        return {
            "bidNtceNo": "R26BK00000001",
            "bidNtceOrd": "00",
            "bidClsfcNo": "0",
            "rbidNo": rebid_no,
            "bidNtceNm": "AI 교육 운영 용역",
            "opengDt": "202607151100",
            "prtcptCnum": "2",
            "progrsDivCdNm": "개찰완료",
            "ntceInsttNm": "조달청",
            "dminsttNm": "OO대학교",
        }

    @staticmethod
    def winner(rebid_no="0"):
        return {
            "bidNtceNo": "R26BK00000001",
            "bidNtceOrd": "00",
            "bidClsfcNo": "0",
            "rbidNo": rebid_no,
            "bidwinnrBizno": "1111111111",
            "bidwinnrNm": "일등기업",
            "sucsfbidAmt": "145,000,000",
            "sucsfbidRate": "87.8788",
            "fnlSucsfDate": "20260716",
        }

    @staticmethod
    def entries(rebid_no="0"):
        return [
            {
                "bidNtceNo": "R26BK00000001",
                "bidNtceOrd": "00",
                "bidClsfcNo": "0",
                "rbidNo": rebid_no,
                "opengRank": "1",
                "prcbdrBizno": "1111111111",
                "prcbdrNm": "일등기업",
                "bidprcAmt": "145,000,000",
                "bidprcrt": "87.8788%",
                "bidprcDt": "20260715103000",
                "bidPrceEvlVal": "19.5",
                "techEvlVal": "75.0",
                "totalEvlAmtVal": "99.9",
            },
            {
                "bidNtceNo": "R26BK00000001",
                "bidNtceOrd": "00",
                "bidClsfcNo": "0",
                "rbidNo": rebid_no,
                "opengRank": "2",
                "prcbdrBizno": "2222222222",
                "prcbdrNm": "이등기업",
                "bidprcAmt": "146000000",
                "bidprcrt": "88.4848",
                "bidprcDt": "20260715103100",
            },
        ]

    def make_client(self):
        summary = self.completed_summary()
        return StubOpeningResultClient(
            [summary],
            winners=[self.winner()],
            entries={
                (summary["bidNtceNo"], summary["bidNtceOrd"], summary["rbidNo"]): self.entries()
            },
        )

    def preview_sheet_export(self, result_ids, destination_id=None):
        return export_results_sheet(
            ExportOpeningResultsSheetRequest(
                result_ids=result_ids,
                destination_id=destination_id,
                dry_run=True,
            ),
            auth=self.auth,
            db=self.db,
        )

    def add_user_result_profile(
        self,
        user_id,
        *,
        organization_id=None,
        keywords="AI,클라우드,연수,선택",
        excluded_keywords="연수구,연수원",
        enabled=True,
    ):
        profile = UserResultProfileModel(
            organization_id=organization_id or self.organization.id,
            user_id=user_id,
            enabled=enabled,
            keywords=keywords,
            excluded_keywords=excluded_keywords,
        )
        self.db.add(profile)
        return profile

    def test_same_payload_is_idempotent(self):
        first = collect_opening_results(self.db, self.request, self.make_client())
        second = collect_opening_results(self.db, self.request, self.make_client())

        self.assertEqual(first.inserted_round_count, 1)
        self.assertEqual(first.inserted_entry_count, 2)
        self.assertEqual(second.inserted_round_count, 0)
        self.assertEqual(second.inserted_entry_count, 0)
        self.assertEqual(
            self.db.scalar(select(func.count(BidOpeningRoundModel.id))), 1
        )
        self.assertEqual(
            self.db.scalar(select(func.count(BidOpeningEntryModel.id))), 2
        )
        self.assertEqual(
            self.db.scalar(select(func.count(BidResultSnapshotModel.id))), 4
        )

    def test_global_collection_collects_all_shared_details_and_matches_user_afterward(self):
        allowed = self.completed_summary()
        allowed["bidNtceNm"] = "교원 직무연수 운영"
        excluded_region = self.completed_summary()
        excluded_region["bidNtceNo"] = "R26BK00000002"
        excluded_region["bidNtceNm"] = "인천 연수구 청사 보수"
        excluded_facility = self.completed_summary()
        excluded_facility["bidNtceNo"] = "R26BK00000003"
        excluded_facility["bidNtceNm"] = "AI 기반 연수원 시설 개선"
        allowed_entries = self.entries()
        excluded_region_entries = self.entries()
        excluded_facility_entries = self.entries()
        client = StubOpeningResultClient(
            [allowed, excluded_region, excluded_facility],
            entries={
                (
                    allowed["bidNtceNo"],
                    allowed["bidNtceOrd"],
                    allowed["rbidNo"],
                ): allowed_entries,
                (
                    excluded_region["bidNtceNo"],
                    excluded_region["bidNtceOrd"],
                    excluded_region["rbidNo"],
                ): excluded_region_entries,
                (
                    excluded_facility["bidNtceNo"],
                    excluded_facility["bidNtceOrd"],
                    excluded_facility["rbidNo"],
                ): excluded_facility_entries,
            },
        )

        result = collect_opening_results(
            self.db,
            self.request,
            client,
        )

        stored = self.db.scalars(select(BidOpeningRoundModel)).all()
        matches = self.db.scalars(select(UserOpeningResultMatchModel)).all()
        self.assertEqual(
            {row.bid_notice_no for row in stored},
            {
                allowed["bidNtceNo"],
                excluded_region["bidNtceNo"],
                excluded_facility["bidNtceNo"],
            },
        )
        self.assertEqual(result.fetched_round_count, 3)
        self.assertEqual(result.skipped_count, 0)
        self.assertEqual(client.fetch_entry_call_count, 3)
        self.assertEqual(
            self.db.scalar(select(func.count(BidOpeningEntryModel.id))),
            6,
        )
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].matched_keywords, "연수")

    def test_default_collection_enqueues_matched_missing_business_amount(self):
        matched = self.completed_summary()
        unmatched = self.completed_summary()
        unmatched["bidNtceNo"] = "R26BK00000002"
        unmatched["bidNtceNm"] = "로봇 자동화 용역"
        client = StubOpeningResultClient([matched, unmatched])

        with (
            patch(
                "app.g2b.opening_results.service.OpeningResultApiConfig.from_env",
                return_value=OpeningResultApiConfig(
                    base_url="https://example.test",
                    service_key="key",
                ),
            ),
            patch(
                "app.g2b.opening_results.service.OpeningResultApiClient",
                return_value=client,
            ),
        ):
            collect_opening_results(self.db, self.request)

        jobs = self.db.scalars(select(BidNoticeEnrichmentJobModel)).all()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].bid_notice_no, matched["bidNtceNo"])
        self.assertEqual(jobs[0].priority, 100)

    def test_same_shared_source_is_matched_independently_per_user_profile(self):
        teammate = UserModel(
            username="keyword-profile-teammate",
            password_salt="salt",
            password_hash="hash",
            role="viewer",
            is_active=True,
        )
        self.db.add(teammate)
        self.db.flush()
        self.db.add_all(
            [
                OrganizationMemberModel(
                    organization_id=self.organization.id,
                    user_id=teammate.id,
                    role="member",
                    is_active=True,
                ),
                UserResultProfileModel(
                    organization_id=self.organization.id,
                    user_id=teammate.id,
                    enabled=True,
                    keywords="클라우드",
                    excluded_keywords="",
                ),
            ]
        )
        my_profile = self.db.scalar(
            select(UserResultProfileModel).where(
                UserResultProfileModel.user_id == self.user.id
            )
        )
        my_profile.keywords = "AI"
        my_profile.excluded_keywords = ""
        self.db.commit()

        ai_summary = self.completed_summary()
        cloud_summary = self.completed_summary()
        cloud_summary["bidNtceNo"] = "R26BK00000002"
        cloud_summary["bidNtceNm"] = "클라우드 전환 컨설팅"
        client = StubOpeningResultClient([ai_summary, cloud_summary])
        collect_opening_results(self.db, self.request, client)
        query = OpeningResultListQuery(
            opened_from=datetime(2026, 7, 14, tzinfo=timezone.utc),
            opened_to=datetime(2026, 7, 16, tzinfo=timezone.utc),
        )

        my_rows, my_total = list_opening_results(
            self.db,
            query,
            organization_id=self.organization.id,
            user_id=self.user.id,
        )
        teammate_rows, teammate_total = list_opening_results(
            self.db,
            query,
            organization_id=self.organization.id,
            user_id=teammate.id,
        )

        self.assertEqual(my_total, 1)
        self.assertEqual(my_rows[0].bid_notice_no, "R26BK00000001")
        self.assertEqual(teammate_total, 1)
        self.assertEqual(teammate_rows[0].bid_notice_no, "R26BK00000002")
        self.assertEqual(
            self.db.scalar(select(func.count(UserOpeningResultMatchModel.id))),
            2,
        )

    def test_collection_fetches_shared_detail_once_for_multiple_user_matches(self):
        teammate = UserModel(
            username="same-result-teammate",
            password_salt="salt",
            password_hash="hash",
            role="viewer",
            is_active=True,
        )
        self.db.add(teammate)
        self.db.flush()
        self.db.add(
            OrganizationMemberModel(
                organization_id=self.organization.id,
                user_id=teammate.id,
                role="member",
                is_active=True,
            )
        )
        self.add_user_result_profile(teammate.id, keywords="AI")
        self.db.commit()
        client = self.make_client()

        collect_opening_results(self.db, self.request, client)

        self.assertEqual(client.fetch_entry_call_count, 1)
        self.assertEqual(
            self.db.scalar(select(func.count(BidOpeningRoundModel.id))),
            1,
        )
        self.assertEqual(
            self.db.scalar(select(func.count(BidOpeningEntryModel.id))),
            2,
        )
        self.assertEqual(
            self.db.scalar(select(func.count(UserOpeningResultMatchModel.id))),
            2,
        )

    def test_member_updates_only_own_profile_and_rematches_db_without_api_call(self):
        teammate = UserModel(
            username="profile-update-member",
            password_salt="salt",
            password_hash="hash",
            role="viewer",
            is_active=True,
        )
        self.db.add(teammate)
        self.db.flush()
        self.db.add(
            OrganizationMemberModel(
                organization_id=self.organization.id,
                user_id=teammate.id,
                role="member",
                is_active=True,
            )
        )
        self.add_user_result_profile(teammate.id, keywords="로봇")
        self.db.commit()
        collect_opening_results(self.db, self.request, self.make_client())
        query = OpeningResultListQuery(
            opened_from=datetime(2026, 7, 14, tzinfo=timezone.utc),
            opened_to=datetime(2026, 7, 16, tzinfo=timezone.utc),
        )
        _, before_total = list_opening_results(
            self.db,
            query,
            organization_id=self.organization.id,
            user_id=teammate.id,
        )
        member_auth = {
            "user_id": teammate.id,
            "username": teammate.username,
            "role": teammate.role,
            "organization_id": self.organization.id,
            "organization_name": self.organization.name,
            "organization_role": "member",
        }

        with patch(
            "app.g2b.opening_results.service.OpeningResultApiClient"
        ) as client_factory:
            response = save_result_profile(
                OpeningResultProfileUpdateRequest(
                    enabled=True,
                    keywords=["AI"],
                    excluded_keywords=[],
                ),
                auth=member_auth,
                db=self.db,
            )

        _, after_total = list_opening_results(
            self.db,
            query,
            organization_id=self.organization.id,
            user_id=teammate.id,
        )
        owner_profile = self.db.scalar(
            select(UserResultProfileModel).where(
                UserResultProfileModel.user_id == self.user.id
            )
        )
        client_factory.assert_not_called()
        self.assertEqual(before_total, 0)
        self.assertEqual(after_total, 1)
        self.assertEqual(response.keywords, ["AI"])
        self.assertEqual(owner_profile.keywords, "AI,클라우드,연수,선택")

    def test_new_user_profile_defaults_to_disabled_and_empty(self):
        teammate = UserModel(
            username="new-profile-member",
            password_salt="salt",
            password_hash="hash",
            role="viewer",
            is_active=True,
        )
        self.db.add(teammate)
        self.db.flush()
        self.db.add(
            OrganizationMemberModel(
                organization_id=self.organization.id,
                user_id=teammate.id,
                role="member",
                is_active=True,
            )
        )
        self.db.commit()
        member_auth = {
            "user_id": teammate.id,
            "username": teammate.username,
            "role": teammate.role,
            "organization_id": self.organization.id,
            "organization_name": self.organization.name,
            "organization_role": "member",
        }

        settings = fetch_result_settings(auth=member_auth, db=self.db)

        self.assertFalse(settings.profile.enabled)
        self.assertEqual(settings.profile.keywords, [])
        self.assertEqual(settings.profile.excluded_keywords, [])

    def test_later_user_match_preserves_already_collected_shared_detail(self):
        collect_opening_results(self.db, self.request, self.make_client())
        round_row = self.db.scalar(select(BidOpeningRoundModel))
        collected_at = round_row.entries_collected_at
        teammate = UserModel(
            username="late-profile-member",
            password_salt="salt",
            password_hash="hash",
            role="viewer",
            is_active=True,
        )
        self.db.add(teammate)
        self.db.flush()
        self.db.add(
            OrganizationMemberModel(
                organization_id=self.organization.id,
                user_id=teammate.id,
                role="member",
                is_active=True,
            )
        )
        self.add_user_result_profile(teammate.id, keywords="AI")
        self.db.commit()

        sync_user_matches(
            self.db,
            organization_id=self.organization.id,
            user_id=teammate.id,
        )
        self.db.commit()
        self.db.refresh(round_row)

        self.assertIsNotNone(collected_at)
        self.assertEqual(round_row.entries_collected_at, collected_at)
        self.assertEqual(
            self.db.scalar(select(func.count(BidOpeningEntryModel.id))),
            2,
        )

    def test_unmatched_shared_round_collects_detail_before_later_profile_match(self):
        profile = self.db.scalar(
            select(UserResultProfileModel).where(
                UserResultProfileModel.user_id == self.user.id
            )
        )
        profile.keywords = "로봇"
        source = self.completed_summary()
        external_key = build_round_external_key(source, BusinessType.SERVICE.value)
        round_row = BidOpeningRoundModel(
            external_key=external_key,
            business_type=BusinessType.SERVICE.value,
            bid_notice_no=source["bidNtceNo"],
            bid_notice_ord=source["bidNtceOrd"],
            bid_class_no=source["bidClsfcNo"],
            rebid_no=source["rbidNo"],
            title=source["bidNtceNm"],
            status=OpeningStatus.OPENED.value,
            opened_at=datetime(2026, 7, 15, 11, 0, tzinfo=timezone.utc),
            collected_at=datetime(2026, 7, 15, 11, 5, tzinfo=timezone.utc),
        )
        self.db.add(round_row)
        self.db.add(
            BidResultSnapshotModel(
                entity_type="ROUND",
                entity_key=external_key,
                payload_hash="existing-round-snapshot",
                raw_payload=json.dumps(source),
            )
        )
        self.db.commit()

        detail_client = StubOpeningResultClient(
            [],
            entries={
                (source["bidNtceNo"], source["bidNtceOrd"], source["rbidNo"]): self.entries()
            },
        )
        collect_opening_results(self.db, self.request, detail_client)

        self.assertEqual(detail_client.fetch_entry_call_count, 1)
        self.db.refresh(round_row)
        self.assertIsNotNone(round_row.entries_collected_at)
        self.assertEqual(
            self.db.scalar(select(func.count(BidOpeningEntryModel.id))),
            2,
        )
        update_user_result_profile(
            self.db,
            organization_id=self.organization.id,
            user_id=self.user.id,
            enabled=True,
            keywords=["AI"],
            excluded_keywords=[],
        )
        sync_user_matches(
            self.db,
            organization_id=self.organization.id,
            user_id=self.user.id,
        )
        self.db.commit()

        self.db.refresh(round_row)
        self.assertIsNotNone(round_row.entries_collected_at)
        self.assertEqual(
            self.db.scalar(select(func.count(BidOpeningEntryModel.id))),
            2,
        )

    def test_empty_delayed_entry_response_stays_pending_and_preserves_existing_rows(self):
        collect_opening_results(self.db, self.request, self.make_client())
        round_row = self.db.scalar(select(BidOpeningRoundModel))
        self.assertEqual(
            self.db.scalar(select(func.count(BidOpeningEntryModel.id))),
            2,
        )

        delayed_client = StubOpeningResultClient([self.completed_summary()])
        collect_opening_results(self.db, self.request, delayed_client)

        self.db.refresh(round_row)
        self.assertIsNone(round_row.entries_collected_at)
        self.assertEqual(
            self.db.scalar(select(func.count(BidOpeningEntryModel.id))),
            2,
        )

    def test_winner_and_rankings_are_organized(self):
        collect_opening_results(self.db, self.request, self.make_client())
        result = self.db.scalar(select(BidOpeningRoundModel))
        self.assertIsNotNone(result)
        self.assertEqual(result.status, OpeningStatus.AWARDED.value)
        self.assertEqual(result.winner_company_name, "일등기업")
        self.assertEqual(result.winning_amount, Decimal("145000000.00"))

        detail = get_opening_result(self.db, result.id)
        self.assertIsNotNone(detail)
        _, entries = detail
        self.assertEqual([entry.rank for entry in entries], [1, 2])
        self.assertTrue(entries[0].is_winner)
        self.assertEqual(entries[0].total_score, Decimal("94.500000"))
        self.assertEqual(entries[0].official_total_score, Decimal("99.900000"))
        self.assertFalse(entries[1].is_winner)

    def test_google_sheet_row_uses_bid_context_and_top_five_scores(self):
        collect_opening_results(self.db, self.request, self.make_client())
        result_id = self.db.scalar(select(BidOpeningRoundModel.id))
        notice = self.add_bid_notice(bid_notice_ord="000")
        notice.estimated_price = "999"
        notice.deadline_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        self.db.flush()
        rows, missing_context_keys, missing_result_ids = build_sheet_rows(
            self.db,
            [result_id],
        )

        self.assertEqual(
            SHEET_HEADERS,
            [
                "공고번호",
                "사업명",
                "발주처",
                "사업금액",
                "제안마감",
                "지역제한여부",
                "2단계 입찰(여부)",
                "1위(이름)",
                "1위 총점(점수)",
                "2위(이름)",
                "2위 총점(점수)",
                "3위(이름)",
                "3위 총점(점수)",
                "4위(이름)",
                "4위 총점(점수)",
                "5위(이름)",
                "5위 총점(점수)",
            ],
        )
        self.assertEqual(missing_context_keys, [])
        self.assertEqual(missing_result_ids, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0][:9],
            [
                "R26BK00000001",
                "AI 교육 운영 용역",
                "OO대학교",
                165000000,
                "2026-07-20 15:00",
                "서울특별시",
                "Y",
                "일등기업",
                "19.5+75=94.50",
            ],
        )
        self.assertEqual(rows[0][9], "이등기업")
        self.assertEqual(rows[0][10], "")
        self.assertEqual(rows[0][11:], ["", "", "", "", "", ""])

    def test_sheet_score_breakdown_contract(self):
        cases = [
            (Decimal("10"), Decimal("85.5"), "10+85.5=95.50"),
            (Decimal("7.5585"), Decimal("76.5"), "7.5585+76.5=84.06"),
            (None, Decimal("85.5"), ""),
            (Decimal("10"), None, ""),
        ]
        for price_score, technical_score, expected in cases:
            with self.subTest(
                price_score=price_score,
                technical_score=technical_score,
            ):
                entry = BidOpeningEntryModel(
                    bid_price_score=price_score,
                    technical_score=technical_score,
                    total_score=Decimal("999"),
                )
                self.assertEqual(_sheet_score_breakdown(entry), expected)

    def test_round_external_key_uses_shared_notice_order_normalization(self):
        base = {
            "bidNtceNo": "R26BK00000001",
            "bidClsfcNo": "0",
            "rbidNo": "0",
        }
        padded = build_round_external_key(
            {**base, "bidNtceOrd": "000"},
            BusinessType.SERVICE.value,
        )
        defaulted = build_round_external_key(
            {**base, "bidNtceOrd": "00"},
            BusinessType.SERVICE.value,
        )
        blank = build_round_external_key(
            {**base, "bidNtceOrd": ""},
            BusinessType.SERVICE.value,
        )

        self.assertEqual(padded, defaulted)
        self.assertEqual(defaulted, blank)
        self.assertEqual(defaulted, "SERVICE|R26BK00000001|0|0|0")

    def test_blank_notice_order_matches_default_order(self):
        collect_opening_results(self.db, self.request, self.make_client())
        result_id = self.db.scalar(select(BidOpeningRoundModel.id))
        self.add_bid_notice(bid_notice_ord="   ")

        rows, missing_context_keys, missing_result_ids = build_sheet_rows(
            self.db,
            [result_id],
        )

        self.assertEqual(missing_result_ids, [])
        self.assertEqual(missing_context_keys, [])
        self.assertEqual(rows[0][1], "AI 교육 운영 용역")

    def test_google_sheet_builds_only_user_selected_results(self):
        first = self.completed_summary()
        second = self.completed_summary()
        second["bidNtceNo"] = "R26BK00000002"
        second["bidNtceNm"] = "선택 대상 사업"
        first_entries = self.entries()
        second_entries = self.entries()
        for entry in second_entries:
            entry["bidNtceNo"] = second["bidNtceNo"]
        collect_opening_results(
            self.db,
            self.request,
            StubOpeningResultClient(
                [first, second],
                entries={
                    (first["bidNtceNo"], first["bidNtceOrd"], first["rbidNo"]): first_entries,
                    (
                        second["bidNtceNo"],
                        second["bidNtceOrd"],
                        second["rbidNo"],
                    ): second_entries,
                },
            ),
        )
        selected_id = self.db.scalar(
            select(BidOpeningRoundModel.id).where(
                BidOpeningRoundModel.bid_notice_no == second["bidNtceNo"]
            )
        )
        self.add_bid_notice(
            bid_notice_no=first["bidNtceNo"],
            bid_notice_ord=first["bidNtceOrd"],
            base_amount=None,
            dedup_suffix="unselected-incomplete",
        )
        self.add_bid_notice(
            bid_notice_no=second["bidNtceNo"],
            bid_notice_ord=second["bidNtceOrd"],
            business_name="선택 대상 공식 사업명",
            is_two_stage_bid=False,
            dedup_suffix="selected",
        )
        rows, missing_context_keys, missing_result_ids = build_sheet_rows(
            self.db,
            [selected_id],
        )

        self.assertEqual([row[0] for row in rows], [second["bidNtceNo"]])
        self.assertEqual(missing_context_keys, [])
        self.assertEqual(missing_result_ids, [])

        writer = Mock()
        writer.upsert.return_value = SheetUpsertResult(inserted_count=1, updated_count=0)
        preview = self.preview_sheet_export([selected_id])
        with patch(
            "app.g2b.opening_results.router.GoogleSheetWriter.from_env",
            return_value=writer,
        ):
            response = export_results_sheet(
                ExportOpeningResultsSheetRequest(
                    result_ids=[selected_id],
                    dry_run=False,
                    expected_preview_token=preview.preview_token,
                ),
                auth=self.auth,
                db=self.db,
            )

        writer.upsert.assert_called_once()
        written_rows = writer.upsert.call_args.args[0]
        self.assertEqual([row[0] for row in written_rows], [second["bidNtceNo"]])
        self.assertTrue(response.written)

    def test_failed_rebid_and_completed_rounds_are_preserved(self):
        failed = self.completed_summary(rebid_no="0")
        failed["progrsDivCdNm"] = "유찰"
        rebid = self.completed_summary(rebid_no="1")
        rebid["progrsDivCdNm"] = "재입찰"
        completed = self.completed_summary(rebid_no="2")
        client = StubOpeningResultClient([failed, rebid, completed])

        collect_opening_results(self.db, self.request, client)
        rows = self.db.execute(
            select(BidOpeningRoundModel).order_by(BidOpeningRoundModel.rebid_no)
        ).scalars().all()

        self.assertEqual(len(rows), 3)
        self.assertEqual(
            [row.status for row in rows],
            [OpeningStatus.FAILED.value, OpeningStatus.REBID.value, OpeningStatus.OPENED.value],
        )

    def test_late_winner_updates_existing_opening_round(self):
        summary = self.completed_summary()
        entries = {
            (summary["bidNtceNo"], summary["bidNtceOrd"], summary["rbidNo"]): self.entries()
        }
        collect_opening_results(
            self.db,
            self.request,
            StubOpeningResultClient([summary], entries=entries),
        )

        response = collect_opening_results(
            self.db,
            self.request,
            StubOpeningResultClient([], winners=[self.winner()], entries=entries),
        )

        round_row = self.db.scalar(select(BidOpeningRoundModel))
        self.assertEqual(response.updated_round_count, 1)
        self.assertEqual(
            self.db.scalar(select(func.count(BidOpeningRoundModel.id))), 1
        )
        self.assertEqual(round_row.status, OpeningStatus.AWARDED.value)
        self.assertEqual(round_row.winner_company_name, "일등기업")
        detail = get_opening_result(self.db, round_row.id)
        self.assertTrue(detail[1][0].is_winner)

    def test_titleless_late_winner_keeps_existing_round_title(self):
        summary = self.completed_summary()
        summary["bidNtceNm"] = "교원 직무연수 운영"
        collect_opening_results(
            self.db,
            self.request,
            StubOpeningResultClient([summary]),
        )

        collect_opening_results(
            self.db,
            self.request,
            StubOpeningResultClient([], winners=[self.winner()]),
        )

        round_row = self.db.scalar(select(BidOpeningRoundModel))
        self.assertEqual(round_row.status, OpeningStatus.AWARDED.value)
        self.assertEqual(round_row.winner_company_name, "일등기업")

    def test_corrected_bid_datetime_does_not_duplicate_participant(self):
        collect_opening_results(self.db, self.request, self.make_client())
        summary = self.completed_summary()
        corrected_entries = self.entries()
        corrected_entries[0]["bidprcDt"] = "20260715104500"

        collect_opening_results(
            self.db,
            self.request,
            StubOpeningResultClient(
                [summary],
                winners=[self.winner()],
                entries={
                    (
                        summary["bidNtceNo"],
                        summary["bidNtceOrd"],
                        summary["rbidNo"],
                    ): corrected_entries
                },
            ),
        )

        self.assertEqual(
            self.db.scalar(select(func.count(BidOpeningEntryModel.id))), 2
        )
        first_entry = self.db.scalar(
            select(BidOpeningEntryModel).where(
                BidOpeningEntryModel.business_no == "1111111111"
            )
        )
        self.assertEqual(first_entry.bid_at.minute, 45)

    def test_participant_removed_from_source_is_removed_from_current_result(self):
        collect_opening_results(self.db, self.request, self.make_client())
        summary = self.completed_summary()

        collect_opening_results(
            self.db,
            self.request,
            StubOpeningResultClient(
                [summary],
                winners=[self.winner()],
                entries={
                    (
                        summary["bidNtceNo"],
                        summary["bidNtceOrd"],
                        summary["rbidNo"],
                    ): self.entries()[:1]
                },
            ),
        )

        self.assertEqual(
            self.db.scalar(select(func.count(BidOpeningEntryModel.id))), 1
        )

    def test_list_and_detail_queries(self):
        collect_opening_results(self.db, self.request, self.make_client())
        rows, total = list_opening_results(
            self.db,
            OpeningResultListQuery(
                q="AI 교육",
                opened_from=datetime(2026, 7, 14, tzinfo=timezone.utc),
                opened_to=datetime(2026, 7, 16, tzinfo=timezone.utc),
                page=1,
                page_size=30,
            ),
        )
        self.assertEqual(total, 1)
        self.assertEqual(rows[0].bid_notice_no, "R26BK00000001")

    def test_list_defaults_to_recent_fourteen_days(self):
        collect_opening_results(self.db, self.request, self.make_client())
        old_round = self.db.scalar(select(BidOpeningRoundModel))
        now = datetime.now(timezone.utc)
        old_round.opened_at = now - timedelta(days=15)
        self.db.add(
            BidOpeningRoundModel(
                external_key="SERVICE|RECENT|0|0|0",
                business_type="SERVICE",
                bid_notice_no="RECENT",
                status=OpeningStatus.OPENED.value,
                opened_at=now - timedelta(days=13),
            )
        )
        self.db.add(
            BidOpeningRoundModel(
                external_key="SERVICE|FUTURE|0|0|0",
                business_type="SERVICE",
                bid_notice_no="FUTURE",
                status=OpeningStatus.OPENED.value,
                opened_at=now + timedelta(days=1),
            )
        )
        self.db.commit()

        rows, total = list_opening_results(
            self.db,
            OpeningResultListQuery(page=1, page_size=30),
        )
        self.assertEqual([row.bid_notice_no for row in rows], ["RECENT"])
        self.assertEqual(total, 1)

        rows, total = list_opening_results(
            self.db,
            OpeningResultListQuery(
                opened_from=now - timedelta(days=16),
                opened_to=now - timedelta(days=14),
                page=1,
                page_size=30,
            ),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(total, 1)
        self.assertEqual(rows[0].bid_notice_no, old_round.bid_notice_no)

    def test_list_route_returns_paginated_response(self):
        collect_opening_results(self.db, self.request, self.make_client())
        round_row = self.db.scalar(select(BidOpeningRoundModel))
        round_row.winner_company_name = "최종낙찰기업"
        self.db.commit()
        self.add_bid_notice(
            business_name="공식 AI 교육 운영 용역",
            demand_agency_name="공식 수요기관",
            region_restriction="서울특별시",
            region_restriction_api_status="API_VALUE",
        )

        response = fetch_results(
            q="AI 교육",
            status=None,
            opened_from=datetime(2026, 7, 14, tzinfo=timezone.utc),
            opened_to=datetime(2026, 7, 16, tzinfo=timezone.utc),
            page=1,
            page_size=30,
            auth=self.auth,
            db=self.db,
        )

        self.assertEqual(response.total, 1)
        self.assertEqual(response.page, 1)
        self.assertEqual(response.items[0].bid_notice_no, "R26BK00000001")
        self.assertEqual(response.items[0].title, "AI 교육 운영 용역")
        self.assertEqual(response.items[0].business_name, "공식 AI 교육 운영 용역")
        self.assertEqual(response.items[0].demand_agency_name, "공식 수요기관")
        self.assertEqual(response.items[0].base_amount, Decimal("165000000"))
        self.assertEqual(response.items[0].region_restriction, "서울특별시")
        self.assertEqual(
            response.items[0].region_restriction_api_status,
            "API_VALUE",
        )
        self.assertTrue(response.items[0].is_two_stage_bid)
        self.assertEqual(response.items[0].first_rank_company_name, "일등기업")
        self.assertEqual(response.items[0].first_rank_bid_price_score, Decimal("19.5"))
        self.assertEqual(response.items[0].first_rank_technical_score, Decimal("75.0"))
        self.assertEqual(response.items[0].winner_company_name, "최종낙찰기업")
        self.assertEqual(response.items[0].sheet_export_status, "READY")
        self.assertTrue(response.items[0].sheet_exportable)
        self.assertEqual(response.items[0].sheet_block_reasons, [])
        self.assertEqual(
            response.items[0].notice_url,
            "https://www.g2b.go.kr/notice/detail",
        )
        self.assertIsNotNone(response.items[0].opened_at.tzinfo)
        self.assertIn("Z", response.model_dump_json())

        detail = fetch_result_detail(
            response.items[0].id,
            auth=self.auth,
            db=self.db,
        )
        self.assertEqual(detail.business_name, "공식 AI 교육 운영 용역")
        self.assertEqual(detail.first_rank_company_name, "일등기업")
        self.assertEqual(detail.winner_company_name, "최종낙찰기업")
        self.assertEqual(detail.sheet_export_status, "READY")
        self.assertEqual(detail.notice_url, "https://www.g2b.go.kr/notice/detail")

    def test_region_api_error_does_not_block_sheet_and_exports_blank_cell(self):
        collect_opening_results(self.db, self.request, self.make_client())
        result_id = self.db.scalar(select(BidOpeningRoundModel.id))
        self.add_bid_notice(
            region_restriction="서울특별시",
            region_restriction_api_status="API_ERROR",
        )
        self.db.commit()

        response = fetch_results(
            q=None,
            status=None,
            opened_from=datetime(2026, 7, 14, tzinfo=timezone.utc),
            opened_to=datetime(2026, 7, 16, tzinfo=timezone.utc),
            page=1,
            page_size=30,
            auth=self.auth,
            db=self.db,
        )
        rows, missing_context_keys, missing_result_ids = build_sheet_rows(
            self.db,
            [result_id],
        )

        self.assertEqual(response.items[0].sheet_export_status, "READY")
        self.assertTrue(response.items[0].sheet_exportable)
        self.assertEqual(response.items[0].sheet_block_reasons, [])
        self.assertEqual(missing_context_keys, [])
        self.assertEqual(missing_result_ids, [])
        self.assertEqual(rows[0][5], "")

    def test_list_route_marks_detail_pending_before_sheet_preview(self):
        collect_opening_results(self.db, self.request, self.make_client())
        round_row = self.db.scalar(select(BidOpeningRoundModel))
        round_row.entries_collected_at = None
        self.add_bid_notice()
        self.db.commit()

        response = fetch_results(
            q=None,
            status=None,
            opened_from=datetime(2026, 7, 14, tzinfo=timezone.utc),
            opened_to=datetime(2026, 7, 16, tzinfo=timezone.utc),
            page=1,
            page_size=30,
            auth=self.auth,
            db=self.db,
        )

        self.assertEqual(response.items[0].sheet_export_status, "DETAIL_PENDING")
        self.assertFalse(response.items[0].sheet_exportable)
        self.assertEqual(response.items[0].sheet_block_reasons, ["entries_collected_at"])

    def test_list_route_filters_sheet_readiness_before_pagination(self):
        collect_opening_results(self.db, self.request, self.make_client())
        ready_round = self.db.scalar(select(BidOpeningRoundModel))
        self.add_bid_notice()
        missing_round = BidOpeningRoundModel(
            external_key="SERVICE|R26BK-MISSING|0|0|0",
            business_type="SERVICE",
            bid_notice_no="R26BK-MISSING",
            bid_notice_ord="0",
            bid_class_no="0",
            rebid_no="0",
            title="AI 공고정보 누락 결과",
            status=OpeningStatus.AWARDED.value,
            opened_at=datetime(2026, 7, 15, 11, 0, tzinfo=timezone.utc),
            entries_collected_at=datetime(2026, 7, 15, 11, 5, tzinfo=timezone.utc),
        )
        self.db.add(missing_round)
        self.db.flush()
        self.db.add(
            UserOpeningResultMatchModel(
                organization_id=self.organization.id,
                user_id=self.user.id,
                round_id=missing_round.id,
                result_external_key=missing_round.external_key,
                matched_keywords="AI",
                is_current_match=True,
            )
        )
        self.db.commit()

        ready = fetch_results(
            q=None,
            status=None,
            opened_from=datetime(2026, 7, 14, tzinfo=timezone.utc),
            opened_to=datetime(2026, 7, 16, tzinfo=timezone.utc),
            sheet_export_status="READY",
            page=1,
            page_size=1,
            auth=self.auth,
            db=self.db,
        )
        blocked = fetch_results(
            q=None,
            status=None,
            opened_from=datetime(2026, 7, 14, tzinfo=timezone.utc),
            opened_to=datetime(2026, 7, 16, tzinfo=timezone.utc),
            sheet_export_status="BLOCKED",
            page=1,
            page_size=1,
            auth=self.auth,
            db=self.db,
        )

        self.assertEqual(ready.total, 1)
        self.assertEqual(ready.items[0].id, ready_round.id)
        self.assertEqual(blocked.total, 1)
        self.assertEqual(blocked.items[0].id, missing_round.id)

    def test_list_route_marks_missing_and_ambiguous_notice_context(self):
        collect_opening_results(self.db, self.request, self.make_client())

        missing = fetch_results(
            q=None,
            status=None,
            opened_from=datetime(2026, 7, 14, tzinfo=timezone.utc),
            opened_to=datetime(2026, 7, 16, tzinfo=timezone.utc),
            page=1,
            page_size=30,
            auth=self.auth,
            db=self.db,
        )
        self.assertEqual(missing.items[0].sheet_export_status, "NOTICE_CONTEXT_MISSING")
        self.assertEqual(missing.items[0].sheet_block_reasons, ["bid_notice_context"])

        self.add_bid_notice(bid_notice_ord="00", dedup_suffix="first")
        self.add_bid_notice(bid_notice_ord="000", dedup_suffix="second")
        self.db.commit()
        ambiguous = fetch_results(
            q=None,
            status=None,
            opened_from=datetime(2026, 7, 14, tzinfo=timezone.utc),
            opened_to=datetime(2026, 7, 16, tzinfo=timezone.utc),
            page=1,
            page_size=30,
            auth=self.auth,
            db=self.db,
        )
        self.assertEqual(
            ambiguous.items[0].sheet_export_status,
            "NOTICE_CONTEXT_AMBIGUOUS",
        )
        self.assertFalse(ambiguous.items[0].sheet_exportable)
        self.assertEqual(
            ambiguous.items[0].sheet_block_reasons,
            ["ambiguous_bid_notice_context"],
        )

    def test_list_route_uses_business_amount_for_nonpriced_notice(self):
        collect_opening_results(self.db, self.request, self.make_client())
        self.add_bid_notice(
            base_amount=Decimal("90000000"),
            prearranged_price_decision_method="비예가",
        )
        self.db.commit()

        response = fetch_results(
            q=None,
            status=None,
            opened_from=datetime(2026, 7, 14, tzinfo=timezone.utc),
            opened_to=datetime(2026, 7, 16, tzinfo=timezone.utc),
            page=1,
            page_size=30,
            auth=self.auth,
            db=self.db,
        )

        self.assertEqual(response.items[0].base_amount, Decimal("90000000"))
        self.assertEqual(response.items[0].prearranged_price_decision_method, "비예가")
        self.assertEqual(response.items[0].sheet_export_status, "READY")
        self.assertTrue(response.items[0].sheet_exportable)

    def test_list_search_matches_official_business_and_demand_agency_names(self):
        collect_opening_results(self.db, self.request, self.make_client())
        self.add_bid_notice(
            business_name="공식 양자컴퓨팅 교육 사업",
            demand_agency_name="공식 디지털혁신 전담기관",
        )
        self.db.commit()

        business_rows, business_total = list_opening_results(
            self.db,
            OpeningResultListQuery(
                q="양자컴퓨팅",
                opened_from=datetime(2026, 7, 14, tzinfo=timezone.utc),
                opened_to=datetime(2026, 7, 16, tzinfo=timezone.utc),
            ),
            organization_id=self.organization.id,
            user_id=self.user.id,
        )
        agency_rows, agency_total = list_opening_results(
            self.db,
            OpeningResultListQuery(
                q="디지털혁신 전담기관",
                opened_from=datetime(2026, 7, 14, tzinfo=timezone.utc),
                opened_to=datetime(2026, 7, 16, tzinfo=timezone.utc),
            ),
            organization_id=self.organization.id,
            user_id=self.user.id,
        )

        self.assertEqual(business_total, 1)
        self.assertEqual(agency_total, 1)
        self.assertEqual(business_rows[0].id, agency_rows[0].id)

    def test_settings_exposes_only_sheet_service_account_email(self):
        credentials = {
            "type": "service_account",
            "client_email": "sheet-writer@icore-test.iam.gserviceaccount.com",
            "private_key": "not-returned",
        }
        with patch.dict(
            "os.environ",
            {"GSHEET_SERVICE_ACCOUNT_JSON": json.dumps(credentials)},
            clear=True,
        ):
            self.assertEqual(
                get_sheet_service_account_email(),
                credentials["client_email"],
            )
            response = fetch_result_settings(auth=self.auth, db=self.db)

        self.assertEqual(
            response.sheet_service_account_email,
            credentials["client_email"],
        )
        self.assertNotIn("private_key", response.model_dump_json())

        with patch.dict(
            "os.environ",
            {"GSHEET_SERVICE_ACCOUNT_JSON": "not-json"},
            clear=True,
        ):
            self.assertIsNone(get_sheet_service_account_email())

        with patch.dict(
            "os.environ",
            {
                "GSHEET_SERVICE_ACCOUNT_EMAIL": (
                    "adc-sheet-writer@icore-test.iam.gserviceaccount.com"
                )
            },
            clear=True,
        ):
            self.assertEqual(
                get_sheet_service_account_email(),
                "adc-sheet-writer@icore-test.iam.gserviceaccount.com",
            )

    def test_two_user_pilot_lists_only_usable_sheet_destinations(self):
        teammate = UserModel(
            username="sheet-pilot-member",
            password_salt="salt",
            password_hash="hash",
            role="viewer",
            is_active=True,
        )
        self.db.add(teammate)
        self.db.flush()
        self.db.add(
            OrganizationMemberModel(
                organization_id=self.organization.id,
                user_id=teammate.id,
                role="member",
                is_active=True,
            )
        )
        admin_personal = SheetDestinationModel(
            organization_id=self.organization.id,
            owner_user_id=self.user.id,
            label="관리자 개인 Sheet",
            spreadsheet_id="admin-personal-sheet",
            tab_name="개찰결과",
            is_active=True,
        )
        member_personal = SheetDestinationModel(
            organization_id=self.organization.id,
            owner_user_id=teammate.id,
            label="구성원 개인 Sheet",
            spreadsheet_id="member-personal-sheet",
            tab_name="개찰결과",
            is_active=True,
        )
        self.db.add_all([admin_personal, member_personal])
        self.db.commit()
        member_auth = {
            "user_id": teammate.id,
            "username": teammate.username,
            "role": teammate.role,
            "organization_id": self.organization.id,
            "organization_name": self.organization.name,
            "organization_role": "member",
        }

        admin_rows = fetch_sheet_destinations(auth=self.auth, db=self.db)
        member_settings = fetch_result_settings(auth=member_auth, db=self.db)
        member_rows = fetch_sheet_destinations(auth=member_auth, db=self.db)

        self.assertEqual(
            {row.label for row in admin_rows},
            {"테스트 공용 Sheet", "관리자 개인 Sheet"},
        )
        self.assertEqual(
            [row.label for row in member_settings.sheet_destinations],
            ["구성원 개인 Sheet"],
        )
        self.assertEqual(
            [row.label for row in member_rows],
            ["구성원 개인 Sheet"],
        )

    def test_member_can_register_personal_but_not_organization_destination(self):
        teammate = UserModel(
            username="sheet-register-member",
            password_salt="salt",
            password_hash="hash",
            role="viewer",
            is_active=True,
        )
        self.db.add(teammate)
        self.db.flush()
        self.db.add(
            OrganizationMemberModel(
                organization_id=self.organization.id,
                user_id=teammate.id,
                role="member",
                is_active=True,
            )
        )
        self.db.commit()
        member_auth = {
            "user_id": teammate.id,
            "username": teammate.username,
            "role": teammate.role,
            "organization_id": self.organization.id,
            "organization_name": self.organization.name,
            "organization_role": "member",
        }

        personal = upsert_sheet_destination(
            SheetDestinationUpsertRequest(
                label="내 개인 Sheet",
                spreadsheet_id="member-created-personal-sheet",
                tab_name="개찰결과",
                scope="PERSONAL",
                is_default=True,
            ),
            auth=member_auth,
            db=self.db,
        )

        self.assertEqual(personal.scope, "PERSONAL")
        self.assertEqual(
            self.db.get(SheetDestinationModel, personal.id).owner_user_id,
            teammate.id,
        )
        with self.assertRaises(HTTPException) as raised:
            upsert_sheet_destination(
                SheetDestinationUpsertRequest(
                    label="권한 없는 공용 Sheet",
                    spreadsheet_id="member-created-organization-sheet",
                    tab_name="개찰결과",
                    scope="ORGANIZATION",
                    is_default=True,
                ),
                auth=member_auth,
                db=self.db,
            )
        self.assertEqual(raised.exception.status_code, 403)

    def test_member_cannot_verify_registered_organization_destination(self):
        teammate = UserModel(
            username="sheet-verify-member",
            password_salt="salt",
            password_hash="hash",
            role="viewer",
            is_active=True,
        )
        self.db.add(teammate)
        self.db.flush()
        self.db.add(
            OrganizationMemberModel(
                organization_id=self.organization.id,
                user_id=teammate.id,
                role="member",
                is_active=True,
            )
        )
        self.db.commit()
        member_auth = {
            "user_id": teammate.id,
            "username": teammate.username,
            "role": teammate.role,
            "organization_id": self.organization.id,
            "organization_name": self.organization.name,
            "organization_role": "member",
        }
        writer_factory = Mock()
        writer_factory.return_value.verify_connection.return_value = Mock(
            spreadsheet_title="조직 공용 Sheet",
            tab_exists=True,
            header_status="MATCH",
        )

        with patch(
            "app.g2b.opening_results.router.GoogleSheetWriter.from_env",
            writer_factory,
        ):
            with self.assertRaises(HTTPException) as raised:
                verify_sheet_destination(
                    SheetDestinationVerifyRequest(
                        spreadsheet_id=self.destination.spreadsheet_id,
                        tab_name=self.destination.tab_name,
                    ),
                    auth=member_auth,
                    db=self.db,
                )

        self.assertEqual(raised.exception.status_code, 404)
        writer_factory.assert_not_called()

    def test_scheduled_collection_runs_once_per_configured_slot(self):
        client = self.make_client()
        now = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)

        first = run_scheduled_opening_results(
            self.db,
            now=now,
            client=client,
        )
        second = run_scheduled_opening_results(
            self.db,
            now=now + timedelta(minutes=10),
            client=client,
        )

        self.assertEqual(first.run_key, "SERVICE:2026071511")
        self.assertEqual(first.window_end - first.window_start, timedelta(hours=3))
        self.assertFalse(first.skipped_existing_run)
        self.assertTrue(second.skipped_existing_run)
        self.assertEqual(first.run_status, "SUCCESS")
        self.assertEqual(second.run_status, "SUCCESS")
        self.assertEqual(client.search_round_call_count, 1)
        self.assertEqual(
            self.db.scalar(select(func.count(BidOpeningCollectionRunModel.id))), 1
        )

    def test_schedule_window_uses_configured_kst_boundaries(self):
        kst = timezone(timedelta(hours=9))
        cases = [
            (
                datetime(2026, 7, 15, 7, 59, tzinfo=kst),
                "SERVICE:2026071417",
                datetime(2026, 7, 14, 14, 0, tzinfo=kst),
                datetime(2026, 7, 14, 17, 0, tzinfo=kst),
            ),
            (
                datetime(2026, 7, 15, 8, 0, tzinfo=kst),
                "SERVICE:2026071508",
                datetime(2026, 7, 14, 17, 0, tzinfo=kst),
                datetime(2026, 7, 15, 8, 0, tzinfo=kst),
            ),
            (
                datetime(2026, 7, 15, 11, 0, tzinfo=kst),
                "SERVICE:2026071511",
                datetime(2026, 7, 15, 8, 0, tzinfo=kst),
                datetime(2026, 7, 15, 11, 0, tzinfo=kst),
            ),
            (
                datetime(2026, 7, 15, 14, 0, tzinfo=kst),
                "SERVICE:2026071514",
                datetime(2026, 7, 15, 11, 0, tzinfo=kst),
                datetime(2026, 7, 15, 14, 0, tzinfo=kst),
            ),
            (
                datetime(2026, 7, 15, 17, 0, tzinfo=kst),
                "SERVICE:2026071517",
                datetime(2026, 7, 15, 14, 0, tzinfo=kst),
                datetime(2026, 7, 15, 17, 0, tzinfo=kst),
            ),
        ]

        for current, expected_key, expected_local_start, expected_local_end in cases:
            with self.subTest(current=current):
                run_key, window_start, window_end = build_scheduled_collection_window(
                    current
                )
                self.assertEqual(run_key, expected_key)
                self.assertEqual(window_start.astimezone(kst), expected_local_start)
                self.assertEqual(window_end.astimezone(kst), expected_local_end)

    def test_manual_collection_requires_system_admin(self):
        with self.assertRaises(HTTPException) as raised:
            collect_results(
                self.request,
                auth={**self.auth, "role": "viewer", "organization_role": "member"},
                db=self.db,
            )

        self.assertEqual(raised.exception.status_code, 403)

    def test_manual_collection_window_is_limited_to_fourteen_days(self):
        with self.assertRaises(ValueError):
            CollectOpeningResultsRequest(
                start_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
                end_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
            )

    def test_global_collection_is_independent_from_legacy_scraper_config(self):
        config = self.db.scalar(select(ScraperConfigModel))
        config.enabled = False
        self.db.commit()
        client = self.make_client()

        result = run_scheduled_opening_results(
            self.db,
            now=datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc),
            client=client,
        )

        self.assertEqual(result.fetched_round_count, 1)
        self.assertEqual(client.search_round_call_count, 1)

    def test_next_collection_slot_does_not_duplicate_previous_results(self):
        client = self.make_client()
        first_now = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)

        first = run_scheduled_opening_results(self.db, now=first_now, client=client)
        snapshot_count = self.db.scalar(select(func.count(BidResultSnapshotModel.id)))
        second = run_scheduled_opening_results(
            self.db,
            now=first_now + timedelta(hours=3),
            client=client,
        )

        self.assertFalse(first.skipped_existing_run)
        self.assertFalse(second.skipped_existing_run)
        self.assertEqual(second.window_start, first.window_end)
        self.assertEqual(client.search_round_call_count, 2)
        self.assertEqual(
            self.db.scalar(select(func.count(BidOpeningCollectionRunModel.id))), 2
        )
        self.assertEqual(
            self.db.scalar(select(func.count(BidOpeningRoundModel.id))), 1
        )
        self.assertEqual(
            self.db.scalar(select(func.count(BidOpeningEntryModel.id))), 2
        )
        self.assertEqual(
            self.db.scalar(select(func.count(BidResultSnapshotModel.id))),
            snapshot_count,
        )

    def test_overnight_collection_slots_are_contiguous(self):
        client = self.make_client()
        first_now = datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)

        first = run_scheduled_opening_results(self.db, now=first_now, client=client)
        second = run_scheduled_opening_results(
            self.db,
            now=first_now + timedelta(hours=15),
            client=client,
        )

        self.assertEqual(first.run_key, "SERVICE:2026071517")
        self.assertEqual(second.run_key, "SERVICE:2026071608")
        self.assertEqual(second.window_start, first.window_end)
        self.assertEqual(client.search_round_call_count, 2)
        self.assertEqual(
            self.db.scalar(select(func.count(BidOpeningCollectionRunModel.id))), 2
        )

    def test_scheduled_collection_catches_up_from_last_successful_window(self):
        now = datetime(2026, 7, 16, 2, 0, tzinfo=timezone.utc)
        _, _, current_window_end = build_scheduled_collection_window(now)
        previous_window_end = current_window_end - timedelta(hours=18)
        self.db.add(
            BidOpeningCollectionRunModel(
                run_key="SERVICE:previous-success",
                business_type="SERVICE",
                window_start=previous_window_end - timedelta(hours=3),
                window_end=previous_window_end,
                status="SUCCESS",
                started_at=previous_window_end - timedelta(hours=3),
                finished_at=previous_window_end,
            )
        )
        self.db.commit()

        response = run_scheduled_opening_results(
            self.db,
            now=now,
            client=self.make_client(),
        )

        self.assertEqual(response.window_start, previous_window_end)
        self.assertEqual(response.window_end, current_window_end)

    def test_scheduled_catchup_window_is_capped_at_fourteen_days(self):
        now = datetime(2026, 7, 20, 2, 0, tzinfo=timezone.utc)
        _, _, current_window_end = build_scheduled_collection_window(now)
        old_window_end = current_window_end - timedelta(days=30)
        self.db.add(
            BidOpeningCollectionRunModel(
                run_key="SERVICE:old-success",
                business_type="SERVICE",
                window_start=old_window_end - timedelta(hours=3),
                window_end=old_window_end,
                status="SUCCESS",
                started_at=old_window_end - timedelta(hours=3),
                finished_at=old_window_end,
            )
        )
        self.db.commit()

        response = run_scheduled_opening_results(
            self.db,
            now=now,
            client=self.make_client(),
        )

        self.assertEqual(
            response.window_start,
            current_window_end - timedelta(days=14),
        )
        self.assertEqual(response.window_end, current_window_end)

    def test_previous_collection_worker_cannot_complete_after_reclaim(self):
        real_collect = collect_opening_results

        def collect_then_lose_lease(db, request, client, **kwargs):
            result = real_collect(db, request, client, **kwargs)
            db.execute(
                update(BidOpeningCollectionRunModel)
                .where(BidOpeningCollectionRunModel.status == "RUNNING")
                .values(
                    claim_token="replacement-worker-token",
                    started_at=datetime.now(timezone.utc),
                )
            )
            db.commit()
            return result

        with patch(
            "app.g2b.opening_results.service.collect_opening_results",
            side_effect=collect_then_lose_lease,
        ):
            response = run_scheduled_opening_results(
                self.db,
                now=datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc),
                client=self.make_client(),
            )

        run = self.db.scalar(select(BidOpeningCollectionRunModel))
        self.assertTrue(response.skipped_existing_run)
        self.assertEqual(run.status, "RUNNING")
        self.assertEqual(run.claim_token, "replacement-worker-token")

    def test_lost_collection_worker_cannot_commit_canonical_rows(self):
        run = BidOpeningCollectionRunModel(
            run_key="SERVICE:lost-before-commit",
            business_type="SERVICE",
            window_start=self.request.start_at,
            window_end=self.request.end_at,
            status="RUNNING",
            claim_token="replacement-worker-token",
            started_at=datetime.now(timezone.utc),
        )
        self.db.add(run)
        self.db.commit()

        with self.assertRaises(OpeningResultCollectionLeaseLostError):
            collect_opening_results(
                self.db,
                self.request,
                self.make_client(),
                collection_run_id=run.id,
                collection_claim_token="expired-worker-token",
            )

        self.assertEqual(
            self.db.scalar(select(func.count(BidOpeningRoundModel.id))),
            0,
        )
        self.assertEqual(
            self.db.scalar(select(func.count(BidOpeningEntryModel.id))),
            0,
        )

    def test_same_business_type_collection_is_globally_serialized(self):
        self.db.add(
            BidOpeningCollectionLeaseModel(
                business_type="SERVICE",
                claim_token="active-worker-token",
                claimed_at=datetime.now(timezone.utc),
            )
        )
        self.db.commit()
        client = self.make_client()

        with self.assertRaises(OpeningResultCollectionConflictError):
            collect_opening_results(self.db, self.request, client)

        self.assertEqual(client.search_round_call_count, 0)
        self.assertEqual(
            self.db.scalar(select(func.count(BidOpeningRoundModel.id))),
            0,
        )

    def test_fresh_running_collection_slot_is_not_started_twice(self):
        client = self.make_client()
        now = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)
        run_key, window_start, window_end = build_scheduled_collection_window(now)
        self.db.add(
            BidOpeningCollectionRunModel(
                run_key=run_key,
                business_type="SERVICE",
                window_start=window_start,
                window_end=window_end,
                status="RUNNING",
                started_at=datetime.now(timezone.utc),
            )
        )
        self.db.commit()

        response = run_scheduled_opening_results(self.db, now=now, client=client)

        self.assertTrue(response.skipped_existing_run)
        self.assertEqual(response.run_status, "RUNNING")
        self.assertEqual(client.search_round_call_count, 0)

    def test_scheduler_route_returns_conflict_while_same_slot_is_running(self):
        running_response = ScheduledCollectOpeningResultsResponse(
            run_key="SERVICE:2026071511",
            window_start=datetime(2026, 7, 14, 23, 0, tzinfo=timezone.utc),
            window_end=datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc),
            run_status="RUNNING",
            skipped_existing_run=True,
            fetched_round_count=0,
            fetched_entry_count=0,
            inserted_round_count=0,
            updated_round_count=0,
            inserted_entry_count=0,
            updated_entry_count=0,
            skipped_count=0,
        )
        with patch(
            "app.g2b.opening_results.router.run_scheduled_opening_results",
            return_value=running_response,
        ):
            with self.assertRaises(HTTPException) as raised:
                collect_results_on_schedule(db=self.db)

        self.assertEqual(raised.exception.status_code, 409)

    def test_stale_running_collection_slot_is_reclaimed_once(self):
        client = self.make_client()
        now = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)
        run_key, window_start, window_end = build_scheduled_collection_window(now)
        self.db.add(
            BidOpeningCollectionRunModel(
                run_key=run_key,
                business_type="SERVICE",
                window_start=window_start,
                window_end=window_end,
                status="RUNNING",
                started_at=datetime.now(timezone.utc) - timedelta(minutes=46),
            )
        )
        self.db.commit()

        first = run_scheduled_opening_results(self.db, now=now, client=client)
        second = run_scheduled_opening_results(self.db, now=now, client=client)

        self.assertFalse(first.skipped_existing_run)
        self.assertTrue(second.skipped_existing_run)
        self.assertEqual(client.search_round_call_count, 1)

    def test_failed_collection_slot_is_retried_and_becomes_success(self):
        now = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)
        failing_client = Mock()
        failing_client.search_rounds.side_effect = OpeningResultApiError("temporary failure")

        with self.assertRaises(OpeningResultApiError):
            run_scheduled_opening_results(
                self.db,
                now=now,
                client=failing_client,
            )

        failed_run = self.db.scalar(select(BidOpeningCollectionRunModel))
        self.assertEqual(failed_run.status, "FAILED")
        self.assertIn("temporary failure", failed_run.error_message)

        retry_client = self.make_client()
        response = run_scheduled_opening_results(
            self.db,
            now=now,
            client=retry_client,
        )

        self.assertFalse(response.skipped_existing_run)
        self.assertEqual(retry_client.search_round_call_count, 1)
        self.assertEqual(
            self.db.scalar(select(func.count(BidOpeningCollectionRunModel.id))),
            1,
        )
        completed_run = self.db.scalar(select(BidOpeningCollectionRunModel))
        self.assertEqual(completed_run.status, "SUCCESS")
        self.assertIsNone(completed_run.error_message)

    def test_entry_failure_retries_without_duplicate_canonical_rows(self):
        now = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)
        failing_client = self.make_client()
        failing_client.fetch_entries = Mock(
            side_effect=OpeningResultApiError("temporary entry failure")
        )

        with self.assertRaises(OpeningResultApiError):
            run_scheduled_opening_results(
                self.db,
                now=now,
                client=failing_client,
            )

        failed_run = self.db.scalar(select(BidOpeningCollectionRunModel))
        self.assertEqual(failed_run.status, "FAILED")
        self.assertEqual(
            self.db.scalar(select(func.count(BidOpeningRoundModel.id))),
            1,
        )
        self.assertEqual(
            self.db.scalar(select(func.count(BidOpeningEntryModel.id))),
            0,
        )

        response = run_scheduled_opening_results(
            self.db,
            now=now,
            client=self.make_client(),
        )

        self.assertEqual(response.run_status, "SUCCESS")
        self.assertEqual(
            self.db.scalar(select(func.count(BidOpeningRoundModel.id))),
            1,
        )
        self.assertEqual(
            self.db.scalar(select(func.count(BidOpeningEntryModel.id))),
            2,
        )

    def test_sheet_export_route_is_dry_run_by_default(self):
        collect_opening_results(self.db, self.request, self.make_client())
        result_id = self.db.scalar(select(BidOpeningRoundModel.id))
        self.add_bid_notice(
            business_name="DB 공식 사업명",
            demand_agency_name="DB 공식 수요기관",
        )

        response = export_results_sheet(
            ExportOpeningResultsSheetRequest(
                result_ids=[result_id],
                notice_contexts=[
                    BidNoticeSheetContext(
                        bid_notice_no="R26BK00000001",
                        bid_notice_ord="00",
                        business_name="브라우저 변조 사업명",
                        demand_agency_name="브라우저 변조 기관",
                        base_amount=Decimal("1"),
                        proposal_deadline=datetime(
                            2026, 7, 20, 6, 0, tzinfo=timezone.utc
                        ),
                        region_restriction="브라우저 변조 지역",
                        is_two_stage_bid=False,
                    )
                ]
            ),
            auth=self.auth,
            db=self.db,
        )

        self.assertFalse(response.written)
        self.assertEqual(response.requested_result_count, 1)
        self.assertEqual(response.row_count, 1)
        self.assertEqual(response.missing_result_ids, [])
        self.assertEqual(response.missing_notice_context_keys, [])
        self.assertEqual(response.preview_rows[0][1], "DB 공식 사업명")
        self.assertEqual(response.preview_rows[0][2], "DB 공식 수요기관")
        self.assertEqual(response.preview_rows[0][3], 165000000)
        self.assertEqual(response.preview_rows[0][6], "Y")
        self.assertEqual(response.preview_rows[0][8], "19.5+75=94.50")
        self.assertEqual(response.destination_scope, "ORGANIZATION")
        self.assertEqual(response.destination_tab_name, "개찰결과")
        self.assertEqual(len(response.preview_token), 64)

    def test_organization_sheet_export_requires_organization_admin(self):
        teammate = UserModel(
            username="member-sheet-export",
            password_salt="salt",
            password_hash="hash",
            role="viewer",
            is_active=True,
        )
        self.db.add(teammate)
        self.db.flush()
        self.db.add(
            OrganizationMemberModel(
                organization_id=self.organization.id,
                user_id=teammate.id,
                role="member",
                is_active=True,
            )
        )
        self.db.commit()
        collect_opening_results(self.db, self.request, self.make_client())
        result_id = self.db.scalar(select(BidOpeningRoundModel.id))
        self.add_bid_notice()

        with self.assertRaises(HTTPException) as raised:
            export_results_sheet(
                ExportOpeningResultsSheetRequest(
                    result_ids=[result_id],
                    destination_id=self.destination.id,
                ),
                auth={
                    "user_id": teammate.id,
                    "role": "viewer",
                    "organization_id": self.organization.id,
                    "organization_name": self.organization.name,
                    "organization_role": "member",
                },
                db=self.db,
            )

        self.assertEqual(raised.exception.status_code, 403)
        self.assertIn("조직 관리자", str(raised.exception.detail))

    def test_member_default_sheet_export_resolves_own_personal_destination(self):
        teammate = UserModel(
            username="member-default-sheet",
            password_salt="salt",
            password_hash="hash",
            role="viewer",
            is_active=True,
        )
        self.db.add(teammate)
        self.db.flush()
        self.db.add(
            OrganizationMemberModel(
                organization_id=self.organization.id,
                user_id=teammate.id,
                role="member",
                is_active=True,
            )
        )
        self.add_user_result_profile(teammate.id)
        personal_destination = SheetDestinationModel(
            organization_id=self.organization.id,
            owner_user_id=teammate.id,
            label="구성원 기본 개인 Sheet",
            spreadsheet_id="member-default-personal-sheet",
            tab_name="개찰결과",
            is_default=False,
            is_active=True,
        )
        self.db.add(personal_destination)
        self.db.commit()
        collect_opening_results(self.db, self.request, self.make_client())
        result_id = self.db.scalar(select(BidOpeningRoundModel.id))
        self.add_bid_notice()
        member_auth = {
            "user_id": teammate.id,
            "username": teammate.username,
            "role": teammate.role,
            "organization_id": self.organization.id,
            "organization_name": self.organization.name,
            "organization_role": "member",
        }

        response = export_results_sheet(
            ExportOpeningResultsSheetRequest(result_ids=[result_id]),
            auth=member_auth,
            db=self.db,
        )

        self.assertEqual(response.destination_id, personal_destination.id)
        self.assertEqual(response.destination_scope, "PERSONAL")

    def test_sheet_write_requires_matching_preview_token(self):
        collect_opening_results(self.db, self.request, self.make_client())
        result_id = self.db.scalar(select(BidOpeningRoundModel.id))
        notice = self.add_bid_notice()
        preview = self.preview_sheet_export([result_id], self.destination.id)
        notice.business_name = "미리보기 이후 변경된 공식 사업명"
        self.db.commit()

        writer_factory = Mock()
        with patch(
            "app.g2b.opening_results.router.GoogleSheetWriter.from_env",
            writer_factory,
        ):
            with self.assertRaises(HTTPException) as raised:
                export_results_sheet(
                    ExportOpeningResultsSheetRequest(
                        result_ids=[result_id],
                        destination_id=self.destination.id,
                        dry_run=False,
                        expected_preview_token=preview.preview_token,
                    ),
                    auth=self.auth,
                    db=self.db,
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("다시 확인", raised.exception.detail)
        writer_factory.assert_not_called()

    def test_sheet_preview_hides_missing_or_unscoped_selected_result(self):
        collect_opening_results(self.db, self.request, self.make_client())
        result_id = self.db.scalar(select(BidOpeningRoundModel.id))
        self.add_bid_notice()

        with self.assertRaises(HTTPException) as raised:
            export_results_sheet(
                ExportOpeningResultsSheetRequest(
                    result_ids=[result_id, 999999],
                ),
                auth=self.auth,
                db=self.db,
            )

        self.assertEqual(raised.exception.status_code, 404)

    def test_sheet_write_is_blocked_when_notice_context_is_missing(self):
        collect_opening_results(self.db, self.request, self.make_client())
        result_id = self.db.scalar(select(BidOpeningRoundModel.id))

        writer_factory = Mock()
        with patch(
            "app.g2b.opening_results.router.GoogleSheetWriter.from_env",
            writer_factory,
        ):
            with self.assertRaises(HTTPException) as raised:
                export_results_sheet(
                    ExportOpeningResultsSheetRequest(
                        result_ids=[result_id],
                        dry_run=False,
                    ),
                    auth=self.auth,
                    db=self.db,
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["missing_notice_context_keys"],
            ["R26BK00000001|00"],
        )
        writer_factory.assert_not_called()

    def test_sheet_write_is_blocked_when_notice_context_is_incomplete(self):
        collect_opening_results(self.db, self.request, self.make_client())
        result_id = self.db.scalar(select(BidOpeningRoundModel.id))
        notice = self.add_bid_notice(
            base_amount=None,
            proposal_deadline=None,
        )
        notice.estimated_price = "165000000"
        notice.deadline_at = datetime(2026, 7, 20, 15, 0)
        self.db.flush()

        writer_factory = Mock()
        with patch(
            "app.g2b.opening_results.router.GoogleSheetWriter.from_env",
            writer_factory,
        ):
            with self.assertRaises(HTTPException) as raised:
                export_results_sheet(
                    ExportOpeningResultsSheetRequest(
                        result_ids=[result_id],
                        notice_contexts=[
                            BidNoticeSheetContext(
                                bid_notice_no="R26BK00000001",
                                bid_notice_ord="00",
                                business_name="브라우저가 보낸 값은 무시",
                                demand_agency_name="브라우저 기관",
                                base_amount=Decimal("165000000"),
                                proposal_deadline=datetime(
                                    2026, 7, 20, 6, 0, tzinfo=timezone.utc
                                ),
                                region_restriction="없음",
                                is_two_stage_bid=True,
                            )
                        ],
                        dry_run=False,
                    ),
                    auth=self.auth,
                    db=self.db,
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["missing_notice_context_keys"],
            ["R26BK00000001|00"],
        )
        writer_factory.assert_not_called()

    def test_sheet_write_is_blocked_for_whitespace_only_official_text(self):
        collect_opening_results(self.db, self.request, self.make_client())
        result_id = self.db.scalar(select(BidOpeningRoundModel.id))
        self.add_bid_notice(demand_agency_name="   ")

        writer_factory = Mock()
        with patch(
            "app.g2b.opening_results.router.GoogleSheetWriter.from_env",
            writer_factory,
        ):
            with self.assertRaises(HTTPException) as raised:
                export_results_sheet(
                    ExportOpeningResultsSheetRequest(
                        result_ids=[result_id],
                        dry_run=False,
                    ),
                    auth=self.auth,
                    db=self.db,
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["missing_notice_context_keys"],
            ["R26BK00000001|00"],
        )
        writer_factory.assert_not_called()

    def test_sheet_context_does_not_join_different_notice_order(self):
        collect_opening_results(self.db, self.request, self.make_client())
        result_id = self.db.scalar(select(BidOpeningRoundModel.id))
        self.add_bid_notice(bid_notice_ord="01")

        rows, missing_context_keys, missing_result_ids = build_sheet_rows(
            self.db,
            [result_id],
        )

        self.assertEqual(missing_result_ids, [])
        self.assertEqual(missing_context_keys, ["R26BK00000001|00"])
        self.assertEqual(rows[0][1:7], ["", "", "", "", "", ""])

    def test_sheet_write_is_blocked_for_ambiguous_official_notice(self):
        collect_opening_results(self.db, self.request, self.make_client())
        result_id = self.db.scalar(select(BidOpeningRoundModel.id))
        self.add_bid_notice(bid_notice_ord="00", dedup_suffix="first")
        self.add_bid_notice(bid_notice_ord="000", dedup_suffix="second")

        writer_factory = Mock()
        with patch(
            "app.g2b.opening_results.router.GoogleSheetWriter.from_env",
            writer_factory,
        ):
            with self.assertRaises(HTTPException) as raised:
                export_results_sheet(
                    ExportOpeningResultsSheetRequest(
                        result_ids=[result_id],
                        dry_run=False,
                    ),
                    auth=self.auth,
                    db=self.db,
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["ambiguous_notice_context_keys"],
            ["R26BK00000001|0"],
        )
        writer_factory.assert_not_called()

    def test_sheet_write_is_blocked_for_multiple_rounds_of_same_notice(self):
        first = self.completed_summary(rebid_no="0")
        second = self.completed_summary(rebid_no="1")
        collect_opening_results(
            self.db,
            self.request,
            StubOpeningResultClient(
                [first, second],
                entries={
                    (first["bidNtceNo"], first["bidNtceOrd"], first["rbidNo"]): self.entries("0"),
                    (second["bidNtceNo"], second["bidNtceOrd"], second["rbidNo"]): self.entries("1"),
                },
            ),
        )
        result_ids = list(
            self.db.scalars(
                select(BidOpeningRoundModel.id).order_by(BidOpeningRoundModel.id)
            )
        )
        self.add_bid_notice()

        writer_factory = Mock()
        with patch(
            "app.g2b.opening_results.router.GoogleSheetWriter.from_env",
            writer_factory,
        ):
            with self.assertRaises(HTTPException) as raised:
                export_results_sheet(
                    ExportOpeningResultsSheetRequest(
                        result_ids=result_ids,
                        dry_run=False,
                    ),
                    auth=self.auth,
                    db=self.db,
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["duplicate_notice_numbers"],
            ["R26BK00000001"],
        )
        writer_factory.assert_not_called()

    def test_sheet_write_rejects_another_users_personal_destination(self):
        collect_opening_results(self.db, self.request, self.make_client())
        result_id = self.db.scalar(select(BidOpeningRoundModel.id))
        other_user = UserModel(
            username="other",
            password_salt="salt",
            password_hash="hash",
            role="viewer",
            is_active=True,
        )
        self.db.add(other_user)
        self.db.flush()
        other_destination = SheetDestinationModel(
            organization_id=self.organization.id,
            owner_user_id=other_user.id,
            label="다른 사용자 Sheet",
            spreadsheet_id="other-sheet",
            tab_name="개찰결과",
            is_active=True,
        )
        self.db.add(other_destination)
        self.db.commit()

        with self.assertRaises(HTTPException) as raised:
            export_results_sheet(
                ExportOpeningResultsSheetRequest(
                    result_ids=[result_id],
                    destination_id=other_destination.id,
                    dry_run=False,
                ),
                auth={**self.auth, "role": "viewer", "organization_role": "member"},
                db=self.db,
            )

        self.assertEqual(raised.exception.status_code, 409)

    def test_same_canonical_result_is_matched_independently_per_user(self):
        second_organization = OrganizationModel(name="다른 조직", slug="other-org")
        second_user = UserModel(
            username="second-org-user",
            password_salt="salt",
            password_hash="hash",
            role="viewer",
            is_active=True,
        )
        self.db.add_all([second_organization, second_user])
        self.db.flush()
        second_profile = UserResultProfileModel(
            organization_id=second_organization.id,
            user_id=second_user.id,
            enabled=True,
            keywords="로봇",
            excluded_keywords="",
        )
        self.db.add_all(
            [
                OrganizationMemberModel(
                    organization_id=second_organization.id,
                    user_id=second_user.id,
                    role="member",
                    is_active=True,
                ),
                second_profile,
            ]
        )
        self.db.commit()

        collect_opening_results(self.db, self.request, self.make_client())
        first_rows, first_total = list_opening_results(
            self.db,
            OpeningResultListQuery(
                opened_from=datetime(2026, 7, 14, tzinfo=timezone.utc),
                opened_to=datetime(2026, 7, 16, tzinfo=timezone.utc),
            ),
            organization_id=self.organization.id,
            user_id=self.user.id,
        )
        second_rows, second_total = list_opening_results(
            self.db,
            OpeningResultListQuery(
                opened_from=datetime(2026, 7, 14, tzinfo=timezone.utc),
                opened_to=datetime(2026, 7, 16, tzinfo=timezone.utc),
            ),
            organization_id=second_organization.id,
            user_id=second_user.id,
        )

        self.assertEqual(self.db.scalar(select(func.count(BidOpeningRoundModel.id))), 1)
        self.assertEqual(first_total, 1)
        self.assertEqual(first_rows[0].matched_keywords, ["AI"])
        self.assertEqual(second_rows, [])
        self.assertEqual(second_total, 0)

        second_profile.keywords = "AI"
        sync_user_matches(
            self.db,
            organization_id=second_organization.id,
            user_id=second_user.id,
        )
        self.db.commit()
        second_rows, second_total = list_opening_results(
            self.db,
            OpeningResultListQuery(
                opened_from=datetime(2026, 7, 14, tzinfo=timezone.utc),
                opened_to=datetime(2026, 7, 16, tzinfo=timezone.utc),
            ),
            organization_id=second_organization.id,
            user_id=second_user.id,
        )
        self.assertEqual(second_total, 1)
        self.assertEqual(second_rows[0].id, first_rows[0].id)
        self.assertEqual(
            self.db.scalar(select(func.count(UserOpeningResultMatchModel.id))),
            2,
        )

    def test_unmatched_organization_cannot_read_or_export_global_result_id(self):
        second_organization = OrganizationModel(name="비매칭 조직", slug="no-match-org")
        second_user = UserModel(
            username="no-match-user",
            password_salt="salt",
            password_hash="hash",
            role="admin",
            is_active=True,
        )
        self.db.add_all([second_organization, second_user])
        self.db.flush()
        self.db.add_all(
            [
                OrganizationMemberModel(
                    organization_id=second_organization.id,
                    user_id=second_user.id,
                    role="admin",
                    is_active=True,
                ),
                OrganizationResultProfileModel(
                    organization_id=second_organization.id,
                    enabled=True,
                    keywords="로봇",
                    excluded_keywords="",
                ),
                SheetDestinationModel(
                    organization_id=second_organization.id,
                    owner_user_id=None,
                    label="다른 조직 Sheet",
                    spreadsheet_id="other-org-sheet",
                    tab_name="개찰결과",
                    is_default=True,
                    is_active=True,
                ),
            ]
        )
        self.db.commit()
        collect_opening_results(self.db, self.request, self.make_client())
        result_id = self.db.scalar(select(BidOpeningRoundModel.id))
        second_auth = {
            "user_id": second_user.id,
            "username": second_user.username,
            "role": "admin",
            "organization_id": second_organization.id,
            "organization_name": second_organization.name,
            "organization_role": "admin",
        }

        self.assertIsNone(
            get_opening_result(
                self.db,
                result_id,
                organization_id=second_organization.id,
                user_id=second_user.id,
            )
        )
        writer_factory = Mock()
        with patch(
            "app.g2b.opening_results.router.GoogleSheetWriter.from_env",
            writer_factory,
        ):
            with self.assertRaises(HTTPException) as raised:
                export_results_sheet(
                    ExportOpeningResultsSheetRequest(
                        result_ids=[result_id],
                        dry_run=False,
                    ),
                    auth=second_auth,
                    db=self.db,
                )
        self.assertEqual(raised.exception.status_code, 404)
        writer_factory.assert_not_called()

    def test_dismissed_result_stays_hidden_after_recollection_but_not_for_teammate(self):
        teammate = UserModel(
            username="teammate",
            password_salt="salt",
            password_hash="hash",
            role="viewer",
            is_active=True,
        )
        self.db.add(teammate)
        self.db.flush()
        self.db.add(
            OrganizationMemberModel(
                organization_id=self.organization.id,
                user_id=teammate.id,
                role="member",
                is_active=True,
            )
        )
        self.add_user_result_profile(teammate.id)
        self.db.commit()
        collect_opening_results(self.db, self.request, self.make_client())
        result_id = self.db.scalar(select(BidOpeningRoundModel.id))

        dismiss_result(
            self.db,
            organization_id=self.organization.id,
            user_id=self.user.id,
            result_id=result_id,
        )
        collect_opening_results(self.db, self.request, self.make_client())
        query = OpeningResultListQuery(
            opened_from=datetime(2026, 7, 14, tzinfo=timezone.utc),
            opened_to=datetime(2026, 7, 16, tzinfo=timezone.utc),
        )
        my_rows, my_total = list_opening_results(
            self.db,
            query,
            organization_id=self.organization.id,
            user_id=self.user.id,
        )
        teammate_rows, teammate_total = list_opening_results(
            self.db,
            query,
            organization_id=self.organization.id,
            user_id=teammate.id,
        )

        self.assertEqual(my_rows, [])
        self.assertEqual(my_total, 0)
        self.assertEqual(teammate_total, 1)
        self.assertEqual(teammate_rows[0].id, result_id)
        self.assertEqual(
            self.db.scalar(select(UserOpeningResultStateModel.state)),
            "DISMISSED",
        )

    def test_dismissed_result_is_restorable_from_archive_for_fourteen_days(self):
        collect_opening_results(self.db, self.request, self.make_client())
        result_id = self.db.scalar(select(BidOpeningRoundModel.id))
        self.add_bid_notice()
        dismiss_result(
            self.db,
            organization_id=self.organization.id,
            user_id=self.user.id,
            result_id=result_id,
        )

        archive = fetch_archived_results(
            page=1,
            page_size=30,
            auth=self.auth,
            db=self.db,
        )
        detail = fetch_archived_result_detail(
            result_id,
            auth=self.auth,
            db=self.db,
        )

        self.assertEqual(archive.total, 1)
        self.assertEqual(archive.items[0].handled_state, "DISMISSED")
        self.assertTrue(archive.items[0].can_restore)
        self.assertEqual(
            archive.items[0].expires_at - archive.items[0].handled_at,
            timedelta(days=14),
        )
        self.assertEqual(detail.id, result_id)
        self.assertEqual(detail.notice_url, "https://www.g2b.go.kr/notice/detail")

    def test_expired_archive_item_is_hidden_and_cannot_be_restored(self):
        collect_opening_results(self.db, self.request, self.make_client())
        result_id = self.db.scalar(select(BidOpeningRoundModel.id))
        dismiss_result(
            self.db,
            organization_id=self.organization.id,
            user_id=self.user.id,
            result_id=result_id,
        )
        state = self.db.scalar(select(UserOpeningResultStateModel))
        state.acted_at = datetime.now(timezone.utc) - timedelta(days=15)
        self.db.commit()

        rows, total = list_archived_results(
            self.db,
            organization_id=self.organization.id,
            user_id=self.user.id,
        )
        _, visible_total = list_opening_results(
            self.db,
            OpeningResultListQuery(
                opened_from=datetime(2026, 7, 14, tzinfo=timezone.utc),
                opened_to=datetime(2026, 7, 16, tzinfo=timezone.utc),
            ),
            organization_id=self.organization.id,
            user_id=self.user.id,
        )

        self.assertEqual(rows, [])
        self.assertEqual(total, 0)
        self.assertEqual(visible_total, 0)
        with self.assertRaises(HTTPException) as raised:
            restore_result_to_inbox(result_id, auth=self.auth, db=self.db)
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(
            self.db.scalar(select(UserOpeningResultStateModel.state)),
            "DISMISSED",
        )

    def test_dismissed_archive_can_be_cleared_after_profile_change(self):
        collect_opening_results(self.db, self.request, self.make_client())
        result_id = self.db.scalar(select(BidOpeningRoundModel.id))
        dismiss_result(
            self.db,
            organization_id=self.organization.id,
            user_id=self.user.id,
            result_id=result_id,
        )
        profile = self.db.scalar(
            select(UserResultProfileModel).where(
                UserResultProfileModel.user_id == self.user.id
            )
        )
        profile.enabled = False
        sync_user_matches(
            self.db,
            organization_id=self.organization.id,
            user_id=self.user.id,
        )
        self.db.commit()

        archive = fetch_archived_results(
            page=1,
            page_size=30,
            auth=self.auth,
            db=self.db,
        )
        response = restore_result_to_inbox(result_id, auth=self.auth, db=self.db)
        refreshed_archive = fetch_archived_results(
            page=1,
            page_size=30,
            auth=self.auth,
            db=self.db,
        )

        self.assertEqual(archive.total, 1)
        self.assertTrue(archive.items[0].can_restore)
        self.assertEqual(response.state, "RESTORED")
        self.assertFalse(response.visible)
        self.assertEqual(refreshed_archive.total, 0)

    def test_restore_dismissed_result_only_restores_current_user(self):
        teammate = UserModel(
            username="restore-teammate",
            password_salt="salt",
            password_hash="hash",
            role="viewer",
            is_active=True,
        )
        self.db.add(teammate)
        self.db.flush()
        self.db.add(
            OrganizationMemberModel(
                organization_id=self.organization.id,
                user_id=teammate.id,
                role="member",
                is_active=True,
            )
        )
        self.add_user_result_profile(teammate.id)
        self.db.commit()
        collect_opening_results(self.db, self.request, self.make_client())
        result_id = self.db.scalar(select(BidOpeningRoundModel.id))
        for user_id in (self.user.id, teammate.id):
            dismiss_result(
                self.db,
                organization_id=self.organization.id,
                user_id=user_id,
                result_id=result_id,
            )

        response = restore_result_to_inbox(
            result_id,
            auth=self.auth,
            db=self.db,
        )
        query = OpeningResultListQuery(
            opened_from=datetime(2026, 7, 14, tzinfo=timezone.utc),
            opened_to=datetime(2026, 7, 16, tzinfo=timezone.utc),
        )
        my_rows, my_total = list_opening_results(
            self.db,
            query,
            organization_id=self.organization.id,
            user_id=self.user.id,
        )
        teammate_rows, teammate_total = list_opening_results(
            self.db,
            query,
            organization_id=self.organization.id,
            user_id=teammate.id,
        )

        self.assertEqual(response.state, "RESTORED")
        self.assertTrue(response.visible)
        self.assertEqual(my_total, 1)
        self.assertEqual(my_rows[0].id, result_id)
        self.assertEqual(teammate_rows, [])
        self.assertEqual(teammate_total, 0)
        remaining_state = self.db.scalar(select(UserOpeningResultStateModel))
        self.assertEqual(remaining_state.user_id, teammate.id)

        with self.assertRaises(HTTPException) as raised:
            restore_result_to_inbox(result_id, auth=self.auth, db=self.db)
        self.assertEqual(raised.exception.status_code, 404)

    def test_restore_does_not_remove_exported_state(self):
        collect_opening_results(self.db, self.request, self.make_client())
        round_row = self.db.scalar(select(BidOpeningRoundModel))
        self.db.add(
            UserOpeningResultStateModel(
                organization_id=self.organization.id,
                user_id=self.user.id,
                result_external_key=round_row.external_key,
                state="EXPORTED",
            )
        )
        self.db.commit()

        with self.assertRaises(ResultAccessError):
            dismiss_result(
                self.db,
                organization_id=self.organization.id,
                user_id=self.user.id,
                result_id=round_row.id,
            )
        with self.assertRaises(HTTPException) as raised:
            restore_result_to_inbox(round_row.id, auth=self.auth, db=self.db)

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(
            self.db.scalar(select(UserOpeningResultStateModel.state)),
            "EXPORTED",
        )

    def test_restore_reports_not_visible_after_organization_export(self):
        collect_opening_results(self.db, self.request, self.make_client())
        round_row = self.db.scalar(select(BidOpeningRoundModel))
        dismiss_result(
            self.db,
            organization_id=self.organization.id,
            user_id=self.user.id,
            result_id=round_row.id,
        )
        self.db.add(
            SheetExportModel(
                destination_id=self.destination.id,
                organization_id=self.organization.id,
                result_external_key=round_row.external_key,
                exported_by_user_id=self.user.id,
                status="SUCCEEDED",
                succeeded_at=datetime.now(timezone.utc),
            )
        )
        self.db.commit()

        response = restore_result_to_inbox(
            round_row.id,
            auth=self.auth,
            db=self.db,
        )
        _, total = list_opening_results(
            self.db,
            OpeningResultListQuery(
                opened_from=datetime(2026, 7, 14, tzinfo=timezone.utc),
                opened_to=datetime(2026, 7, 16, tzinfo=timezone.utc),
            ),
            organization_id=self.organization.id,
            user_id=self.user.id,
        )

        self.assertEqual(response.state, "RESTORED")
        self.assertFalse(response.visible)
        self.assertEqual(total, 0)

    def test_dismiss_tombstone_survives_canonical_row_recreation(self):
        collect_opening_results(self.db, self.request, self.make_client())
        original_round = self.db.scalar(select(BidOpeningRoundModel))
        dismiss_result(
            self.db,
            organization_id=self.organization.id,
            user_id=self.user.id,
            result_id=original_round.id,
        )
        for entry in self.db.scalars(
            select(BidOpeningEntryModel).where(
                BidOpeningEntryModel.round_id == original_round.id
            )
        ):
            self.db.delete(entry)
        for match in self.db.scalars(
            select(OrganizationOpeningResultMatchModel).where(
                OrganizationOpeningResultMatchModel.round_id == original_round.id
            )
        ):
            self.db.delete(match)
        for match in self.db.scalars(
            select(UserOpeningResultMatchModel).where(
                UserOpeningResultMatchModel.round_id == original_round.id
            )
        ):
            self.db.delete(match)
        self.db.delete(original_round)
        self.db.commit()

        collect_opening_results(self.db, self.request, self.make_client())
        recreated_round = self.db.scalar(select(BidOpeningRoundModel))
        rows, total = list_opening_results(
            self.db,
            OpeningResultListQuery(
                opened_from=datetime(2026, 7, 14, tzinfo=timezone.utc),
                opened_to=datetime(2026, 7, 16, tzinfo=timezone.utc),
            ),
            organization_id=self.organization.id,
            user_id=self.user.id,
        )

        self.assertEqual(recreated_round.external_key, "SERVICE|R26BK00000001|0|0|0")
        self.assertEqual(rows, [])
        self.assertEqual(total, 0)
        self.assertEqual(
            self.db.scalar(select(UserOpeningResultStateModel.state)),
            "DISMISSED",
        )

    def test_successful_shared_sheet_export_hides_result_for_whole_organization(self):
        teammate = UserModel(
            username="sheet-teammate",
            password_salt="salt",
            password_hash="hash",
            role="viewer",
            is_active=True,
        )
        self.db.add(teammate)
        self.db.flush()
        self.db.add(
            OrganizationMemberModel(
                organization_id=self.organization.id,
                user_id=teammate.id,
                role="member",
                is_active=True,
            )
        )
        self.add_user_result_profile(teammate.id)
        self.db.commit()
        collect_opening_results(self.db, self.request, self.make_client())
        result_id = self.db.scalar(select(BidOpeningRoundModel.id))
        self.add_bid_notice()
        writer = Mock()
        writer.upsert.return_value = SheetUpsertResult(inserted_count=1, updated_count=0)
        preview = self.preview_sheet_export([result_id], self.destination.id)
        with patch(
            "app.g2b.opening_results.router.GoogleSheetWriter.from_env",
            return_value=writer,
        ):
            response = export_results_sheet(
                ExportOpeningResultsSheetRequest(
                    result_ids=[result_id],
                    destination_id=self.destination.id,
                    dry_run=False,
                    expected_preview_token=preview.preview_token,
                ),
                auth=self.auth,
                db=self.db,
            )

        query = OpeningResultListQuery(
            opened_from=datetime(2026, 7, 14, tzinfo=timezone.utc),
            opened_to=datetime(2026, 7, 16, tzinfo=timezone.utc),
        )
        _, my_total = list_opening_results(
            self.db,
            query,
            organization_id=self.organization.id,
            user_id=self.user.id,
        )
        _, teammate_total = list_opening_results(
            self.db,
            query,
            organization_id=self.organization.id,
            user_id=teammate.id,
        )
        export_record = self.db.scalar(select(SheetExportModel))
        archive = fetch_archived_results(
            page=1,
            page_size=30,
            auth=self.auth,
            db=self.db,
        )
        teammate_archive = fetch_archived_results(
            page=1,
            page_size=30,
            auth={
                **self.auth,
                "user_id": teammate.id,
                "username": teammate.username,
                "role": teammate.role,
                "organization_role": "member",
            },
            db=self.db,
        )

        self.assertTrue(response.written)
        self.assertEqual(my_total, 0)
        self.assertEqual(teammate_total, 0)
        self.assertEqual(export_record.status, "SUCCEEDED")
        self.assertEqual(
            self.db.scalar(select(UserOpeningResultStateModel.state)),
            "EXPORTED",
        )
        self.assertEqual(archive.total, 1)
        self.assertEqual(archive.items[0].handled_state, "EXPORTED")
        self.assertFalse(archive.items[0].can_restore)
        self.assertEqual(teammate_archive.total, 1)
        self.assertEqual(teammate_archive.items[0].handled_state, "EXPORTED")
        self.assertFalse(teammate_archive.items[0].can_restore)

        teammate_profile = self.db.scalar(
            select(UserResultProfileModel).where(
                UserResultProfileModel.user_id == teammate.id
            )
        )
        teammate_profile.enabled = False
        sync_user_matches(
            self.db,
            organization_id=self.organization.id,
            user_id=teammate.id,
        )
        self.db.commit()
        teammate_archive_after_profile_change = fetch_archived_results(
            page=1,
            page_size=30,
            auth={
                **self.auth,
                "user_id": teammate.id,
                "username": teammate.username,
                "role": teammate.role,
                "organization_role": "member",
            },
            db=self.db,
        )

        self.assertEqual(teammate_archive_after_profile_change.total, 1)
        self.assertEqual(
            teammate_archive_after_profile_change.items[0].handled_state,
            "EXPORTED",
        )

    def test_shared_sheet_export_stays_hidden_after_next_collection_slot(self):
        teammate = UserModel(
            username="scheduled-export-teammate",
            password_salt="salt",
            password_hash="hash",
            role="viewer",
            is_active=True,
        )
        self.db.add(teammate)
        self.db.flush()
        self.db.add(
            OrganizationMemberModel(
                organization_id=self.organization.id,
                user_id=teammate.id,
                role="member",
                is_active=True,
            )
        )
        self.db.commit()
        client = self.make_client()
        first_now = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)
        run_scheduled_opening_results(self.db, now=first_now, client=client)
        result_id = self.db.scalar(select(BidOpeningRoundModel.id))
        self.add_bid_notice()
        preview = self.preview_sheet_export([result_id], self.destination.id)
        writer = Mock()
        writer.upsert.return_value = SheetUpsertResult(inserted_count=1, updated_count=0)
        with patch(
            "app.g2b.opening_results.router.GoogleSheetWriter.from_env",
            return_value=writer,
        ):
            export_results_sheet(
                ExportOpeningResultsSheetRequest(
                    result_ids=[result_id],
                    destination_id=self.destination.id,
                    dry_run=False,
                    expected_preview_token=preview.preview_token,
                ),
                auth=self.auth,
                db=self.db,
            )

        second = run_scheduled_opening_results(
            self.db,
            now=first_now + timedelta(hours=3),
            client=client,
        )
        query = OpeningResultListQuery(
            opened_from=datetime(2026, 7, 14, tzinfo=timezone.utc),
            opened_to=datetime(2026, 7, 16, tzinfo=timezone.utc),
        )
        _, admin_total = list_opening_results(
            self.db,
            query,
            organization_id=self.organization.id,
            user_id=self.user.id,
        )
        _, teammate_total = list_opening_results(
            self.db,
            query,
            organization_id=self.organization.id,
            user_id=teammate.id,
        )

        self.assertFalse(second.skipped_existing_run)
        self.assertEqual(second.inserted_round_count, 0)
        self.assertEqual(
            self.db.scalar(select(func.count(BidOpeningRoundModel.id))),
            1,
        )
        self.assertEqual(admin_total, 0)
        self.assertEqual(teammate_total, 0)

    def test_shared_sheet_export_does_not_hide_same_result_from_another_organization(
        self,
    ):
        other_organization = OrganizationModel(
            name="다른 매칭 조직",
            slug="other-matched-organization",
        )
        other_user = UserModel(
            username="other-matched-user",
            password_salt="salt",
            password_hash="hash",
            role="viewer",
            is_active=True,
        )
        self.db.add_all([other_organization, other_user])
        self.db.flush()
        self.db.add_all(
            [
                OrganizationMemberModel(
                    organization_id=other_organization.id,
                    user_id=other_user.id,
                    role="member",
                    is_active=True,
                ),
                OrganizationResultProfileModel(
                    organization_id=other_organization.id,
                    enabled=True,
                    keywords="AI",
                    excluded_keywords="",
                ),
                UserResultProfileModel(
                    organization_id=other_organization.id,
                    user_id=other_user.id,
                    enabled=True,
                    keywords="AI",
                    excluded_keywords="",
                ),
            ]
        )
        self.db.commit()
        collect_opening_results(self.db, self.request, self.make_client())
        result_id = self.db.scalar(select(BidOpeningRoundModel.id))
        self.add_bid_notice()
        preview = self.preview_sheet_export([result_id], self.destination.id)
        writer = Mock()
        writer.upsert.return_value = SheetUpsertResult(inserted_count=1, updated_count=0)
        with patch(
            "app.g2b.opening_results.router.GoogleSheetWriter.from_env",
            return_value=writer,
        ):
            export_results_sheet(
                ExportOpeningResultsSheetRequest(
                    result_ids=[result_id],
                    destination_id=self.destination.id,
                    dry_run=False,
                    expected_preview_token=preview.preview_token,
                ),
                auth=self.auth,
                db=self.db,
            )

        query = OpeningResultListQuery(
            opened_from=datetime(2026, 7, 14, tzinfo=timezone.utc),
            opened_to=datetime(2026, 7, 16, tzinfo=timezone.utc),
        )
        _, first_organization_total = list_opening_results(
            self.db,
            query,
            organization_id=self.organization.id,
            user_id=self.user.id,
        )
        other_rows, other_organization_total = list_opening_results(
            self.db,
            query,
            organization_id=other_organization.id,
            user_id=other_user.id,
        )

        self.assertEqual(first_organization_total, 0)
        self.assertEqual(other_organization_total, 1)
        self.assertEqual(other_rows[0].id, result_id)

    def test_personal_sheet_export_only_hides_current_users_copy(self):
        teammate = UserModel(
            username="personal-sheet-teammate",
            password_salt="salt",
            password_hash="hash",
            role="viewer",
            is_active=True,
        )
        self.db.add(teammate)
        self.db.flush()
        self.db.add(
            OrganizationMemberModel(
                organization_id=self.organization.id,
                user_id=teammate.id,
                role="member",
                is_active=True,
            )
        )
        self.add_user_result_profile(teammate.id)
        personal_destination = SheetDestinationModel(
            organization_id=self.organization.id,
            owner_user_id=self.user.id,
            label="내 개인 Sheet",
            spreadsheet_id="personal-sheet",
            tab_name="개찰결과",
            is_default=True,
            is_active=True,
        )
        self.db.add(personal_destination)
        self.db.commit()
        collect_opening_results(self.db, self.request, self.make_client())
        result_id = self.db.scalar(select(BidOpeningRoundModel.id))
        self.add_bid_notice()
        writer = Mock()
        writer.upsert.return_value = SheetUpsertResult(inserted_count=1, updated_count=0)
        preview = self.preview_sheet_export([result_id], personal_destination.id)
        with patch(
            "app.g2b.opening_results.router.GoogleSheetWriter.from_env",
            return_value=writer,
        ):
            export_results_sheet(
                ExportOpeningResultsSheetRequest(
                    result_ids=[result_id],
                    destination_id=personal_destination.id,
                    dry_run=False,
                    expected_preview_token=preview.preview_token,
                ),
                auth=self.auth,
                db=self.db,
            )

        query = OpeningResultListQuery(
            opened_from=datetime(2026, 7, 14, tzinfo=timezone.utc),
            opened_to=datetime(2026, 7, 16, tzinfo=timezone.utc),
        )
        _, my_total = list_opening_results(
            self.db,
            query,
            organization_id=self.organization.id,
            user_id=self.user.id,
        )
        _, teammate_total = list_opening_results(
            self.db,
            query,
            organization_id=self.organization.id,
            user_id=teammate.id,
        )
        self.assertEqual(my_total, 0)
        self.assertEqual(teammate_total, 1)

    def test_personal_sheet_export_tombstone_survives_canonical_row_recreation(self):
        teammate = UserModel(
            username="personal-recreation-teammate",
            password_salt="salt",
            password_hash="hash",
            role="viewer",
            is_active=True,
        )
        self.db.add(teammate)
        self.db.flush()
        self.db.add(
            OrganizationMemberModel(
                organization_id=self.organization.id,
                user_id=teammate.id,
                role="member",
                is_active=True,
            )
        )
        self.add_user_result_profile(teammate.id)
        personal_destination = SheetDestinationModel(
            organization_id=self.organization.id,
            owner_user_id=self.user.id,
            label="재생성 검증 개인 Sheet",
            spreadsheet_id="personal-recreation-sheet",
            tab_name="개찰결과",
            is_default=True,
            is_active=True,
        )
        self.db.add(personal_destination)
        self.db.commit()
        collect_opening_results(self.db, self.request, self.make_client())
        original_round = self.db.scalar(select(BidOpeningRoundModel))
        self.add_bid_notice()
        preview = self.preview_sheet_export([original_round.id], personal_destination.id)
        writer = Mock()
        writer.upsert.return_value = SheetUpsertResult(inserted_count=1, updated_count=0)
        with patch(
            "app.g2b.opening_results.router.GoogleSheetWriter.from_env",
            return_value=writer,
        ):
            export_results_sheet(
                ExportOpeningResultsSheetRequest(
                    result_ids=[original_round.id],
                    destination_id=personal_destination.id,
                    dry_run=False,
                    expected_preview_token=preview.preview_token,
                ),
                auth=self.auth,
                db=self.db,
            )
        for entry in self.db.scalars(
            select(BidOpeningEntryModel).where(
                BidOpeningEntryModel.round_id == original_round.id
            )
        ):
            self.db.delete(entry)
        for match in self.db.scalars(
            select(OrganizationOpeningResultMatchModel).where(
                OrganizationOpeningResultMatchModel.round_id == original_round.id
            )
        ):
            self.db.delete(match)
        for match in self.db.scalars(
            select(UserOpeningResultMatchModel).where(
                UserOpeningResultMatchModel.round_id == original_round.id
            )
        ):
            self.db.delete(match)
        self.db.delete(original_round)
        self.db.commit()

        collect_opening_results(self.db, self.request, self.make_client())
        recreated_round = self.db.scalar(select(BidOpeningRoundModel))
        query = OpeningResultListQuery(
            opened_from=datetime(2026, 7, 14, tzinfo=timezone.utc),
            opened_to=datetime(2026, 7, 16, tzinfo=timezone.utc),
        )
        _, owner_total = list_opening_results(
            self.db,
            query,
            organization_id=self.organization.id,
            user_id=self.user.id,
        )
        teammate_rows, teammate_total = list_opening_results(
            self.db,
            query,
            organization_id=self.organization.id,
            user_id=teammate.id,
        )

        self.assertIsNot(recreated_round, original_round)
        self.assertEqual(recreated_round.external_key, original_round.external_key)
        self.assertEqual(owner_total, 0)
        self.assertEqual(teammate_total, 1)
        self.assertEqual(teammate_rows[0].id, recreated_round.id)

    def test_member_personal_sheet_write_hides_only_members_copy(self):
        teammate = UserModel(
            username="personal-sheet-member-writer",
            password_salt="salt",
            password_hash="hash",
            role="viewer",
            is_active=True,
        )
        self.db.add(teammate)
        self.db.flush()
        self.db.add(
            OrganizationMemberModel(
                organization_id=self.organization.id,
                user_id=teammate.id,
                role="member",
                is_active=True,
            )
        )
        self.add_user_result_profile(teammate.id)
        personal_destination = SheetDestinationModel(
            organization_id=self.organization.id,
            owner_user_id=teammate.id,
            label="구성원 개인 Sheet",
            spreadsheet_id="member-writer-personal-sheet",
            tab_name="개찰결과",
            is_default=True,
            is_active=True,
        )
        self.db.add(personal_destination)
        self.db.commit()
        collect_opening_results(self.db, self.request, self.make_client())
        result_id = self.db.scalar(select(BidOpeningRoundModel.id))
        self.add_bid_notice()
        member_auth = {
            "user_id": teammate.id,
            "username": teammate.username,
            "role": teammate.role,
            "organization_id": self.organization.id,
            "organization_name": self.organization.name,
            "organization_role": "member",
        }
        preview = export_results_sheet(
            ExportOpeningResultsSheetRequest(
                result_ids=[result_id],
                destination_id=personal_destination.id,
            ),
            auth=member_auth,
            db=self.db,
        )
        writer = Mock()
        writer.upsert.return_value = SheetUpsertResult(inserted_count=1, updated_count=0)

        with patch(
            "app.g2b.opening_results.router.GoogleSheetWriter.from_env",
            return_value=writer,
        ):
            response = export_results_sheet(
                ExportOpeningResultsSheetRequest(
                    result_ids=[result_id],
                    destination_id=personal_destination.id,
                    dry_run=False,
                    expected_preview_token=preview.preview_token,
                ),
                auth=member_auth,
                db=self.db,
            )

        query = OpeningResultListQuery(
            opened_from=datetime(2026, 7, 14, tzinfo=timezone.utc),
            opened_to=datetime(2026, 7, 16, tzinfo=timezone.utc),
        )
        _, admin_total = list_opening_results(
            self.db,
            query,
            organization_id=self.organization.id,
            user_id=self.user.id,
        )
        _, member_total = list_opening_results(
            self.db,
            query,
            organization_id=self.organization.id,
            user_id=teammate.id,
        )
        export_record = self.db.scalar(select(SheetExportModel))

        self.assertTrue(response.written)
        self.assertEqual(admin_total, 1)
        self.assertEqual(member_total, 0)
        self.assertEqual(export_record.exported_by_user_id, teammate.id)

    def test_sheet_failure_keeps_result_visible_and_retry_can_succeed(self):
        collect_opening_results(self.db, self.request, self.make_client())
        result_id = self.db.scalar(select(BidOpeningRoundModel.id))
        self.add_bid_notice()
        preview = self.preview_sheet_export([result_id], self.destination.id)
        failing_writer = Mock()
        failing_writer.upsert.side_effect = RuntimeError("temporary Google failure")
        with patch(
            "app.g2b.opening_results.router.GoogleSheetWriter.from_env",
            return_value=failing_writer,
        ):
            with self.assertRaises(HTTPException) as raised:
                export_results_sheet(
                    ExportOpeningResultsSheetRequest(
                        result_ids=[result_id],
                        destination_id=self.destination.id,
                        dry_run=False,
                        expected_preview_token=preview.preview_token,
                    ),
                    auth=self.auth,
                    db=self.db,
                )
        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(self.db.scalar(select(SheetExportModel.status)), "FAILED")
        self.assertIsNone(self.db.scalar(select(UserOpeningResultStateModel.id)))
        self.assertIsNone(
            self.db.get(SheetDestinationModel, self.destination.id).export_lock_token
        )
        query = OpeningResultListQuery(
            opened_from=datetime(2026, 7, 14, tzinfo=timezone.utc),
            opened_to=datetime(2026, 7, 16, tzinfo=timezone.utc),
        )
        _, visible_after_failure = list_opening_results(
            self.db,
            query,
            organization_id=self.organization.id,
            user_id=self.user.id,
        )
        collect_opening_results(self.db, self.request, self.make_client())
        _, visible_after_recollection = list_opening_results(
            self.db,
            query,
            organization_id=self.organization.id,
            user_id=self.user.id,
        )
        self.assertEqual(visible_after_failure, 1)
        self.assertEqual(visible_after_recollection, 1)

        successful_writer = Mock()
        successful_writer.upsert.return_value = SheetUpsertResult(
            inserted_count=1,
            updated_count=0,
        )
        with patch(
            "app.g2b.opening_results.router.GoogleSheetWriter.from_env",
            return_value=successful_writer,
        ):
            response = export_results_sheet(
                ExportOpeningResultsSheetRequest(
                    result_ids=[result_id],
                    destination_id=self.destination.id,
                    dry_run=False,
                    expected_preview_token=preview.preview_token,
                ),
                auth=self.auth,
                db=self.db,
            )
        self.assertTrue(response.written)
        self.assertEqual(self.db.scalar(select(SheetExportModel.status)), "SUCCEEDED")
        _, visible_after_retry = list_opening_results(
            self.db,
            query,
            organization_id=self.organization.id,
            user_id=self.user.id,
        )
        self.assertEqual(visible_after_retry, 0)

    def test_same_sheet_destination_allows_only_one_export_batch_at_a_time(self):
        first = self.completed_summary()
        second = self.completed_summary()
        second["bidNtceNo"] = "R26BK00000002"
        second["bidNtceNm"] = "AI 두 번째 사업"
        collect_opening_results(
            self.db,
            self.request,
            StubOpeningResultClient([first, second]),
        )
        rounds = list(
            self.db.scalars(
                select(BidOpeningRoundModel).order_by(BidOpeningRoundModel.id)
            )
        )
        first_claim = claim_sheet_exports(
            self.db,
            destination=self.destination,
            organization_id=self.organization.id,
            user_id=self.user.id,
            rounds=[rounds[0]],
        )
        with self.assertRaises(SheetExportConflictError):
            claim_sheet_exports(
                self.db,
                destination=self.destination,
                organization_id=self.organization.id,
                user_id=self.user.id,
                rounds=[rounds[1]],
            )

        fail_sheet_exports(
            self.db,
            claim_batch=first_claim,
            error_message="test cleanup",
        )
        self.assertIsNone(
            self.db.get(SheetDestinationModel, self.destination.id).export_lock_token
        )

    def test_existing_sheet_destination_cannot_change_identity_or_scope(self):
        with self.assertRaises(SheetDestinationConflictError):
            save_sheet_destination(
                self.db,
                organization_id=self.organization.id,
                user_id=self.user.id,
                destination_id=self.destination.id,
                label="범위 변경 시도",
                spreadsheet_id=self.destination.spreadsheet_id,
                tab_name=self.destination.tab_name,
                scope="PERSONAL",
                is_default=True,
                can_manage_organization=True,
            )

        self.db.refresh(self.destination)
        self.assertIsNone(self.destination.owner_user_id)

    def test_same_physical_sheet_cannot_be_registered_with_personal_alias(self):
        with self.assertRaises(SheetDestinationConflictError):
            save_sheet_destination(
                self.db,
                organization_id=self.organization.id,
                user_id=self.user.id,
                destination_id=None,
                label="공용 Sheet의 개인 별칭",
                spreadsheet_id=self.destination.spreadsheet_id,
                tab_name=self.destination.tab_name,
                scope="PERSONAL",
                is_default=True,
                can_manage_organization=True,
            )

    def test_same_physical_sheet_cannot_be_registered_by_another_organization(self):
        other_organization = OrganizationModel(name="다른 조직", slug="sheet-conflict-org")
        other_user = UserModel(
            username="sheet-conflict-user",
            password_salt="salt",
            password_hash="hash",
            role="admin",
            is_active=True,
        )
        self.db.add_all([other_organization, other_user])
        self.db.flush()

        with self.assertRaises(SheetDestinationConflictError):
            save_sheet_destination(
                self.db,
                organization_id=other_organization.id,
                user_id=other_user.id,
                destination_id=None,
                label="다른 조직의 중복 Sheet",
                spreadsheet_id=self.destination.spreadsheet_id,
                tab_name=self.destination.tab_name,
                scope="ORGANIZATION",
                is_default=True,
                can_manage_organization=True,
            )

    def test_existing_sheet_destination_can_update_label_without_duplication(self):
        updated = save_sheet_destination(
            self.db,
            organization_id=self.organization.id,
            user_id=self.user.id,
            destination_id=self.destination.id,
            label="이름만 변경한 공용 Sheet",
            spreadsheet_id=self.destination.spreadsheet_id,
            tab_name=self.destination.tab_name,
            scope="ORGANIZATION",
            is_default=True,
            can_manage_organization=True,
        )

        self.assertEqual(updated.id, self.destination.id)
        self.assertEqual(updated.label, "이름만 변경한 공용 Sheet")
        self.assertEqual(
            self.db.scalar(select(func.count(SheetDestinationModel.id))),
            1,
        )

    def test_sheet_destination_save_normalizes_full_google_sheet_url(self):
        spreadsheet_id = "1AbC_new-personal-sheet"
        destination = save_sheet_destination(
            self.db,
            organization_id=self.organization.id,
            user_id=self.user.id,
            destination_id=None,
            label="URL로 추가한 개인 Sheet",
            spreadsheet_id=(
                f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid=0"
            ),
            tab_name="개인개찰결과",
            scope="PERSONAL",
            is_default=True,
            can_manage_organization=True,
        )

        self.assertEqual(destination.spreadsheet_id, spreadsheet_id)
        self.assertEqual(destination.owner_user_id, self.user.id)

    def test_sheet_target_verification_blocks_another_users_personal_destination(self):
        other_user = UserModel(
            username="verify-owner",
            password_salt="salt",
            password_hash="hash",
            role="viewer",
            is_active=True,
        )
        self.db.add(other_user)
        self.db.flush()
        destination = SheetDestinationModel(
            organization_id=self.organization.id,
            owner_user_id=other_user.id,
            label="다른 사용자 개인 Sheet",
            spreadsheet_id="private-sheet-id",
            tab_name="개찰결과",
            is_active=True,
        )
        self.db.add(destination)
        self.db.commit()

        with self.assertRaises(SheetDestinationAccessError):
            ensure_sheet_target_access(
                self.db,
                organization_id=self.organization.id,
                user_id=self.user.id,
                spreadsheet_id=destination.spreadsheet_id,
                tab_name=destination.tab_name,
            )
        ensure_sheet_target_access(
            self.db,
            organization_id=self.organization.id,
            user_id=other_user.id,
            spreadsheet_id=destination.spreadsheet_id,
            tab_name=destination.tab_name,
        )

    def test_deleted_sheet_destination_can_be_reactivated_without_new_identity(self):
        original_id = self.destination.id
        deactivate_sheet_destination(
            self.db,
            organization_id=self.organization.id,
            user_id=self.user.id,
            destination_id=original_id,
            can_manage_organization=True,
        )

        reactivated = save_sheet_destination(
            self.db,
            organization_id=self.organization.id,
            user_id=self.user.id,
            destination_id=None,
            label="다시 연결한 공용 Sheet",
            spreadsheet_id=self.destination.spreadsheet_id,
            tab_name=self.destination.tab_name,
            scope="ORGANIZATION",
            is_default=True,
            can_manage_organization=True,
        )

        self.assertEqual(reactivated.id, original_id)
        self.assertTrue(reactivated.is_active)

    def test_expired_sheet_lock_cannot_be_completed_by_previous_worker(self):
        collect_opening_results(self.db, self.request, self.make_client())
        round_row = self.db.scalar(select(BidOpeningRoundModel))
        claim = claim_sheet_exports(
            self.db,
            destination=self.destination,
            organization_id=self.organization.id,
            user_id=self.user.id,
            rounds=[round_row],
        )
        self.db.execute(
            update(SheetDestinationModel)
            .where(SheetDestinationModel.id == self.destination.id)
            .values(export_lock_token="replacement-worker-token")
        )
        self.db.commit()

        with self.assertRaises(SheetExportConflictError):
            complete_sheet_exports(
                self.db,
                claim_batch=claim,
                organization_id=self.organization.id,
                user_id=self.user.id,
            )

        self.assertEqual(self.db.scalar(select(SheetExportModel.status)), "PENDING")
        self.assertIsNone(self.db.scalar(select(UserOpeningResultStateModel.id)))

    def test_deactivated_sheet_destination_cannot_be_claimed_for_export(self):
        collect_opening_results(self.db, self.request, self.make_client())
        round_row = self.db.scalar(select(BidOpeningRoundModel))
        destination_id = self.destination.id
        deactivate_sheet_destination(
            self.db,
            organization_id=self.organization.id,
            user_id=self.user.id,
            destination_id=destination_id,
            can_manage_organization=True,
        )

        with self.assertRaises(SheetExportConflictError):
            claim_sheet_exports(
                self.db,
                destination=self.destination,
                organization_id=self.organization.id,
                user_id=self.user.id,
                rounds=[round_row],
            )

        self.assertIsNone(self.db.scalar(select(SheetExportModel.id)))

    def test_status_mapping(self):
        self.assertEqual(normalize_status("개찰완료"), OpeningStatus.OPENED)
        self.assertEqual(normalize_status("최종낙찰"), OpeningStatus.AWARDED)
        self.assertEqual(normalize_status("유찰"), OpeningStatus.FAILED)
        self.assertEqual(normalize_status("재입찰"), OpeningStatus.REBID)

    def test_collection_window_normalizes_naive_datetime(self):
        request = CollectOpeningResultsRequest(
            start_at=datetime(2026, 7, 14),
            end_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
        )

        self.assertIsNotNone(request.start_at.tzinfo)
        self.assertIsNotNone(request.end_at.tzinfo)


class OpeningResultRouterTests(unittest.TestCase):
    def test_router_is_registered(self):
        from main import app

        paths = {route.path for route in app.routes}
        self.assertIn("/api/v1/results", paths)
        self.assertIn("/api/v1/results/internal/collect", paths)
        self.assertIn("/api/v1/results/internal/enrich-context", paths)
        self.assertIn("/api/v1/results/export/sheet", paths)
        self.assertIn("/api/v1/results/archive", paths)
        self.assertIn("/api/v1/results/archive/{result_id}", paths)
        self.assertIn("/api/v1/results/sheet-destinations/verify", paths)
        self.assertIn("/api/v1/results/{result_id}/restore", paths)
        self.assertIn("/api/v1/results/{result_id}", paths)

    def test_spreadsheet_url_is_normalized_to_id(self):
        spreadsheet_id = "1AbC_test-sheet-id"
        self.assertEqual(
            normalize_spreadsheet_id(
                f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid=0"
            ),
            spreadsheet_id,
        )
        self.assertEqual(normalize_spreadsheet_id(spreadsheet_id), spreadsheet_id)
        with self.assertRaises(ValueError):
            normalize_spreadsheet_id("https://example.com/not-a-sheet")

    def test_sheet_connection_verification_is_read_only(self):
        service = FakeSheetService(header=SHEET_HEADERS)
        writer = GoogleSheetWriter("sheet-id", "개찰결과", service=service)

        verification = writer.verify_connection()

        self.assertEqual(verification.spreadsheet_title, "개찰결과 테스트")
        self.assertTrue(verification.tab_exists)
        self.assertEqual(verification.header_status, "MATCH")
        self.assertEqual(
            [call[0] for call in service.calls],
            ["getSpreadsheet", "get"],
        )

        legacy_service = FakeSheetService(header=LEGACY_SHEET_HEADERS)
        legacy_verification = GoogleSheetWriter(
            "sheet-id",
            "개찰결과",
            service=legacy_service,
        ).verify_connection()
        self.assertEqual(legacy_verification.header_status, "MATCH")
        self.assertEqual(
            [call[0] for call in legacy_service.calls],
            ["getSpreadsheet", "get"],
        )

    def test_sheet_connection_verification_reports_empty_mismatch_and_missing_tab(self):
        empty = GoogleSheetWriter(
            "sheet-id",
            "개찰결과",
            service=FakeSheetService(header=[]),
        ).verify_connection()
        mismatch = GoogleSheetWriter(
            "sheet-id",
            "개찰결과",
            service=FakeSheetService(header=["다른", "헤더"]),
        ).verify_connection()
        missing_service = FakeSheetService(tab_names=["다른탭"])
        missing = GoogleSheetWriter(
            "sheet-id",
            "개찰결과",
            service=missing_service,
        ).verify_connection()

        self.assertEqual(empty.header_status, "EMPTY")
        self.assertEqual(mismatch.header_status, "MISMATCH")
        self.assertFalse(missing.tab_exists)
        self.assertEqual(missing.header_status, "NOT_CHECKED")
        self.assertEqual([call[0] for call in missing_service.calls], ["getSpreadsheet"])

    def test_verify_sheet_destination_returns_normalized_readiness(self):
        spreadsheet_id = "1AbC_test-sheet-id"
        writer = GoogleSheetWriter(
            spreadsheet_id,
            "개찰결과",
            service=FakeSheetService(header=[]),
        )
        with patch(
            "app.g2b.opening_results.router.GoogleSheetWriter.from_env",
            return_value=writer,
        ), patch(
            "app.g2b.opening_results.router.get_sheet_service_account_email",
            return_value="sheet-writer@icore-test.iam.gserviceaccount.com",
        ), patch(
            "app.g2b.opening_results.router.ensure_sheet_target_access",
        ):
            response = verify_sheet_destination(
                SheetDestinationVerifyRequest(
                    spreadsheet_id=(
                        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
                    ),
                    tab_name="개찰결과",
                ),
                auth={"organization_id": 1, "user_id": 1},
                db=Mock(),
            )

        self.assertEqual(response.spreadsheet_id, spreadsheet_id)
        self.assertEqual(response.header_status, "EMPTY")
        self.assertTrue(response.connection_ready)
        self.assertEqual(
            response.sheet_service_account_email,
            "sheet-writer@icore-test.iam.gserviceaccount.com",
        )

    def test_sheet_writer_updates_and_inserts_selected_rows_in_one_batch(self):
        service = FakeSheetService(
            existing_rows=[["R26BK00000001", "기존 사업명"]]
        )
        writer = GoogleSheetWriter("sheet-id", "개찰결과", service=service)
        existing_row = ["R26BK00000001", "수정 사업명", *("" for _ in range(15))]
        new_row = ["R26BK00000002", "신규 사업명", *("" for _ in range(15))]

        result = writer.upsert([existing_row, new_row])

        self.assertEqual(result.updated_count, 1)
        self.assertEqual(result.inserted_count, 1)
        self.assertEqual(
            [call[0] for call in service.calls],
            [
                "get",
                "get",
                "getSpreadsheet",
                "batchUpdate",
                "formatBatchUpdate",
            ],
        )
        self.assertNotIn("clear", [call[0] for call in service.calls])
        value_call = next(
            call for call in service.calls if call[0] == "batchUpdate"
        )
        self.assertEqual(
            value_call[1]["body"]["data"][0]["range"],
            "'개찰결과'!A2:Q2",
        )
        self.assertEqual(
            value_call[1]["body"]["data"][1]["range"],
            "'개찰결과'!A3:Q3",
        )

    def test_sheet_writer_formats_business_amount_with_thousands_separator(self):
        service = FakeSheetService()
        writer = GoogleSheetWriter("sheet-id", "개찰결과", service=service)
        row = [
            "R26BK00000001",
            "사업명",
            "발주처",
            100000,
            *("" for _ in range(13)),
        ]

        writer.upsert([row])

        value_call = next(
            call for call in service.calls if call[0] == "batchUpdate"
        )
        self.assertEqual(
            value_call[1]["body"]["data"][0]["values"][0][3],
            100000,
        )
        format_call = next(
            call for call in service.calls if call[0] == "formatBatchUpdate"
        )
        request = format_call[1]["body"]["requests"][0]["repeatCell"]
        self.assertEqual(
            request["range"],
            {
                "sheetId": 1,
                "startRowIndex": 1,
                "startColumnIndex": 3,
                "endColumnIndex": 4,
            },
        )
        self.assertEqual(
            request["cell"]["userEnteredFormat"]["numberFormat"],
            {"type": "NUMBER", "pattern": "#,##0"},
        )

    def test_sheet_writer_migrates_legacy_base_amount_header(self):
        service = FakeSheetService(header=LEGACY_SHEET_HEADERS)
        writer = GoogleSheetWriter("sheet-id", "개찰결과", service=service)
        row = ["R26BK00000001", "사업명", *("" for _ in range(15))]

        result = writer.upsert([row])

        self.assertEqual(result.inserted_count, 1)
        self.assertEqual(
            [call[0] for call in service.calls],
            [
                "get",
                "update",
                "get",
                "getSpreadsheet",
                "batchUpdate",
                "formatBatchUpdate",
            ],
        )
        self.assertEqual(
            service.calls[1][1]["body"]["values"],
            [SHEET_HEADERS],
        )

    def test_sheet_writer_rejects_duplicate_existing_notice_before_batch(self):
        service = FakeSheetService(
            existing_rows=[
                ["R26BK00000001", "기존 사업명"],
                ["R26BK00000001", "중복 사업명"],
            ]
        )
        writer = GoogleSheetWriter("sheet-id", "개찰결과", service=service)
        row = ["R26BK00000001", "수정 사업명", *("" for _ in range(15))]

        with self.assertRaises(SheetExportConfigurationError):
            writer.upsert([row])

        self.assertEqual([call[0] for call in service.calls], ["get", "get"])


if __name__ == "__main__":
    unittest.main()
