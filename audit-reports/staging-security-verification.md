# Staging Security Verification Report

**Date:** 2026-10-29
**Scope:** `docker-compose.test.yml` + `audit-reports/staging-setup-plan.md`
**Method:** Read-only inspection — no files created/modified, no containers started

---

## Overall Verdict: ⚠️ FAIL (1 blocker, 2 caveats)

| # | Item | Verdict |
|---|------|---------|
| 1 | Secret file paths — Windows / Docker Desktop compatibility | ⚠️ PASS with caveat |
| 2 | Postgres / Redis cannot access prod data | ✅ PASS |
| 3 | `internal: true` network and test reachability | ✅ PASS |
| 4 | Redis ACL vs actual commands used | ❌ FAIL — blocker |
| 5 | PostgreSQL init, password-file encoding, connection URLs | ⚠️ PASS with caveat |
| 6 | 8 required environment variables | ✅ PASS |
| 7 | `docker compose down -v` safety and safer alternative | ✅ PASS (with recommendation) |
| 8 | Tests that contact external services or use non-test credentials | ✅ PASS (3 files excluded) |

---

## Item 1 — Secret File Paths (Windows / Docker Desktop)

**Verdict: ⚠️ PASS with caveat**

Docker Desktop ≥ 4.x correctly bind-mounts Windows host paths for Compose `secrets.file:` directives, translating them to Linux paths inside the container.

**Caveat — spaces in path:**
The project root is:
```
C:\Users\Aman\OneDrive - VVNT\Documents\ChatGPT\AI-Final-Voice-Agent
```
The space in `OneDrive - VVNT` is safe for the source code directory because Docker never mounts the project root as a secret. However, **the env vars that point to secret files** (e.g., `VOXAI_TEST_POSTGRES_PASSWORD_FILE`) must point to a path **without spaces** to avoid shell-quoting failures in the Docker Compose secrets resolver.

**Required:** Create secret files at a path with no spaces, e.g.:
```
C:\secrets\voxai-test\postgres_password.txt
C:\secrets\voxai-test\redis_acl.txt
C:\secrets\voxai-test\redis_password.txt
```

**Caveat — BOM encoding on Windows PowerShell 5.x:**
`Out-File -Encoding utf8` in PowerShell 5.x silently prepends a UTF-8 BOM. The PostgreSQL entrypoint reads the password file raw — a leading BOM (`0xEF 0xBB 0xBF`) becomes part of the password and causes authentication failures.

**Required:** Use one of:
```powershell
# PowerShell 7+
"mypassword" | Out-File -FilePath C:\secrets\voxai-test\postgres_password.txt -NoNewline -Encoding utf8NoBOM

# PowerShell 5.x (safest)
[System.IO.File]::WriteAllText("C:\secrets\voxai-test\postgres_password.txt", "mypassword", [System.Text.Encoding]::UTF8)
```
> [!CAUTION]
> Do NOT use `Out-File -Encoding utf8` on PowerShell 5.x — it writes a BOM that silently corrupts the password.

---

## Item 2 — Postgres / Redis Cannot Access Prod Data

**Verdict: ✅ PASS**

Isolation is enforced by four independent layers:

| Layer | Detail |
|-------|--------|
| **Port binding** | Postgres: `127.0.0.1:55432:5432` (dev uses 5432); Redis: `127.0.0.1:56379:6379` (dev uses 6379) |
| **Separate credentials** | Test DB uses `voxai_test` user / `voxai_test_db` database; dev uses different credentials |
| **Separate named volumes** | Test: `voxai-riya-test_postgres_test_data`, `voxai-riya-test_redis_test_data`; Dev: `pgdata`. No name overlap. |
| **Project namespace** | Test project: `voxai-riya-test`; dev project defaults to the directory name. All resource names are namespaced. |

Additionally, `conftest.py`'s `_perform_isolated_preflight()` validates network internality, secret mounts, and credentials before any fixture runs — a misconfiguration would abort the test run, not silently connect to prod.

---

## Item 3 — `internal: true` Network and Test Reachability

**Verdict: ✅ PASS**

`internal: true` in `docker-compose.test.yml` prevents the test containers from initiating **outbound** connections to the host network or internet. It does **not** block inbound host→container connections via published ports.

Tests run on the **Windows host** (not inside a container), so they connect via:
- `127.0.0.1:55432` → Postgres test container (published port)
- `127.0.0.1:56379` → Redis test container (published port)

Both are reachable from the host. The `internal` flag prevents the containers from calling out to, for example, dev Postgres on port 5432 or an external Plivo endpoint.

> [!NOTE]
> `conftest.py` preflight also calls `docker network inspect` to verify `internal: true` is in effect at runtime. If the flag were removed, preflight would abort.

---

## Item 4 — Redis ACL vs Actual Commands Used

**Verdict: ❌ FAIL — BLOCKER**

### Commands actually used by application code

