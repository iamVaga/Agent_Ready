# Privacy boundaries and data inventory

Privacy policy version: privacy-guard v1. No existing MongoDB records, stored
results, or historical user records are deleted or migrated by this change.

| Input / path | Processing and egress | Storage / retention |
| --- | --- | --- |
| Scan URL, discovered URLs, public HTML/JSON-LD and HTTP headers | Raw data is used in memory for structural checks so email/address presence, schema validity, product prices and readiness scoring still work. Final scan evidence, URLs and generated fixes are redacted. There is no Node LLM call. | MongoDB stores redacted findings and page URLs, not raw fetched pages, response headers or cookies. Existing scan history is retained; legacy records are redacted on response/report generation. |
| API JSON responses and HTML/email reports | Central redaction at JSON response, response-shaping and HTML report boundaries; no-store response headers. No personal greeting. | No new response cache. Public report IDs remain bearer-style access identifiers. |
| Lead/report-delivery form | Only email is collected by the UI. Recipient address is used transiently to send the requested report through Resend. Names/phone from legacy clients are not retained. Server records canonical delivery-only consent text. | New MongoDB lead records contain redacted contact placeholders and delivery metadata; provider errors are generic. Internal notifications omit contact information. Contact-based follow-up on new leads is no longer possible. Historical lead records remain untouched. |
| Agent chat, tasks and shared memory | Redact user text before the routing LLM and all Fixer messages/tool results before the coding LLM. Redact agent replies, transcripts, facts and queued payloads. | New Redis writes expire after 24 hours; transcript capped at 12 entries. Local fallback uses the same expiry, swept on activity. User routing IDs remain necessary for delivery and memory keys. |
| Tester pages and forms | Public pages are parsed in memory. Customer inputs are replaced with reserved synthetic identities; free text and structured data are redacted; non-JSON script contents are dropped. No login, account, profile or customer/order endpoints. | Cookie jars and request logs are discarded on Browser.close, including failure paths. Pages/product HTML live only for the current run and are excluded from persisted reports. |
| Tester outbound requests | Existing public-only SSRF/DNS-pinning, port, proxy, redirect, size and timeout protections remain. Cart fields are allowlisted (product/variant/quantity/options and ephemeral CSRF/nonce). Customer, payment and unknown fields are blocked. Checkout/payment POST is blocked. Sensitive query keys are blocked at every redirect hop. Cross-site POST is blocked. | Operational CSRF tokens and session cookies exist only within the HTTP session. They are never intentionally logged, passed to an LLM or included in evidence. |
| Fixer source, build output, diffs, exceptions and progress | Protected-file checks and execution gate remain. Source/tool output is redacted immediately before each LLM call. Traces record tool names, not arguments/results. Published PR text and locally exported result diffs are redacted. | New jobs use unique managed temporary directories. Failed diagnoses, completed jobs and inactive pending jobs are cleaned up; cleanup checks root containment. Server logs are redacted and capped at 16 KB. No deletion of user repositories or historical records is scheduled. |
| Application/library/CLI logs | Shared Python logging record factory redacts messages and omits exception payloads/tracebacks. CLI print helpers redact output. Node startup/CLI console wrappers and privacyLog drop error objects and redact values. HTTP log URLs omit credentials, all query parameters and fragments. No request bodies, cookie jars or form contents are logged by request code. | Deployment platform, reverse-proxy, vendor and operating-system logging are external to these controls. Configure them separately. |

## Shopping workflow changes

Product discovery, comparison and ordinary product/variant/quantity cart actions
remain available. Forms requiring customer details, unknown fields, login or
payment are refused rather than filled from the page. Synthetic identities use
example.invalid and fictional test values; they are never submitted to a live
checkout. Public checkout pages may be read, but checkout/payment forms are never
submitted. Checkout links requiring private query/path tokens are withheld, and
reports state that the user must open the store directly. No order is placed.
If a store needs a cart field outside the allowlist, review its purpose before
adding a focused exception. No broad checkout override is provided.

Report email is still supported, but the contact details are not retained for
follow-up. Resend necessarily receives the destination address and report. Its
retention and any delivery logs must be managed in that service. The UI now states
this limited delivery purpose and no longer asks for name or phone.

## Residual risks and operational limits

- Redaction is deterministic and conservative, not proof of anonymization. It can
  miss unlabelled names, unfamiliar international addresses/identifiers, unusual
  payment formats, obfuscation, deeply encoded values or secrets without a known
  format/label. Product identifiers resembling phone/card numbers may be masked.
- Raw public HTML, repository files, user input and delivery addresses exist
  transiently in process memory. Python/Node cannot guarantee secure memory erasure.
  Do not scan authenticated customer sessions or private customer datasets.
- Public merchant servers can have side effects on GET or disguise a purchase as
  an ordinary cart route/field. Arbitrary merchant semantics cannot be inferred
  reliably. Use authorized test stores; network policy and a dedicated test service
  remain useful additional controls.
- Fixer prompt injection is not solved by redaction. Generated diffs need review.
  Repository execution remains disabled unless an operator supplies real isolation
  and explicitly opts in, as described in SECURITY-NOTES.md. Its localhost shopping
  tests remain blocked by the previous public-only Tester policy.
- Existing historical records/results and backups may contain personal data. They
  are not silently purged. Any historical cleanup needs a separately approved
  retention/migration plan. No automatic MongoDB TTL index was added.
- Provider processing (ASI:One/OpenAI-compatible endpoint, Fetch.ai messaging,
  Redis/Mongo hosting and Resend) and infrastructure access logs need separate
  configuration and access controls. Pseudonymous scan IDs and chat routing IDs
  can correlate activity. Rate-limit maps retain routing identifiers in memory.
- Tests use seeded fixtures and mocked providers. They prove the implemented
  boundaries against those fixtures, not universal PII detection or production
  vendor retention. Mongo hooks are tested without writing a real database.

## Verification

- npm run build
- npm --prefix agentready-api run check
- npm --prefix agentready-api run test:security
- npm --prefix agentready-api run test:privacy (requires local socket access)
- node agentready-api/dist/scripts/testSsrf.js
- PYTHONPATH=agents python -m unittest discover -s agents/tests -v
