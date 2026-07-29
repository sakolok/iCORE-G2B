import unittest
from unittest.mock import MagicMock

from sqlalchemy.exc import OperationalError

from app.data.bootstrap import _backfill_source_payload


class MySqlLockWaitTimeout(Exception):
    def __init__(self):
        super().__init__(1205, "Lock wait timeout exceeded; try restarting transaction")


class BootstrapSourcePayloadBackfillTest(unittest.TestCase):
    def test_skips_backfill_when_mysql_lock_wait_timeout_occurs(self):
        engine = MagicMock()
        connection = engine.begin.return_value.__enter__.return_value
        connection.execute.side_effect = OperationalError(
            "UPDATE scraper_notices",
            {},
            MySqlLockWaitTimeout(),
        )

        _backfill_source_payload(engine)

        connection.execute.assert_called_once()

    def test_reraises_non_lock_operational_errors(self):
        engine = MagicMock()
        connection = engine.begin.return_value.__enter__.return_value
        connection.execute.side_effect = OperationalError(
            "UPDATE scraper_notices",
            {},
            Exception(1146, "Table does not exist"),
        )

        with self.assertRaises(OperationalError):
            _backfill_source_payload(engine)
