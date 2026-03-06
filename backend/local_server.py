#!/usr/bin/env python3
"""
Minimal local development server.

Mimics API Gateway's Lambda proxy integration so you can run the Lambda
handlers locally without SAM or Docker.

Usage:
    cd backend
    python3 local_server.py

The React Vite dev server proxies /api/* to localhost:3001 (see vite.config.ts).
"""
import importlib.util
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(__file__)

# Make shared/ importable by all handlers
sys.path.insert(0, os.path.join(BASE_DIR, "shared"))


def _load_dotenv() -> None:
    """
    Parse backend/.env and populate os.environ (no third-party deps).
    Existing env vars always take precedence (setdefault), so you can
    override individual values by exporting them in your shell.
    Skips blank lines and # comments; strips surrounding quotes from values.
    """
    env_path = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(env_path):
        return
    loaded = []
    with open(env_path) as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)
                loaded.append(key)
    if loaded:
        print(f"  Loaded .env ({', '.join(loaded)})")


def _load_handler_from_file(file_path: str, unique_module_name: str, attr: str = "lambda_handler"):
    """
    Load a Python module from an absolute file path and give it a unique name
    in sys.modules so repeated calls don't collide via the module cache.
    """
    spec = importlib.util.spec_from_file_location(unique_module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load spec from {file_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[unique_module_name] = mod
    spec.loader.exec_module(mod)   # type: ignore[union-attr]
    return getattr(mod, attr)


# Route table: path → handler callable (loaded once at startup)
def _build_routes() -> dict:
    fn_dir = os.path.join(BASE_DIR, "functions")
    return {
        "/solar/today":   _load_handler_from_file(
            os.path.join(fn_dir, "solar_data", "handler.py"),
            "solar_data_handler",
        ),
        "/recommendation": _load_handler_from_file(
            os.path.join(fn_dir, "recommendation", "handler.py"),
            "recommendation_handler",
        ),
    }


PORT = int(os.environ.get("LOCAL_PORT", "3001"))


class DevHandler(BaseHTTPRequestHandler):
    routes: dict = {}

    def log_message(self, fmt, *args):  # noqa: ARG002  quieter logs
        status = args[1] if len(args) > 1 else "?"
        print(f"  {self.command} {self.path} → {status}")

    def _send(self, status: int, body: dict):
        payload = json.dumps(body, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET,OPTIONS")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):
        self._send(200, {})

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        params = {k: v[0] for k, v in qs.items()}

        handler_fn = self.routes.get(parsed.path)
        if handler_fn is None:
            self._send(404, {"error": f"No route for GET {parsed.path}"})
            return

        # Minimal API Gateway Lambda proxy event
        event = {
            "httpMethod": "GET",
            "path": parsed.path,
            "queryStringParameters": params if params else None,
            "headers": {},
            "body": None,
            "isBase64Encoded": False,
        }

        try:
            result = handler_fn(event, None)
            raw_body = result.get("body", "{}")
            body = json.loads(raw_body) if isinstance(raw_body, str) else raw_body
            self._send(result.get("statusCode", 200), body)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            self._send(500, {"error": str(exc)})


def main():
    _load_dotenv()
    print("Loading route handlers...")
    DevHandler.routes = _build_routes()
    print(f"Local API server running on http://localhost:{PORT}")
    print(f"  GET /solar/today")
    print(f"  GET /recommendation?current_soc=0.3&target_soc=0.8")
    print(f"  Ctrl-C to stop\n")
    server = HTTPServer(("localhost", PORT), DevHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
