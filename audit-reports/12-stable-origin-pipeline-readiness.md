# VoxAI Riya — Stable Origin and Calling Pipeline Readiness Audit

**Audit time:** 2026-10-29 05:40 UTC (Updated)  
**Method:** Read-only source/configuration review, DNS resolution queries, local Windows process/port/firewall/Tailscale inspection, and read-only SSH inspection of the Oracle VM. No application code, `.env`, compose files, Asterisk, OpenResty, DNS, firewall, or Tailscale configurations were altered. No real calls, Plivo API requests, webhooks, lead syncs, or database writes were initiated.

---

## Executive Result & Architecture Status

The target deployment architecture is **Option A**:
- **Windows machine**: FastAPI backend (`apps/api`), Celery worker/scheduler, Voice pipeline worker (`app.voice_pipeline.worker`), and RTP socket media processing (`app.voice_pipeline.rtp_bridge`).
- **Oracle VM (`161.118.180.20`)**: Asterisk PBX, PJSIP/Plivo trunk configuration, OpenResty reverse proxy (`openship-edge`), and Tailscale peer (`100.80.52.68`).
- **Network paths**:
  1. **Plivo webhooks**: Plivo PSTN/API → HTTPS (`vvnt-voice.duckdns.org:443`) → Oracle OpenResty → Tailscale private route → Windows FastAPI (`http://100.80.145.109:8001`).
  2. **Asterisk ARI Control**: Windows ARI client → Tailscale private route → Oracle Asterisk (`http://100.80.52.68:8088/asterisk/ari`).
  3. **Voice Media (RTP)**: Oracle Asterisk `externalMedia` ↔ Tailscale private route ↔ Windows voice worker RTP sockets (UDP `10000–10020`).

### Critical Verified Findings
1. **Tailscale Private Connectivity: VERIFIED & DIRECT.**
   - Windows Tailscale interface is active at `100.80.145.109` (network category: `Private`).
   - Oracle VM Tailscale interface is active at `100.80.52.68`.
   - Tailscale reports active direct peering (`direct 161.118.180.20:41641`).
   - **Live HTTP reachability over Tailscale was tested and succeeded:** Oracle VM executed `curl -m 5 -s -i http://100.80.145.109:8001/health` and received `HTTP/1.1 200 OK` from the Windows Uvicorn backend.
2. **Asterisk ARI Connectivity: VERIFIED.**
   - Asterisk is active on Oracle VM (`asterisk.service` up 2+ weeks).
   - Asterisk HTTP/ARI server is enabled on `0.0.0.0:8088` with prefix `/asterisk`.
   - The Windows Docker backend container established and currently holds an active ARI WebSocket connection (`TCP 100.80.145.109:52939 -> 100.80.52.68:8088 ESTABLISHED`).
3. **Plivo Ingress / OpenResty Route: CONFIRMED GAP (Blocker).**
   - DNS: `vvnt-voice.duckdns.org` resolves to `161.118.180.20` across public resolvers (Google `8.8.8.8`, Cloudflare `1.1.1.1`).
   - OpenResty rendered active configuration (`openresty -T`) has **NO virtual host for `vvnt-voice.duckdns.org`**.
   - There is **NO SSL/TLS certificate for `vvnt-voice.duckdns.org`** under `/etc/letsencrypt/live/`.
   - Unmatched requests hitting OpenResty select the fallback virtual host (`server_name _;`), present a mismatched certificate (`openship-default-cert`), and return HTTP 404 without proxying.
4. **Local Configuration (`.env`): BLOCKERS IDENTIFIED.**
   - `BACKEND_URL` is set to `https://exactly-blog-lab-acrobat.trycloudflare.com` (temporary Cloudflare quick tunnel), NOT `https://vvnt-voice.duckdns.org`.
   - `TELEPHONY_PROVIDER_MODE` in `.env` has an inline comment (`plivo        # mock | exotel...`). Pydantic-settings strips comments and parses it as `'plivo'`, but raw text parsers could fail.
   - `ARI_BASE_URL` (`http://100.80.52.68:8088/asterisk/ari`) and `VOICE_MEDIA_HOST_EXTERNAL` (`100.80.145.109`) correctly match the active Tailscale IPs.
5. **RTP Socket Allocation & Port Conflict: CONFIRMED GAP (Blocker).**
   - In `docker-compose.yml`, the `backend` container publishes UDP `10000-10020`, whereas `voice-worker` (which actually creates RTP sockets) has no published ports and is not running.
   - If running `voice-worker` natively on Windows while Docker `backend` is running, Docker Desktop proxy (`com.docker.backend`, PID 11684) holds the host UDP bindings on `0.0.0.0:10000-10020`, dropping or intercepting incoming RTP packets.
   - In `app.voice_pipeline.rtp_bridge`, `MediaSessionManager.allocate_port()` uses a probe-close-rebind sequence that introduces a bind race condition.
   - When the configured port pool is exhausted, the allocator falls back to an OS ephemeral UDP port. Windows Firewall rule `VoxAI RTP from Oracle Tailscale` only allows UDP `10000–10020`, so ephemeral fallback ports will be dropped.
6. **Asterisk Dialplan & Trunk State:**
   - Asterisk `from-trunk` context contains the exact required pattern `_call_. => Stasis(voxai_voice_app,plivo_outbound,${EXTEN:5})` and `voxai_voice_app => Stasis(voxai_voice_app,inbound,${EXTEN})`.
   - PJSIP trunk `plivo-trunk-endpoint` reports `Unavailable` with 0 active channels. This is normal when no active calls or registrations exist, as Plivo IP-based trunking relies on Identify CIDR matches.
   - `plivo-trunk-endpoint` has `direct_media=true`. Asterisk mixing bridges with `externalMedia` require verification to ensure RTP does not attempt to bypass Asterisk.
