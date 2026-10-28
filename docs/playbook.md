# Playbook: putting an LLM step into an automation safely

The pattern behind this project is "**validate, ask, decide, act, answer**". It applies to any low-code tool (n8n,
Zapier, Make, Workato) and any system of record (Salesforce, HubSpot, Zendesk). Use this as a checklist when you
build the next one.

## The pattern
```
trigger ──> validate input ──> ask the LLM ──> decide with rules ──> act on the CRM ──> answer the caller
              │ bad? 400        │ down/garbled?     │ rules win           │ down? retry, then 502
              └─ stop           └─ safe fallback    └─ escalate to human  └─ return the triage anyway
```

## Principles
1. **The model suggests, rules decide.** Anything that matters (escalation, minimum priority, what may be written to the
   CRM) is enforced in code after the model answers. A model can be wrong, or talked into being wrong.
2. **Treat all text as untrusted:** the customer's message *and* the model's reply. Wrap customer text in tags, tell the model
   it is data, detect instructions aimed at the assistant, and accept only values from fixed lists on the way out.
3. **Always have a fallback.** If the LLM is down, slow or returns garbage, create the record anyway, marked for a person,
   instead of dropping the ticket.
4. **Keep sensitive data out of downstream systems.** Redact card numbers and national ids before the CRM write.
5. **Retry the network, not the logic.** External calls have timeouts and a few retries; failures end in an explicit
   error response, never a hung request.
6. **Every path ends in a response.** The caller always learns whether it worked (201), was malformed (400), or the CRM
   failed (502, with the triage result so nothing is lost).
7. **Keep logic out of the canvas.** Code nodes are generated from files that have unit tests. The canvas only wires things.
8. **Test offline first.** Mock the CRM and the LLM (including their failure modes), then run a labeled set against the
   real model and measure.
9. **Measure what the business cares about:** category and priority accuracy, how many escalations were caught, and that
   no sensitive text reached the CRM.

## Checklist before go-live
- [ ] Input validation with clear 400 errors
- [ ] LLM call has a timeout, retries and a fallback
- [ ] Output validated against fixed lists; confidence and sentiment checked
- [ ] Injection and sensitive-data handling tested
- [ ] CRM writes use real field names and picklist values; errors surface as 502, not silence
- [ ] Secrets only in environment variables or the tool's credential store
- [ ] A labeled set and a number you can show: accuracy and escalation recall
- [ ] A runbook line for each failure flag (`llm_unavailable`, `llm_invalid_output`, `prompt_injection_suspected`)

## Adapting it
| To change | Do this |
|---|---|
| Another CRM (HubSpot, Zendesk) | Replace "Find Account", "Build Case" and "Create Case". Keep everything before them. |
| Salesforce Flow instead of a REST call | Create a record-triggered Flow on Case that reads the AI fields; have the workflow send them as custom fields. |
| Another LLM | Set `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`. Any OpenAI-compatible endpoint works. |
| Voice (call transcripts) | Put a transcription step before "Validate & Normalize" and map the call to `description`. |
| New categories | Edit `CATEGORIES` and the prompt in `nodes_js/lib.js`, add test cases, rebuild with `scripts/build_workflow.py`. |

## Known limits of this reference build
- No de-duplication: sending the same ticket twice creates two cases. A production version should key on an external id
  (a Salesforce External ID field) before creating.
- Retries on the Case create can duplicate a case if the first attempt succeeded but the response was lost.
- The injection detector is pattern-based; a cleverly worded attack could slip past it. The other rules still apply.
