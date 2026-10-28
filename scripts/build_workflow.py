"""Build workflows/case-triage.json for n8n.

The workflow is generated so the Code-node logic comes from nodes_js/lib.js, the file the unit tests cover.
Run:  python scripts/build_workflow.py
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NS = uuid.UUID("7d0f4c52-3c1e-4a63-9f0b-0f0f0f0f0f0f")  # fixed namespace -> stable node ids between builds

lib_source = (ROOT / "nodes_js" / "lib.js").read_text(encoding="utf8")
LIB = re.sub(r"\nif \(typeof module !== 'undefined'.*\Z", "\n", lib_source, flags=re.S)  # drop the test-only export block

SF = "{{ $env.SF_INSTANCE_URL }}/services/data/{{ $env.SF_API_VERSION || 'v60.0' }}"


def node(name, kind, version, position, parameters, **extra):
    return {"id": str(uuid.uuid5(NS, name)), "name": name, "type": kind, "typeVersion": version,
            "position": position, "parameters": parameters, **extra}


def code(name, position, glue):
    return node(name, "n8n-nodes-base.code", 2, position,
                {"jsCode": LIB + "\n// ---- node logic ----\n" + glue.strip() + "\n"})


def if_node(name, position, expression):
    return node(name, "n8n-nodes-base.if", 2.2, position, {
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"},
            "conditions": [{"id": str(uuid.uuid5(NS, name + "-c")), "leftValue": expression, "rightValue": "",
                            "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
            "combinator": "and"},
        "options": {}})


def respond(name, position, body, status):
    return node(name, "n8n-nodes-base.respondToWebhook", 1.1, position,
                {"respondWith": "json", "responseBody": body, "options": {"responseCode": status}})


def http(name, position, method, url, auth, body=None, query=None, retries=3, timeout=30000):
    params = {"method": method, "url": url, "sendHeaders": True,
              "headerParameters": {"parameters": [{"name": "Authorization", "value": auth}]},
              "options": {"timeout": timeout}}
    if body is not None:
        params.update({"sendBody": True, "specifyBody": "json", "jsonBody": body})
    if query is not None:
        params.update({"sendQuery": True, "queryParameters": {"parameters": query}})
    return node(name, "n8n-nodes-base.httpRequest", 4.2, position, params,
                retryOnFail=True, maxTries=retries, waitBetweenTries=1500, onError="continueRegularOutput")


NODES = [
    node("Webhook", "n8n-nodes-base.webhook", 2, [0, 300],
         {"httpMethod": "POST", "path": "triage", "responseMode": "responseNode", "options": {}},
         webhookId=str(uuid.uuid5(NS, "webhook"))),

    code("Validate & Normalize", [220, 300], """
const input = $input.first().json;
return [{ json: normalizeTicket(input.body, input.headers) }];
"""),

    if_node("Valid?", [440, 300], "={{ $json.valid }}"),
    respond("Respond 400", [660, 480], "={{ JSON.stringify({ error: 'invalid_ticket', details: $json.errors }) }}", 400),

    code("Build LLM Request", [660, 220], """
const ticket = $input.first().json.ticket;
return [{ json: { ticket, llm_request: buildLlmRequest(ticket, $env.LLM_MODEL || 'gpt-4o-mini') } }];
"""),

    http("LLM Triage", [880, 220], "POST", "={{ $env.LLM_BASE_URL }}/chat/completions", "=Bearer {{ $env.LLM_API_KEY }}",
         body="={{ JSON.stringify($json.llm_request) }}", timeout=60000),

    code("Parse & Decide", [1100, 220], """
const prev = $('Build LLM Request').first().json;
const res = $input.first().json;
const content = !res.error && res.choices && res.choices[0] && res.choices[0].message
  ? res.choices[0].message.content : null;
const llm = content === null ? { ok: false, reason: 'unavailable' } : parseTriage(content);
const decision = decide(prev.ticket, llm);
return [{ json: { ticket: prev.ticket, decision, account_query: accountQuery(prev.ticket.account_name) } }];
"""),

    if_node("Has Account?", [1320, 220], "={{ $json.account_query !== '' }}"),

    http("Find Account", [1540, 140], "GET", "=" + SF + "/query", "=Bearer {{ $env.SF_ACCESS_TOKEN }}",
         query=[{"name": "q", "value": "={{ $json.account_query }}"}]),

    code("Build Case", [1760, 220], """
const p = $('Parse & Decide').first().json;
let accountId = '';
try {
  const found = $('Find Account').first().json;
  if (found && found.records && found.records[0]) accountId = found.records[0].Id;
} catch (e) { /* the account lookup did not run for this ticket */ }
return [{ json: { ticket: p.ticket, decision: p.decision, account_found: accountId !== '',
                  case_body: buildCase(p.ticket, p.decision, accountId) } }];
"""),

    http("Create Case", [1980, 220], "POST", "=" + SF + "/sobjects/Case", "=Bearer {{ $env.SF_ACCESS_TOKEN }}",
         body="={{ JSON.stringify($json.case_body) }}"),

    code("Format Response", [2200, 220], """
const prev = $('Build Case').first().json;
const res = $input.first().json;
const d = prev.decision;
const triage = { category: d.category, priority: d.priority, sentiment: d.sentiment, summary: d.summary,
                 escalated: d.status === 'Escalated', flags: d.flags };
if (res.error || !res.id) {
  const detail = String((res.error && (res.error.message || res.error.description)) || 'unknown error').slice(0, 200);
  return [{ json: { ok: false, response: { error: 'crm_error', detail, triage } } }];
}
return [{ json: { ok: true, response: { case_id: res.id, status: d.status, account_found: prev.account_found, ...triage } } }];
"""),

    if_node("Created?", [2420, 220], "={{ $json.ok }}"),
    respond("Respond 201", [2640, 140], "={{ JSON.stringify($json.response) }}", 201),
    respond("Respond 502", [2640, 320], "={{ JSON.stringify($json.response) }}", 502),
]


def link(*targets):
    return {"main": [[{"node": t, "type": "main", "index": 0}] for t in targets]}


CONNECTIONS = {
    "Webhook": link("Validate & Normalize"),
    "Validate & Normalize": link("Valid?"),
    "Valid?": link("Build LLM Request", "Respond 400"),          # output 0 = true, 1 = false
    "Build LLM Request": link("LLM Triage"),
    "LLM Triage": link("Parse & Decide"),
    "Parse & Decide": link("Has Account?"),
    "Has Account?": link("Find Account", "Build Case"),
    "Find Account": link("Build Case"),
    "Build Case": link("Create Case"),
    "Create Case": link("Format Response"),
    "Format Response": link("Created?"),
    "Created?": link("Respond 201", "Respond 502"),
}

WORKFLOW = {
    "id": "caseTriage0001",
    "name": "AI Case Triage",
    "nodes": NODES,
    "connections": CONNECTIONS,
    "active": False,
    "settings": {"executionOrder": "v1"},
    "pinData": {},
    "meta": {"templateCredsSetupCompleted": True},
}


def main() -> None:
    out = ROOT / "workflows" / "case-triage.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(WORKFLOW, indent=2) + "\n", encoding="utf8")
    print(f"wrote {out.relative_to(ROOT)} ({len(NODES)} nodes)")


if __name__ == "__main__":
    main()
