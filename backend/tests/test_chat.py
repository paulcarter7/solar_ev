"""
Unit tests for the chat router Lambda.

Covers:
- _classify: routes documents vs data queries, handles unexpected output
- lambda_handler: happy path for both routes, missing query, missing env,
  Bedrock error, Lambda invoke error
"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))
from conftest import load_handler

chat = load_handler("chat")

ENV = {
    "RAG_QUERY_FUNCTION_NAME": "solar-ev-rag-query",
    "DATA_QUERY_FUNCTION_NAME": "solar-ev-data-query",
}


def _event(body: dict | None = None) -> dict:
    return {
        "httpMethod": "POST",
        "path": "/chat",
        "body": json.dumps(body) if body is not None else None,
        "headers": {},
        "queryStringParameters": None,
    }


def _nova_response(text: str) -> dict:
    mock_body = MagicMock()
    mock_body.read.return_value = json.dumps({
        "output": {"message": {"content": [{"text": text}], "role": "assistant"}},
        "stopReason": "end_turn",
    }).encode()
    return {"body": mock_body}


def _lambda_payload(status: int, body: dict) -> dict:
    mock_payload = MagicMock()
    mock_payload.read.return_value = json.dumps({
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }).encode()
    return {"Payload": mock_payload}


# ---------------------------------------------------------------------------
# _classify
# ---------------------------------------------------------------------------

class TestClassify(unittest.TestCase):
    def _mock_invoke(self, text: str):
        return patch.object(chat._bedrock, "invoke_model", return_value=_nova_response(text))

    def test_documents_query(self):
        with self._mock_invoke("documents"):
            result = chat._classify("What are peak hours on my rate plan?")
        self.assertEqual(result, "documents")

    def test_data_query(self):
        with self._mock_invoke("data"):
            result = chat._classify("How much did I produce yesterday?")
        self.assertEqual(result, "data")

    def test_unexpected_output_defaults_to_documents(self):
        with self._mock_invoke("I'm not sure"):
            result = chat._classify("Something ambiguous")
        self.assertEqual(result, "documents")

    def test_data_with_trailing_whitespace(self):
        with self._mock_invoke("  data  "):
            result = chat._classify("What was my battery SOC last Tuesday?")
        self.assertEqual(result, "data")


# ---------------------------------------------------------------------------
# lambda_handler
# ---------------------------------------------------------------------------

class TestLambdaHandler(unittest.TestCase):
    def _mock_classify(self, route: str):
        return patch.object(chat, "_classify", return_value=route)

    def test_missing_query_returns_400(self):
        with patch.dict(os.environ, ENV):
            result = chat.lambda_handler(_event({"query": ""}), None)
        self.assertEqual(result["statusCode"], 400)

    def test_null_body_returns_400(self):
        with patch.dict(os.environ, ENV):
            result = chat.lambda_handler(_event(None), None)
        self.assertEqual(result["statusCode"], 400)

    def test_document_query_invokes_rag_lambda(self):
        rag_body = {"response": "Peak hours are 4–9pm.", "sources": [], "route": "documents"}
        with (
            patch.dict(os.environ, ENV),
            self._mock_classify("documents"),
            patch.object(chat, "_invoke_lambda",
                         return_value={"statusCode": 200, "body": json.dumps(rag_body)}) as mock_invoke,
        ):
            result = chat.lambda_handler(_event({"query": "What are peak hours?"}), None)

        self.assertEqual(result["statusCode"], 200)
        mock_invoke.assert_called_once_with(ENV["RAG_QUERY_FUNCTION_NAME"], _event({"query": "What are peak hours?"}))
        body = json.loads(result["body"])
        self.assertEqual(body["route"], "documents")

    def test_data_query_invokes_data_lambda(self):
        data_body = {"response": "You produced 22 kWh.", "intent": {}, "result": {}}
        with (
            patch.dict(os.environ, ENV),
            self._mock_classify("data"),
            patch.object(chat, "_invoke_lambda",
                         return_value={"statusCode": 200, "body": json.dumps(data_body)}) as mock_invoke,
        ):
            result = chat.lambda_handler(_event({"query": "How much yesterday?"}), None)

        self.assertEqual(result["statusCode"], 200)
        mock_invoke.assert_called_once_with(ENV["DATA_QUERY_FUNCTION_NAME"], _event({"query": "How much yesterday?"}))
        body = json.loads(result["body"])
        self.assertEqual(body["route"], "data")

    def test_route_injected_into_response(self):
        inner_body = {"response": "Some answer.", "sources": []}
        with (
            patch.dict(os.environ, ENV),
            self._mock_classify("documents"),
            patch.object(chat, "_invoke_lambda",
                         return_value={"statusCode": 200, "body": json.dumps(inner_body)}),
        ):
            result = chat.lambda_handler(_event({"query": "What is NEM 3.0?"}), None)

        body = json.loads(result["body"])
        self.assertIn("route", body)
        self.assertEqual(body["route"], "documents")

    def test_missing_function_name_env_returns_503(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            self._mock_classify("data"),
        ):
            result = chat.lambda_handler(_event({"query": "How much yesterday?"}), None)
        self.assertEqual(result["statusCode"], 503)

    def test_bedrock_error_returns_502(self):
        from botocore.exceptions import ClientError
        err = {"Error": {"Code": "ThrottlingException", "Message": "Too many"}}
        with (
            patch.dict(os.environ, ENV),
            patch.object(chat, "_classify", side_effect=ClientError(err, "InvokeModel")),
        ):
            result = chat.lambda_handler(_event({"query": "Anything?"}), None)
        self.assertEqual(result["statusCode"], 502)

    def test_unexpected_error_returns_500(self):
        with (
            patch.dict(os.environ, ENV),
            patch.object(chat, "_classify", side_effect=RuntimeError("boom")),
        ):
            result = chat.lambda_handler(_event({"query": "Anything?"}), None)
        self.assertEqual(result["statusCode"], 500)


if __name__ == "__main__":
    unittest.main()