7. **Call Lifecycle & Exception Cleanup Risks:**
   - At Stasis start, `call.provider_call_id` (which held the Plivo Request UUID) is overwritten with the Asterisk channel ID. The application UUID is preserved, but provider-level status/hangup tracking loses the Plivo ID.
   - In `_handle_stasis_start`, if ARI bridge or externalMedia creation encounters an exception, existing bridges, channels, and RTP sockets are not cleaned up, causing resource leakage.

**Readiness Decision:**
**PIPELINE READY FOR CONTROLLED STAGING TEST: NO.**  
**REAL OUTBOUND CALLING: NOT READY.**

---

## 1. DNS and Public Ingress (Oracle VM)

| Item | Expected | Verified State | Status |
|---|---|---|---|
| DNS Resolution | `vvnt-voice.duckdns.org` → `161.118.180.20` | Resolves to `161.118.180.20` via system DNS, 8.8.8.8, and 1.1.1.1 | **VERIFIED** |
| Oracle VM Host Ports | 80/tcp, 443/tcp, 8088/tcp, 5060/udp | Bound on 0.0.0.0: OpenResty (80, 443), Asterisk (8088, 5060) | **VERIFIED** |
| OpenResty Container | `openship-edge` running in host-network mode | Running (`ghcr.io/oblien/openship-edge:0.6.8`), up 3 weeks | **VERIFIED** |
| OpenResty Virtual Host | Virtual host for `vvnt-voice.duckdns.org` | Missing. Config only has `_`, `161.118.180.20`, and `medencyclopedia` | **BLOCKER** |
| SSL/TLS Certificate | SAN covering `vvnt-voice.duckdns.org` | Missing. Only `medencyclopedia` and `medicalencyclopedia` exist | **BLOCKER** |
| Unmatched HTTPS Behavior | Fallback to `server_name _;` | Returns 404 with mismatched fallback cert (`openship-default-cert`) | **BLOCKER** |
| Existing Applications | `openship-medical-rag-chatbot` on 127.0.0.1:20001 | Active and untouched; must remain isolated from VoxAI | **VERIFIED** |
| VM Host Port 8001 | No conflict on Oracle VM | No process listening on 8001 on Oracle VM | **VERIFIED** |

---

## 2. Tailscale & Private Network Paths

| Path | Source | Destination | Protocol / Port | Verified Status |
|---|---|---|---|---|
| **API Health Check** | Oracle VM (`100.80.52.68`) | Windows Backend (`100.80.145.109`) | TCP 8001 | **VERIFIED (HTTP 200 OK)** |
| **ARI Event Stream** | Windows Backend (`100.80.145.109`) | Oracle Asterisk (`100.80.52.68`) | TCP 8088 | **VERIFIED (ESTABLISHED WebSocket)** |
| **Plivo Webhook Proxy** | Oracle OpenResty (`100.80.52.68`) | Windows Backend (`100.80.145.109`) | TCP 8001 | **BLOCKED by missing OpenResty vhost** |
| **Asterisk → Worker RTP** | Oracle Asterisk (`100.80.52.68`) | Windows Worker (`100.80.145.109`) | UDP 10000–10020 | **BLOCKED by Docker UDP port publication / worker absence** |
| **Worker → Asterisk RTP** | Windows Worker (`100.80.145.109`) | Oracle Asterisk (`100.80.52.68`) | UDP 10000–20000 | **Route verified; runtime media unverified** |

### Firewall Rule Verifications
- **Windows Firewall**:
  - `VoxAI RTP from Oracle Tailscale`: Enabled, Direction In, RemoteIP `100.80.52.68/32`, LocalPort `10000-10020`, Protocol UDP, Action Allow.
  - `Tailscale-In`: Enabled, Direction In, Profiles Domain/Private, LocalIP `100.80.145.109/32`, Action Allow. (Tailscale connection profile is classified as `Private`).
- **Oracle VM Netfilter / iptables**:
  - `ts-input`: `-A ts-input -i tailscale0 -j ACCEPT`.
  - `ts-forward`: `-A ts-forward -o tailscale0 -j ACCEPT`.
  - `OUTPUT`: `-P OUTPUT ACCEPT`.

---

## 3. Windows Environment & Local Configuration

| Setting / Component | Configured / Observed Value | Target / Requirement | Status |
|---|---|---|---|
| Python Environment | Python 3.13.15 (`.venv313`) | Core modules import cleanly without bytecode | **VERIFIED** |
| `BACKEND_URL` | `https://exactly-blog-lab-acrobat.trycloudflare.com` | `https://vvnt-voice.duckdns.org` | **BLOCKER** |
| `TELEPHONY_PROVIDER_MODE` | `plivo` (with inline comment in file) | `plivo` (supported key in factory registry) | **VERIFIED** |
| `ARI_BASE_URL` | `http://100.80.52.68:8088/asterisk/ari` | `http://100.80.52.68:8088/asterisk/ari` | **VERIFIED** |
| `VOICE_MEDIA_HOST_EXTERNAL` | `100.80.145.109` | `100.80.145.109` (Active Windows Tailscale IP) | **VERIFIED** |
| `SIP_PUBLIC_HOST` | `161.118.180.20` | `161.118.180.20` (Oracle VM public IP) | **VERIFIED** |
| Running Backend | Docker container `ai-final-voice-agent-backend-1` | Listening on port 8001 | **RUNNING** |
| Running Voice Worker | Not running | Dedicated `python -m app.voice_pipeline.worker` | **NOT RUNNING** |
| UDP Port 10000–10020 | Bound by `com.docker.backend` (PID 11684) | Must route to voice worker process | **CONFLICT** |

