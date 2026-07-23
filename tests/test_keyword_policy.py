import json
import unittest
from datetime import time
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app.data.bootstrap import ensure_schema_compatibility
from app.data.models import Base, ScraperConfigModel
from app.g2b.keyword_policy import evaluate_keyword_title, normalize_keywords
from app.schemas import ScraperConfig
from app.services.cloud_scheduler_service import _build_body
from app.g2b.bid_notices.service import (
    _fetch_g2b_notices,
    get_scraper_config,
    upsert_scraper_config,
)
from cloudrun.g2b_worker.main import NoticeRow, _fetch_g2b_rows


class FakeWorkerResponse:
    status_code = 200
    headers = {"content-type": "application/json"}
    text = "{}"
    content = b"{}"

    def __init__(self, items):
        self.items = items

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "response": {
                "header": {"resultCode": "00", "resultMsg": "OK"},
                "body": {
                    "items": {"item": self.items},
                    "totalCount": len(self.items),
                },
            }
        }


class KeywordPolicyTests(unittest.TestCase):
    def test_include_is_or_and_exclude_has_priority(self):
        keywords = ["AI", "클라우드", "연수"]
        excluded = ["연수구", "연수원"]

        self.assertTrue(
            evaluate_keyword_title("교직원 AI 활용 교육", keywords, excluded).keep
        )
        self.assertTrue(
            evaluate_keyword_title("공공 클라우드 전환", keywords, excluded).keep
        )
        self.assertTrue(
            evaluate_keyword_title("교원 직무연수 운영", keywords, excluded).keep
        )
        self.assertFalse(
            evaluate_keyword_title("인천 연수구 청사 보수", keywords, excluded).keep
        )
        self.assertFalse(
            evaluate_keyword_title("연수원 시설 개선", keywords, excluded).keep
        )
        self.assertFalse(
            evaluate_keyword_title("AI 기반 연수원 교육", keywords, excluded).keep
        )

    def test_normalization_and_case_insensitive_partial_matching(self):
        self.assertEqual(normalize_keywords([" AI ", "ai", "ＡＩ", "클라우드"]), ["AI", "클라우드"])
        self.assertTrue(evaluate_keyword_title("생성형ａｉ교육", ["AI"]).keep)
        self.assertTrue(evaluate_keyword_title("OpenAI 활용 교육", ["AI"]).keep)
        self.assertFalse(evaluate_keyword_title("maintenance 용역", ["AI"]).keep)
        self.assertFalse(evaluate_keyword_title("training 시스템", ["AI"]).keep)

    def test_api_scraper_source_setting_remains_available(self):
        with patch("app.g2b.bid_notices.service.requests.get") as mocked_get:
            mocked_get.return_value.raise_for_status.return_value = None
            mocked_get.return_value.json.return_value = [
                {"notice_id": "notice-1", "title": "AI 교육 운영"}
            ]

            notices = _fetch_g2b_notices(["AI"])

        self.assertEqual([notice.notice_id for notice in notices], ["notice-1"])

    def test_worker_filters_api_rows_before_dedup(self):
        items = [
            {"id": "1", "title": "교원 직무연수 운영"},
            {"id": "2", "title": "인천 연수구 청사 보수"},
            {"id": "3", "title": "AI 기반 연수원 시설"},
            {"id": "4", "title": "일반 시설공사"},
            {"id": "5", "title": "AI 교원교육", "agency": "인천광역시 연수구"},
        ]

        def extract(item):
            return NoticeRow(
                notice_id=item["id"],
                title=item["title"],
                agency=item.get("agency", ""),
            )

        with patch.dict(
            "os.environ",
            {"TEST_SOURCE_URL": "https://example.test", "TEST_SERVICE_KEY": "key"},
            clear=False,
        ), patch(
            "cloudrun.g2b_worker.main.requests.get",
            return_value=FakeWorkerResponse(items),
        ), patch(
            "cloudrun.g2b_worker.main._fetch_last_run_at",
            return_value=None,
        ):
            rows = _fetch_g2b_rows(
                source_url_env="TEST_SOURCE_URL",
                service_key_env="TEST_SERVICE_KEY",
                keyword_param_name="bidNtceNm",
                keywords=["AI", "연수"],
                excluded_keywords=["연수구", "연수원"],
                row_extractor=extract,
                source_label="test",
            )

        self.assertEqual([row.notice_id for row in rows], ["1", "5"])
        self.assertEqual(rows[0].matched_keyword, "연수")
        self.assertEqual(rows[1].matched_keyword, "AI")

    def test_config_persists_excluded_keywords_and_scheduler_sends_them(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            db.add(
                ScraperConfigModel(
                    enabled=True,
                    notify_times="09:00:00",
                    gsheet_ids="",
                    receiver_emails="admin@example.com",
                    keywords="기존",
                    excluded_keywords="",
                )
            )
            db.commit()
            saved = upsert_scraper_config(
                db,
                ScraperConfig(
                    enabled=True,
                    notify_times=[time(hour=9)],
                    receiver_emails=["admin@example.com"],
                    keywords=["AI", "클라우드", "연수"],
                    excluded_keywords=["연수구", "연수원"],
                ),
            )
            loaded = get_scraper_config(db)

        payload = json.loads(_build_body(saved, time(hour=9)).decode("utf-8"))
        self.assertEqual(loaded.excluded_keywords, ["연수구", "연수원"])
        self.assertEqual(payload["excluded_keywords"], ["연수구", "연수원"])
        engine.dispose()

    def test_existing_database_gets_excluded_keyword_column(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE scraper_configs ("
                    "id INTEGER PRIMARY KEY, keywords TEXT NOT NULL"
                    ")"
                )
            )

        ensure_schema_compatibility(engine)

        columns = {column["name"] for column in inspect(engine).get_columns("scraper_configs")}
        self.assertIn("excluded_keywords", columns)
        engine.dispose()

    def test_existing_notice_database_gets_sheet_context_columns(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE scraper_notices (id INTEGER PRIMARY KEY)")
            )
            connection.execute(text("INSERT INTO scraper_notices (id) VALUES (1)"))

        ensure_schema_compatibility(engine)

        columns = {
            column["name"] for column in inspect(engine).get_columns("scraper_notices")
        }
        self.assertTrue(
            {
                "bid_notice_no",
                "bid_notice_ord",
                "business_name",
                "demand_agency_name",
                "base_amount",
                "prearranged_price_decision_method",
                "proposal_deadline",
                "region_restriction",
                "is_two_stage_bid",
                "work_type",
                "procurement_type",
                "official_base_amount",
                "source_payload",
            }.issubset(columns)
        )
        with engine.connect() as connection:
            source_payload = connection.execute(
                text("SELECT source_payload FROM scraper_notices WHERE id = 1")
            ).scalar_one()
        self.assertEqual(source_payload, "{}")
        indexes = {
            index["name"] for index in inspect(engine).get_indexes("scraper_notices")
        }
        self.assertIn("ix_scraper_notices_bid_notice_no", indexes)
        engine.dispose()

    def test_existing_sheet_destination_gets_export_lock_columns(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE sheet_destinations (id INTEGER PRIMARY KEY)")
            )

        ensure_schema_compatibility(engine)

        columns = {
            column["name"] for column in inspect(engine).get_columns("sheet_destinations")
        }
        self.assertTrue(
            {"export_lock_token", "export_lock_claimed_at"}.issubset(columns)
        )
        engine.dispose()

    def test_existing_collection_run_gets_claim_token_column(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE g2b_opening_collection_runs (id INTEGER PRIMARY KEY)")
            )

        ensure_schema_compatibility(engine)

        columns = {
            column["name"]
            for column in inspect(engine).get_columns("g2b_opening_collection_runs")
        }
        self.assertIn("claim_token", columns)
        engine.dispose()

    def test_existing_opening_round_gets_entries_collected_column(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE g2b_opening_rounds (id INTEGER PRIMARY KEY)")
            )

        ensure_schema_compatibility(engine)

        columns = {
            column["name"] for column in inspect(engine).get_columns("g2b_opening_rounds")
        }
        self.assertIn("entries_collected_at", columns)
        engine.dispose()

    def test_existing_sheet_destination_gets_physical_target_unique_index(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE sheet_destinations ("
                    "id INTEGER PRIMARY KEY, "
                    "spreadsheet_id VARCHAR(240) NOT NULL, "
                    "tab_name VARCHAR(120) NOT NULL"
                    ")"
                )
            )

        ensure_schema_compatibility(engine)

        unique_columns = {
            tuple(index["column_names"])
            for index in inspect(engine).get_indexes("sheet_destinations")
            if index.get("unique")
        }
        self.assertIn(("spreadsheet_id", "tab_name"), unique_columns)
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
