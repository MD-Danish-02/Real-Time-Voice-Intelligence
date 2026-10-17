# Security review

Reviewed by reading the code and by tests that run against real Postgres/Redis. "Verified" = an automated test or live request exercised it. Nothing here was penetration-tested by a third party.

| Area | Result |
|---|---|
| SQL injection | Verified-by-design: SQLAlchemy bound params everywhere, incl. the raw pgvector query (`text()` with bound params). |
| IDOR / cross-tenant | Verified: leads, calls, knowledge (search + delete + reprocess), CRM connections/history, `tenant_id` in query ignored (`test_isolation_and_calls.py`). |
| Missing authorization / RBAC | Verified: viewer blocked from admin/manager actions; refresh token rejected as access token. |
| WebSocket isolation | Verified live: tenant A receives its events, tenant B does not; bad token rejected. |
| Webhook forgery / replay | Verified: bad/missing signature 401, unknown connection 404, stale timestamp 400, duplicate delivery processed once; telephony webhook URLs are HMAC-signed per call; OAuth `state` is a signed, provider-bound, 10-min token. |
| SSRF (website ingestion) | Verified for loopback, link-local/metadata IP, `file://`, `ftp://`. Redirects disabled. Residual: DNS-rebinding TOCTOU between resolve and fetch (mitigate with an egress proxy in production). |
| Uploads / path traversal | Verified: extension allow-list, magic bytes, 10 MB cap, sanitised name (`../../` stripped), per-tenant directory. Malware scanning is a documented hook (ClamAV) - **not installed**. |
| XSS | React escapes output; no `dangerouslySetInnerHTML` in the codebase. |
| Secrets | CRM tokens Fernet-encrypted (verified not plaintext in DB); log scrubber for sensitive keys; connection API never returns tokens (verified). |
| Auth | Argon2id, JWT access + rotating server-side refresh tokens (reuse rejected - verified), auth rate limit (429 verified), global per-token/IP API rate limit registered. |
| Arbitrary tool execution | Agent can only invoke 12 named tools; unknown tool names are rejected (verified); tools are tenant/lead-scoped and gated by tenant rules. |
| Security headers | Verified: `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy` on every response; HSTS added when `DEBUG=false`. |
| API keys | Verified: `vx_...` keys authenticate API requests (fixed `admin`-equivalent role), are tenant-scoped, hashed at rest (never stored/returned in plaintext after creation), revocation takes effect immediately, and `last_used_at` updates. |
| Audit logging | Verified: signup, login, CRM connect/disconnect, lead import, lead DNC changes, call created/ended, knowledge upload, API key create/revoke, and business-rules changes are written to `audit_logs` and readable only by tenant admins/owners (`GET /audit-logs`, tenant-isolated - verified). |
| Prod misconfig | Verified: `ENV=production` refuses to boot with default JWT secret, demo seed on, or any mock provider. |

## Findings fixed during review
1. OAuth callback required a bearer header (impossible for a browser redirect) and trusted an unsigned `state` -> now a signed state, no bearer needed.
2. Telephony status webhook was unauthenticated -> per-call signed URL.
3. API rate-limit middleware existed but was not registered -> registered.
4. Signup returned a refresh token that was not stored server-side (would 401 on refresh) -> issued via the normal login path.
5. Webhook-detected new leads never entered the call automation (only sync did) -> fixed + tested.
6. SAVEPOINT per record in sync so one bad row cannot discard a whole sync job.

## Open issues / accepted risks (please read)
* API keys carry a single fixed role (admin-equivalent) - no scopes/permissions yet, so an integration key can do anything an admin user can via the API.
* **Tokens live in `localStorage`** (XSS exposure if any XSS is ever introduced). Consider httpOnly cookies + CSRF tokens.
* WebSocket auth token is passed in the query string (may appear in proxy logs); use short-lived WS tickets in production.
* **Password reset and email verification have tables only - no endpoints/emails.**
* HubSpot webhook signature uses a simplified scheme; use the vendor's current v3 signature.
* Recording storage, encryption at rest for recordings, signed URLs and retention jobs are **not implemented**.
* No account lockout beyond IP/token rate limits; no audit-log writes are wired yet (table exists).
* Compliance layer is configurable rules only (hours, timezone, DNC, retries, consent flag); it encodes no jurisdiction-specific law - you must configure and validate it for your country.