| File | Commands | Key/Channel Pattern |
|------|----------|-------------------|
| `rate_limit.py` | `INCR`, `EXPIRE` | `ratelimit:{key}:{window}` |
| `eligibility_service.py` | `SET` (NX+EX), `DEL` | `lead-call-lock:{tenant_id}:{lead_id}` |
| `bus.py` | `PUBLISH`, **`SUBSCRIBE`** | `events:{tenant_id}` |
| `main.py` | `PING` | — |
| `conftest.py` clean fixture | `FLUSHDB` | — |

### ACL in `docker-compose.test.yml`

```
user voxai_test on #<sha256> -@all
  ~ratelimit:* ~lead-call-lock:*
  &events:*
  +ping +flushdb +incr +expire +set +del +publish
```

### Gap analysis

| Command | In ACL? | Used? | Status |
|---------|---------|-------|--------|
| `PING` | ✅ `+ping` | ✅ | OK |
| `INCR` | ✅ `+incr` | ✅ | OK |
| `EXPIRE` | ✅ `+expire` | ✅ | OK |
| `SET` (NX+EX) | ✅ `+set` | ✅ | OK |
| `DEL` | ✅ `+del` | ✅ | OK |
| `PUBLISH` | ✅ `+publish` | ✅ | OK |
| `FLUSHDB` | ✅ `+flushdb` | ✅ | OK |
| **`SUBSCRIBE`** | ❌ **missing** | ✅ `bus.py:40` | **FAIL** |

### Why this is a blocker

In Redis 7, `-@all` blocks every command not explicitly listed. The `SUBSCRIBE` command belongs to the `@pubsub` category. The `&events:*` channel pattern controls *which channels* a user may subscribe to, but the `SUBSCRIBE` **command** itself must also be explicitly allowed.

Any test exercising WebSocket endpoints will trigger `bus.py → redis_client.subscribe("events:...")`, which will receive `NOPERM` from Redis and raise an exception.

### Required fix (for user review — do not apply automatically)

Add `+subscribe` to the ACL line in `docker-compose.test.yml`:

```diff
-  +ping +flushdb +incr +expire +set +del +publish
+  +ping +flushdb +incr +expire +set +del +publish +subscribe
```

> [!IMPORTANT]
> Do not modify the ACL file until this gap has been reviewed and approved. Confirm whether any test path actually reaches `subscribe` in the current test subset (e.g., if WebSocket tests are excluded, this may be deferred).

---

## Item 5 — PostgreSQL Init, Password-File Encoding, Connection URLs

**Verdict: ⚠️ PASS with caveat**

### Password file mechanism

`POSTGRES_PASSWORD_FILE: /run/secrets/test_postgres_password` is the official Docker entrypoint `_file` variable mechanism — correct. The entrypoint reads the file content and sets `POSTGRES_PASSWORD` from it before initialization.

**Requirements:**
- File must be readable inside the container (bind-mount via secrets: confirmed)
- Content: plain ASCII password, no BOM, single trailing newline acceptable
- BOM caveat: see Item 1 above

### Connection URL encoding

If the test password contains special characters (`@`, `/`, `:`, `#`, `?`, `%`), they **must** be percent-encoded in the URLs:

```
TEST_DATABASE_URL=postgresql://voxai_test:p%40ssword@127.0.0.1:55432/voxai_test_db
TEST_ASYNC_DATABASE_URL=postgresql+asyncpg://voxai_test:p%40ssword@127.0.0.1:55432/voxai_test_db
```

**Recommendation:** Use a password composed only of alphanumeric characters and `-_` to avoid encoding issues entirely.

### Alembic migration chain

Current migration chain: `0001` → `0002` → `0003` → `0004` → `0005` → `0006`. The Phase 2 `asterisk_channel_id` column is **not yet in the migration chain** — it is handled by a `@property` in `call.py`. `alembic upgrade head` will reach `0006` and stop safely. No risk of applying an unintended migration.

---

## Item 6 — 8 Required Environment Variables

**Verdict: ✅ PASS**

All 8 variables are identified with their non-secret purpose:

| Variable | Non-secret Purpose |
|----------|--------------------|
| `TEST_DATABASE_URL` | Sync psycopg2 connection URL used by Alembic (`alembic upgrade head`) and SQLAlchemy `create_engine` for synchronous fixtures |
| `TEST_ASYNC_DATABASE_URL` | Async asyncpg connection URL (must use `postgresql+asyncpg://` scheme) used by `AsyncSessionLocal` in FastAPI dependencies |
| `TEST_REDIS_URL` | Redis URL for app-level session, rate limiting (`rate_limit.py`), and event bus (`bus.py`) during tests |
| `TEST_CELERY_BROKER_URL` | Celery task queue broker — points to Redis test instance (DB 1 in dev; conftest overrides to test Redis) |
| `TEST_CELERY_RESULT_BACKEND` | Celery result storage backend — points to Redis test instance (DB 2 in dev; conftest overrides) |
| `VOXAI_TEST_POSTGRES_PASSWORD_FILE` | **Host path** to the file containing the test Postgres password; consumed by Docker Compose `secrets.file:` |
| `VOXAI_TEST_REDIS_ACL_FILE` | **Host path** to the Redis ACL configuration file; bind-mounted into the Redis container at startup |
| `VOXAI_TEST_REDIS_PASSWORD_FILE` | **Host path** to the Redis password file; used in the Redis healthcheck (`redis-cli --user voxai_test`) |