---

## 4. End-to-End Outbound Call Pipeline Trace

```
[1. POST /calls/dial]
         │
         ▼
[2. call_service.initiate_outbound_call] ── Creates Call row (UUID)
         │
         ▼
[3. PlivoTelephonyProvider.dial] ── Sends REST Originate to Plivo API
         │                         Passes answer_url: https://vvnt-voice.duckdns.org/api/v1/plivo/xml/{call_id}?sig={sig}
         │                         Passes hangup_url: https://vvnt-voice.duckdns.org/api/v1/plivo/status/{call_id}?sig={sig}
         ▼
[4. Plivo answers & executes answer_url webhook]
         │
         ▼
[5. Oracle OpenResty HTTPS Ingress] ── Terminate TLS (vvnt-voice.duckdns.org)
         │                             Proxy to http://100.80.145.109:8001/api/v1/plivo/xml/{call_id}
         ▼
[6. Windows FastAPI plivo_answer_xml] ── Validates HMAC sig & Plivo signature
         │                               Returns XML: <Dial><Sip>sip:call_{call_id}@161.118.180.20</Sip></Dial>
         ▼
[7. Plivo SIP Leg to Asterisk:5060]
         │
         ▼
[8. Asterisk Dialplan 'from-trunk'] ── Matches '_call_.'
         │                             Executes Stasis(voxai_voice_app,plivo_outbound,{call_id})
         ▼
[9. Asterisk ARI WebSocket Event] ── Sends StasisStart over Tailscale to Windows
         │
         ▼
[10. Windows Event Handler] ── Extracts call_id, correlates Call row
         │                     Creates mixing bridge `bridge_{call_id}`
         │                     Allocates local RTP socket (e.g., 10000)
         │                     Calls ARI externalMedia: 100.80.145.109:10000 (slin16)
         │                     Discovers Asterisk RTP endpoint (100.80.52.68:{rtp_port})
         ▼
[11. Bidirectional RTP Stream over Tailscale]
         │  Asterisk -> Windows (100.80.145.109:10000)
         │  Windows -> Asterisk (100.80.52.68:{rtp_port})
         ▼
[12. AI Voice Pipeline] ── Silero VAD → Groq Whisper STT → Groq LLM → Kokoro TTS (16kHz PCM)
         │
         ▼
[13. Call Hangup & Cleanup] ── StasisEnd / Hangup webhook → finalize_call → close sockets & bridges
```

### Stage-by-Stage Verification Table

| Stage | Code Location | Required Configuration | Current Evidence | Status | Safe Verification Method |
|---|---|---|---|---|---|
| 1. Initiation | `calls.py:19` | Auth JWT | Code verified | Ready | Mock unit test |
| 2. Provider Selection | `factory.py:7` | `TELEPHONY_PROVIDER_MODE=plivo` | `get_telephony_provider('plivo')` instantiates `PlivoTelephonyProvider` | **VERIFIED** | Unit test |
| 3. Plivo REST Dial | `plivo.py:49` | `PLIVO_AUTH_ID`, `PLIVO_AUTH_TOKEN`, `PLIVO_CALLER_ID` | Credentials present in `.env` | Credential presence verified; API uncalled | Mocked httpx test |
| 4. Callback URL Build | `plivo.py:33` | `BACKEND_URL=https://vvnt-voice.duckdns.org` | Currently set to Cloudflare quick tunnel | **BLOCKER** | String unit test |
| 5. Public HTTPS Route | OpenResty | `vvnt-voice-duckdns-org.conf` & cert | Missing vhost & cert | **BLOCKER** | `openresty -T` & curl `/health` |
| 6. Webhook Handler | `plivo.py:82` | `SIP_PUBLIC_HOST=161.118.180.20` | Route registered; XML generator verified | **VERIFIED** | Unit test with mocked request |
| 7. SIP Acceptance | Asterisk PJSIP | `plivo-trunk-endpoint` | Endpoint reports `Unavailable`; Identify matches Plivo CIDRs | Verified config | Staging SIP call |
| 8. Dialplan Routing | `extensions.conf` | `from-trunk` context | `_call_. => Stasis(voxai_voice_app,plivo_outbound,${EXTEN:5})` present | **VERIFIED** | `asterisk -rx 'dialplan show from-trunk'` |
| 9. ARI Event Stream | `ari_client.py` | `ARI_BASE_URL`, user/password | Active established WebSocket connection between Windows & Oracle | **VERIFIED** | Socket inspection |
| 10. Call Correlation | `event_handler.py:88` | Application Call UUID | Overwrites `provider_call_id` with Asterisk channel ID | **CODE RISK** | Unit test |
| 11. Bridge Creation | `event_handler.py:161` | ARI mixing bridge | Present in code | Verified in code | Mocked ARI test |
| 12. External Media | `event_handler.py:172` | `VOICE_MEDIA_HOST_EXTERNAL=100.80.145.109` | IP matches Tailscale | **VERIFIED** | Mocked ARI test |
| 13. RTP Allocation | `rtp_bridge.py:268` | Pool 10000–10020 | Probe-close-rebind race; fallback to ephemeral port escapes firewall | **BLOCKER** | Port allocator test |
| 14. RTP Media Flow | Network | Windows Firewall & Oracle iptables | Firewall rules exist; Docker port publication conflict | **BLOCKER** | Local loopback UDP test |
| 15. Audio Processing | `session.py` / Kokoro | Kokoro ONNX model files | Models loaded and verified on CPU | **VERIFIED** | Unit synthesis test |
| 16. Partial Cleanup | `event_handler.py:219` | Cleanup on exception | Exception block catches but leaks bridges/sockets | **CODE RISK** | Error injection test |
| 17. Finalization | `call_service.py:193` | Status callback | Endpoint updates status and duration in DB | Verified in code | Webhook mock test |

