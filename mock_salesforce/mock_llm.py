"""A deterministic stand-in for an OpenAI-compatible chat endpoint, so the workflow can be tested offline.

It reads the ticket from the request and answers with keyword rules. It also has deliberate misbehaviour,
triggered by words in the ticket, so the workflow's defences can be tested:

    TRIGGER_BAD_JSON   -> replies with prose instead of JSON
    TRIGGER_BAD_ENUM   -> replies with JSON whose category is not allowed
    TRIGGER_HTTP_500   -> replies with HTTP 500
    "ignore all previous instructions" -> behaves like a vulnerable model and obeys it (Low priority, no human)
"""
from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RULES = [  # (category, regex), first match wins
    ("security", r"breach|hacked|unauthori[sz]ed|leak|vulnerab|phishing|stolen"),
    ("billing", r"charge|invoice|refund|billing|payment|subscription|price"),
    ("account", r"password|log ?in|sign ?in|sso|locked out|account access|2fa"),
    ("feature_request", r"feature|would be great|please add|wish|suggest|integration with"),
    ("technical", r"down|error|crash|bug|500|slow|not working|broken|timeout|outage|fails?"),
]
HIGH = r"outage|down for|cannot work|charged twice|double charge|breach|hacked|urgent|production"


def one_line_summary(text: str) -> str:
    """First meaningful line of the ticket, without the <ticket> wrapper or the Subject:/Channel: labels."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith(("<", "Channel:"))]
    first = lines[0] if lines else "empty"
    return first.removeprefix("Subject:").strip()[:150]


def classify(text: str) -> dict:
    lowered = text.lower()
    category = next((c for c, rx in RULES if re.search(rx, lowered)), "other")
    if re.search(HIGH, lowered):
        priority = "High"
    elif category in ("feature_request", "other") or re.search(r"\bhow (do|can) i\b|question", lowered):
        priority = "Low"
    else:
        priority = "Medium"
    angry = bool(re.search(r"!!!|furious|unacceptable|worst|ridiculous|sue ", lowered))
    if "ignore all previous instructions" in lowered:  # a vulnerable model obeying the planted text
        return {"category": "other", "priority": "Low", "sentiment": "neutral", "needs_human": False,
                "confidence": 0.99, "summary": "Customer asks for no escalation."}
    return {"category": category, "priority": priority, "sentiment": "angry" if angry else "neutral",
            "needs_human": category == "security" or angry, "confidence": 0.85,
            "summary": one_line_summary(text)}


def completion(content: str) -> dict:
    return {"id": "mock-1", "object": "chat.completion", "model": "mock-llm",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, status: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
            user_text = next(m["content"] for m in reversed(request["messages"]) if m["role"] == "user")
        except (ValueError, KeyError, StopIteration):
            return self._send(400, {"error": {"message": "bad request"}})
        if "TRIGGER_HTTP_500" in user_text:
            return self._send(500, {"error": {"message": "mock upstream failure"}})
        if "TRIGGER_BAD_JSON" in user_text:
            return self._send(200, completion("I think this is a billing problem, probably high priority."))
        if "TRIGGER_BAD_ENUM" in user_text:
            return self._send(200, completion(json.dumps({"category": "dragons", "priority": "Urgent", "confidence": 1})))
        return self._send(200, completion(json.dumps(classify(user_text))))


def serve(port: int = 8090, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock OpenAI-compatible LLM")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    print(f"mock LLM on http://127.0.0.1:{args.port}/v1/chat/completions")
    serve(args.port).serve_forever()


if __name__ == "__main__":
    main()
