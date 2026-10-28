"""A small stand-in for the Salesforce REST API, for testing the workflow without a Salesforce org.

It implements only what the workflow uses (SOQL query on Account, create/get Case) and mimics Salesforce's
behaviour that matters to an integration: bearer auth, JSON error arrays with an errorCode, required-field and
picklist validation. Test hooks live under /_debug and are not part of Salesforce.

    python -m mock_salesforce --port 8089            # token defaults to "mock-token" (MOCK_SF_TOKEN overrides)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PRIORITIES = {"High", "Medium", "Low"}
STATUSES = {"New", "Working", "Escalated", "Closed"}
ORIGINS = {"Phone", "Email", "Web"}


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.accounts = {
            "001000000000001": "Acme Corporation", "001000000000002": "Globex Logistics",
            "001000000000003": "Initech Software", "001000000000004": "Umbrella Health",
        }
        self.cases: dict[str, dict] = {}
        self.fail_next: list[int] = []          # statuses to return for the next Case creations (retry testing)
        self.request_log: list[dict] = []

    def next_case_id(self) -> str:
        return f"500{len(self.cases) + 1:012d}"


def sf_error(code: str, message: str, fields: list[str] | None = None) -> list[dict]:
    return [{"message": message, "errorCode": code, "fields": fields or []}]


def make_handler(state: State, token: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # keep test output quiet
            pass

        # -- helpers ----------------------------------------------------------------
        def _send(self, status: int, payload=None):
            body = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _authorized(self) -> bool:
            if self.headers.get("Authorization") == f"Bearer {token}":
                return True
            self._send(401, sf_error("INVALID_SESSION_ID", "Session expired or invalid"))
            return False

        def _json_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                return json.loads(raw or b"{}")
            except ValueError:
                return None

        # -- routes ---------------------------------------------------------------------
        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            if url.path == "/_debug/cases":
                with state.lock:
                    return self._send(200, list(state.cases.values()))
            if url.path == "/_debug/requests":
                with state.lock:
                    return self._send(200, state.request_log)
            if not self._authorized():
                return
            with state.lock:
                state.request_log.append({"method": "GET", "path": url.path})
            match = re.fullmatch(r"/services/data/v[\d.]+/query", url.path)
            if match:
                return self._query(urllib.parse.parse_qs(url.query).get("q", [""])[0])
            match = re.fullmatch(r"/services/data/v[\d.]+/sobjects/Case/(\w+)", url.path)
            if match:
                with state.lock:
                    case = state.cases.get(match.group(1))
                return self._send(200, case) if case else self._send(404, sf_error("NOT_FOUND", "The requested resource does not exist"))
            self._send(404, sf_error("NOT_FOUND", "The requested resource does not exist"))

        def _query(self, soql: str):
            named = re.search(r"FROM Account WHERE Name = '((?:[^'\\]|\\.)*)'", soql)
            if not soql.strip().upper().startswith("SELECT") or not named:
                return self._send(400, sf_error("MALFORMED_QUERY", "Only 'SELECT ... FROM Account WHERE Name = ...' is supported by this mock"))
            wanted = re.sub(r"\\(.)", r"\1", named.group(1))
            with state.lock:
                rows = [{"attributes": {"type": "Account"}, "Id": i, "Name": n} for i, n in state.accounts.items() if n == wanted]
            self._send(200, {"totalSize": len(rows), "done": True, "records": rows})

        def do_POST(self):
            url = urllib.parse.urlparse(self.path)
            if url.path == "/_debug/reset":
                with state.lock:
                    state.reset()
                return self._send(200, {"ok": True})
            if url.path == "/_debug/fail_next":
                params = self._json_body() or {}
                with state.lock:
                    state.fail_next = [int(params.get("status", 503))] * int(params.get("count", 1))
                return self._send(200, {"ok": True})
            if not self._authorized():
                return
            if not re.fullmatch(r"/services/data/v[\d.]+/sobjects/Case", url.path):
                return self._send(404, sf_error("NOT_FOUND", "The requested resource does not exist"))
            with state.lock:
                state.request_log.append({"method": "POST", "path": url.path})
                if state.fail_next:
                    return self._send(state.fail_next.pop(0), sf_error("SERVER_UNAVAILABLE", "Injected failure"))
            body = self._json_body()
            if not isinstance(body, dict):
                return self._send(400, sf_error("JSON_PARSER_ERROR", "Malformed JSON"))
            if not str(body.get("Subject", "")).strip():
                return self._send(400, sf_error("REQUIRED_FIELD_MISSING", "Required fields are missing: [Subject]", ["Subject"]))
            for field, allowed in (("Priority", PRIORITIES), ("Status", STATUSES), ("Origin", ORIGINS)):
                if field in body and body[field] not in allowed:
                    return self._send(400, sf_error("INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST",
                                                   f"{field}: bad value for restricted picklist field: {body[field]}", [field]))
            if "AccountId" in body and body["AccountId"] not in state.accounts:
                return self._send(400, sf_error("FIELD_INTEGRITY_EXCEPTION", "Account ID: id value of incorrect type", ["AccountId"]))
            unknown = set(body) - {"Subject", "Description", "Priority", "Status", "Origin", "AccountId"}
            if unknown:
                return self._send(400, sf_error("INVALID_FIELD", f"No such column '{sorted(unknown)[0]}' on sobject of type Case"))
            with state.lock:
                case_id = state.next_case_id()
                state.cases[case_id] = {"Id": case_id, "Status": "New", "Priority": "Medium", "Origin": "Web", **body}
            self._send(201, {"id": case_id, "success": True, "errors": []})

    return Handler


def serve(port: int = 8089, token: str | None = None, host: str = "127.0.0.1") -> tuple[ThreadingHTTPServer, State]:
    state = State()
    server = ThreadingHTTPServer((host, port), make_handler(state, token or os.environ.get("MOCK_SF_TOKEN", "mock-token")))
    return server, state


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock Salesforce REST API")
    parser.add_argument("--port", type=int, default=8089)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    server, _ = serve(args.port, host=args.host)
    print(f"mock Salesforce on http://{args.host}:{args.port} (Authorization: Bearer $MOCK_SF_TOKEN)")
    server.serve_forever()


if __name__ == "__main__":
    main()