---

## 5. Safe Test Results

All tests executed with `--noconftest -p no:cacheprovider` to prevent any execution of database truncation or Redis flush fixtures:

1. `apps/api/tests/test_plivo_outbound_routing.py`:
   - Command: `python -m pytest --noconftest -p no:cacheprovider apps/api/tests/test_plivo_outbound_routing.py -q`
   - **Result: 14 passed in 0.45s.** Validates SIP URI formatting, dialplan matching, and XML construction.
2. `apps/api/tests/test_voice_calling_end_to_end.py::test_rtp_packetizer_and_depacketizer`:
   - Command: `python -m pytest --noconftest -p no:cacheprovider apps/api/tests/test_voice_calling_end_to_end.py::test_rtp_packetizer_and_depacketizer -q`
   - **Result: 1 passed in 6.00s.** Validates in-memory 16kHz PCM audio packetization and depacketization into RFC 3550 RTP packets.
3. `apps/api/tests/test_plivo.py`:
   - Command: `python -m pytest --noconftest -p no:cacheprovider apps/api/tests/test_plivo.py -q`
   - **Result: 18 passed, 4 failed.**
   - **Failure Analysis:**
     * `test_verify_call_webhook_signature_valid` & `test_verify_call_webhook_signature_invalid`: Test author omitted `from app.core.signing import verify_call_webhook_signature`.
     * `test_dial_success` & `test_get_call`: Test assigned `AsyncMock` to synchronous `httpx.Response.json()`, causing an unawaited coroutine error. The underlying application code correctly handles synchronous JSON.
4. **RTP Port Allocator Boundary Test (Isolated In-Memory Verification):**
   - Command: In-memory simulation of `MediaSessionManager` with pool size 3.
   - **Result: Confirmed defect.** Allocates ports 10000, 10001, 10002; the 4th allocation falls back to OS ephemeral port `56256`, which escapes the Windows Firewall rule.

---

## 6. Prioritized Implementation Plan (Option A)

> [!IMPORTANT]
> **READ-ONLY AUDIT ONLY. None of the following steps have been applied. Each step requires explicit user approval before execution.**

### Phase A — Configuration and Local Correctness
1. **Sanitize `.env` file entries:**
   - **Change:** Set `BACKEND_URL=https://vvnt-voice.duckdns.org`. Remove inline comment from `TELEPHONY_PROVIDER_MODE=plivo`. Ensure `VOICE_MEDIA_HOST_EXTERNAL=100.80.145.109`.
   - **Evidence:** Avoids quick tunnel expiry and raw text parser failures.
   - **Risk:** Webhooks will route to DuckDNS, which requires Phase C to be in place.
   - **Validation:** Python config inspection test.
   - **Approval:** Required before `.env` edit.
2. **Resolve Docker vs. Host-Native Worker Execution Model:**
   - **Decision:** In Option A, run the Voice Worker natively on Windows (`.venv313`) OR update `docker-compose.yml` so that `voice-worker` owns the UDP port mappings.
   - **Recommended Choice:** If keeping Docker, remove `10000-10020:10000-10020/udp` from `backend` service and place it on `voice-worker` under `profiles: ["telephony"]`. If running natively, stop Docker container UDP mapping so the host voice worker can receive UDP packets directly from the Tailscale interface.
   - **Evidence:** Docker Desktop proxy currently intercepts UDP 10000–10020.
   - **Approval:** Required before Compose file edits or service restarts.
3. **Fix Test Suite Defects:**
   - **Change:** In `apps/api/tests/test_plivo.py`, import `verify_call_webhook_signature` and use `MagicMock(return_value=...)` for synchronous `resp.json()`.
   - **Evidence:** 4 failing tests in unit suite are test-code defects, not application bugs.
   - **Validation:** Re-run `pytest --noconftest apps/api/tests/test_plivo.py`.
   - **Approval:** Required before test file edits.

### Phase B — Private Connectivity & Firewall Restrictions
1. **Restrict Oracle Asterisk ARI HTTP Bind / Access:**
   - **Change:** Restrict TCP 8088 on Oracle VM to `tailscale0` interface or enforce Tailscale source IP restriction (`100.80.145.109/32`) via iptables. Currently Asterisk is bound to `0.0.0.0:8088`.
   - **Evidence:** Security hardening against public internet port scanning.
   - **Risk:** Interruption to ARI if firewall rule is malformed.
   - **Validation:** Test `curl http://100.80.52.68:8088/asterisk/ari/asterisk/info` from Windows over Tailscale.
   - **Approval:** Required before Oracle iptables modification.
2. **Verify Windows Inbound Rule for Upstream Webhooks:**
   - **Change:** Add an explicit Windows Firewall inbound rule for TCP 8001 allowing remote IP `100.80.52.68/32` (Oracle Tailscale IP).
   - **Evidence:** Currently covered under broad `Tailscale-In` rule; explicit rule ensures stability if network category changes.
   - **Approval:** Required before Windows Firewall rule creation.

