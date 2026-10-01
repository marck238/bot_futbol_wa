import asyncio
from src.services.news_service import get_team_news_modifier

async def main():
    print("🔍 Consultando noticias para 'Real Madrid'...")
    mod = await get_team_news_modifier("Real Madrid")
    print(f"Resultado modificador: {mod}")

if __name__ == "__main__":
    asyncio.run(main())