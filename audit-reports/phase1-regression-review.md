# Phase 1 — Broader Regression Review

**Date:** 2026-10-29  
**Reviewer:** Antigravity (read-only audit)  
**Scope:** Application-code changes from Phase 1 (rtp_bridge.py, event_handler.py, call.py) plus Fix 1 and Fix 2 patches.  
**Branch:** `main` (working tree only — uncommitted)

---

## 1. Repository State (`git status --short`)

26 modified tracked files + 7 untracked files of interest:

| Category | Count | Notes |
|----------|-------|-------|
| Modified tracked files (`M`) | 26 | All pre-existing user changes |
| Untracked test files (`??`) | 5 | `test_phase1_rtp_and_ari.py`, `test_kokoro_tts.py`, `test_voice_pipeline_regressions.py`, and standalone `test_*.py` at `apps/api/` root |
| Untracked `audit-reports/` dir | 1 | This report |
| Other untracked | misc | `.vscode/`, WAV diagnostic files, `docker-compose.test.yml`, scripts |

Phase 1 targeted files (`event_handler.py`, `rtp_bridge.py`) are among the 26 modified tracked files. `call.py` is untracked (matches `.gitignore` pattern `models/`).

---

## 2. Test Execution

### 2a. Command and result

```
python -m pytest tests/test_phase1_rtp_and_ari.py \
                 tests/test_plivo_outbound_routing.py \
                 tests/test_voice_pipeline_regressions.py \
                 --noconftest -p no:cacheprovider -v
```

**Result: 37 passed, 1 warning — exit 0 — 11.93 s**

### 2b. Test breakdown by file

| File | Tests | Result |
|------|-------|--------|
| `test_phase1_rtp_and_ari.py` | 11 | ✅ 11 passed |
| `test_plivo_outbound_routing.py` | 14 | ✅ 14 passed |
| `test_voice_pipeline_regressions.py` | 12 | ✅ 12 passed |
| **Total** | **37** | **✅ 37 passed** |

### 2c. Tests skipped (require isolated infrastructure)

The following test files require the full `conftest.py` preflight (isolated Docker Postgres + Redis containers with validated secret mounts). Running them without that infrastructure would either fail the preflight or silently connect to production data. They are excluded by `--noconftest`.

| File | Dependency | Safe to skip? |
|------|-----------|---------------|
| `test_workers.py` | `conftest.py` + PostgreSQL + Redis + Celery eager mode | ✅ Yes |
| `test_crm_sync.py` | `conftest.py` + live DB | ✅ Yes |
| `test_auth_rbac.py` | `conftest.py` | ✅ Yes |
| `test_isolation_and_calls.py` | `conftest.py` | ✅ Yes |
| `test_lead_import.py` | `conftest.py` | ✅ Yes |
| `test_dnc_and_pagination.py` | `conftest.py` | ✅ Yes |
| `test_eligibility.py` | `conftest.py` | ✅ Yes |
| `test_audit_and_api_keys.py` | `conftest.py` | ✅ Yes |
| `test_password_reset.py` | `conftest.py` | ✅ Yes |
| `test_google_sheets_and_custom_api.py` | `conftest.py` | ✅ Yes |
| `test_ssrf.py` | `conftest.py` | ✅ Yes |
| `test_voice_agent_security.py` | `conftest.py` | ✅ Yes |
| `test_voice_calling_end_to_end.py` | `conftest.py` + Asterisk ARI + live calls | ✅ Yes — also requires live telephony |
| `test_plivo.py` | Plivo API credentials | ✅ Yes |
| `test_real_stt_transcription.py` | Groq API credentials | ✅ Yes |
| `test_kokoro_tts.py` | Kokoro ONNX model files | ✅ Yes |

### 2d. Warning

```
RuntimeWarning: coroutine 'AsyncMockMixin._execute_mock_call' was never awaited
  event_handler.py:217: voice_session.set_transport_playback_controls(...)
```

This warning is cosmetic and pre-existing. `set_transport_playback_controls` is called synchronously in production; the test mocks it with `AsyncMock`, creating an unawaited coroutine. The test still passes; the production code path is correct.

---

## 3. Code Path Review

### 3a. Socket ownership — success, failure, hangup paths