### Phase C — Stable Webhook Routing (Oracle OpenResty & SSL)
1. **Obtain Let's Encrypt Certificate for `vvnt-voice.duckdns.org`:**
   - **Change:** Run Certbot on Oracle VM using the existing webroot/ACME challenge setup:
     `sudo certbot certonly --webroot -w /var/lib/openship/edge/acme -d vvnt-voice.duckdns.org`
   - **Evidence:** DNS already resolves to Oracle VM. No cert currently exists in `/etc/letsencrypt/live/`.
   - **Risk:** Rate limiting if run repeatedly.
   - **Approval:** Required before running certbot.
2. **Add Dedicated OpenResty Virtual Host:**
   - **Change:** Create `/var/lib/openship/edge/sites-enabled/vvnt-voice-duckdns-org.conf`:
     - Listen 80 (ACME challenge location + 301 HTTPS redirect).
     - Listen 443 ssl with the new certificate.
     - Location `/api/v1/plivo/` and `/health` proxies to `http://100.80.145.109:8001`.
     - Preserves `Host`, `X-Real-IP`, `X-Forwarded-For`, and `X-Forwarded-Proto https`.
   - **Evidence:** `curl -m 5 http://100.80.145.109:8001/health` from Oracle VM already succeeds.
   - **Validation:** `sudo docker exec openship-edge openresty -t` before graceful reload (`openresty -s reload`). Unrelated sites (`medencyclopedia`) remain completely untouched.
   - **Approval:** Required before creating file or reloading OpenResty.

### Phase D — RTP, ARI & Pipeline Robustness
1. **Deterministic RTP Socket Allocation:**
   - **Change:** In `app.voice_pipeline.rtp_bridge.MediaSessionManager.allocate_port()`:
     - Keep the bound socket open and transfer ownership directly to `CallMediaSession` to eliminate the probe-close-rebind race condition.
     - When the 10000–10020 pool is exhausted, raise an explicit `RTPPoolExhaustedError` rather than allocating an ephemeral port that escapes the firewall policy.
   - **Evidence:** Ephemeral fallback ports are blocked by Windows Firewall.
   - **Approval:** Required before modifying `rtp_bridge.py`.
2. **Preserve Plivo Request ID Alongside Asterisk Channel ID:**
   - **Change:** Add `asterisk_channel_id` column to `calls` table (or store Plivo request UUID in a dedicated column) instead of overwriting `call.provider_call_id` in `_handle_stasis_start`.
   - **Evidence:** Overwriting provider ID destroys provider-level hangup/status tracking capability.
   - **Approval:** Required before schema or handler changes.
3. **Idempotent Cleanup on ARI Setup Exception:**
   - **Change:** In `_handle_stasis_start` exception handler, explicitly close any created `media_session`, hang up `media_chan_id`, destroy `bridge_id`, and hang up `channel_id`.
   - **Evidence:** Exception currently catches and logs, leaving orphaned channels and leaked sockets.
   - **Approval:** Required before modifying `event_handler.py`.
4. **Empirically Validate Asterisk `direct_media`:**
   - **Change:** Test whether `direct_media=true` on `plivo-trunk-endpoint` causes SIP re-INVITEs that disrupt the `externalMedia` mixing bridge during an active call. If so, set `direct_media=false`.
   - **Approval:** Required before editing Asterisk configuration.

### Phase E — Controlled Staging Validation Gates
1. **Gate 1: Mock & Syntax Verification.**
   - All unit tests pass with `--noconftest`.
   - OpenResty config syntax validated (`openresty -t`).
2. **Gate 2: Private Connectivity & Public Health Verification.**
   - `curl -k https://vvnt-voice.duckdns.org/health` returns `{"status":"ok"}` with valid SSL certificate.
   - Oracle VM to Windows Tailscale latency < 50ms.
3. **Gate 3: Mock Webhook Delivery Through Ingress.**
   - Send simulated Plivo POST request to `https://vvnt-voice.duckdns.org/api/v1/plivo/xml/{mock_uuid}`.
   - Verify request arrives at Windows backend with proper `X-Forwarded-Proto` and signature headers.
4. **Gate 4: Single Number Controlled Live Staging Call.**
   - **Explicit User Approval Required.**
   - Dial one verified, pre-consenting mobile number.
   - Verify: Plivo webhook → OpenResty → Windows Backend → XML answer generated → Asterisk StasisStart received → Mixing bridge created → ExternalMedia RTP stream bidirectional → Kokoro TTS speech audible → Clean hangup.


---

## 7. Phase 1 Implementation Plan: RTP Socket Ownership, Allocation, Provider Config & Call-ID Correlation

**Planning Date:** 2026-10-29 05:45 UTC  
**Scope:** Phase 1 execution details for RTP socket ownership, race-free port allocation, Docker Compose conflict resolution, Plivo/Asterisk Call ID separation, ARI failure cleanup, and provider configuration validation.  
**Strict Constraint:** This section contains the planning and design specification only. No code, configuration, or environment changes have been applied.

---

### Task 1: Inspection of Current State & Conflict Evidence

1. **`docker-compose.yml` Service Declarations:**
   - Line 28: `backend` service exposes `"ports: [\"${API_HOST_PORT:-8001}:8000\", \"10000-10020:10000-10020/udp\"]"`.
   - Line 36: `voice-worker` service runs `command: python -m app.voice_pipeline.worker` under `profiles: ["telephony"]`, but has **zero** port publications.
   - Line 53: `asterisk` service in local Compose also declares `"10000-10020:10000-10020/udp"`, creating a port collision if the `telephony` profile is activated locally.
