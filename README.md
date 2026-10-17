# VoxAI

VoxAI combines the Bolt workspace UI with the FastAPI platform backend:

- `apps/web` — Bolt Next.js application; the established visual system and navigation are retained.
- `apps/api` — FastAPI, PostgreSQL/pgvector, Redis, Celery, auth/RBAC, CRM, knowledge, calls, meetings, and analytics.
- `infrastructure/` — Asterisk configuration and monitoring configuration.
- `docs/architecture-audit.md` — pre-change comparison of the two source projects and the integration map.
- `docs/backend-reference/` — backend source project documentation, including known implementation gaps.

## Local development

1. Copy `.env.example` to `.env`. Keep the development provider modes as `mock` for a local workspace, and replace the development JWT/encryption secrets before sharing the stack.
2. Start the services:

```powershell
docker compose up -d --build
```

3. Open <http://localhost:3000>. FastAPI docs are at <http://localhost:8001/docs>.

The app requires a backend URL at frontend build time. The default compose build uses `http://localhost:8001`, which is correct for a browser running on the same machine. For a hosted deployment, set the `NEXT_PUBLIC_API_URL` build argument to the externally reachable API URL and configure `FRONTEND_URL`, `BACKEND_URL`, and `CORS_ORIGINS` consistently.

## Password recovery

The login page links to a single-use password reset flow. Reset tokens expire after 30 minutes, are stored hashed, and resetting a password revokes the user's refresh sessions. Configure `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM_EMAIL`, and optional SMTP credentials in `.env` for email delivery. In local development without SMTP, the backend logs the one-time reset link; view it with `docker compose logs -f backend`. Production startup requires SMTP and STARTTLS to be configured.

## Optional services

```powershell
docker compose --profile telephony up -d --build
docker compose --profile observability up -d --build
```

The telephony profile starts Asterisk with ARI enabled. It does not by itself establish a verified live audio bridge to Pipecat or a PSTN carrier.

## Integrations and secrets

Development modes and exact environment variable names are documented in `.env.example`. Set the appropriate CRM OAuth client ID/secret and exact registered redirect URI before connecting HubSpot, Salesforce, Zoho, Pipedrive, or Google. Google Sheets configuration follows OAuth and then asks for a spreadsheet ID, tab, and column mapping. Provider credentials are stored server-side encrypted.

The `mock` modes are for local development and automated tests. They are not production integrations. Production startup rejects mock modes; use real provider credentials, HTTPS, a secure refresh cookie, production secrets, a managed database/Redis, and private object storage before deployment. Restrict backend egress to approved public provider endpoints as an additional SSRF boundary; tenant-configured CRM and website URLs receive URL and DNS checks, but DNS rebinding requires network-level enforcement.

## Current implementation limits

The workspace now calls FastAPI directly. Auth uses short-lived access tokens held in memory and rotating refresh tokens in an HttpOnly cookie. CRM, lead import/DNC, knowledge, calls, meetings, analytics, integration status, business rules, workspace users, and API key screens use backend APIs and tenant-scoped responses.

The source audit identifies voice readiness as incomplete: ARI lifecycle handling and a Pipecat processor exist, but the Asterisk RTP transport/session glue, inbound DID tenant routing, and a complete barge-in-capable streaming VAD/STT/LLM/TTS path have not been verified end to end. No real inbound/outbound call should be considered operational until those items are implemented, provider credentials and SIP routing are configured, and an actual audio-path test passes. Google Calendar per-tenant OAuth token persistence/refresh now has an implementation, but it has not been live-tested with Google credentials. S3 recording capture and signed URL delivery remain unimplemented. See `docs/architecture-audit.md` for current integration findings; `docs/backend-reference/` preserves the source project's original documentation.

No real CRM, telephony, calendar, WhatsApp, or object-storage credentials are included. The normal app does not seed calls, leads, meetings, or analytics.
