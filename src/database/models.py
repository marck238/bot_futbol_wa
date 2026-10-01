from src.database.db import obtener_conexion

def inicializar_tablas():
    """
    Crea las tablas necesarias en PostgreSQL si no existen.
    """
    sql_partidos = """
    CREATE TABLE IF NOT EXISTS partidos (
        id SERIAL PRIMARY KEY,
        equipo_local VARCHAR(100) NOT NULL,
        equipo_visitante VARCHAR(100) NOT NULL,
        fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        prob_local FLOAT,
        prob_empate FLOAT,
        prob_visitante FLOAT,
        cuota_local FLOAT,
        cuota_empate FLOAT,
        cuota_visitante FLOAT,
        ev_maximo FLOAT,
        recomendacion VARCHAR(100)
    );
    """

    sql_historial = """
    CREATE TABLE IF NOT EXISTS historial_apuestas (
        id SERIAL PRIMARY KEY,
        partido_id INT REFERENCES partidos(id),
        mercado VARCHAR(50) NOT NULL,
        cuota FLOAT NOT NULL,
        stake FLOAT NOT NULL,
        resultado VARCHAR(20) DEFAULT 'PENDIENTE', -- GANADA, PERDIDA, ANULADA
        retorno FLOAT DEFAULT 0.0,
        fecha_registro TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """

    conn = obtener_conexion()
    if conn:
        try:
            with conn.cursor() as cursor:
                cursor.execute(sql_partidos)
                cursor.execute(sql_historial)
                conn.commit()
            print("✅ Tablas 'partidos' e 'historial_apuestas' verificadas/creadas correctamente.")
        except Exception as e:
            print(f"❌ Error al crear tablas: {e}")
        finally:
            conn.close()

if __name__ == "__main__":
    inicializar_tablas()