2. **Windows UDP Port Ownership:**
   - Active host bindings verified via `netstat -ano -p udp | findstr 10000`: All 21 UDP ports (`10000–10020`) are held on `0.0.0.0` by PID `11684` (`com.docker.backend`).
   - The running container mapped to these ports is `ai-final-voice-agent-backend-1` (Uvicorn HTTP server).
   - The Uvicorn process inside `backend-1` does not listen on UDP; any RTP packet forwarded by Docker into the container hits no listener and is dropped.
3. **Voice Worker Startup Model:**
   - Entry point: `apps/api/app/voice_pipeline/worker.py` invokes `asyncio.run(run_event_loop(handle_ari_event))`.
   - In uncommitted repository changes, the background ARI listener was removed from `app/main.py` lifespan, establishing the architectural requirement that `voice-worker` must be the sole consumer of ARI events.
   - Currently, `voice-worker` is **not running** in Docker and **not running** as a native Windows process.
4. **Current Allocator Behavior (`apps/api/app/voice_pipeline/rtp_bridge.py`):**
   - Probe-Close-Rebind Race: `MediaSessionManager.allocate_port()` temporarily binds `s.bind(("0.0.0.0", port))`, immediately calls `s.close()`, and returns `port`. `CallMediaSession.start()` later re-binds the socket on that port. Between `close()` and `start()`, another thread or process can bind the port.
   - Ephemeral Fallback: When ports 10000–10020 are in use, it binds `s.bind(("0.0.0.0", 0))` and returns an OS ephemeral port (e.g. 56256). Because Windows Firewall rule `VoxAI RTP from Oracle Tailscale` strictly permits `10000–10020`, all return media to ephemeral ports is blackholed.

---

### Task 2: Comparative Architecture Analysis & Recommendation

#### Approach 1: Host-Native Voice Worker on Windows (`.venv313`)
- **Execution:** Run `python -m app.voice_pipeline.worker` natively using the existing `.venv313` Python 3.13.15 environment on Windows.
- **Advantages:**
  1. **Direct Tailscale Socket Binding:** Binds directly to the host network interface (`100.80.145.109`), eliminating Docker Desktop WSL2 NAT, packet proxying, and port forwarding latency.
  2. **Native AI Model Execution:** Direct local access to Kokoro ONNX model files (`models/kokoro/kokoro-v1.0.onnx`, `voices-v1.0.bin`) without needing Docker volume mounts.
  3. **No Symmetric RTP Translation Issues:** Outbound RTP packets originating from the host socket preserve the source IP `100.80.145.109`, which Asterisk recognizes without NAT rewriting.
- **Disadvantages / Requirements:**
  1. The worker needs access to PostgreSQL and Redis. Because `.env` currently uses Docker service names (`postgres:5432`, `redis:6379`), host-native execution requires either publishing those ports to `localhost` in Compose or resolving them in Windows `hosts`.
  2. UDP ports `10000–10020` must be completely removed from `docker-compose.yml` so Docker does not bind them.

#### Approach 2: Fully Containerized Voice Worker in Docker
- **Execution:** Run `voice-worker` as a Compose service alongside `backend`, `postgres`, and `redis`.
- **Advantages:**
  1. Seamless internal DNS resolution for `postgres:5432` and `redis:6379`.
  2. Unified lifecycle managed via Docker Compose.
- **Disadvantages / Critical Flaws on Windows Host:**
  1. **Missing Model Assets:** The Docker image (`apps/api/Dockerfile`) does not package `models/kokoro/` (only `app/`, `alembic/`, `tests/` are copied). Containerized workers crash when initializing Kokoro TTS unless a volume mount (`./models:/app/models:ro`) is added.
  2. **Docker Desktop WSL2 UDP Forwarding Incompatibilities:** Forwarding a range of 21 UDP ports across the Windows host to WSL2 VM over a Tailscale interface often experiences packet loss, source IP address rewriting (SNAT to virtual bridge `172.x.x.x`), and asymmetric routing that breaks Asterisk `UNICASTRTP` return paths.

#### Recommendation:
**Adopt Approach 1 (Host-Native Voice Worker) for the Real-Time Media Leg, with Docker providing DB/Redis and HTTP Backend.**
- To make this seamless:
  1. Publish PostgreSQL (`5432:5432`) and Redis (`6379:6379`) to `localhost` in Compose (or use `localhost` database URLs for local development).
  2. Remove `10000-10020:10000-10020/udp` from `backend` in `docker-compose.yml`.
  3. Run the Voice Worker natively in `.venv313` where Tailscale UDP connectivity and Kokoro ONNX CPU inference are already verified.

---

### Task 3: Design of Race-Free RTP Port Allocator

#### Architectural Flaws in Current Code:
1. `s.bind(("0.0.0.0", port))` followed by `s.close()` releases the port back to the OS before `CallMediaSession.start()` binds it.
2. No concurrency locking around `_allocated_ports`.
3. Uncontrolled fallback to OS ephemeral ports (`port = 0`).

