import psycopg2
from psycopg2.extras import RealDictCursor
from config import config

def obtener_conexion():
    """
    Retorna una conexión activa a PostgreSQL.
    """
    try:
        conn = psycopg2.connect(config.DATABASE_URL, cursor_factory=RealDictCursor)
        return conn
    except Exception as e:
        print(f"❌ Error al conectar a la Base de Datos: {e}")
        return None

if __name__ == "__main__":
    print("🔌 Probando conexión a PostgreSQL...")
    conn = obtener_conexion()
    if conn:
        print("✅ Conexión exitosa a la Base de Datos.")
        conn.close()
