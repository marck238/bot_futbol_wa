import asyncio
from sqlalchemy import text
from src.infrastructure.database import engine

async def main():
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT id, match_description, recommended_market, odds, ev_percentage, kelly_stake, match_datetime FROM bet_analysis ORDER BY match_datetime ASC;")
        )
        rows = result.fetchall()
        
        print(f"\n📊 TOTAL DE PARTIDOS EN NEON DB: {len(rows)}\n" + "="*60)
        if not rows:
            print("⚠️ La base de datos no contiene registros actualmente.")
        else:
            for r in rows:
                print(f"ID #{r[0]} | ⚽ {r[1]}")
                print(f"   📅 Fecha Partido: {r[6]}")
                print(f"   🎯 Mercado: {r[2]} @ {r[3]}")
                print(f"   📈 EV+: +{r[4]}% | Stake Kelly: {r[5]}%\n" + "-"*60)

if __name__ == "__main__":
    asyncio.run(main())