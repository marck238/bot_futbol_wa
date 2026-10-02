import os
from urllib.parse import urlparse, urlunparse
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy import BigInteger, String, DateTime, Float, Boolean, ForeignKey, func

DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL:
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
    elif DATABASE_URL.startswith("postgresql://"):
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

    parsed = urlparse(DATABASE_URL)
    DATABASE_URL = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, "", parsed.fragment))

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

    filters: Mapped[list["Filter"]] = relationship("Filter", back_populates="user", cascade="all, delete-orphan")

class Filter(Base):
    __tablename__ = "filters"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    market: Mapped[str] = mapped_column(String(50), default="OVER_2.5_GOALS")  # OVER_2.5, OVER_1.5, BOTH_TEAMS_SCORE, CORNERS_9.5
    min_expected_prob: Mapped[float] = mapped_column(Float, default=60.0)      # Probabilidad Poisson Mínima (%)
    min_odd: Mapped[float] = mapped_column(Float, default=1.70)               # Cuota Mínima exigida
    min_ev: Mapped[float] = mapped_column(Float, default=3.0)                  # Valor Esperado Mínimo EV+ (%)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship("User", back_populates="filters")

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)