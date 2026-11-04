# Staging Setup Plan — Isolated Test Environment

**Date:** 2026-10-29  
**Author:** Antigravity (read-only inspection)  
**Status:** PLAN ONLY — nothing started, no files modified

---

## 1. Repository State Confirmed

### `docker-compose.test.yml` — **Already exists**

> [!IMPORTANT]
> `docker-compose.test.yml` **already exists** at the repository root and is well-designed. It must **not** be recreated or modified.

Key properties of the existing file:

| Property | Value |
|----------|-------|
| Compose project name | `voxai-riya-test` |
| PostgreSQL port | `127.0.0.1:55432:5432` (loopback only) |
| Redis port | `127.0.0.1:56379:6379` (loopback only) |
| PostgreSQL image | `pgvector/pgvector:pg16` (matches dev stack) |
| Redis image | `redis:7-alpine` (matches dev stack) |
| Network | `voxai-riya-test_test_only` — marked `internal: true` (no external network access) |
| Volume names | `voxai-riya-test_postgres_test_data`, `voxai-riya-test_redis_test_data` |
| Credentials | Injected exclusively via Docker Secrets from host files — **not embedded in the file** |
| DB name / user | `voxai_test` / `voxai_test` — completely separate from dev `voxai`/`voxai` |

The file is safe, correct, and complete. **No changes required.**

---

## 2. Isolation Architecture

```mermaid
flowchart TD
    subgraph "Test Process (Windows host)"
        T["pytest --noconftest=OFF\napps/api/tests/"]
        T --> PG["127.0.0.1:55432\n(postgres-test container)"]
        T --> RD["127.0.0.1:56379\n(redis-test container)"]
    end

    subgraph "voxai-riya-test Compose stack"
        PG
        RD
        NET["test_only network\n(internal: true)"]
        PG --- NET
        RD --- NET
    end

    subgraph "Dev / Prod services (untouched)"
        DEV["postgres:5432\nredis:6379\nbackend:8001"]
    end

    T -. "never touches" .-> DEV
```

### Why the existing design prevents production access

1. **Separate ports** — test stack listens on `55432` (Postgres) and `56379` (Redis). Dev stack uses `5432` and `6379`. Port collision is impossible.
2. **Separate Docker project name** (`voxai-riya-test` vs default `ai-final-voice-agent`) — named volumes and networks cannot collide.
3. **Separate credentials** — `voxai_test`/`voxai_test` vs dev `voxai`/`voxai`. Even if both Postgres instances run simultaneously, a test connection cannot authenticate against the dev instance.
4. **`internal: true` network** — the `test_only` bridge network has no external routing. Containers cannot reach the internet or other host services during the test lifecycle.
5. **`conftest.py` preflight** — `_perform_isolated_preflight()` validates all of the above at runtime before any test fixture runs. It checks container labels, network internality, port bindings, secret mounts, and DB credentials. Any mismatch raises `RuntimeError` and aborts the test run.
6. **`conftest.py` clean fixture** — truncates all public tables and `FLUSHDB`s Redis before every test. This guarantees test independence and destroys any leftover data.
7. **loopback bind (`127.0.0.1:`)** — both exposed ports are bound to localhost only. They cannot be reached from the Oracle VM, Tailscale peers, or the internet.

---

## 3. Test File Classification

### Group A — Require `conftest.py` preflight (PostgreSQL + Redis)

These 11 files use `client`, `tenant_a`, `tenant_b`, `migrated`, `clean` fixtures from `conftest.py` and interact with a real database.

| File | What it tests |
|------|--------------|
| `test_auth_rbac.py` | Signup, login, JWT refresh, session management |
| `test_audit_and_api_keys.py` | Audit trail, API key creation and scoping |
| `test_crm_sync.py` | CRM provider sync, webhook HMAC, lead normalization |
| `test_dnc_and_pagination.py` | Do-not-call list, pagination, cursor correctness |
| `test_eligibility.py` | Lead eligibility engine, business-rule evaluation |
| `test_google_sheets_and_custom_api.py` | OAuth failure cases, custom API URL validation |
| `test_isolation_and_calls.py` | Cross-tenant isolation, call queueing, pre-dial checks |
| `test_lead_import.py` | CSV/Excel lead import, deduplication, field mapping |
| `test_password_reset.py` | Single-use token, session revocation, enumeration protection |
| `test_voice_agent_security.py` | Full orchestrator turn with DB-backed tool execution |
| `test_workers.py` | Celery tasks (eager mode) against real test DB + Redis |

### Group B — Require `conftest.py` + external services

