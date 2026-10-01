import asyncio
from src.infrastructure.database import engine, Base
from src.domain.models import Team, Match, BetAnalysis

async def init_models():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("✅ Tablas creadas con éxito en PostgreSQL.")

if __name__ == "__main__":
    asyncio.run(init_models())
