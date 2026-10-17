# Migrations
Alembic in `apps/api/alembic`. `alembic upgrade head` / `alembic downgrade base` (both verified against a real Postgres). Revision `0001` creates the schema, the `vector` extension, the partial unique index and the HNSW/composite indexes. The app never calls `create_all()`. New change: `alembic revision --autogenerate -m "..."` then review the diff.