| File | Extra dependency | Safe to skip in isolation? |
|------|-----------------|---------------------------|
| `test_voice_calling_end_to_end.py` | Live Asterisk ARI + DB | ✅ Skip — needs telephony profile |
| `test_real_stt_transcription.py` | Groq API key + network | ✅ Skip — network required |
| `test_plivo.py` | Mostly mocked; reads `settings` only | ⚠️ **Partially safe** — see §4 |

### Group C — No database dependency (already passing without stack)

| File | Status |
|------|--------|
| `test_phase1_rtp_and_ari.py` | ✅ 11/11 passed |
| `test_plivo_outbound_routing.py` | ✅ 14/14 passed |
| `test_voice_pipeline_regressions.py` | ✅ 12/12 passed |
| `test_ssrf.py` | ✅ No DB — can run anytime |
| `test_kokoro_tts.py` | ✅ No DB — needs Kokoro model files locally |

---

## 4. Required Environment Variables

The `conftest.py` preflight reads these from the shell environment (not from `.env`):

| Variable | Purpose | Example value |
|----------|---------|---------------|
| `TEST_DATABASE_URL` | Sync psycopg2 URL for Alembic + `create_engine` | `postgresql+psycopg2://voxai_test:<pw>@127.0.0.1:55432/voxai_test` |
| `TEST_ASYNC_DATABASE_URL` | Async asyncpg URL for `AsyncSessionLocal` | `postgresql+asyncpg://voxai_test:<pw>@127.0.0.1:55432/voxai_test` |
| `TEST_REDIS_URL` | Redis URL for app session / rate-limiting | `redis://voxai_test:<pw>@127.0.0.1:56379/0` |
| `TEST_CELERY_BROKER_URL` | Celery broker (same Redis, DB 0) | `redis://voxai_test:<pw>@127.0.0.1:56379/0` |
| `TEST_CELERY_RESULT_BACKEND` | Celery result backend (same Redis, DB 0) | `redis://voxai_test:<pw>@127.0.0.1:56379/0` |
| `VOXAI_TEST_POSTGRES_PASSWORD_FILE` | Absolute path to a file containing the PG password | e.g. `C:\secrets\voxai_test_pg.txt` |
| `VOXAI_TEST_REDIS_ACL_FILE` | Absolute path to the Redis ACL file | e.g. `C:\secrets\voxai_test_redis.acl` |
| `VOXAI_TEST_REDIS_PASSWORD_FILE` | Absolute path to a file containing the Redis password | e.g. `C:\secrets\voxai_test_redis_pw.txt` |

