# Testing
```bash
cd apps/api && pytest            # 47 tests, needs Postgres(vector)+Redis (TEST_DATABASE_URL, TEST_REDIS_URL)
cd apps/web && npm test          # 13 tests (vitest + Testing Library); npx tsc --noEmit; npm run build
```
Backend covers: eligibility rules, auth/refresh rotation/RBAC, tenant isolation (leads, knowledge vectors, CRM, IDOR), CRM sync + dedup, webhook signature/idempotency/replay, duplicate-call protection incl. DB partial index, final pre-dial check, worker tasks (eager), callbacks, agent tools + orchestrator + interruption, analysis validation, SSRF/upload validation, ARI lifecycle, analytics zero-state. Not covered: live providers, live audio, browser E2E (no browser available in the build sandbox), load/perf.
