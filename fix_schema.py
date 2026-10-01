import asyncio
from sqlalchemy import text
from src.infrastructure.database import engine

async def main():
    async with engine.begin() as conn:
        await conn.execute(
            text("ALTER TABLE bet_analysis ADD COLUMN IF NOT EXISTS match_datetime TIMESTAMP WITH TIME ZONE;")
        )
        print("✅ Columna match_datetime agregada exitosamente a Neon DB.")

if __name__ == "__main__":
    asyncio.run(main())