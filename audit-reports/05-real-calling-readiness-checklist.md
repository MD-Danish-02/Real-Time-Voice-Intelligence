# First controlled outbound-call readiness checklist

**Decision: BLOCKED / NOT READY.** Resolve all items marked Blocked before considering a call. No call was placed for this audit.

| Item | Status | Evidence / completion requirement |
|---|---|---|
| Project API/UI starts | **Verified** | At audit time Compose backend was healthy on host port 8001 and frontend was up on 3000. This is local service health only. |
| PostgreSQL and Redis health | **Verified** | Both Compose services reported healthy. No call/job data was queried. |
| Authenticated user access | **Unknown** | Prior login failed generically; prove authorized login and tenant access without exposing credentials. |
| Google Sheets OAuth/config | **Partially Verified** | Code exists and sync success was previously reported. Reconfirm OAuth token validity, correct tenant, spreadsheet/range mapping, consent, required columns, and no unintended rows before enabling automation. |
| Lead phone/data eligibility | **Unknown** | Do not inspect/report personal values. Verify one authorized test lead, valid E.164 destination, consent/legal basis, DNC status, and allowed call window via a controlled process. |
| Duplicate-call and retry protections | **Partially Verified** | Source has tenant-scoped lead upsert, automation outbox, eligibility lock, active-job checks, and call-job storage. Verify state transitions and worker retry behavior before enabling dispatch. |
| Celery worker and scheduler | **Blocked** | Worker exited 137 and scheduler is stopped. Do not start them until pending/locked jobs and dispatch scope are inspected and the user explicitly approves. |
| Provider mode and credentials | **Unknown** | Environment key names are present, but this audit did not disclose or validate their values. Verify Plivo account, auth, application permissions, caller ID ownership, India/PSTN eligibility, limits, and billing controls in provider console. |
| Plivo outbound request | **Implemented but unverified** | Source call path exists. No Plivo API request was made. One controlled call needs explicit separate approval due cost/external effect. |
| Public HTTPS callback origin | **Blocked** | Historical configured-host DNS result was NXDOMAIN. Temporary tunnel test did not establish stable production callback connectivity. Confirm intended public DNS, TLS, reverse proxy, authentication/signature validation, and POST status callback routing. |
| Plivo signatures and callbacks | **Partially Verified** | Unsigned GET returned 401 through temporary tunnel historically. Test real signed answer/status callbacks only as part of separately approved controlled call. |
| Asterisk trunk/PJSIP | **Unknown** | Historical ARI connectivity was successful; endpoint `Unavailable` alone was inconclusive. Recheck loaded endpoint, identify matching, transport, codecs, NAT, SIP reachability, and provider-side trunk state. |
| Asterisk outbound dialplan | **Blocked** | Historical Oracle runtime lacked `_call_.` outbound route; repository has it. Verify actual loaded `from-trunk` route and Stasis args on the VM before calling. |
| ARI consumer | **Blocked** | The dedicated voice-worker is the intended sole ARI event consumer but is not running. Verify no duplicate consumer; start only after controlled queue/call preflight approval. |
| RTP/network two-way audio | **Blocked** | No packet-level evidence. Verify Asterisk→worker externalMedia destination, worker UDP listener range, reverse RTP destination, Tailscale/interface route, Oracle/Windows/cloud firewall narrow rules, and NAT/direct_media. |
| Call ID correlation | **Blocked** | Source overwrites Plivo provider ID with Asterisk channel ID. Keep identifiers separate and test callback/hangup/status correlation with mocks before a live call. |
| Setup failure cleanup | **Pending** | Partial ARI media setup can leave resources if an exception follows allocation. Add or verify idempotent unwind before production. |
| Voice language and pronunciation | **Partially Verified** | Automatic Hindi/English/Hinglish source exists; prior tests had transcripts and audible TTS, but current pronunciation/listening and preference persistence are not established. |
| VAD and interruption | **Blocked** | Historical false transcripts/barge-ins and Energy fallback. Verify silence, genuine interruption, first-word preservation, no duplicate response, and long-turn cancellation acoustically. |
| Latency | **Unknown for current build** | Historical single-turn first-audio measurements were several seconds. Re-measure monotonic end-speech→first audible response on the current runtime after media is stable. |
| Test isolation | **Blocked** | pytest fixture can migrate, truncate test PostgreSQL, and FLUSHDB. Test files/secrets absent; Docker Desktop host mount failed. Do not run until container identity, mounts, volumes, URL endpoints, and credentials are verified. |
| Cost/consent/failure plan | **Pending** | Set provider spend/usage limits, obtain consent and lawful basis, use an authorized test destination, cap attempts/duration, monitor manually, have stop/hangup path, and document rollback before approval. |

## Exact gate before considering a call

1. Correct and regression-test provider/Asterisk ID separation and partial cleanup.
2. Verify the remote Asterisk loaded dialplan contains the outbound extension and correct Stasis arguments.
3. Resolve stable publicly reachable HTTPS callback origin; verify route/signature behavior without initiating calls.
4. Verify provider account/trunk/caller ID/destination eligibility and cost limits.
5. Verify the sole voice-worker and bidirectional RTP path; do not infer media readiness from ARI connectivity.
6. Audit pending call jobs and prevent unintended dispatch before starting worker/scheduler.
7. Obtain separate explicit approval naming the authorized destination and permitted test scope before any paid call.
