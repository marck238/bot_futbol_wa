import asyncio
from sqlalchemy import text
from src.infrastructure.database import engine
from src.domain.models import Base

async def run_fix():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text("ALTER TABLE bet_analysis ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP;")
        )
        await conn.execute(
            text("ALTER TABLE bet_analysis ADD COLUMN IF NOT EXISTS match_datetime TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP;")
        )
    print("✅ ¡Columna 'match_datetime' agregada con éxito a Neon DB!")

if __name__ == "__main__":
    asyncio.run(run_fix())