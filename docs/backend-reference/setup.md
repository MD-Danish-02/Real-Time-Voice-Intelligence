# Setup

```bash
git clone <repo> && cd voxai-platform
cp .env.example .env          # set JWT_SECRET / ENCRYPTION_KEY (openssl rand -hex 32)
docker compose up --build     # postgres(pgvector), redis, migrate, backend, worker, scheduler, frontend, minio
```
* Migrations run automatically via the `migrate` service (or `docker compose run --rm migrate`).
* Optional demo data: `docker compose exec backend python seed.py` -> login `demo@example.com` / `DemoPass123!` (tenant name starts with "DEMO").
* Web: http://localhost:3000 - API docs: http://localhost:8000/docs - `/health`, `/ready`, `/metrics`.
* Telephony stack: `docker compose --profile telephony up` (Asterisk). Observability: `--profile observability` (Prometheus/Grafana).

## Without Docker
Python 3.12 + Postgres 16 with the `vector` extension + Redis. `pip install -r apps/api/requirements.txt`, `alembic upgrade head`, `uvicorn app.main:app`, `celery -A app.workers.celery_app.celery_app worker` and `... beat`; web: `cd apps/web && npm i && npm run dev`.
Heavy optional voice deps (`pipecat-ai`, `faster-whisper`, `sentence-transformers`): `pip install -r apps/api/requirements-voice.txt`.
