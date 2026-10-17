from typing import AsyncGenerator, Generator

from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

# Sync engine (used by Celery workers / Alembic / scripts)
_sync_kwargs = {"connect_args": {"check_same_thread": False}} if "sqlite" in settings.DATABASE_URL else {"pool_pre_ping": True}
engine = create_engine(settings.DATABASE_URL, future=True, **_sync_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

# Async engine (used by FastAPI request handlers)
_async_kwargs = {}
if settings.ENV == "test":
    _async_kwargs = {"poolclass": NullPool}
elif "sqlite" in settings.ASYNC_DATABASE_URL:
    _async_kwargs = {}
else:
    _async_kwargs = {"pool_pre_ping": True}
async_engine = create_async_engine(settings.ASYNC_DATABASE_URL, future=True, **_async_kwargs)
AsyncSessionLocal = async_sessionmaker(bind=async_engine, expire_on_commit=False, class_=AsyncSession)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


async def get_async_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session
