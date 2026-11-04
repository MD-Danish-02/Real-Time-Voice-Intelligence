# Handoff for the real-calling phase

## State left after audit

- The application checkout and existing 38 working-tree changes were preserved. Only the new audit documents were added.
- No app source, `.env`, Compose configuration, provider credentials, Docker settings, database, Redis state, Asterisk configuration, ACL, or system permission was changed.
- No real call, Plivo request, Google Sheets sync, Celery dispatch, migration, or test was run.
- At audit time backend/frontend/PostgreSQL/Redis were running; worker and scheduler were stopped; the optional voice-worker was not running.
- Local UI/health were previously observed available. Login outcome remains unresolved.

## Ready now

- Source-defined Google Sheets sync/outbox/eligibility and call-job workflow.
- Source-defined Plivo callback validation, Plivo originate, Asterisk ARI, externalMedia/RTP, and Riya speech pipeline.
- Dedicated local application API, UI, PostgreSQL, and Redis are up and healthy at the time of inspection.

## Must be resolved before a real call

1. Separate Plivo provider call ID from Asterisk channel ID and cover with focused mocked tests.
2. Verify and, if necessary, minimally apply the outbound route on the actual Asterisk host; verify the loaded context rather than only the repository file.
3. Establish stable public HTTPS/WSS/API callback routing and confirm exact current DNS/TLS/proxy reachability.
4. Verify Plivo account, trunk, number, permissions, destination policy, budget cap, and legal consent for the authorized test recipient.
5. Verify no unintended pending jobs before worker/scheduler activation; keep call dispatch disabled until scope is known.
6. Verify the sole ARI voice-worker, actual RTP directions, host advertisement, firewall, and NAT/direct-media behavior.
7. Fix or verify idempotent cleanup on partial ARI/media setup failure.
8. Re-test false STT/VAD/barge-in, Hindi/English/Hinglish, playback cancellation, and latency in the browser with actual audio.
9. Complete isolated test infrastructure and run focused tests only after dedicated test PostgreSQL/Redis identity and destructive fixture targeting are proven.

## Preflight for next session

- Review current `git status` before editing; preserve all existing changes.
- Confirm the intended environment (local Docker stack versus Windows worker + Oracle Asterisk) and one authoritative ARI event consumer.
- Keep worker/scheduler stopped while checking queued/locked call jobs.
- Confirm stable public callback origin and actual Asterisk loaded dialplan.
- Do not make an outbound call until the user explicitly approves the exact controlled test and destination.

**No real call was initiated during this audit.** The project is not ready for a controlled real-call attempt based on current verified evidence.
