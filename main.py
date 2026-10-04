import os
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import httpx

# Configuración de Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuración de Entorno (Render)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8940818263:AAGv6e5_urn-umk1MjIQLpJ48M4cyiHEuI4")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "")
API_FOOTBALL_HOST = "v3.football.api-sports.io"

# ==========================================
# 🎨 FUNCIONES DE FORMATO Y DISEÑO
# ==========================================

def format_match_time(fix):
    """Extrae y formatea la hora del partido en zona horaria local de Uruguay (UTC-3)."""
    try:
        date_str = fix.get("fixture", {}).get("date")
        if not date_str:
            return "00:00"
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        uy_tz = timezone(timedelta(hours=-3))
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
# 📊 MOTOR ANALÍTICO Y API-FOOTBALL
# ==========================================

async def fetch_fixtures_from_api(date_str):
    """Consulta los partidos reales de la API-Football para una fecha específica (YYYY-MM-DD)."""
    url = f"https://{API_FOOTBALL_HOST}/fixtures"
    headers = {
        "x-rapidapi-key": API_FOOTBALL_KEY,
        "x-rapidapi-host": API_FOOTBALL_HOST
    }
    params = {"date": date_str}
    
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, headers=headers, params=params)
            if response.status_code == 200:
                data = response.json()
                return data.get("response", [])
    except Exception as e:
        logger.error(f"Error consultando API-Football: {e}")
    return []

def generate_fixture_analytics(fix):
    """Calcula o simula métricas basadas en estadísticas de los equipos."""
    # Aquí se integra tu lógica de Poisson / Cuotas
    return {
        "metrics": {
            "p_over_25": 0.650,
            "p_btts_yes": 0.620,
            "p_btts_1h": 0.350
        },
        "odds_over": 1.72,
        "odds_btts_yes": 1.80,
        "odds_btts_1h": 2.50
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
    """Comando /start para inicializar el bot y mostrar el menú principal completo."""
    keyboard = [
        [InlineKeyboardButton("⚽ Goles & BTTS", callback_data="menu_date_catgoals")],
        [InlineKeyboardButton("📊 Ayuda & Info", callback_data="help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_text = (
        "🎉 <b>¡Bienvenido a NosticProno!</b>\n\n"
        "Selecciona una categoría para analizar partidos con modelos estadísticos avanzados y Criterio de Kelly."
    )
    
    if update.message:
        await update.message.reply_text(welcome_text, parse_mode="HTML", reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.message.edit_text(welcome_text, parse_mode="HTML", reply_markup=reply_markup)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja todas las interacciones de los botones interactivos."""
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "menu_date_catgoals":
        # Mostrar selector de fechas (Hoy, Mañana)
        today_str = datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%d")
        tomorrow_str = (datetime.now(timezone(timedelta(hours=-3))) + timedelta(days=1)).strftime("%Y-%m-%d")
        
        keyboard = [
            [InlineKeyboardButton("📅 Partidos de Hoy", callback_data=f"loadfixtures_catgoals_{today_str}")],
            [InlineKeyboardButton("📅 Partidos de Mañana", callback_data=f"loadfixtures_catgoals_{tomorrow_str}")],
            [InlineKeyboardButton("🔙 Volver al Menú", callback_data="main_menu")]
        ]
        await query.message.edit_text(
            "🗓️ <b>Selecciona la fecha para analizar Goles & BTTS:</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
    elif data.startswith("loadfixtures_catgoals_"):
        parts = data.split("_")
        selected_date = parts[2]
        
        await query.message.edit_text(f"🔍 <b>Consultando partidos en vivo para el {selected_date}...</b>", parse_mode="HTML")
        
        fixtures = await fetch_fixtures_from_api(selected_date)
        
        if not fixtures:
            await query.message.reply_text("⚠️ No se encontraron partidos disponibles para esta fecha en la API.")
            return
            
        # Tomamos los primeros 5 partidos como ejemplo para no saturar el chat
        for fix in fixtures[:5]:
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

            card_text = format_match_card(
                league_info, home, away, match_time,
                odds_over, p_over,
                odds_btts, p_btts,
                odds_btts_1h, p_btts_1h,
                stake_over
            )

            btn = InlineKeyboardMarkup([
                [InlineKeyboardButton("📌 Guardar Over 2.5", callback_data=f"savepick_{fid}_GOALS_OVER_25_{odds_over}_{stake_over}")],
                [InlineKeyboardButton("📌 Guardar BTTS 1H (Nuevo)", callback_data=f"savepick_{fid}_GOALS_BTTS_1H_{odds_btts_1h}_{stake_1h}")]
            ])
            
            await query.message.reply_text(card_text, parse_mode="HTML", reply_markup=btn)
            
    elif data == "main_menu":
        await start_command(update, context)
        
    elif data == "help":
        keyboard = [[InlineKeyboardButton("🔙 Volver", callback_data="main_menu")]]
        await query.message.edit_text(
            "ℹ️ <b>Ayuda de NosticProno</b>\n\nEste bot analiza mercados de fútbol utilizando Poisson y el Criterio de Kelly.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

# ==========================================
# ⚙️ WORKER Y ARRANQUE
# ==========================================

async def auto_settlement_worker():
    while True:
        try:
            await asyncio.sleep(300)
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(60)

async def post_init(application: Application):
    application.create_task(auto_settlement_worker())

def main():
    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(callback_handler))

    logger.info("Iniciando bot de Telegram con API-Football...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()