| Path | Behaviour | Correct? |
|------|-----------|---------|
| `create_session` — normal | Port allocated, socket bound, `_allocated_sockets[port] = sock`, passed to session, `_sessions[call_id] = session` — all under lock | ✅ |
| `create_session` — `__init__` raises | `release_port(port)` closes socket and removes from `_allocated_sockets`; `_sessions` never populated; original exception re-raised | ✅ Fix 1 |
| `create_session` — `start()` raises | Same as above | ✅ Fix 1 |
| `close_session` — normal | `_sessions.pop`, `session.close()`, `_allocated_sockets.pop`, `sock.close()` — all under lock; idempotent | ✅ |
| `_handle_channel_hangup` — call_id found in mapping | `close_session(str(call_id))` called **before** DB query; safe even if DB returns None | ✅ Fix 2 |
| `_handle_channel_hangup` — call_id not in mapping, provider_call_id fallback succeeds | `close_session(str(call.id))` called after fallback DB lookup; `if call_id is None` guard prevents double-call | ✅ Fix 2 |
| `_cleanup_failed_stasis_start` | `media_session_manager.close_session(call_id)` inside `try/except`; each teardown step independently protected | ✅ |

**Residual risk:** If both the `_channel_to_call_id` path and the `provider_call_id` fallback path return different UUIDs for the same channel (impossible under current code, but theoretically possible with corrupt state), `close_session` would be called with the wrong UUID. Currently safe.

### 3b. Channel-to-call mapping staleness and premature removal

`_channel_to_call_id` is populated by `_handle_stasis_start` and consumed (popped) by:
- `_handle_channel_hangup` (line 348)
- `_cleanup_failed_stasis_start` (line 294)

**Staleness scenario:** If a `StasisEnd` or `ChannelHangupRequest` event fires for the *external media channel* (not the caller channel), `_handle_channel_hangup` runs with the media channel ID. That ID is never added to `_channel_to_call_id`, so `pop` returns `None` — safe, no stale entry is created. The actual caller channel hangup event fires separately and correctly pops its own entry.

**Premature removal:** Not possible — the map is populated once in `_handle_stasis_start` and popped once in `_handle_channel_hangup`. There is no concurrent writer to the same `channel_id` key.

### 3c. `provider_call_id` vs Asterisk channel ID separation

Confirmed via codebase search:

| Location | What `provider_call_id` holds |
|----------|-------------------------------|
| Plivo outbound calls | Plivo `request_uuid` — set by `call_service.py:75,162` from `DialResult.provider_call_id`; **never overwritten** by `event_handler.py` |
| Inbound SIP calls | Asterisk `channel_id` — set at `event_handler.py:141` (correct: inbound calls have no separate Plivo UUID) |
| `call.asterisk_channel_id` (property) | Asterisk `channel_id` for outbound Plivo calls — set at `event_handler.py:91` without touching `provider_call_id` |

No regression. The Phase 1 fix correctly replaced `call.provider_call_id = channel_id` with `call.asterisk_channel_id = channel_id` for the Plivo outbound path.

### 3d. Call finalization and post-call processing idempotency

`finalize_call` (`call_service.py:194`):
- Sets `call.status`, `call.ended_at`, `call.duration_seconds`, `call.end_reason`, updates `LeadCallState.calling_status`.
- Has **no early-exit guard** if call is already finalized; if called twice, it overwrites with the second invocation's values.

However, `_handle_channel_hangup` has an idempotency guard at line 366:
```python
if call.status in {"completed", "failed", "no_answer", "busy"}:
    return
```
This prevents `finalize_call` from being called twice for the same call, so double-finalization is not possible under normal operation.

`execute_post_call_pipeline` (`call_service.py:292`):
- Called once after `finalize_call` inside `if voice_session:` guard.
- If the post-call pipeline fails, the exception is caught and logged at `event_handler.py:394`; it does not propagate and does not re-run.

No regression.

### 3e. Database queries depending on transient `asterisk_channel_id` property

Full codebase search found `asterisk_channel_id` in only two files:
- `call.py` — property definition
- `event_handler.py:91` — setter (write-only)

No DB query anywhere uses `asterisk_channel_id` as a filter. The two DB lookup paths in `_handle_channel_hangup` and `_handle_channel_state_change` use `Call.id` (UUID) and `Call.provider_call_id` — both persisted columns. **No query depends on the transient property.**

### 3f. Test assertion quality

Reviewed for common mocking anti-patterns:

