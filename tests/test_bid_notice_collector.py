import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.data.models import Base, ScraperNoticeModel
from app.g2b.bid_notices.collector import collect_bid_notices
from app.g2b.bid_notices.matching import (
    sync_user_bid_notice_matches,
    update_user_bid_notice_profile,
)
from app.g2b.bid_notices.models import (
    BidNoticeCollectionRunModel,
    UserBidNoticeMatchModel,
)


class BidNoticeCollectorTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @patch("app.g2b.bid_notices.collector._fetch_operation")
    def test_collection_keeps_business_and_official_base_amount_separate(self, fetch_operation):
        fetch_operation.return_value = [
            {
                "bidNtceNo": "R26BK000001",
                "bidNtceOrd": "000",
                "bidNtceNm": "AI 교육 운영 용역",
                "dminsttNm": "테스트 교육청",
                "presmptPrce": "100000000",
                "VAT": "10000000",
                "bsisAmount": "95000000",
                "bidNtceDt": "202607231000",
                "bidClseDt": "202607301700",
                "prtcptPsblRgnNm": "서울특별시",
                "bidMethdNm": "2단계경쟁",
            }
        ]

        result = collect_bid_notices(
            self.db,
            start_date=date(2026, 7, 20),
            end_date=date(2026, 7, 23),
            business_types=["SERVICE"],
        )

        stored = self.db.scalar(select(ScraperNoticeModel))
        run = self.db.scalar(select(BidNoticeCollectionRunModel))
        self.assertEqual(result["fetched_count"], 1)
        self.assertEqual(result["inserted_count"], 1)
        self.assertEqual(stored.base_amount, Decimal("110000000"))
        self.assertEqual(stored.official_base_amount, Decimal("95000000"))
        self.assertEqual(stored.bid_notice_ord, "000")
        self.assertEqual(stored.work_type, "용역")
        # SQLite strips timezone metadata; the source KST wall-clock value must
        # still remain unchanged after the shared persistence round trip.
        self.assertEqual(stored.published_at.hour, 10)
        self.assertEqual(run.status, "SUCCESS")

    def test_personal_profile_matches_only_its_owner_and_exclusion_wins(self):
        now = datetime.now(timezone.utc)
        notice = ScraperNoticeModel(
            dedup_key="bid-notice-test",
            notice_id="R26BK000002",
            title="AI 교육 운영 용역",
            bid_notice_no="R26BK000002",
            bid_notice_ord="00",
            business_name="AI 교육 운영 용역",
            first_seen_at=now,
            last_seen_at=now,
            published_at=now,
            source_payload="{}",
        )
        self.db.add(notice)
        self.db.commit()

        update_user_bid_notice_profile(
            self.db,
            organization_id=1,
            user_id=10,
            enabled=True,
            keywords=["AI"],
            excluded_keywords=["운영"],
        )
        update_user_bid_notice_profile(
            self.db,
            organization_id=1,
            user_id=11,
            enabled=True,
            keywords=["AI"],
            excluded_keywords=[],
        )
        sync_user_bid_notice_matches(self.db, organization_id=1, user_id=10, now=now)
        sync_user_bid_notice_matches(self.db, organization_id=1, user_id=11, now=now)
        self.db.commit()

        matches = self.db.scalars(select(UserBidNoticeMatchModel)).all()
        self.assertEqual([(item.user_id, item.notice_id) for item in matches], [(11, notice.id)])
