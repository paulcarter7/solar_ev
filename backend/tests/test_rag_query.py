"""
Unit tests for the rag_query Lambda handler.

Covers:
- lambda_handler: missing query (400), happy path (200), no chunks found,
  Bedrock error (502), malformed body (400)
- _retrieve_chunks: maps pg8000 rows to dicts correctly
- _generate: assembles context and returns Claude's text
"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))
from conftest import load_handler

rag_query = load_handler("rag_query")


def _event(body: dict | None = None) -> dict:
    return {
        "httpMethod": "POST",
        "path": "/chat",
        "body": json.dumps(body) if body is not None else None,
        "headers": {},
        "queryStringParameters": None,
    }


FAKE_EMBEDDING = [0.1] * 1024
FAKE_CHUNKS = [
    {"doc_name": "pge-rate.pdf", "content": "Super off-peak is 9am–2pm.", "page_start": 3, "distance": 0.12},
    {"doc_name": "nem3.pdf",     "content": "NEM 3.0 reduces export credits.", "page_start": 7, "distance": 0.25},
]


# ---------------------------------------------------------------------------
# _retrieve_chunks
# ---------------------------------------------------------------------------

class TestRetrieveChunks(unittest.TestCase):
    def test_maps_rows_to_dicts(self):
        mock_conn = MagicMock()
        mock_conn.run.return_value = [
            ("pge-rate.pdf", "Some content.", 3, 0.15),
            ("nem3.pdf", "Other content.", 7, 0.30),
        ]
        chunks = rag_query._retrieve_chunks(mock_conn, FAKE_EMBEDDING)

        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0]["doc_name"], "pge-rate.pdf")
        self.assertEqual(chunks[0]["content"], "Some content.")
        self.assertEqual(chunks[0]["page_start"], 3)
        self.assertAlmostEqual(chunks[0]["distance"], 0.15)

    def test_empty_result_returns_empty_list(self):
        mock_conn = MagicMock()
        mock_conn.run.return_value = []
        chunks = rag_query._retrieve_chunks(mock_conn, FAKE_EMBEDDING)
        self.assertEqual(chunks, [])


# ---------------------------------------------------------------------------
# _generate
# ---------------------------------------------------------------------------

class TestGenerate(unittest.TestCase):
    def _mock_bedrock_response(self, text: str):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "output": {"message": {"content": [{"text": text}], "role": "assistant"}},
            "stopReason": "end_turn",
        }).encode()
        return {"body": mock_resp}

    def test_returns_claude_text(self):
        expected = "Your super off-peak window is 9am to 2pm."
        with patch.object(rag_query._bedrock, "invoke_model",
                          return_value=self._mock_bedrock_response(expected)):
            result = rag_query._generate("When is super off-peak?", FAKE_CHUNKS, "claude-model")

        self.assertEqual(result, expected)

    def test_handles_no_chunks_gracefully(self):
        expected = "I could not find relevant information."
        mock_invoke = MagicMock(return_value=self._mock_bedrock_response(expected))
        with patch.object(rag_query._bedrock, "invoke_model", mock_invoke):
            result = rag_query._generate("Some question?", [], "claude-model")

        self.assertEqual(result, expected)
        # Verify the prompt signals missing context to Claude
        call_body = json.loads(mock_invoke.call_args[1]["body"])
        user_text = call_body["messages"][0]["content"][0]["text"]
        self.assertIn("No relevant document context", user_text)


# ---------------------------------------------------------------------------
# lambda_handler
# ---------------------------------------------------------------------------

class TestLambdaHandler(unittest.TestCase):
    def _setup_mocks(self, chunks=None):
        """Return a context manager that patches Bedrock, Neon, and SSM."""
        if chunks is None:
            chunks = FAKE_CHUNKS

        mock_conn = MagicMock()

        return (
            patch.object(rag_query, "_embed", return_value=FAKE_EMBEDDING),
            patch.object(rag_query, "_retrieve_chunks", return_value=chunks),
            patch.object(rag_query, "_generate", return_value="Here is the answer."),
            patch.object(rag_query, "_resolve_neon_dsn", return_value="postgresql://fake"),
            patch("neon.get_connection", return_value=mock_conn),
        )

    def test_missing_query_returns_400(self):
        result = rag_query.lambda_handler(_event({"query": ""}), None)
        self.assertEqual(result["statusCode"], 400)
        self.assertIn("query", json.loads(result["body"])["error"])

    def test_null_body_returns_400(self):
        result = rag_query.lambda_handler(_event(None), None)
        self.assertEqual(result["statusCode"], 400)

    def test_malformed_json_returns_400(self):
        event = _event()
        event["body"] = "{not valid json"
        result = rag_query.lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 400)

    def test_happy_path_returns_response_and_sources(self):
        mocks = self._setup_mocks()
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4]:
            result = rag_query.lambda_handler(_event({"query": "What is super off-peak?"}), None)

        self.assertEqual(result["statusCode"], 200)
        body = json.loads(result["body"])
        self.assertEqual(body["response"], "Here is the answer.")
        self.assertIsInstance(body["sources"], list)
        self.assertEqual(len(body["sources"]), len(FAKE_CHUNKS))

    def test_no_chunks_returns_off_topic_response(self):
        mocks = self._setup_mocks(chunks=[])
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4]:
            result = rag_query.lambda_handler(_event({"query": "What is the capital of France?"}), None)

        self.assertEqual(result["statusCode"], 200)
        body = json.loads(result["body"])
        self.assertEqual(body["sources"], [])
        self.assertIn("only answer questions", body["response"])

    def test_high_distance_chunks_returns_off_topic_response(self):
        # Distance above threshold — query is unrelated to documents
        far_chunks = [
            {"doc_name": "pge-rate.pdf", "content": "Some content.", "page_start": 1, "distance": 0.9},
        ]
        mocks = self._setup_mocks(chunks=far_chunks)
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4]:
            result = rag_query.lambda_handler(_event({"query": "Who won the World Cup?"}), None)

        self.assertEqual(result["statusCode"], 200)
        body = json.loads(result["body"])
        self.assertIn("only answer questions", body["response"])

    def test_within_threshold_proceeds_to_generation(self):
        # Distance below threshold — should generate a real answer
        close_chunks = [
            {"doc_name": "pge-rate.pdf", "content": "Super off-peak is 9am–2pm.", "page_start": 3, "distance": 0.3},
        ]
        mocks = self._setup_mocks(chunks=close_chunks)
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4]:
            result = rag_query.lambda_handler(_event({"query": "When is super off-peak?"}), None)

        self.assertEqual(result["statusCode"], 200)
        body = json.loads(result["body"])
        self.assertEqual(body["response"], "Here is the answer.")

    def test_bedrock_client_error_returns_502(self):
        from botocore.exceptions import ClientError
        error_response = {"Error": {"Code": "ModelNotReadyException", "Message": "Not ready"}}
        with (
            patch.object(rag_query, "_embed",
                         side_effect=ClientError(error_response, "InvokeModel")),
            patch.object(rag_query, "_resolve_neon_dsn", return_value="postgresql://fake"),
        ):
            result = rag_query.lambda_handler(_event({"query": "Anything?"}), None)

        self.assertEqual(result["statusCode"], 502)

    def test_unexpected_error_returns_500(self):
        with (
            patch.object(rag_query, "_embed", side_effect=RuntimeError("boom")),
            patch.object(rag_query, "_resolve_neon_dsn", return_value="postgresql://fake"),
        ):
            result = rag_query.lambda_handler(_event({"query": "Anything?"}), None)

        self.assertEqual(result["statusCode"], 500)


if __name__ == "__main__":
    unittest.main()
