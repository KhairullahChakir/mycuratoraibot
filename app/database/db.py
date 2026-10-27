from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text

from app.config import DATABASE_URL
from app.settings import async_database_url


engine = create_async_engine(
    async_database_url(DATABASE_URL),
    echo=False,
    hide_parameters=True,
    pool_pre_ping=True,
    pool_recycle=300,
    connect_args={"timeout": 15, "command_timeout": 60},
)


AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def init_db():
    # Import models so SQLAlchemy knows which tables to create.
    import app.database.models

    async with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            # Serialize startup schema work across bot processes.
            await connection.execute(text("SELECT pg_advisory_xact_lock(761902031)"))
        await connection.run_sync(Base.metadata.create_all)
        from app.database.migrations import migrate_homework
        await connection.run_sync(migrate_homework)