#### Proposed Design (`MediaSessionManager` & `CallMediaSession`):
```python
class RTPPortPoolExhaustedError(RuntimeError):
    """Raised when all ports in the approved RTP pool are exhausted."""
    pass

class MediaSessionManager:
    def __init__(self, port_start: int = 10000, port_end: int = 10020) -> None:
        self.port_start = port_start
        self.port_end = port_end
        self._sessions: dict[str, CallMediaSession] = {}
        self._allocated_ports: dict[int, socket.socket] = {}
        self._lock = asyncio.Lock()

    async def allocate_session(
        self,
        call_id: str,
        on_audio_received: Optional[Callable[[bytes], None]] = None,
        on_interrupted: Optional[Callable[[], None]] = None,
    ) -> CallMediaSession:
        async with self._lock:
            if call_id in self._sessions:
                return self._sessions[call_id]

            selected_port = None
            bound_socket = None

            for port in range(self.port_start, self.port_end + 1):
                if port not in self._allocated_ports:
                    try:
                        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        s.setblocking(False)
                        s.bind(("0.0.0.0", port))
                        selected_port = port
                        bound_socket = s
                        break
                    except OSError:
                        continue

            if selected_port is None or bound_socket is None:
                raise RTPPortPoolExhaustedError(
                    f"RTP pool exhausted: all ports {self.port_start}-{self.port_end} are allocated or busy."
                )

            self._allocated_ports[selected_port] = bound_socket

            session = CallMediaSession(
                call_id=call_id,
                local_port=selected_port,
                bound_socket=bound_socket,  # Socket ownership transferred immediately
                on_audio_received=on_audio_received,
                on_interrupted=on_interrupted,
            )
            session.start()  # Uses pre-bound socket without re-binding
            self._sessions[call_id] = session
            return session

    async def close_session(self, call_id: str) -> None:
        async with self._lock:
            session = self._sessions.pop(call_id, None)
            if session:
                port = session.local_port
                session.close()  # Closes socket and cancels async loop tasks
                self._allocated_ports.pop(port, None)
```

#### Key Guarantees:
- **Zero Race Condition:** Sockets are bound once and retained continuously.
- **Deterministic Port Range:** Never allocates an ephemeral port; strictly respects the firewall-allowed range `10000–10020`.
- **Fail-Safe Exhaustion:** Raises `RTPPortPoolExhaustedError` cleanly so the caller can trigger Asterisk hangup and record call failure instead of sending silent/broken audio.

---

### Task 4: Exact Docker Compose & Port Publication Plan

To prevent Docker Desktop proxy (`com.docker.backend`) from reserving UDP `10000–10020`:

1. **Modify `docker-compose.yml` (`backend` service):**
   - **Current:** `ports: ["${API_HOST_PORT:-8001}:8000", "10000-10020:10000-10020/udp"]`
   - **Planned Change:** `ports: ["${API_HOST_PORT:-8001}:8000"]`
   - **Effect:** Docker ceases binding UDP ports `10000–10020` on Windows, leaving them available for native voice-worker binding.
2. **Remove Redundant Port Range on `asterisk` Service:**
   - Asterisk runs remotely on Oracle VM (`161.118.180.20`), not via local Docker Compose.
   - Remove `"10000-10020:10000-10020/udp"` from the dormant `asterisk` service definition in `docker-compose.yml` to prevent accidental conflict.
3. **Database & Redis Port Accessibility for Host Voice Worker:**
   - In `docker-compose.yml`, add host port mappings:
     * `postgres`: `ports: ["5432:5432"]`
     * `redis`: `ports: ["6379:6379"]`
   - This allows the native Windows Voice Worker in `.venv313` to connect to `localhost:5432` and `localhost:6379` while the backend runs inside Docker or natively.

---

### Task 5: Call ID Preservation & Comprehensive ARI Teardown Plan

#### Part A: Preserving Plivo Request UUID & Asterisk Channel ID
- **Current Defect:**
  In `apps/api/app/telephony/asterisk/event_handler.py:89`:
  ```python
  call.provider_call_id = channel_id
  ```
  This discards the Plivo `request_uuid` (needed for Plivo REST hangup and status verification) and replaces it with the Asterisk channel ID.
- **Proposed Database & Model Schema Update:**
  In `apps/api/app/models/call.py`:
  ```python
  # provider_call_id retains Plivo Request UUID (e.g. cbe6a894-6b9f-43fb-b5cf-9bb2ee88dbd2)
  provider_call_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
  # NEW: dedicated column for Asterisk channel ID (e.g. 1698547281.42)
  asterisk_channel_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
  ```
- **Event Handler Update (`event_handler.py`):**
  - Line 89: `call.asterisk_channel_id = channel_id` (leaving `call.provider_call_id` intact).
  - In `_handle_channel_state_change` and `_handle_channel_hangup`:
    Query: `select(Call).where(or_(Call.asterisk_channel_id == channel_id, Call.provider_call_id == channel_id))`
  - This ensures complete, bidirectional correlation between Plivo callbacks and Asterisk ARI events.

#### Part B: Idempotent ARI Partial-Setup Cleanup
- **Current Defect:**
  In `_handle_stasis_start` (lines 219–224), an exception caught during external media channel creation or bridge addition logs an error but does not destroy created resources.
