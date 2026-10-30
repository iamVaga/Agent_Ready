# Outbound request and Fixer security

Node requests allow HTTP on port 80 and HTTPS on 443, validate every DNS answer,
pin the validated address for each socket, disable environment proxies, and check
redirects manually. Caller transport overrides are ignored. Fetcher consumes streams
with a body cap; gzip file expansion also has an output cap.

Tester GET, POST (including multipart), fresh GET and redirects share a transport
that validates all DNS answers and connects to a pinned IP with the original Host
and TLS SNI. Each hop has a separate connection pool. Environment proxies are disabled.
Only default web ports are supported. Responses are limited to 5 MiB; compressed
responses are rejected (Accept-Encoding: identity). Cross-origin redirects discard
form bodies and caller headers. Cookies retain normal HTTPX domain/path semantics.

Fixer repository content and Tester output are explicitly untrusted prompt data.
Read/search/edit tools share canonical protected-path checks, including traversal
and symlinks; known credential files and private key extensions are denied.
The arbitrary generator-script tool is disabled. Repository install/build/start is
disabled unless an operator sets FIXER_ISOLATED_EXECUTION=1 in an independently
provisioned disposable sandbox. Child processes receive a minimal environment.
This flag does not itself create isolation. Never enable it on the application host.

Remaining limitations:
- Prompt text cannot guarantee immunity to injection. Review generated diffs and
  PR text. Secrets hardcoded in ordinary source files can still reach the model.
- Third-party installs/builds/start commands execute arbitrary code when opted in.
  A sandbox must deny credentials, host mounts and private-network egress.
- Public-only Tester policy intentionally blocks Fixer localhost shopping tests;
  a future isolated execution service needs a narrow per-job local-origin capability.
- Fixer asset checking and GitHub integration are separate from Tester transport;
  they require additional isolation/egress policy before enabling execution.
- Public servers may relay requests; application filtering cannot constrain their
  server-side behavior. Network egress rules provide defense in depth.
- Tests use deterministic DNS/transport fixtures for legitimate public destinations;
  they do not require live external sites or prove production firewall behavior.

Validation commands:
- npm run build
- npm --prefix agentready-api run test:security
- node agentready-api/dist/scripts/testSsrf.js
- npm --prefix agentready-api run check
- PYTHONPATH=agents python -m unittest discover -s agents/tests -v
