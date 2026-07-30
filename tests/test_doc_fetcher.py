import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.g2b.bid_notices.document_analysis import AttachmentDownloadError
from doc_fetcher_main import FetchDocumentRequest, fetch_document, health


class DocumentFetcherTests(unittest.TestCase):
    def test_health(self):
        self.assertEqual(health(), {"status": "ok"})

    @patch("doc_fetcher_main._download_attachment")
    def test_returns_downloaded_document(self, download_attachment):
        download_attachment.return_value = (b"document", "application/pdf")

        response = fetch_document(
            FetchDocumentRequest(url="https://www.g2b.go.kr/file/notice.pdf")
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, b"document")
        self.assertEqual(response.media_type, "application/pdf")

    @patch("doc_fetcher_main._download_attachment")
    def test_returns_gateway_timeout_for_retryable_download_error(
        self, download_attachment
    ):
        download_attachment.side_effect = AttachmentDownloadError(
            "DOWNLOAD_CONNECT_TIMEOUT", retryable=True
        )

        with self.assertRaises(HTTPException) as raised:
            fetch_document(
                FetchDocumentRequest(url="https://www.g2b.go.kr/file/notice.pdf")
            )

        self.assertEqual(raised.exception.status_code, 504)
        self.assertEqual(raised.exception.detail, "DOWNLOAD_CONNECT_TIMEOUT")


if __name__ == "__main__":
    unittest.main()
