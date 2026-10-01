import os

# Obtiene la URL de la variable de entorno. Si no existe, usa la local por defecto.
DATABASE_URL = os.getenv(
    "DATABASE_URL", 
    "postgresql://postgres:password@localhost:5432/bot_futbol"
)

# Nota: Render/Neon a veces entregan la URL iniciando con 'postgres://'. 
# SQLAlchemy o asyncpg requieren 'postgresql://'
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)