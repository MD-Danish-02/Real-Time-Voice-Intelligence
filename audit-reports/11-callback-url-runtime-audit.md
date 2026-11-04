# VoxAI Riya Callback URL and Runtime Configuration Audit

**Audit time:** 2026-10-27 11:17 UTC  
**Scope:** Read-only review of the previous post-tunnel and callback-routing reports, application source, relevant tests, and configuration presence indicators. No callback route, provider API, call, job, worker, or lead sync was invoked. No configuration or application file was changed.

## Executive result

The source constructs Plivo answer and status callback URLs from a configurable backend origin, normally `BACKEND_URL`, and attaches a per-call `sig` query parameter. The Plivo adapter does not send its incoming generic webhook path to Plivo: it extracts only the origin and builds Plivo-specific paths. The dedicated FastAPI routes match those paths and accept the methods the adapter configures.

The local project `.env` currently has `BACKEND_URL` configured with a host matching the Quick Tunnel domain pattern. This was checked in memory; the value is intentionally omitted. This does **not** establish that it is the currently active Quick Tunnel origin or that the running backend loaded that value. Docker daemon/container metadata was unavailable during this inspection, so the effective running backend configuration remains unknown.

**Real outbound calling remains NOT READY.** No real Plivo callback has been delivered, signature validation through the live tunnel is unverified, and two-way voice media has not been verified.

## 1. Source-code outbound call paths

### Authenticated direct-dial API

