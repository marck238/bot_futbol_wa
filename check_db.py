import os
import asyncio
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

async def check():
    if not DATABASE_URL:
        print("❌ Error: No se encontró DATABASE_URL en el archivo .env")
        return

    db_url = DATABASE_URL
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif db_url.startswith("postgresql://") and not db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    engine = create_async_engine(db_url)

    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT match_description, odds, ev_percentage, kelly_stake FROM bet_analysis ORDER BY id DESC LIMIT 5;")
            )
            rows = result.fetchall()

            print("\n--- 📊 ÚLTIMAS APUESTAS REGISTRADAS EN NEON ---")
            if not rows:
                print("⚠️ No se encontraron registros recientes (o ningún partido superó el EV+ de 3%).")
            else:
                for r in rows:
                    print(f"⚽ {r.match_description} | Cuota: {r.odds} | EV+: {r.ev_percentage}% | Kelly: {r.kelly_stake}%")
            print("------------------------------------------------\n")
    except Exception as e:
        print(f"❌ Error al consultar la base de datos: {e}")
    finally:
        await engine.dispose()

if __name__ == "__main__":
    asyncio.run(check())