**Additional env vars** set by `conftest.py` at runtime (override what's in `.env`):

```python
os.environ.update({
    "ENV": "test",
    "DATABASE_URL": os.environ["TEST_DATABASE_URL"],
    "ASYNC_DATABASE_URL": os.environ["TEST_ASYNC_DATABASE_URL"],
    "REDIS_URL": os.environ["TEST_REDIS_URL"],
    "CRM_PROVIDER_MODE": "mock",
    "ALLOW_DEMO_SEED": "true",
    "CELERY_BROKER_URL": os.environ["TEST_CELERY_BROKER_URL"],
    "CELERY_RESULT_BACKEND": os.environ["TEST_CELERY_RESULT_BACKEND"],
})
```

These overrides mean **even if `.env` contains production URLs, the test run uses the test URLs**. This is an important safety layer.

---

## 5. Secret File Format Requirements

### PostgreSQL password file (`VOXAI_TEST_POSTGRES_PASSWORD_FILE`)
```
<password>
```
A single line containing the password only. No trailing spaces. This must match the password encoded in the `TEST_DATABASE_URL` (percent-encoded if it contains special characters).

### Redis ACL file (`VOXAI_TEST_REDIS_ACL_FILE`)
```
user default off
user voxai_test on #<64-char-lowercase-hex-SHA256-of-password> -@all ~ratelimit:* ~lead-call-lock:* &events:* +ping +flushdb +incr +expire +set +del +publish
```

The SHA-256 must be computed from the raw password bytes. Example (PowerShell):
```powershell
$pw = "YourTestPassword123"
$hash = [System.Security.Cryptography.SHA256]::Create().ComputeHash(
    [System.Text.Encoding]::UTF8.GetBytes($pw)
)
-join ($hash | ForEach-Object { $_.ToString("x2") })
```

### Redis password file (`VOXAI_TEST_REDIS_PASSWORD_FILE`)
```
<password>
```
Same password used in the ACL file. Must match `TEST_REDIS_URL` credential.

> [!CAUTION]
> Do **not** put passwords in shell history, environment files, or version control. Use secret files on a path outside the repository, with restrictive file permissions (`icacls /inheritance:d /grant:r "%USERNAME%:R"`).

---

## 6. Alembic Migration Path

`alembic/env.py` reads `settings.DATABASE_URL`. Since `conftest.py` overrides `DATABASE_URL` to `TEST_DATABASE_URL` before the `migrated` fixture runs, Alembic automatically targets the test database.

Migration chain (6 revisions):
```
0001_initial_schema
  → 0002_add_pages_processed_to_crm_sync_jobs
    → 0003_add_lead_import_jobs_table
      → 0004_manual_do_not_call
        → 0005_lead_custom_fields
          → 0006_integration_credentials
```

The `migrated` fixture (session-scoped, autouse) runs `alembic upgrade head` once per test session. The `clean` fixture (function-scoped, autouse) truncates all tables and flushes Redis before each individual test.

> [!NOTE]
> The pending Phase 2 migration (`asterisk_channel_id` column) is **not yet in the migration chain**. The test suite will run against the 6-revision schema. This is acceptable — the `@property` implementation in `call.py` handles the missing column safely.

---

## 7. Exact Commands (for review — do not execute yet)

### Step 1 — Create secret files (once, outside the repo)

```powershell
# Choose a strong random password (32+ chars)
$pw = -join ((65..90 + 97..122 + 48..57) | Get-Random -Count 32 | ForEach-Object { [char]$_ })

# Create a secrets directory outside the repo
New-Item -ItemType Directory -Path "C:\secrets\voxai-test" -Force

# Write the password file
$pw | Out-File -FilePath "C:\secrets\voxai-test\pg_password.txt" -NoNewline -Encoding utf8

# Compute SHA-256 for ACL
$hash = -join ([System.Security.Cryptography.SHA256]::Create().ComputeHash(
    [System.Text.Encoding]::UTF8.GetBytes($pw)
) | ForEach-Object { $_.ToString("x2") })

# Write ACL file
@"
user default off
user voxai_test on #$hash -@all ~ratelimit:* ~lead-call-lock:* ``&events:* +ping +flushdb +incr +expire +set +del +publish
"@ | Out-File -FilePath "C:\secrets\voxai-test\redis.acl" -NoNewline -Encoding ascii

# Write Redis password file (same password)
$pw | Out-File -FilePath "C:\secrets\voxai-test\redis_password.txt" -NoNewline -Encoding utf8
```

### Step 2 — Set environment variables (shell session only — not persisted)

```powershell
# Percent-encode the password for URL use (simplified — adjust if pw contains special chars)
$pw = Get-Content "C:\secrets\voxai-test\pg_password.txt" -Raw

$env:VOXAI_TEST_POSTGRES_PASSWORD_FILE = "C:\secrets\voxai-test\pg_password.txt"
$env:VOXAI_TEST_REDIS_ACL_FILE         = "C:\secrets\voxai-test\redis.acl"
$env:VOXAI_TEST_REDIS_PASSWORD_FILE    = "C:\secrets\voxai-test\redis_password.txt"

# URL-encode password (replace special chars as needed)
$pw_encoded = [Uri]::EscapeDataString($pw)

$env:TEST_DATABASE_URL         = "postgresql+psycopg2://voxai_test:${pw_encoded}@127.0.0.1:55432/voxai_test"
$env:TEST_ASYNC_DATABASE_URL   = "postgresql+asyncpg://voxai_test:${pw_encoded}@127.0.0.1:55432/voxai_test"
$env:TEST_REDIS_URL            = "redis://voxai_test:${pw_encoded}@127.0.0.1:56379/0"
$env:TEST_CELERY_BROKER_URL    = "redis://voxai_test:${pw_encoded}@127.0.0.1:56379/0"
$env:TEST_CELERY_RESULT_BACKEND = "redis://voxai_test:${pw_encoded}@127.0.0.1:56379/0"
```

### Step 3 — Start the isolated test stack

```powershell
cd "c:\Users\Aman\OneDrive - VVNT\Documents\ChatGPT\AI-Final-Voice-Agent"
docker compose -f docker-compose.test.yml -p voxai-riya-test up -d --wait
```

`--wait` blocks until both healthchecks pass. Expected: ~15-30 seconds.

### Step 4 — Verify isolation (before running any tests)

```powershell
# Confirm containers belong to the test project and NOT the dev project
docker ps --filter "label=com.docker.compose.project=voxai-riya-test" --format "table {{.Names}}\t{{.Ports}}\t{{.Status}}"

# Confirm the test network is internal (no gateway)
docker network inspect voxai-riya-test_test_only --format "Internal: {{.Internal}}"

# Confirm test DB is reachable and is NOT the dev DB
docker exec voxai-riya-test-postgres-test-1 psql -U voxai_test -c "SELECT current_database(), current_user, inet_server_addr(), inet_server_port();"
```

### Step 5 — Run Group A + C DB-backed tests

```powershell
cd "c:\Users\Aman\OneDrive - VVNT\Documents\ChatGPT\AI-Final-Voice-Agent\apps\api"
python -m pytest tests/ `
    --ignore=tests/test_voice_calling_end_to_end.py `
    --ignore=tests/test_real_stt_transcription.py `
    --ignore=tests/test_kokoro_tts.py `
    -p no:cacheprovider -v
```

> [!NOTE]
> `conftest.py` is now loaded (no `--noconftest`). The preflight validates the running containers before any test fixture executes. If anything is misconfigured it aborts cleanly.

### Step 6 — Teardown (after all tests pass)

```powershell
docker compose -f docker-compose.test.yml -p voxai-riya-test down -v
```

`-v` removes the named volumes (`voxai-riya-test_postgres_test_data`, `voxai-riya-test_redis_test_data`), destroying all test data. The dev stack volumes are untouched.

---

## 8. Test Files Excluded and Why

| File | Reason for exclusion |
|------|---------------------|
| `test_voice_calling_end_to_end.py` | Calls `AsteriskARIClient` against live Asterisk; imports `FasterWhisperSTTProvider` and `ChatterboxEngine`; requires telephony profile and real ARI connection |
| `test_real_stt_transcription.py` | Sends audio to live Groq Whisper API — requires `GROQ_API_KEY` and network bypass |
| `test_kokoro_tts.py` | Requires Kokoro ONNX model files at `models/kokoro/kokoro-v1.0.onnx` and `voices-v1.0.bin`; no DB dependency — run separately with `--noconftest` if models are present |

---

## 9. Port Conflict Pre-check

Before starting the test stack, verify ports 55432 and 56379 are free:

```powershell
netstat -ano | Select-String ":55432|:56379"
# Expected: no output (both ports free)
```

If either port is occupied, the Docker container will fail to bind and the healthcheck will time out without affecting any other service.

---

## 10. Risks

| # | Risk | Likelihood | Impact | Mitigation |
|---|------|-----------|--------|-----------|
| R1 | Port 55432 or 56379 already in use (e.g., another Postgres or Redis) | Low | Test stack fails to start — dev stack unaffected | Pre-check ports; stop conflicting process |
| R2 | Alembic connects to wrong DB if env vars not set before pytest | Medium | Migrations run against dev DB | `conftest.py` preflight validates URLs before any fixture runs — safe fail |
| R3 | `clean` fixture truncates tables — data loss if pointed at wrong DB | Low (preflight guards against it) | Catastrophic if it hits prod | Double-protected: secret validation + URL validation + separate credentials |
| R4 | `docker-compose.test.yml` `down -v` deletes volumes — intended | Certain | Destroys test-only data — by design | Volumes are named `voxai-riya-test_*`; dev volumes named `pgdata` — no collision |
| R5 | Windows Docker Desktop disk space for extra volumes (~500 MB) | Very low | Docker Desktop warning | Pre-check with `docker system df` |
| R6 | Phase 2 migration (`asterisk_channel_id`) not yet in Alembic chain | Certain | `test_voice_calling_end_to_end.py` may fail if it inserts to that column | Column is a transient `@property` — no SQL column required yet; safe |

---

## 11. Recommendation

> [!NOTE]
> **✅ SAFE TO PROCEED**

The isolated test stack **can be created and run safely**. The design is already in place (`docker-compose.test.yml` exists and is correct). No application code, `.env`, or production configuration needs to change.

The only action required before execution:
1. Create three secret files outside the repository (§5, §7 Step 1)
2. Set eight environment variables in the PowerShell session (§7 Step 2)
3. Start the two containers (§7 Step 3)
4. Run the test suite (§7 Step 5)

The `conftest.py` preflight provides a hard safety gate that will abort the run if any configuration deviates from the expected isolated setup.

**Recommended exclusions for the first run:**  
`test_voice_calling_end_to_end.py`, `test_real_stt_transcription.py`, `test_kokoro_tts.py`

**Estimated first-run time:** 3–8 minutes (migrations + 11 DB-backed test files)

---

*Generated: 2026-10-29 | Read-only inspection | No files modified | No services started*

