# Executive summary

**Audit date:** 2026-10-27  
**Project root:** `C:\Users\Aman\OneDrive - VVNT\Documents\ChatGPT\AI-Final-Voice-Agent`  
**Readiness:** **NOT READY for a controlled real outbound call.**

## Current state

This is an existing FastAPI + Next.js application with PostgreSQL/pgvector, Redis/Celery, Google Sheets CRM support, Plivo REST integration, and an Asterisk ARI/RTP voice path. It is not a greenfield project. The checkout is on `main` at `e3afee2`; 38 pre-existing working-tree changes were present before these reports were created. Those changes were preserved. The repository did not contain an `audit-reports` directory before this audit.

At the time of the audit, Docker Compose showed the backend healthy, frontend running, PostgreSQL healthy, and Redis healthy. The migration one-shot service had exited with status 0. The worker had exited with status 137 and the scheduler was exited; the optional telephony voice-worker was not running. The UI and health endpoint had previously returned HTTP 200, but login had previously failed with the generic incorrect-email/password message. Authentication success was not verified in this audit.

## Accomplished

- Google Sheets OAuth/configuration, sync, lead upsert, and durable `lead.created` automation retry paths exist in source. A successful sync was reported in prior project testing, but was not re-run or independently verified here.
- Outbound call API, Plivo provider, signed answer/status callback routes, Asterisk ARI handling, externalMedia bridge, RTP session manager, and voice pipeline exist in source.
- Automatic per-utterance Hindi/English/Hinglish selection, streamed LLM-to-TTS coordination, Kokoro voice routing, interruption hooks, and test-only database/Redis preflight code are present in the current checkout.
- The normal Compose stack currently has the API/UI and data services running, but its asynchronous dispatch and telephony event consumers are stopped.

## Main blockers before any real call

1. **No end-to-end call evidence:** no authorized live Plivo→Asterisk→RTP→Riya conversation has been performed. Packet-level two-way audio, provider callbacks, hangup, and cleanup remain unverified.
2. **Asterisk routing is unresolved:** historical Oracle inspection found the loaded `from-trunk` context lacked the required `call_<UUID>` / `plivo_outbound` route, despite the route being present in repository configuration. No later remote reload/loaded-dialplan evidence is available.
3. **Public callback origin is unresolved:** prior DNS checks reported NXDOMAIN for the configured backend origin; a temporary tunnel answer-route request reached the application and returned the expected unsigned-request rejection, but the tunnel URL was not made permanent or configured. Current public callback reachability is unknown.
4. **Provider/correlation risk:** the ARI handler replaces the call's Plivo provider ID with an Asterisk channel ID. Provider API hangup/status operations need a Plivo identifier, so the two identifiers must be separated and regression-verified before use.
5. **Telephony worker and media network:** the dedicated voice-worker is not running; current Compose state also has the Celery worker and scheduler stopped. RTP reachability between the Windows worker and remote Asterisk, firewall rules, effective `direct_media` behavior, and callback topology have no current packet-level proof.
6. **Voice quality and false-interruption risk:** prior browser logs contained false transcripts and repeated interruptions. Earlier runtime evidence reported Energy fallback VAD and long first-audio delays. Current acoustic behavior was not re-tested or listened to.
7. **Isolation test setup is incomplete:** focused pytest fixtures are destructive by design. The test-only Compose setup lacks the reviewed PostgreSQL initialization and Redis ACL files/secrets; Docker Desktop previously denied a read-only mount from the test-secret directory. Do not run pytest until isolation and runtime identity are proven.

## Recommendation

Do not place a real call yet. First fix and test provider-call-ID correlation, establish the public HTTPS callback origin and loaded Asterisk outbound route, then validate the voice-worker/RTP path with a controlled non-billable or explicitly approved test method. Keep worker/scheduler stopped until queued jobs and call-dispatch safeguards are reviewed. See [05-real-calling-readiness-checklist.md](./05-real-calling-readiness-checklist.md) and [06-next-steps-plan.md](./06-next-steps-plan.md).

## Evidence limits

Source presence is not runtime proof. The environment values were checked only for configured/present status; their values are intentionally omitted. Credentials, account permissions, caller ID ownership, database rows, lead data, and customer numbers were not disclosed or independently validated. No provider request, test, migration, outbound call, or configuration change was made for this audit.