- **Proposed Cleanup Implementation:**
  ```python
  async def _cleanup_failed_stasis_start(
      ari: AsteriskARIClient,
      channel_id: str,
      call: Optional[Call],
      bridge_id: Optional[str] = None,
      media_chan_id: Optional[str] = None,
  ) -> None:
      """Ensures zero resource leaks if media setup fails halfway."""
      # 1. Close media session socket
      if call:
          try:
              await media_session_manager.close_session(str(call.id))
          except Exception as e:
              logger.warning("cleanup_media_session_failed", error=str(e))

      # 2. Hang up externalMedia channel
      if media_chan_id:
          try:
              await ari.hangup_channel(media_chan_id)
          except Exception as e:
              logger.warning("cleanup_media_channel_failed", error=str(e))
      _active_media_channels.pop(channel_id, None)

      # 3. Destroy mixing bridge
      if bridge_id:
          try:
              await ari.destroy_bridge(bridge_id)
          except Exception as e:
              logger.warning("cleanup_bridge_failed", error=str(e))
      _active_bridges.pop(channel_id, None)

      # 4. Remove active voice session if created
      _active_voice_sessions.pop(channel_id, None)

      # 5. Hang up caller channel
      try:
          await ari.hangup_channel(channel_id)
      except Exception as e:
          logger.warning("cleanup_caller_channel_failed", error=str(e))
  ```

---

### Task 6: Provider-Mode Source Registry Validation

- **Registry Inspection (`apps/api/app/providers/telephony/factory.py`):**
  ```python
  _REGISTRY: dict[str, type[TelephonyProvider]] = {
      "mock": MockTelephonyProvider,
      "exotel": ExotelTelephonyProvider,
      "plivo": PlivoTelephonyProvider,
      "generic_sip": GenericSIPTelephonyProvider,
  }
  ```
- **Source Constraints:**
  - `get_telephony_provider(provider_name)` strictly matches against keys in `_REGISTRY`.
  - Leading/trailing whitespace or unparsed comment text (e.g. `"plivo        # comment"`) will raise `ValueError("Unknown telephony provider: ...")` if consumed by a parser that does not strip inline comments.
- **Precise Supported Value:**
  `TELEPHONY_PROVIDER_MODE=plivo` (exact string, lowercase, no trailing comment on the same line).

---

### Task 7: Isolated Test Suite Specifications (Non-Destructive)

All test cases are designed to execute with `pytest --noconftest -p no:cacheprovider` to strictly avoid database wipes, migrations, or real provider API traffic.

1. **`test_rtp_allocator_exhaustion`**:
   - Initialize `MediaSessionManager(port_start=10000, port_end=10002)` (3 slots).
   - Allocate 3 sessions. Assert ports 10000, 10001, 10002 are assigned.
   - Attempt 4th allocation. Assert `RTPPortPoolExhaustedError` is raised.
   - Assert no socket is left open and no ephemeral fallback port is used.
2. **`test_rtp_allocator_concurrency`**:
   - Initialize `MediaSessionManager(port_start=10000, port_end=10019)` (20 slots).
   - Concurrently request 20 allocations via `asyncio.gather()`.
   - Assert all 20 allocated ports are distinct and within `10000–10019`.
   - Assert zero collision exceptions.
3. **`test_rtp_allocator_release_and_reuse`**:
   - Allocate port 10000 for `call_1`.
   - Close session for `call_1`. Assert socket is closed (`fileno() == -1`).
   - Immediately allocate for `call_2`. Assert port 10000 is re-assigned cleanly without `WinError 10048` (address already in use).
4. **`test_call_id_correlation_plivo_and_asterisk`**:
   - In-memory mock DB with `Call(id=uuid.uuid4(), provider_call_id="plivo_req_12345")`.
   - Call `_handle_stasis_start` with `args=["plivo_outbound", str(call.id)]` and `channel={"id": "ast_chan_9876"}`.
   - Assert `call.provider_call_id == "plivo_req_12345"` (preserved).
   - Assert `call.asterisk_channel_id == "ast_chan_9876"` (recorded).
5. **`test_ari_media_bridge_setup_partial_failure_cleanup`**:
   - Mock ARI client where `create_bridge` succeeds, `media_session` succeeds, but `create_external_media_channel` raises `RuntimeError("Asterisk ARI timeout")`.
   - Execute `_handle_stasis_start`.
   - Assert `ari.destroy_bridge` was invoked.
   - Assert `media_session_manager.close_session` was invoked (port released).
   - Assert `ari.hangup_channel(channel_id)` was invoked.
   - Assert `_active_bridges` and `_active_media_channels` have no dangling references.

---

### Task 8: Risk Analysis & Step-by-Step Rollback Plans

| Component | Proposed Change | Identified Risk | Mitigation / Verification | Rollback Plan |
|---|---|---|---|---|
| **RTP Allocator** (`rtp_bridge.py`) | Retain bound socket; raise exception on pool exhaustion | Improper socket option handling on Windows | Test with local non-blocking loopback test | `git checkout apps/api/app/voice_pipeline/rtp_bridge.py` |
| **Docker Compose** (`docker-compose.yml`) | Remove UDP ports from `backend`; publish DB/Redis ports to host | Brief container recreation interruption | Run `docker compose config` syntax validation first | `git checkout docker-compose.yml` and `docker compose up -d` |
| **Call Model & DB** (`call.py`) | Add `asterisk_channel_id` column with index | Migration syntax error or schema lock | Use nullable column; test autogenerated Alembic migration | Run `alembic downgrade -1` and revert model file |
| **ARI Event Handler** (`event_handler.py`) | Add cleanup helper on exception; preserve Plivo request ID | Secondary exception during cleanup masking root error | Wrap each cleanup action in `try...except` block | `git checkout apps/api/app/telephony/asterisk/event_handler.py` |
| **Config** (`.env`) | Sanitize `TELEPHONY_PROVIDER_MODE=plivo` and `BACKEND_URL` | Typo breaking environment loading | Validate with non-mutating `python -B -c "from app.core.config import settings"` | Restore `.env` from backup copy |
