# Errors and troubleshooting history

## Confirmed or previously observed

| Issue | Evidence/status |
|---|---|
| Test database authentication and isolation | Prior test setup could not authenticate to its configured test database. The current `conftest.py` now requires test-only PostgreSQL/Redis endpoints and validates Docker container identity before imports. The fixtures migrate, truncate public test tables, and flush the configured test Redis DB. Required test secret files and variables are absent, and tests were not run. |
| Silero module missing | Previous browser/runtime report said `ModuleNotFoundError: No module named 'silero_vad'`, so Energy fallback was active at that point. The requirements list includes `silero-vad`, but the running backend image was not rebuilt for this audit; current runtime VAD implementation is not freshly confirmed. |
| False transcripts / repeated barge-in | Prior browser logs included unexpected transcripts and repeated speech-start/interruption events. This proves the symptoms occurred in that session, not their acoustic cause. No live microphone test was repeated here. |
| Slow first audio | Historical browser test reported about 7.4 s, 7.4 s, and 15.9 s to first generated audio on three typed turns; other reported end-of-speech-to-audio values were higher. These are individual historical observations, not a benchmark. Current code contains LLM phrase-to-TTS streaming; actual current delivery latency is unmeasured. |
| Public callback DNS | Previous Windows and public resolver checks reportedly returned NXDOMAIN for the configured backend hostname. A temporary Quick Tunnel reached the unsigned answer route and received HTTP 401. The temporary tunnel was not saved as project configuration. Current DNS/public HTTPS status is unknown. |
| Docker Desktop test-secret bind mount | A harmless read-only marker probe from the test-secret directory failed with `stat /host_mnt/...: operation not permitted`; marker cleanup succeeded. Path was added to Docker Desktop sharing settings and gRPC-FUSE was observed, but a subsequent probe still failed. Diagnostic bundle review did not identify a cause. The bundle was not uploaded. The test-only mount remains unverified. |
| Outbound Asterisk dialplan | Historical Oracle inspection found the required outbound extension absent from loaded `from-trunk`; the repository currently has the route. No evidence confirms the remote live dialplan was patched and reloaded. |
| Login failure | The frontend previously returned “Email or password is incorrect.” The actual user/account cause was not established in the prior read-only investigation. This audit did not inspect password hashes, accounts, or change credentials. |
| Worker stopped | Current `docker compose ps -a` showed worker exited 137 and scheduler exited 0; backend, frontend, PostgreSQL, Redis were up. This is a current status observation, not a diagnosis of the historical worker exit. |

## Provider-call correlation defect

The Plivo service stores the provider response identifier in `Call.provider_call_id`; the Asterisk outbound `StasisStart` handler then assigns the Asterisk channel ID to that same field. Plivo hangup/get-call methods use the field as a Plivo API call identifier. The conflation is visible in `apps/api/app/telephony/asterisk/event_handler.py` and `apps/api/app/providers/telephony/plivo.py`; it risks breaking status/hangup and correlating callbacks. Keep separate provider and Asterisk channel identifiers before any live call.

## Partial setup cleanup risk

The event handler has normal hangup cleanup for known bridge, externalMedia channel, and media-session entries. Its bridge/media setup exception handler logs and records failure but does not visibly unwind every resource created earlier in that same `try` block. If setup fails after partial allocation, an orphan bridge, channel, or RTP session is possible. This is a source-level risk, not a reproduced leak.

## Documentation drift

`README.md` and `docs/backend-reference/voice.md` describe the RTP/session glue as not wired, while current `event_handler.py` now creates externalMedia and calls `create_live_voice_session`. Treat the older text as stale. It remains accurate that end-to-end voice and audio are unverified. `DEPLOYMENT_CHECKLIST.md` contains deployment assumptions and historic IP examples; do not copy those blindly into current configuration.

## Actions not taken in this audit

No call, Plivo API request, Google Sheets sync, Celery dispatch, migration, test, Docker restart, bind-mount probe, credential change, or diagnostic upload was performed. Historical events are identified as historical rather than newly reproduced.
