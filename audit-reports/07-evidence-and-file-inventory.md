# Evidence and file inventory

## Evidence scope

- Repository root inspected: existing `AI-Final-Voice-Agent` checkout on branch `main`, HEAD `e3afee2` (`Initial commit: Real-Time Voice Intelligence`). Git status showed 38 pre-existing changed/untracked entries before report creation; `git diff --stat` showed changes across 25 tracked files. No app/config file was changed for this audit.
- Current read-only command: `docker compose ps -a`. It showed backend healthy, frontend up, PostgreSQL healthy, Redis healthy, migration exited 0, scheduler exited 0, worker exited 137. No voice-worker container was shown.
- Earlier local UI/API evidence in this conversation: UI login route and backend health page returned HTTP 200. This does not prove login success.
- Historical reports in this conversation: 35 focused tests were previously reported as passing; exact command/output was not available for independent verification. Other earlier test attempts were blocked by test DB auth/unavailability. No tests were run for this audit.
- Historical browser evidence: false speech transcripts/interruption events; energy fallback VAD; long TTS/first-audio timing. Prior reported three typed-turn first generated-audio figures: approximately 7.4 s, 7.4 s, 15.9 s. Not a controlled benchmark and not re-measured.
- Historical Oracle evidence: ARI HTTP 200 / WS 101; loaded `from-trunk` had inbound-only route and lacked outbound `call_<UUID>` route; runtime RTP pool was reported as 10000–20000; `direct_media=true`. No remote connection was made for this audit.
- Historical public callback evidence: configured hostname resolution reportedly NXDOMAIN; temporary HTTPS tunnel reached answer route and unsigned request returned HTTP 401. Tunnel was not persisted in configuration.
- Historical Docker Desktop test mount evidence: harmless read-only mount probe failed `operation not permitted`; marker cleanup succeeded. Existing diagnostic ZIP was reviewed locally, not uploaded, and did not identify the failure.
- `.env` was checked in memory only for presence; no values were printed. Required normal app keys had nonempty entries, but validity was not checked. Test-only URL and secret-path variables were absent; test-secret directory had no files.
- No database rows, secrets, phone numbers, tokens, or customer speech were read for this audit.

## Relevant files inspected

### Application and workflow

- `README.md` — local run instructions, architecture summary; some voice readiness claims are stale.
- `DEPLOYMENT_CHECKLIST.md` — Oracle/Asterisk/Plivo, public media host, SIP, callback, and firewall readiness guidance.
- `apps/api/app/main.py` — FastAPI app/lifespan; ARI listener intentionally excluded from API process.
- `apps/api/app/api/v1/crm.py` — CRM connect/OAuth/config/sheets/sync APIs.
- `apps/api/app/services/crm_service.py` — OAuth persistence, sheet sync, lead upsert, outbox retry.
- `apps/api/app/services/eligibility_service.py`, `apps/api/app/automation/engine.py` — lead eligibility, idempotent call-job creation, automation retry.
- `apps/api/app/workers/celery_app.py`, `apps/api/app/workers/tasks.py` — periodic reconciliation, dispatch, dial task, callback tasks.
- `apps/api/app/api/v1/calls.py`, `apps/api/app/services/call_service.py` — authenticated manual dial and call lifecycle.
- `apps/api/app/providers/telephony/plivo.py`, `apps/api/app/api/v1/plivo.py` — Plivo request construction, answer/status callback URLs and validation.
- `apps/api/app/telephony/asterisk/event_handler.py`, `ari_client.py`, `apps/api/app/voice_pipeline/worker.py` — ARI events, bridge/session setup, dedicated event consumer.
- `apps/api/app/voice_pipeline/rtp_bridge.py`, `session.py`, `vad.py`, `orchestrator.py` — UDP RTP, session/VAD/interruption, language, LLM/TTS stream.
- `apps/api/app/providers/voice/` — mock/Groq STT/LLM and Kokoro/other TTS implementations.
- `apps/api/app/models/` — tenant, CRM, lead, call, job, transcript, scheduling, integration, user models.
- `apps/api/alembic/versions/0001_initial_schema.py` through `0006_integration_credentials.py` — six migration revisions.

### Infrastructure and tests

- `docker-compose.yml` — normal local services, dependencies, ports, volumes, optional telephony profile.
- `docker-compose.test.yml` — isolated test port/network/volume design; not ready to use because expected secret/init files and environment setup are missing and Docker Desktop bind mounts failed.
- `apps/api/tests/conftest.py` — fail-closed test preflight; autouse fixtures migrate, truncate test PostgreSQL tables, and FLUSHDB configured test Redis. Never aim it at app data.
- `apps/api/tests/test_crm_sync.py`, `test_workers.py`, `test_plivo_outbound_routing.py`, `test_voice_calling_end_to_end.py`, and voice-pipeline tests — existing focused tests; not run during audit.
- `infrastructure/asterisk/extensions.conf`, `extensions-voxai-patch.conf`, `pjsip.conf`, `rtp.conf`, `FIREWALL_REQUIREMENTS.md` — repo dialplan/PJSIP/RTP expectations; repository state does not prove Oracle loaded state.
- `docs/architecture-audit.md`, `docs/backend-reference/voice.md`, `docs/SECRETS-ACCESS.md` — historical integration findings; backend-reference voice document conflicts with newer event-handler code and should be treated as stale where it says media wiring is absent.
- `apps/api/requirements.txt`, `requirements-voice.txt`, API Dockerfile, frontend `package.json` — Python, voice, and UI dependency/runtime definitions.

## Versions and configuration categories

- API image is based on Python 3.12 slim; prior host environment report was `.venv313`, Python 3.13.15. Do not conflate container and host environments.
- Frontend uses Next.js 15 / React 18; Node image is 20.
- API dependencies include FastAPI, SQLAlchemy/Alembic, psycopg2/asyncpg, Redis 5, Celery 5, pgvector, and voice extras. Voice extras include `silero-vad` and ONNX Runtime, but the active running image's installed runtime was not verified in this audit.
- Configuration names include `BACKEND_URL`, database/Redis URLs, provider mode keys, Groq, Plivo, ARI, SIP host, media host/RTP range, and Google OAuth. Names/presence only; no values are included.

## Commands/actions performed for this audit

- Read-only Git status/stat/log and repository file inventory.
- Read-only source/configuration search and inspection.
- Read-only `docker compose ps -a` (current status above).
- No tests, builds, migrations, provider requests, syncs, calls, containers changes, database queries, or Redis operations.
