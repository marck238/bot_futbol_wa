from datetime import datetime, timedelta
from sqlalchemy import text

def get_next_weekend_range():
    """Calcula el rango exacto de Sábado (00:00 hs) a Domingo (23:59 hs) inmediatamente próximos."""
    today = datetime.now()
    # Días faltantes hasta el próximo Sábado (Lunes=0 ... Sábado=5)
    days_until_saturday = (5 - today.weekday()) % 7
    if days_until_saturday == 0 and today.weekday() == 5:
        days_until_saturday = 7

    saturday_start = (today + timedelta(days=days_until_saturday)).replace(hour=0, minute=0, second=0, microsecond=0)
    sunday_end = (saturday_start + timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=999999)
    
    return saturday_start, sunday_end

async def get_weekend_predictions_message(engine):
    sat_start, sun_end = get_next_weekend_range()
    
    async with engine.connect() as conn:
        query = text("""
            SELECT match_description, recommended_market, odds, ev_percentage, kelly_stake, match_datetime
            FROM bet_analysis
            WHERE match_datetime >= :sat AND match_datetime <= :sun
            ORDER BY match_datetime ASC;
        """)
        result = await conn.execute(query, {"sat": sat_start, "sun": sun_end})
        rows = result.fetchall()

    if not rows:
        sat_str = sat_start.strftime("%d/%m/%Y")
        sun_str = sun_end.strftime("%d/%m/%Y")
        return (
            f"⚠️ *No hay partidos EV+ para el próximo fin de semana*\n\n"
            f"📅 *Rango buscado:* {sat_str} al {sun_str}\n"
            f"💡 *Motivo:* Sin jornadas en las ligas monitoreadas por receso/Fecha FIFA.\n\n"
            f"👉 Presiona *📋 Todos* en el menú para ver los partidos agendados a partir del 10/10/2026."
        )

    msg = f"⚽ *Pronósticos EV+ para el próximo fin de semana ({sat_start.strftime('%d/%m')} - {sun_end.strftime('%d/%m')}):*\n\n"
    for r in rows:
        msg += f"📌 *{r[0]}*\n"
        msg += f"🎯 Mercado: {r[1]} @ {r[2]}\n"
        msg += f"📈 EV+: +{r[3]}% | Stake: {r[4]}%\n"
        msg += f"📅 Fecha: {r[5].strftime('%d/%m %H:%M')}\n\n"
    return msg