import asyncio
from sqlalchemy import text
from src.infrastructure.database import engine

async def main():
    async with engine.begin() as conn:
        # 1. Eliminar partidos pasados de fecha de prueba
        await conn.execute(
            text("DELETE FROM bet_analysis WHERE match_description LIKE '%Peñarol%';" )
        )
        # 2. Eliminar predicciones con EV negativo o sin valor
        await conn.execute(
            text("DELETE FROM bet_analysis WHERE ev_percentage <= 0;" )
        )
        print("🧹 Base de datos purgada: se eliminaron partidos pasados y registros sin EV+.")

if __name__ == "__main__":
    asyncio.run(main())