#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
(cd apps/api && pytest -q)
(cd apps/web && npx tsc --noEmit && npm test && npm run build)
