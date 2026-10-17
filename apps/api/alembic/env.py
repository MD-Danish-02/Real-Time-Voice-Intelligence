from alembic import context
from sqlalchemy import create_engine, pool

from app.core.config import settings
from app.models import Base  # noqa: F401 - registers all models

target_metadata = Base.metadata


def run_migrations_online():
    engine = create_engine(settings.DATABASE_URL, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
