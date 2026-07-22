import unittest
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.data.models import Base, OrganizationModel, UserModel
from app.g2b.pre_specifications.client import (
    PreSpecificationApiClient,
    PreSpecificationApiConfig,
    normalize_source_item,
)
from app.g2b.pre_specifications.models import PreSpecificationModel, UserPreSpecificationStateModel
from app.g2b.pre_specifications.schemas import PreSpecificationListQuery
from app.g2b.pre_specifications.service import (
    archive_pre_specifications,
    deadline_status,
    list_archived_pre_specifications,
    list_pre_specifications,
    mark_exported,
    restore_removed_sheet_pre_specifications,
    restore_archived_pre_specifications,
    upsert_pre_specifications,
)
from app.g2b.pre_specifications.sheet_export import SHEET_HEADERS, build_rows


KST = ZoneInfo("Asia/Seoul")


class PreSpecificationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.organization = OrganizationModel(name="Test", slug="test")
        self.user = UserModel(username="tester", password_salt="salt", password_hash="hash")
        self.db.add_all([self.organization, self.user])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_source_payload_maps_to_stable_contract(self):
        item = normalize_source_item({"bfSpecRgstNo": "R001", "prdctClsfcNoNm": "AI 교육", "asignBdgtAmt": "1,000", "rgstDt": "202607201030", "opninRgstClseDt": "202607211200", "specDocFileUrl1": "https://example.test/file"})
        self.assertEqual(item["bf_spec_rgst_no"], "R001")
        self.assertEqual(item["allocated_budget"], Decimal("1000"))
        self.assertEqual(len(item["attachments"]), 1)

    def test_upsert_preserves_source_identity_and_records_snapshot(self):
        inserted, updated = upsert_pre_specifications(self.db, [{"bf_spec_rgst_no": "R001", "business_name": "클라우드 교육", "demand_agency_name": "교육청", "allocated_budget": 1000, "registered_at": "202607201030", "raw": {"bfSpecRgstNo": "R001", "version": 1}}])
        self.assertEqual((inserted, updated), (1, 0))
        inserted, updated = upsert_pre_specifications(self.db, [{"bf_spec_rgst_no": "R001", "business_name": "클라우드 교육 변경", "raw": {"bfSpecRgstNo": "R001", "version": 2}}])
        self.assertEqual((inserted, updated), (0, 1))
        self.assertEqual(self.db.get(PreSpecificationModel, "R001").business_name, "클라우드 교육 변경")

    def test_list_supports_and_keywords_exclusion_and_exported_state(self):
        upsert_pre_specifications(self.db, [
            {"bf_spec_rgst_no": "R001", "business_name": "AI 교육 연수", "demand_agency_name": "A교육청"},
            {"bf_spec_rgst_no": "R002", "business_name": "AI 연수구 홍보", "demand_agency_name": "B구청"},
        ])
        query = PreSpecificationListQuery(keywords=["AI", "교육"], keyword_mode="AND", excluded_keywords=["연수구"])
        rows, total, _ = list_pre_specifications(self.db, query, organization_id=self.organization.id, user_id=self.user.id)
        self.assertEqual(total, 1)
        self.assertEqual(rows[0].bf_spec_rgst_no, "R001")
        mark_exported(self.db, organization_id=self.organization.id, user_id=self.user.id, ids=["R001"])
        rows, total, exported = list_pre_specifications(self.db, query, organization_id=self.organization.id, user_id=self.user.id)
        self.assertEqual(total, 0)
        self.assertEqual(exported, {"R001"})

    def test_deadline_and_sheet_rows_are_kst_safe(self):
        row = PreSpecificationModel(bf_spec_rgst_no="R001", business_name="교육", allocated_budget=Decimal("1000"), opinion_deadline=datetime(2026, 7, 20, 23, 59, tzinfo=KST), attachments_json="[]")
        self.assertEqual(deadline_status(None), "UNKNOWN")
        values = build_rows([row])
        self.assertEqual(len(SHEET_HEADERS), len(values[0]))
        self.assertEqual(values[0][0], "R001")

    def test_archive_moves_items_out_of_work_list_and_restore_returns_them(self):
        upsert_pre_specifications(self.db, [
            {"bf_spec_rgst_no": "R001", "business_name": "AI 교육"},
            {"bf_spec_rgst_no": "R002", "business_name": "클라우드 전환"},
        ])
        query = PreSpecificationListQuery()

        updated, missing = archive_pre_specifications(
            self.db,
            organization_id=self.organization.id,
            user_id=self.user.id,
            ids=["R001", "UNKNOWN"],
        )
        self.assertEqual((updated, missing), (1, ["UNKNOWN"]))

        rows, total, _ = list_pre_specifications(self.db, query, organization_id=self.organization.id, user_id=self.user.id)
        self.assertEqual((total, [row.bf_spec_rgst_no for row in rows]), (1, ["R002"]))
        archived, archived_total = list_archived_pre_specifications(self.db, query, organization_id=self.organization.id, user_id=self.user.id)
        self.assertEqual((archived_total, [row.bf_spec_rgst_no for row in archived]), (1, ["R001"]))

        restored, missing = restore_archived_pre_specifications(
            self.db,
            organization_id=self.organization.id,
            user_id=self.user.id,
            ids=["R001"],
        )
        self.assertEqual((restored, missing), (1, []))
        rows, total, _ = list_pre_specifications(self.db, query, organization_id=self.organization.id, user_id=self.user.id)
        self.assertEqual((total, [row.bf_spec_rgst_no for row in rows]), (2, ["R002", "R001"]))

    def test_sheet_reconcile_restores_exported_item_removed_from_sheet(self):
        upsert_pre_specifications(self.db, [
            {"bf_spec_rgst_no": "R001", "business_name": "시트에 남은 사업"},
            {"bf_spec_rgst_no": "R002", "business_name": "시트에서 삭제한 사업"},
        ])
        mark_exported(self.db, organization_id=self.organization.id, user_id=self.user.id, ids=["R001", "R002"])

        restored_count = restore_removed_sheet_pre_specifications(
            self.db,
            organization_id=self.organization.id,
            user_id=self.user.id,
            sheet_ids=["R001"],
        )

        self.assertEqual(restored_count, 1)
        rows, total, exported = list_pre_specifications(
            self.db,
            PreSpecificationListQuery(include_exported=True),
            organization_id=self.organization.id,
            user_id=self.user.id,
        )
        self.assertEqual((total, [row.bf_spec_rgst_no for row in rows]), (2, ["R002", "R001"]))
        self.assertEqual(exported, {"R001"})


if __name__ == "__main__":
    unittest.main()
