"""Send the labeled tickets through the running workflow and score the results.

    python scripts/eval_triage.py                                # webhook on localhost:5678
    python scripts/eval_triage.py --delay 4 --out eval_results   # slower, for rate-limited LLM tiers

Scores: category accuracy, priority accuracy, escalation precision/recall, expected-flag checks, and whether
sensitive text ever reached the CRM (needs the mock Salesforce debug endpoint).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def post(url: str, payload: dict, timeout: int = 180) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read() or b"{}")
        except ValueError:
            return error.code, {}


def score(tickets: list[dict], results: dict[str, dict], stored: dict[str, str]) -> dict:
    n = len(tickets)
    category_ok = priority_ok = tp = fp = fn = flags_ok = flags_total = leaks = leak_checks = failed = 0
    rows = []
    for t in tickets:
        expect, got = t["expect"], results[t["id"]]
        if got.get("status_code") != 201:
            failed += 1
        c_ok = got.get("category") in expect["category"]
        p_ok = got.get("priority") in expect["priority"]
        category_ok += c_ok
        priority_ok += p_ok
        escalated = bool(got.get("escalated"))
        tp += escalated and expect["escalate"]
        fp += escalated and not expect["escalate"]
        fn += (not escalated) and expect["escalate"]
        f_ok = True
        for flag in expect.get("flags", []):
            flags_total += 1
            has = flag in got.get("flags", [])
            flags_ok += has
            f_ok &= has
        if "not_stored" in expect:
            leak_checks += 1
            leaked = expect["not_stored"] in stored.get(got.get("case_id", ""), "")
            leaks += leaked
            f_ok &= not leaked
        rows.append({"id": t["id"], "category": got.get("category"), "priority": got.get("priority"), "escalated": escalated,
                     "category_ok": c_ok, "priority_ok": p_ok, "flags_ok": f_ok, "status_code": got.get("status_code")})
    return {
        "tickets": n, "created": n - failed, "failed_requests": failed,
        "category_accuracy": round(category_ok / n, 3), "priority_accuracy": round(priority_ok / n, 3),
        "escalation_precision": round(tp / (tp + fp), 3) if tp + fp else None,
        "escalation_recall": round(tp / (tp + fn), 3) if tp + fn else None,
        "flag_checks": f"{flags_ok}/{flags_total}", "sensitive_text_leaks": f"{leaks}/{leak_checks}", "rows": rows,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--webhook", default="http://localhost:5678/webhook/triage")
    parser.add_argument("--mock-sf", default="http://127.0.0.1:8089", help="mock Salesforce base URL (for the leak check); '' to skip")
    parser.add_argument("--delay", type=float, default=0.0, help="seconds to wait between tickets")
    parser.add_argument("--out", default="eval_results")
    parser.add_argument("--label", default="run")
    args = parser.parse_args(argv)

    tickets = json.loads((ROOT / "data" / "labeled_tickets.json").read_text(encoding="utf8"))["tickets"]
    results: dict[str, dict] = {}
    for t in tickets:
        payload = {k: t[k] for k in ("subject", "description", "channel") if k in t}
        if t.get("account_name"):
            payload["account_name"] = t["account_name"]
        started = time.time()
        status, body = post(args.webhook, payload)
        results[t["id"]] = {**body, "status_code": status, "seconds": round(time.time() - started, 2)}
        print(f"{t['id']} {status} {body.get('category')}/{body.get('priority')} escalated={body.get('escalated')} {results[t['id']]['seconds']}s")
        if args.delay:
            time.sleep(args.delay)

    stored: dict[str, str] = {}
    if args.mock_sf:
        with urllib.request.urlopen(args.mock_sf + "/_debug/cases", timeout=10) as response:
            stored = {c["Id"]: c.get("Description", "") + c.get("Subject", "") for c in json.loads(response.read())}
    summary = score(tickets, results, stored)
    summary["mean_seconds"] = round(sum(r["seconds"] for r in results.values()) / len(results), 2)

    out = Path(args.out)
    out.mkdir(exist_ok=True)
    (out / f"{args.label}.json").write_text(json.dumps({"summary": {k: v for k, v in summary.items() if k != "rows"},
                                                         "rows": summary["rows"]}, indent=2), encoding="utf8")
    print("\n" + json.dumps({k: v for k, v in summary.items() if k != "rows"}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
