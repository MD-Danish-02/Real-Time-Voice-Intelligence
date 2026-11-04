# VoxAI Riya Callback Routing Audit

**Audit date:** 2026-10-27
**Method:** Read-only source, test, Compose, and prior-audit review. No callback URL was requested; no Plivo/provider request, call, job, or synchronization was triggered.
**Decision:** **REAL OUTBOUND CALLING — NOT READY**

## 1. Callback route and HTTP method inventory

The application default API prefix is `/api/v1` ([config.py](../apps/api/app/core/config.py#L19)); `main.py` mounts the API router under that prefix ([main.py](../apps/api/app/main.py#L70)). There is no `API_V1_PREFIX` override in the local project `.env` at this inspection. The running container's effective environment was not read, so the live prefix is not independently confirmed.

| Route | Method | Purpose and result |
|---|---|---|
| `/api/v1/plivo/xml/{call_id}` | `GET`, `POST` | Plivo answer-XML endpoint. The path ID must parse as a UUID and match an existing Plivo call. On success it returns `application/xml` containing a Plivo `<Response><Dial><Sip>…</Sip></Dial></Response>` using `call_{call_id}` and the configured SIP host. If that SIP host is absent, it returns fallback `<Speak>` and `<Hangup>` XML instead. See [plivo.py](../apps/api/app/api/v1/plivo.py#L82). |
| `/api/v1/plivo/status/{call_id}` | `POST` | Plivo status callback. The path ID must parse as a UUID and match an existing Plivo call. It reads form field `CallStatus`; `Duration` or `CallDuration` is optional for final statuses. Recognized statuses update/finalize the database record; it returns JSON `{"ok": true}`. A missing or unrecognized status is not rejected and can still receive that success response without updating status. See [plivo.py](../apps/api/app/api/v1/plivo.py#L146). |
| `/api/v1/webhooks/telephony/{provider}/{call_id}` | `POST` | Generic signed call-status webhook. It accepts form data or JSON; status can be `Status` or `CallStatus`, and provider call ID can be `CallSid`, `CallUUID`, or `provider_call_id`. It requires the app-generated `sig` query value and returns JSON. The Plivo adapter’s configured answer/hangup URLs use the dedicated `/plivo/` routes, not this generic route. See [webhooks.py](../apps/api/app/api/v1/webhooks.py#L71). |
| Plivo recording callback | Not found | No recording callback route was found in the Plivo router. The Plivo provider currently maps `recording_url` to `None` in its call-status response ([plivo.py](../apps/api/app/providers/telephony/plivo.py#L113)). Asterisk ARI recording operations exist separately; they are not a Plivo HTTP callback route. |

The answer route performs a database lookup but does not itself call Plivo. Its XML instructs Plivo to create the SIP leg. The status route is state-changing: successful status callbacks commit call-state updates and can finalize a call. Neither route was invoked for this audit.

## 2. Authentication and signature requirements

- Both dedicated Plivo routes require a valid Plivo signature header. Accepted header names, in selection order, are `X-Plivo-Signature-V3`, `X-Plivo-Signature-V2`, and `X-Plivo-Signature` ([plivo.py](../apps/api/app/api/v1/plivo.py#L33)). V2/V3 nonce headers are `X-Plivo-Signature-V3-Nonce` or `X-Plivo-Signature-V2-Nonce` ([plivo.py](../apps/api/app/api/v1/plivo.py#L42)). The verifier uses configured `PLIVO_AUTH_TOKEN`; no value was displayed or checked here.
- The source verifier supports nonce-based SHA-256 for V2/V3 and parameter-based SHA-256 plus legacy SHA-1 for V1 ([signing.py](../apps/api/app/core/signing.py#L40)). Form parameters are included when the request content type indicates a form.
- It reconstructs candidate signed URLs from forwarded scheme/host headers, the request URL, and configured `BACKEND_URL` ([plivo.py](../apps/api/app/api/v1/plivo.py#L56)). A proxy/tunnel that changes the externally signed host, scheme, path, or query can therefore cause signature mismatch unless another candidate matches. The effective deployed `BACKEND_URL` and tunnel-forwarded headers were not inspected in this audit.
- The adapter appends an app-generated `sig` query parameter, HMAC-derived using `JWT_SECRET`, to both dedicated callback URLs ([provider plivo.py](../apps/api/app/providers/telephony/plivo.py#L33)). In the dedicated handlers this HMAC is optional: if present, an invalid value returns 401, but absence does not reject the request; the Plivo signature header remains mandatory ([plivo.py](../apps/api/app/api/v1/plivo.py#L101)). The generic telephony webhook does require its HMAC query value.
- The callback URLs are built from the supplied webhook URL’s origin, or fall back to `BACKEND_URL`; `call_service` uses `webhook_base_url` or `settings.BACKEND_URL` ([call_service.py](../apps/api/app/services/call_service.py#L101)). The Plivo adapter strips a supplied webhook URL down to scheme/authority, then appends its dedicated callback paths and per-call signature ([provider plivo.py](../apps/api/app/providers/telephony/plivo.py#L24)).
- Source review found that a failed Plivo signature check logs a short prefix of the received signature ([plivo.py](../apps/api/app/api/v1/plivo.py#L78)). Avoid sharing raw application logs; query strings and signature material should be redacted.

## 3. URL construction and configuration dependencies

- `BACKEND_URL` controls the public origin used when the call service has no explicit webhook base. The Plivo adapter constructs answer and status URLs under `/api/v1/plivo/xml/{call_id}` and `/api/v1/plivo/status/{call_id}`, respectively ([provider plivo.py](../apps/api/app/providers/telephony/plivo.py#L33)). The value itself is intentionally omitted. The prior post-tunnel report says the temporary URL was not persisted in application configuration at that time ([09-post-quick-tunnel-verification.md](./09-post-quick-tunnel-verification.md#L22)); whether the current runtime setting has since changed is unknown.
- `PLIVO_AUTH_TOKEN` is required for provider signature validation; `JWT_SECRET` signs the optional per-call query value; `SIP_PUBLIC_HOST` is required for the normal SIP-dial XML. If the SIP host is missing, the endpoint returns a spoken service-unavailable message and hangs up. Current effective container values are unverified.
- The operator previously reported a Quick Tunnel pointing to the backend on host port **8001** and a successful public `/health` response. Compose maps host port 8001 to backend container port **8000** ([docker-compose.yml](../docker-compose.yml#L26)). This port arrangement is compatible with that reported tunnel origin. A proxy on the same Compose network could instead target `backend:8000`; an external tunnel/proxy cannot use that Docker-only service name unless it shares that network.
- The prior `/health` success proves only that the tunnel reached a health responder at that time. No Plivo answer/status path, POST form, signature header, or signature canonicalization was tested. The current tunnel URL is not included here.

## 4. Cloudflare-to-backend routing compatibility

- The reported Quick Tunnel uses a single HTTP origin at the host-published backend port and has no application path rewrite in the reported command. This makes the source route paths structurally compatible, but path-specific HTTP POST forwarding and signature-header preservation remain **unverified**.
- The checked-in [nginx.conf](../infrastructure/nginx/nginx.conf#L4) is a template, not evidence of the active Quick Tunnel path. It proxies `/api/` to the backend and configures WebSocket upgrade under `/ws/`; no active Nginx proxy was established in the prior audit. If that template were used, the voice test socket under `/api/v1/voice/ws/test` would not match the `/ws/` upgrade location.
- Quick Tunnel `/health` success does not establish callback authentication compatibility or that Cloudflare/Tunnel currently routes the Plivo callback paths to FastAPI. No tunnel configuration or callback request was inspected in this audit.

## 5. WebSocket, SIP, RTP, and media requirements

- The dashboard event WebSocket is `/ws/events` (mounted at the app root, not under `/api/v1`) and requires an access JWT query parameter before accepting the socket ([ws.py](../apps/api/app/api/v1/ws.py#L13)).
- The local browser voice-test WebSocket is `/api/v1/voice/ws/test` (also a tenant-path variant); it exchanges binary 16 kHz PCM16 audio and JSON control/events ([voice_test.py](../apps/api/app/api/v1/voice_test.py#L29)). No route-level authentication dependency is present there. If the public tunnel exposes the whole backend, external accessibility of this test route is a security concern unless another verified access control protects it. That external control was not checked.
- Live telephone audio does not travel through the HTTPS Quick Tunnel. The ARI handler creates an Asterisk `externalMedia` channel using RTP over UDP and the `slin16` format, targeting the configured media host plus the Python media-session port ([event_handler.py](../apps/api/app/telephony/asterisk/event_handler.py#L169)). The media bridge binds a UDP socket and transmits/receives RTP ([rtp_bridge.py](../apps/api/app/voice_pipeline/rtp_bridge.py#L131)). This requires a separate reachable UDP path between Asterisk and the media worker; the HTTP tunnel cannot establish that path.
- The RTP allocator uses configured ports (source defaults 10000–10020) but falls back to an ephemeral UDP port when the pool is exhausted ([rtp_bridge.py](../apps/api/app/voice_pipeline/rtp_bridge.py#L268)). Compose publishes UDP 10000–10020 ([docker-compose.yml](../docker-compose.yml#L28)); reachability under exhaustion is therefore not established. SIP signaling to `SIP_PUBLIC_HOST` is also a separate path from the HTTPS callback tunnel.

## 6. Verified facts, unresolved questions, and blockers

**Verified from source or prior recorded evidence**

- Dedicated Plivo answer and status route definitions and methods are present in code.
- Provider URL builders use `BACKEND_URL` or a supplied webhook origin, and create separate answer/status paths.
- Source requires a Plivo signature header; the per-call HMAC query value is optional in the dedicated Plivo handlers.
- The operator previously reported a successful public `/health` request through the Quick Tunnel; the prior report records that as operator-provided evidence.
- Static tests exist for signature algorithms, callback URL building, XML structure, Plivo routing, and Asterisk call-ID/Stasis routing ([test_plivo.py](../apps/api/tests/test_plivo.py#L22), [test_plivo_outbound_routing.py](../apps/api/tests/test_plivo_outbound_routing.py#L34)). Tests were reviewed but not run.

**Not verified / blockers**

- No genuine Plivo-signed POST has been delivered; callback routing, required headers, form encoding, and signature canonicalization through the current Quick Tunnel remain unverified.
- The effective running `BACKEND_URL`, `SIP_PUBLIC_HOST`, and Plivo auth configuration were not inspected. The earlier report says the temporary tunnel URL was not persisted then; current configuration is unknown.
- Tunnel callback-path routing and any Cloudflare Access or other external guard are unknown. The unauthenticated-at-route-level voice-test WebSocket may be exposed by a public origin.
- SIP origination through the Plivo trunk, Asterisk acceptance, UDP RTP reachability in both directions, interruption handling, and call cleanup have not been proven by these callback checks.
- The status handler does not reject missing/unrecognized `CallStatus`, so it can return `{"ok": true}` without applying a call-state update.
- No Plivo recording callback is implemented in the inspected route/provider code.

## 7. Single safest next step

Read the effective (redacted) `BACKEND_URL` setting and the current Cloudflare Tunnel public-hostname/ingress mapping side by side, confirming that the tunnel targets the backend’s reachable host port 8001 and that the generated callback paths remain unchanged. Do this without invoking either callback route. Then review external access controls for the browser voice-test WebSocket before considering a separately approved callback test.

**Real outbound calling remains NOT READY.** No callback route was requested, no provider operation was made, and no call or media session was initiated for this audit.
