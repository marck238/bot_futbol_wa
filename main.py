import os
import logging
import asyncio
from datetime import datetime, timedelta
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import httpx

# Configuración de Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Token del Bot de Telegram (Obtenido desde variables de entorno de Render)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8940818263:AAGv6e5_urn-umk1MjIQLpJ48M4cyiHEuI4")

# ==========================================
# 🎨 FUNCIONES DE FORMATO Y DISEÑO
# ==========================================

def format_match_time(fix):
    """Extrae y formatea la hora del partido en zona horaria local (Uruguay)."""
    try:
        date_str = fix.get("fixture", {}).get("date")
        if not date_str:
            return "00:00"
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        uy_tz = pytz.timezone("America/Montevideo")
        dt_uy = dt.astimezone(uy_tz)
        return dt_uy.strftime("%H:%M")
    except Exception:
        return "00:00"

def format_match_card(league_info, home, away, match_time, odds_over, p_over, odds_btts, p_btts, odds_btts_1h, p_btts_1h, stake_over):
    """
    Genera el texto formateado en HTML con el nuevo diseño moderno de tarjeta de pronóstico.
    """
    return (
        f"⚽ <b>{home} vs {away}</b>\n"
        f"🌐 <i>{league_info}</i> | ⏰ <code>{match_time} HS</code>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📈 <b>ANÁLISIS DE MERCADOS</b>\n\n"
        f"• <b>Más de 2.5 Goles:</b> {odds_over:.2f}  <code>(Prob: {p_over*100:.1f}%)</code>\n"
        f"• <b>Ambos Anotan (BTTS):</b> {odds_btts:.2f}  <code>(Prob: {p_btts*100:.1f}%)</code>\n"
        f"• 🔥 <b>BTTS 1ª Mitad:</b> {odds_btts_1h:.2f}  <code>(Prob: {p_btts_1h*100:.1f}%)</code>\n\n"
        f"🎯 <b>Stake Kelly (Over 2.5):</b> <code>{stake_over}%</code>\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )

# ==========================================
# 📊 FUNCIONES DE CÁLCULO Y ANALÍTICA
# ==========================================

def generate_fixture_analytics(fix):
    """
    Simula o calcula las métricas y cuotas basadas en Poisson y estadísticas previas.
    (Ajusta esta función según tu lógica interna de probabilidades actual).
    """
    # Valores de ejemplo/integrados de tu motor analítico
    return {
        "metrics": {
            "p_over_25": 0.705,
            "p_btts_yes": 0.708,
            "p_btts_1h": 0.342
        },
        "odds_over": 1.50,
        "odds_btts_yes": 1.47,
        "odds_btts_1h": 2.69
    }

def calculate_kelly_stake(probability, odds):
    """Calcula el porcentaje de stake usando el Criterio de Kelly."""
    try:
        b = odds - 1
        if b <= 0:
            return 0.0
        q = 1.0 - probability
        kelly = (b * probability - q) / b
        stake = max(0.0, kelly * 100)
        return round(stake, 2)
    except Exception:
        return 1.0

# ==========================================
# 🤖 MANEJADORES DE TELEGRAM (HANDLERS)
# ==========================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start para inicializar el bot y mostrar el menú principal."""
    keyboard = [
        [InlineKeyboardButton("⚽ Goles & BTTS", callback_data="catgoals")],
        [InlineKeyboardButton("📊 Ayuda & Info", callback_data="help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_text = (
        "🎉 <b>¡Bienvenido a NosticProno!</b>\n\n"
        "Selecciona una categoría del menú inferior para comenzar a analizar partidos con modelos estadísticos avanzados."
    )
    
    if update.message:
        await update.message.reply_text(welcome_text, parse_mode="HTML", reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.message.edit_text(welcome_text, parse_mode="HTML", reply_markup=reply_markup)

async def date_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja las interacciones de los botones del menú y categorías."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data == "catgoals":
        # Simulación de partidos obtenidos de la API
        target_fixtures = [
            {
                "fixture": {"id": 101, "date": "2026-10-04T09:00:00Z"},
                "teams": {"home": {"name": "Finland U18"}, "away": {"name": "Wales U18"}},
                "league": {"name": "Friendlies"}
            }
        ]
        
        await query.message.reply_text("🔍 <b>Analizando partidos y cuotas para Goles & BTTS...</b>", parse_mode="HTML")
        
        for fix in target_fixtures:
            fid = fix.get("fixture", {}).get("id", 0)
            teams = fix.get("teams", {})
            league_info = fix.get("league", {}).get("name", "Fútbol")
            home = teams.get("home", {}).get("name", "Local")
            away = teams.get("away", {}).get("name", "Visitante")
            
            match_time = format_match_time(fix)
            analytics = generate_fixture_analytics(fix)
            
            p_over = analytics["metrics"]["p_over_25"]
            p_btts = analytics["metrics"]["p_btts_yes"]
            p_btts_1h = analytics["metrics"]["p_btts_1h"]
            
            odds_over = analytics["odds_over"]
            odds_btts = analytics["odds_btts_yes"]
            odds_btts_1h = analytics["odds_btts_1h"]
            
            stake_over = calculate_kelly_stake(p_over, odds_over)
            stake_1h = calculate_kelly_stake(p_btts_1h, odds_btts_1h)

            # Construcción de la tarjeta con la nueva función de diseño
            card_text = format_match_card(
                league_info, home, away, match_time,
                odds_over, p_over,
                odds_btts, p_btts,
                odds_btts_1h, p_btts_1h,
                stake_over
            )

            # Botones inline para guardar picks
            btn = InlineKeyboardMarkup([
                [InlineKeyboardButton("📌 Guardar Over 2.5", callback_data=f"savepick_{fid}_GOALS_OVER_25_{odds_over}_{stake_over}")],
                [InlineKeyboardButton("📌 Guardar BTTS 1H (Nuevo)", callback_data=f"savepick_{fid}_GOALS_BTTS_1H_{odds_btts_1h}_{stake_1h}")]
            ])
            
            await query.message.reply_text(card_text, parse_mode="HTML", reply_markup=btn)
            
    elif data == "help":
        await query.message.edit_text(
            "ℹ️ <b>Ayuda de NosticProno</b>\n\nUtiliza los botones del menú principal para explorar pronósticos calculados mediante distribución de Poisson y Criterio de Kelly.",
            parse_mode="HTML"
        )

# ==========================================
# ⚙️ WORKER EN SEGUNDO PLANO Y ARRANQUE
# ==========================================

async def auto_settlement_worker():
    """Worker automático en segundo plano para liquidación de apuestas."""
    while True:
        try:
            # Lógica de liquidación periódica
            await asyncio.sleep(300)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error en auto_settlement_worker: {e}")
            await asyncio.sleep(60)

async def post_init(application: Application):
    """Inicia tareas en segundo plano al arrancar la aplicación de Telegram."""
    application.create_task(auto_settlement_worker())
    logger.info("Worker de liquidación automática iniciado correctamente.")

def main():
    """Función principal para configurar y ejecutar el bot."""
    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Registro de manejadores (Handlers)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(date_callback_handler))

    logger.info("Iniciando bot de Telegram...")
    # Inicia el bot limpiando actualizaciones pendientes para evitar conflictos en despliegues
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()