`POST /api/v1/calls/dial` is the direct API entry point. It requires the manager authorization dependency and delegates to `initiate_outbound_call` ([calls.py](../apps/api/app/api/v1/calls.py#L19)). The service selects the provider from an explicit argument or `TELEPHONY_PROVIDER_MODE`, and selects the URL origin from an explicit `webhook_base_url` or `settings.BACKEND_URL` ([call_service.py](../apps/api/app/services/call_service.py#L101)).

The service creates a `Call` row, flushes it, and then builds a generic telephony webhook URL containing the call UUID and an internal HMAC query value. It passes that URL and the same UUID in `metadata.call_id` to the selected provider ([call_service.py](../apps/api/app/services/call_service.py#L141)).

### Worker-dispatched call path

The Celery dial task invokes `dispatch_call_job` and passes `settings.BACKEND_URL` as the webhook base ([tasks.py](../apps/api/app/workers/tasks.py#L107)). After its final eligibility check, the service creates and flushes a `Call`, constructs a generic signed webhook URL, and passes both that URL and the call UUID in provider metadata ([call_service.py](../apps/api/app/services/call_service.py#L42)).

Both paths therefore converge on the telephony provider interface. The worker path does not bypass the Plivo adapter's URL builder.

## 2. Call ID and Plivo URL construction

`Call` uses the shared UUID primary-key mixin, whose default is `uuid.uuid4`; the ID is materialized by the database-session flush before URL construction ([base.py](../apps/api/app/db/base.py#L13), [call.py](../apps/api/app/models/call.py#L12)). The same ID is:

- embedded in the generic webhook URL built by the service;
- passed to the provider as `metadata.call_id`;
- used by the Plivo adapter to build both callback paths.

The adapter extracts `scheme://authority` from the supplied webhook URL when it is parseable, discarding its path and query. If it cannot obtain an origin, it falls back to `settings.BACKEND_URL` ([plivo.py](../apps/api/app/providers/telephony/plivo.py#L24)). It then builds:

- Answer XML route: `/api/v1/plivo/xml/{call_id}`
- Status/hangup route: `/api/v1/plivo/status/{call_id}`

Both generated callback URLs include `?sig=...`. The outbound request payload sets both callback methods to `POST` ([plivo.py](../apps/api/app/providers/telephony/plivo.py#L33)). No complete URL or hostname is included in this report.

## 3. Internal `sig` and Plivo signature validation

The `sig` query value is an application-generated HMAC-SHA256 over `call-webhook:{call_id}`, keyed by `JWT_SECRET`, truncated to 40 hexadecimal characters ([signing.py](../apps/api/app/core/signing.py#L32)). The adapter computes it for each dedicated Plivo URL.

On both dedicated Plivo handlers, `sig` is **optional**: if present, it must validate or the handler returns 401; if absent, processing continues to the separate Plivo signature check ([plivo.py](../apps/api/app/api/v1/plivo.py#L101)). Thus the query parameter supplements the provider signature and is not sufficient by itself.

The Plivo signature header is required. The verifier looks for `X-Plivo-Signature-V3`, `X-Plivo-Signature-V2`, then `X-Plivo-Signature`; V2/V3 nonce headers are also recognized ([plivo.py](../apps/api/app/api/v1/plivo.py#L26)). The shared verifier implements nonce-based HMAC-SHA256 for nonce-bearing requests, and parameter-based SHA-256 with a legacy SHA-1 fallback over candidate URL plus sorted form parameters ([signing.py](../apps/api/app/core/signing.py#L40)). Form fields are collected only when the request content type identifies form data.

For canonical URL candidates, source tries a URL reconstructed from forwarded scheme/host headers, the request URL, and configured `BACKEND_URL`, each including the route path and query string ([plivo.py](../apps/api/app/api/v1/plivo.py#L56)). This is source behavior only: no real provider signature, proxy header set, or live callback was exercised. The handler logs a prefix of a rejected signature in one failure path; raw application logs should therefore remain redacted.

## 4. Route and method compatibility

The default API prefix is `/api/v1` and `main.py` mounts the API router beneath that prefix ([config.py](../apps/api/app/core/config.py#L19), [main.py](../apps/api/app/main.py#L70)). The local project `.env` has no API-prefix override according to the prior callback audit; the effective running value was not inspectable here.

| Adapter callback | FastAPI route | Registered methods | Adapter method | Source compatibility |
|---|---|---|---|---|
| Answer XML | `/api/v1/plivo/xml/{call_id}` | GET, POST | POST | Match |
| Status/hangup | `/api/v1/plivo/status/{call_id}` | POST | POST | Match |

The source paths and configured methods are compatible. This does not prove the active tunnel forwards these paths, preserves signature headers and query strings, or reaches this backend. The answer handler returns XML after validating the call; the status handler processes callback form status and updates persistence. Neither route was requested during this audit.

## 5. Configuration and runtime evidence

### Verified from source/local configuration

- `BACKEND_URL` is the configurable origin for both service paths by default; `Settings` reads `.env` and process environment settings ([config.py](../apps/api/app/core/config.py#L9)).
- The Plivo provider falls back to `BACKEND_URL` if the supplied webhook URL cannot be parsed into an origin ([plivo.py](../apps/api/app/providers/telephony/plivo.py#L24)).
- The repository-local `.env` contains a `BACKEND_URL` value, and an in-memory pattern check found that its host matches a Quick Tunnel domain pattern. The value was not printed, copied into this report, or changed.
- The previous post-tunnel report records operator-provided evidence of successful public `/health` access, while explicitly leaving callbacks and media unverified ([09-post-quick-tunnel-verification.md](./09-post-quick-tunnel-verification.md#L10)).

### Not verified at runtime

- Docker CLI was present, but Docker daemon metadata could not be retrieved in this inspection context. No running backend container configuration was available to inspect. This is an access/daemon-metadata limitation, not evidence that the backend is stopped.
- Whether the backend process/container has a `BACKEND_URL` override, whether it loaded the repository `.env`, and whether its effective value matches the currently active Quick Tunnel URL are unknown.
- The exact current Quick Tunnel URL was not read or recorded. The `.env` pattern match alone cannot show it is the same URL, is current, or is reachable.
- Effective `JWT_SECRET`, `PLIVO_AUTH_TOKEN`, provider mode, caller configuration, proxy headers, and tunnel routing were not inspected. No secret values were read for this report.
- No provider request or live signature validation was performed.

## 6. Existing test evidence

The source tests include mocked signature-verification cases and URL-builder assertions for answer/status callback paths and `sig` presence ([test_plivo.py](../apps/api/tests/test_plivo.py#L22)). These tests use mock credentials and a patched HTTP client for provider dialing. Their presence supports the intended code contract, but tests were **not run** for this audit and they do not demonstrate live Plivo acceptance, callback delivery, tunnel routing, or media behavior.

## 7. Readiness, limitations, and next step

**Readiness: REAL OUTBOUND CALLING — NOT READY.** Source URL construction and route/method matching are confirmed. Runtime callback-origin equality, live Plivo signature compatibility, tunnel callback forwarding, Asterisk/RTP connectivity, and two-way voice media remain unverified.

**Single safest next step:** On the host where the backend and Quick Tunnel are running, perform a read-only comparison that emits only whether the effective backend `BACKEND_URL` origin exactly matches the active tunnel origin (match/no match), without displaying either value. Do not invoke callback routes. Then use the result to decide whether a separately approved non-call callback test is appropriate.

No source, environment, DNS, tunnel, container, database, or provider configuration was changed. No call, callback, job, sync, or test was run.
