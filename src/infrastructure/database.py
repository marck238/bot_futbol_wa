import os
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/apuestas_db")

def fix_asyncpg_url(url_str: str) -> str:
    if url_str.startswith("postgresql://"):
        url_str = url_str.replace("postgresql://", "postgresql+asyncpg://", 1)
    
    parsed = urlparse(url_str)
    query_dict = parse_qs(parsed.query)
    
    # Traducir sslmode -> ssl
    if "sslmode" in query_dict:
        ssl_val = query_dict.pop("sslmode")[0]
        if "ssl" not in query_dict:
            query_dict["ssl"] = [ssl_val]
            
    # Eliminar parámetros que asyncpg no acepta
    for param in ["channel_binding"]:
        query_dict.pop(param, None)
        
    new_query = urlencode(query_dict, doseq=True)
    return urlunparse(parsed._replace(query=new_query))

engine = create_async_engine(fix_asyncpg_url(DATABASE_URL), echo=True)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