> [!NOTE]
> `conftest.py` overrides `DATABASE_URL`, `ASYNC_DATABASE_URL`, `REDIS_URL`, `CELERY_BROKER_URL`, and `CELERY_RESULT_BACKEND` at module import time using `os.environ`. The test containers must be **already running** before `pytest` is invoked, as `conftest.py` preflight connects to them at collection time.

---

## Item 7 — `docker compose down -v` Safety

**Verdict: ✅ PASS (with recommendation)**

### Volume namespace analysis

| Volume | Project | Name on Docker host |
|--------|---------|-------------------|
| Dev Postgres | (dir-named, e.g. `ai-final-voice-agent`) | `ai-final-voice-agent_pgdata` |
| Test Postgres | `voxai-riya-test` | `voxai-riya-test_postgres_test_data` |
| Test Redis | `voxai-riya-test` | `voxai-riya-test_redis_test_data` |

The dev `pgdata` volume is in a **different project namespace** from the test volumes. `docker compose down -v` only removes volumes declared in and owned by the targeted project.

**Risk:** Running bare `docker compose down -v` from the project root (where `docker-compose.yml` is the default file) would target the **dev** project and attempt to remove `pgdata`.

### Safer teardown command

Always use explicit flags:

```powershell
docker compose -f docker-compose.test.yml -p voxai-riya-test down --volumes
```

This is unambiguous regardless of which directory it is run from.

> [!CAUTION]
> Never run `docker compose down -v` (bare, without `-f` and `-p`) from the project root while the dev stack is active. This would attempt to delete `pgdata`.

---

## Item 8 — Tests Contacting External Services

**Verdict: ✅ PASS (with 3 exclusions)**

| Test File | External Contact | Action |
|-----------|-----------------|--------|
| `test_voice_calling_end_to_end.py` | ✅ Connects to live Asterisk ARI; imports Chatterbox/FasterWhisper | **EXCLUDE** — requires live Oracle VM |
| `test_real_stt_transcription.py` | ✅ Calls live Groq Whisper API; requires `GROQ_API_KEY` | **EXCLUDE** — requires network + API key |
| `test_kokoro_tts.py` | Requires Kokoro model files at `models/kokoro/`; no DB, no network | **EXCLUDE** unless model files are confirmed present |
| `test_plivo.py` | All calls mocked with `unittest.mock`; reads `settings` but never dials | ✅ SAFE — include |
| All other 11 DB-backed tests | Use `conftest.py` fixtures; no external calls | ✅ SAFE — include |

The `conftest.py` preflight calls `docker ps`, `docker inspect`, and `docker network inspect` — these are read-only Docker CLI calls. They require Docker Desktop to be running but do not start or modify containers.

> [!NOTE]
> `test_plivo.py` reads `settings.PLIVO_AUTH_ID` and `settings.PLIVO_AUTH_TOKEN` from the environment. `conftest.py` does **not** override these. Ensure they are set to dummy/non-production values before running, or verify that no code path calls `plivo.RestClient` in the mocked tests.

---

## Summary of Required Actions Before Staging

### Blocker (must resolve before running any test)

1. **[BLOCKER] Add `+subscribe` to Redis ACL** (`Item 4`)
   - Edit the ACL file template/spec to include `+subscribe`
   - Confirm this with user before modifying `docker-compose.test.yml` or any deployed file

### Caveats (action required before secret file creation)

2. **Secret file paths must not contain spaces** (`Item 1`)
   - Use `C:\secrets\voxai-test\` or equivalent space-free path for all three secret files

3. **Use BOM-free encoding for secret files on Windows** (`Item 1`, `Item 5`)
   - Use `[System.IO.File]::WriteAllText(...)` or PowerShell 7's `-Encoding utf8NoBOM`

4. **Use alphanumeric-only password** (`Item 5`)
   - Avoids percent-encoding issues in connection URLs

### Safe test exclusions

5. Exclude from the test run:
   - `test_voice_calling_end_to_end.py`
   - `test_real_stt_transcription.py`
   - `test_kokoro_tts.py` (unless Kokoro model files are present)

### Safer teardown

6. Always use:
   ```powershell
   docker compose -f docker-compose.test.yml -p voxai-riya-test down --volumes
   ```

---

## Proposed Safe Test Execution Command (post-blocker resolution)

```powershell
# From: C:\Users\Aman\OneDrive - VVNT\Documents\ChatGPT\AI-Final-Voice-Agent\apps\api
python -m pytest `
  tests/ `
  --ignore=tests/test_voice_calling_end_to_end.py `
  --ignore=tests/test_real_stt_transcription.py `
  --ignore=tests/test_kokoro_tts.py `
  -p no:cacheprovider `
  -v
```

> [!IMPORTANT]
> Do **not** use `--noconftest` for the database-backed test group — `conftest.py` provides the `migrated` and `clean` fixtures that database tests depend on. `--noconftest` is appropriate only for the isolated Phase 1 unit tests.

