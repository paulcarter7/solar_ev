"""
Unit tests for the doc_ingest Lambda handler.

Covers:
- _chunk_text: size, overlap, empty input
- _extract_text: delegates to pypdf correctly
- lambda_handler: happy path, empty PDF, S3 read failure
"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import boto3
from moto import mock_aws

sys.path.insert(0, os.path.dirname(__file__))
from conftest import load_handler

doc_ingest = load_handler("doc_ingest")

BUCKET = "test-documents-bucket"
KEY = "pge-rate-schedule.pdf"
DOC_NAME = "pge-rate-schedule.pdf"


def _s3_event(bucket: str = BUCKET, key: str = KEY) -> dict:
    return {"Records": [{"s3": {"bucket": {"name": bucket}, "object": {"key": key}}}]}


# ---------------------------------------------------------------------------
# _chunk_text
# ---------------------------------------------------------------------------

def _make_pages(words_per_page: list[int]) -> list[tuple[int, str]]:
    """Helper: build (page_num, text) pairs from a list of word counts per page."""
    pages = []
    counter = 0
    for i, count in enumerate(words_per_page):
        text = " ".join(f"w{counter + j}" for j in range(count))
        pages.append((i + 1, text))
        counter += count
    return pages


class TestChunkPages(unittest.TestCase):
    def test_short_text_produces_single_chunk(self):
        pages = _make_pages([100])
        chunks = doc_ingest._chunk_pages(pages)
        self.assertEqual(len(chunks), 1)
        text, page = chunks[0]
        self.assertEqual(page, 1)

    def test_long_text_is_split(self):
        pages = _make_pages([1000])
        chunks = doc_ingest._chunk_pages(pages)
        self.assertGreater(len(chunks), 1)

    def test_each_chunk_has_content(self):
        pages = _make_pages([600])
        chunks = doc_ingest._chunk_pages(pages)
        for text, _ in chunks:
            self.assertTrue(text.strip())

    def test_empty_pages_return_no_chunks(self):
        pages = [(1, "   ")]
        chunks = doc_ingest._chunk_pages(pages)
        self.assertEqual(chunks, [])

    def test_page_number_tracks_source_page(self):
        # 300 words on page 1, 300 words on page 2 → second chunk starts on page 2
        pages = _make_pages([300, 300])
        chunks = doc_ingest._chunk_pages(pages)
        self.assertGreaterEqual(len(chunks), 2)
        _, page1 = chunks[0]
        _, page2 = chunks[-1]
        self.assertEqual(page1, 1)
        self.assertEqual(page2, 2)

    def test_overlap_causes_word_repetition(self):
        pages = _make_pages([600])
        chunks = doc_ingest._chunk_pages(pages)
        self.assertGreaterEqual(len(chunks), 2)
        text0, _ = chunks[0]
        text1, _ = chunks[1]
        words0 = set(text0.split())
        words1 = set(text1.split())
        self.assertTrue(words0 & words1)  # overlap means shared words


# ---------------------------------------------------------------------------
# _extract_text
# ---------------------------------------------------------------------------

class TestExtractPages(unittest.TestCase):
    def test_returns_page_numbers_and_text(self):
        mock_page1 = MagicMock()
        mock_page1.extract_text.return_value = "Page one text."
        mock_page2 = MagicMock()
        mock_page2.extract_text.return_value = "Page two text."

        with patch("pypdf.PdfReader") as mock_reader_cls:
            mock_reader_cls.return_value.pages = [mock_page1, mock_page2]
            result = doc_ingest._extract_pages(b"fake-pdf-bytes")

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], (1, "Page one text."))
        self.assertEqual(result[1], (2, "Page two text."))

    def test_handles_none_page_text(self):
        mock_page = MagicMock()
        mock_page.extract_text.return_value = None

        with patch("pypdf.PdfReader") as mock_reader_cls:
            mock_reader_cls.return_value.pages = [mock_page]
            result = doc_ingest._extract_pages(b"fake-pdf-bytes")

        self.assertEqual(result, [(1, "")])


# ---------------------------------------------------------------------------
# lambda_handler
# ---------------------------------------------------------------------------

class TestLambdaHandler(unittest.TestCase):
    def _mock_embed(self):
        return [0.1] * 1024

    def _make_mock_conn(self):
        conn = MagicMock()
        conn.run.return_value = []
        return conn

    @mock_aws
    def test_happy_path_stores_chunks(self):
        mock_s3 = boto3.client("s3", region_name="us-east-1")
        mock_s3.create_bucket(Bucket=BUCKET)
        mock_s3.put_object(Bucket=BUCKET, Key=KEY, Body=b"fake-pdf")

        mock_conn = self._make_mock_conn()

        fake_pages = [(1, "word " * 600)]
        with (
            patch.object(doc_ingest, "_s3", mock_s3),
            patch.object(doc_ingest, "_extract_pages", return_value=fake_pages),
            patch.object(doc_ingest, "_embed", return_value=self._mock_embed()),
            patch.object(doc_ingest, "_resolve_neon_dsn", return_value="postgresql://fake"),
            patch("neon.get_connection", return_value=mock_conn),
            patch("neon.ensure_schema"),
        ):
            result = doc_ingest.lambda_handler(_s3_event(), None)

        self.assertEqual(result["statusCode"], 200)
        body = json.loads(result["body"])
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["doc"], DOC_NAME)
        self.assertGreater(body["chunks"], 0)

        # DELETE before INSERT + one INSERT per chunk
        calls = [str(c) for c in mock_conn.run.call_args_list]
        self.assertTrue(any("DELETE" in c for c in calls))
        self.assertTrue(any("INSERT" in c for c in calls))

    @mock_aws
    def test_empty_pdf_returns_empty_status(self):
        mock_s3 = boto3.client("s3", region_name="us-east-1")
        mock_s3.create_bucket(Bucket=BUCKET)
        mock_s3.put_object(Bucket=BUCKET, Key=KEY, Body=b"fake-pdf")

        with (
            patch.object(doc_ingest, "_s3", mock_s3),
            patch.object(doc_ingest, "_extract_pages", return_value=[(1, "   ")]),
            patch.object(doc_ingest, "_resolve_neon_dsn", return_value="postgresql://fake"),
        ):
            result = doc_ingest.lambda_handler(_s3_event(), None)

        self.assertEqual(result["statusCode"], 200)
        body = json.loads(result["body"])
        self.assertEqual(body["status"], "empty")

    @mock_aws
    def test_s3_read_failure_raises(self):
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        # Key does not exist — S3 will raise ClientError

        with self.assertRaises(Exception):
            doc_ingest.lambda_handler(_s3_event(key="nonexistent.pdf"), None)

    @mock_aws
    def test_key_with_prefix_uses_filename_only(self):
        mock_s3 = boto3.client("s3", region_name="us-east-1")
        mock_s3.create_bucket(Bucket=BUCKET)
        mock_s3.put_object(Bucket=BUCKET, Key="docs/subfolder/rate.pdf", Body=b"fake-pdf")

        mock_conn = self._make_mock_conn()

        fake_pages = [(1, "word " * 600)]
        with (
            patch.object(doc_ingest, "_s3", mock_s3),
            patch.object(doc_ingest, "_extract_pages", return_value=fake_pages),
            patch.object(doc_ingest, "_embed", return_value=self._mock_embed()),
            patch.object(doc_ingest, "_resolve_neon_dsn", return_value="postgresql://fake"),
            patch("neon.get_connection", return_value=mock_conn),
            patch("neon.ensure_schema"),
        ):
            result = doc_ingest.lambda_handler(
                _s3_event(key="docs/subfolder/rate.pdf"), None
            )

        body = json.loads(result["body"])
        self.assertEqual(body["doc"], "rate.pdf")


if __name__ == "__main__":
    unittest.main()
