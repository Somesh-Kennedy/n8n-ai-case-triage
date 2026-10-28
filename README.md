# AI Case Triage: an n8n workflow with an LLM step

A support ticket arrives at a **webhook**. An LLM reads it and proposes a category, priority and sentiment, and
whether a person should look at it. The workflow applies its own safety rules to that proposal and creates the result as a
**Case in Salesforce** through the REST API. The caller gets the case id and the triage back.

The interesting part is not calling an LLM from n8n. It is everything around the call that makes it safe to
put in front of real customer data: validation, fallbacks, redaction, retries, and rules that the model cannot override.
See [docs/playbook.md](docs/playbook.md) for the reusable pattern and a go-live checklist.

```
                                    ┌─ invalid ──> 400
POST /webhook/triage ─> validate ───┤
                                    └─ valid ──> LLM triage ─> parse + rules ─> find Account ─> create Case ─┬─ ok ──> 201
                                                  (retry,        (model suggests,   (SOQL, escaped)  (retry)  │
                                                   fallback)      rules decide)                               └─ down ─> 502 + triage
```

## What the rules do (in code, not in the prompt)
| Rule | Why |
|---|---|
| Missing subject/description, unknown channel → 400, nothing created | Garbage in never reaches the CRM |
| Customer text is wrapped in `<ticket>` tags and the prompt says it is untrusted | First line of defence against injection |
| Text that addresses the assistant ("ignore all previous instructions", "system notice to the assistant", ...) is flagged, the case is escalated, and priority can not be lowered | A model that obeys planted text cannot downgrade a ticket |
| The model's reply is accepted only if category/priority come from fixed lists; confidence and sentiment are sanity-checked | The model's output is untrusted too |
| Security issues are always High and escalated; angry customers and low confidence are escalated | Routing is decided by rules, not by the model's mood |
| LLM down, slow or returning garbage → case still created, marked Escalated with a flag (`llm_unavailable`, `llm_invalid_output`) | A model outage must not drop tickets |
| Card numbers (Luhn-checked) and SSNs are redacted before the CRM write | Sensitive data stays out of downstream systems |
| Account name is escaped before it goes into SOQL | Injection through a ticket field |
| LLM and CRM calls have timeouts and 3 attempts; a CRM outage returns **502 with the triage result** | Nothing is lost, the caller knows what happened |

## Layout
```
workflows/case-triage.json     the n8n workflow (generated; import it into n8n)
nodes_js/lib.js                all Code-node logic, as plain functions
scripts/build_workflow.py      builds the workflow JSON, inlining lib.js into the Code nodes
scripts/run_stack.py           runs mock Salesforce + mock LLM + n8n locally and activates the workflow
scripts/eval_triage.py         scores the labeled tickets against the running workflow
mock_salesforce/               stand-ins for the Salesforce REST API and an OpenAI-compatible LLM (incl. failure modes)
data/labeled_tickets.json      32 labeled tickets with a written rubric
tests/                         JS unit tests, mock tests, workflow-structure tests, end-to-end tests
docs/playbook.md               the reusable pattern, checklist, and how to adapt it
```
The Code nodes are generated from `nodes_js/lib.js`, the file the unit tests cover, so the logic that ships is the logic
that was tested. A test fails if the committed workflow drifts from the generator.

## Run it
**Quick local run (no external accounts).** Needs Node 20-22 and Python 3.10+:
```bash
cd .n8n-runtime && npm install n8n && cd ..     # once; a large install
python -m venv .venv && .venv/Scripts/pip install pytest
python scripts/run_stack.py                     # starts the mocks and n8n; leave it running
curl -X POST localhost:5678/webhook/triage -H "content-type: application/json" \
  -d '{"subject":"Charged twice","description":"I was charged twice for October.","channel":"email","account_name":"Globex Logistics"}'
```
**With a real LLM and a real Salesforce org:** copy `.env.example` to `.env`, fill in the values, export them, and import
`workflows/case-triage.json` into n8n (the workflow reads `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `SF_INSTANCE_URL`,
`SF_ACCESS_TOKEN`; n8n needs `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`). `docker-compose.yml` is provided as well
(it has not been run on the machine this was built on).

## Tests
```bash
node --test tests/js/lib.test.js                      # 27 unit tests of the triage logic
.venv/Scripts/python -m pytest                        # + mock-service and workflow-structure tests (31)
N8N_E2E=1 .venv/Scripts/python -m pytest tests/test_e2e.py   # 11 end-to-end tests, with the stack running
```
The end-to-end tests drive the real workflow in n8n: a normal ticket, a known and an unknown account, a SOQL-breaking
account name, invalid input, a security ticket, prompt injection against a vulnerable (mock) model, a card number, a garbled
LLM reply, an LLM outage, a brief CRM outage (retried), and a long CRM outage (502).

## What has and has not been verified
**Verified** (n8n 2.22.6, Windows, Node 22):
- The workflow imports, activates, and handles all 11 end-to-end scenarios above.
- Retries are real: with 2 injected CRM failures the mock Salesforce received 3 attempts and stored exactly 1 case; with 10
  injected failures it received 3 attempts, stored 0 cases, and the caller received a 502 containing the triage.
- With the 32 labeled tickets sent through the workflow against the **mock** LLM: all 32 created cases, the injection ticket was escalated
  even though the mock model "obeyed" it, and the card number was redacted and never stored
  ([eval_results/mock_llm.json](eval_results/mock_llm.json)). The accuracy in that file (81% category) belongs to the mock's keyword
  rules and says nothing about a real model.

**Not verified:**
- **Accuracy of a real LLM.** The labeled set (and `scripts/eval_triage.py`) are ready, but it has not been run against a real model
  yet, so no accuracy number is claimed.
- **A real Salesforce org.** The CRM side was tested against a mock that reproduces the API's error formats, not a live org.
- The Docker Compose file.

## Limitations
- No de-duplication: sending the same ticket twice creates two cases. A production version should check an External ID first.
- A retry after a lost response can create a duplicate case.
- The injection detector is pattern-based; a cleverly worded attack could slip past it (the other rules still apply).
- Account matching is an exact name match.