| Test | Assertion type | Hides failures? |
|------|---------------|-----------------|
| `test_create_session_releases_port_on_init_failure` | Checks `len(mgr._allocated_sockets) == 0` directly against the manager's own dict | ✅ No |
| `test_create_session_releases_port_on_start_failure` | Same | ✅ No |
| `test_hangup_closes_media_session_when_db_returns_none` | `mock_close.assert_called_once_with(str(call_id))` — exact call args verified | ✅ No |
| `test_hangup_closes_media_session_via_provider_call_id_fallback` | `mock_close.assert_called_once_with(str(call_uuid))` | ✅ No |
| `test_stasis_start_preserves_plivo_request_uuid` | Checks that `mock_call.provider_call_id` is unchanged **and** `asterisk_channel_id` is set to channel_id | ✅ No |
| All cleanup tests | Check map state post-cleanup via `assert key not in dict` | ✅ No |

No test relies on `assert True` / `assert mock.called` alone. All assertions are specific.

---

## 4. Skipped Test Categories — Rationale

| Category | Reason |
|----------|--------|
| DB-backed integration tests | Require `conftest.py` preflight with isolated Docker Postgres/Redis; running without it would silently use production DB or fail opaquely |
| `test_voice_calling_end_to_end.py` | Explicitly connects to Asterisk ARI and places outbound calls via Plivo API |
| `test_real_stt_transcription.py` | Requires live Groq API key and network access |
| `test_plivo.py` | Requires live Plivo credentials |
| Standalone `test_*.py` at `apps/api/` root | Not under `tests/`; likely ad-hoc diagnostic scripts; not part of the managed test suite |

---

## 5. Remaining Risks

| # | Risk | Severity | Mitigation |
|---|------|----------|-----------|
| R1 | `asterisk_channel_id` lost on process restart — Plivo outbound calls become uncorrelatable until the next `StasisStart` re-seeds `_channel_to_call_id` | **Medium** | Phase 2 migration: `ALTER TABLE calls ADD COLUMN asterisk_channel_id VARCHAR(255)` |
| R2 | `_channel_to_call_id` is a module-level dict — not shared across multiple uvicorn workers or Celery workers | **Medium** | Single ARI WebSocket listener is expected to handle all events for its process; multi-worker deployment must ensure ARI events are pinned to one worker |
| R3 | `finalize_call` has no idempotency at the SQL level — only the in-memory status guard prevents double-write | **Low** | Guard is reliable within one process; cross-process redundancy not an issue for current single-worker ARI design |
| R4 | Cosmetic `RuntimeWarning` in `test_stasis_start_preserves_plivo_request_uuid` | **Cosmetic** | Fix: change `mock_vs_create` to return a `MagicMock` with `set_transport_playback_controls` as a sync method |
| R5 | 15 test files not run — DB-backed, telephony, external API | **Informational** | Must run with `docker-compose.test.yml` preflight in a proper staging environment before production deployment |

---

## 6. Verdict

> **PASS WITH CONCERNS**

All 37 runnable isolated tests pass. No regressions introduced by Phase 1 or the Fix 1/Fix 2 patches. Code-path analysis confirms correct socket ownership, channel mapping integrity, `provider_call_id` separation, and idempotent finalization.

The two Medium concerns (R1, R2) are pre-existing architectural constraints, not regressions from these patches.

---

## 7. Minimum Next Steps Before Controlled Staging Tests

1. **Stand up the isolated test stack** (`docker-compose.test.yml`) and run all DB-backed tests with valid `TEST_*` environment variables and secret mounts. This validates CRM sync, lead import, call worker, RBAC, and other flows that cannot be mocked.

2. **Apply the `asterisk_channel_id` migration** (R1):
   ```sql
   ALTER TABLE calls ADD COLUMN asterisk_channel_id VARCHAR(255);
   CREATE INDEX ix_calls_asterisk_channel_id ON calls (asterisk_channel_id);
   ```
   Then replace the `@property` in `call.py` with a proper `mapped_column`.

3. **Confirm single-worker ARI constraint** (R2): Document or enforce that the ARI WebSocket listener and the `_channel_to_call_id` dict must live in the same OS process. If multi-worker uvicorn is used, pin ARI events to worker 0 or move the dict to Redis.

4. **Fix the cosmetic test warning** (R4): Change `create_live_voice_session` mock to return a `MagicMock` with sync `set_transport_playback_controls`.

5. **End-to-end staging test** with real Plivo call and Asterisk: place one outbound call, verify `_channel_to_call_id` populates, hangup fires, media session closes, `finalize_call` runs, transcript is stored.

---

*Generated: 2026-10-29 by Antigravity (read-only audit)*

