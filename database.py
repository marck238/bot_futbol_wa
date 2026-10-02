import os
from urllib.parse import urlparse, urlunparse
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import BigInteger, String, DateTime, func

DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL:
    # 1. Asegurar el prefijo de driver asíncrono postgresql+asyncpg
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
    elif DATABASE_URL.startswith("postgresql://"):
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

    # 2. Remover los parámetros de la URL (?sslmode=..., ?channel_binding=...) que causan error en asyncpg
    parsed = urlparse(DATABASE_URL)
    DATABASE_URL = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, "", parsed.fragment))

# 3. Pasar el parámetro SSL requerido por Neon a través de connect_args
engine = create_async_engine(
    DATABASE_URL,
    connect_args={"ssl": "require"},
    echo=False
)

AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    role: Mapped[str] = mapped_column(String, default="USER")
    status: Mapped[str] = mapped_column(String, default="ACTIVE")
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())

async def init_db():
    """Crea las tablas en Neon si no existen."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)