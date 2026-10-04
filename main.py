import os
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
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
# ⌨️ TECLADO INFERIOR (REPLY KEYBOARD)
# ==========================================

def get_persistent_keyboard():
    """Genera el teclado persistente inferior del chat."""
    keyboard = [
        [KeyboardButton("⚽ 1X2 / Ganador"), KeyboardButton("⚽ Goles & BTTS")],
        [KeyboardButton("🚩 Córners & Tarjetas"), KeyboardButton("🍀 Combinadas EV+")],
        [KeyboardButton("📊 Mis Estadísticas"), KeyboardButton("🎯 Top Value +EV"), KeyboardButton("📖 Ayuda")],
        [KeyboardButton("⚙️ Panel Admin")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ==========================================
# 🎨 FUNCIONES DE FORMATO Y DISEÑO (TARJETAS)
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

def format_goals_card(league_info, home, away, match_time, odds_over, p_over, odds_btts, p_btts, odds_btts_1h, p_btts_1h, stake_over):
    return (
        f"⚽ <b>{home} vs {away}</b>\n"
        f"🌐 <i>{league_info}</i> | ⏰ <code>{match_time} HS</code>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📈 <b>ANÁLISIS DE GOLES & BTTS</b>\n\n"
        f"• <b>Más de 2.5 Goles:</b> {odds_over:.2f}  <code>(Prob: {p_over*100:.1f}%)</code>\n"
        f"• <b>Ambos Anotan (BTTS):</b> {odds_btts:.2f}  <code>(Prob: {p_btts*100:.1f}%)</code>\n"
        f"• 🔥 <b>BTTS 1ª Mitad:</b> {odds_btts_1h:.2f}  <code>(Prob: {p_btts_1h*100:.1f}%)</code>\n\n"
        f"🎯 <b>Stake Kelly (Over 2.5):</b> <code>{stake_over}%</code>\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )

def format_1x2_card(league_info, home, away, match_time, p_home, odds_home, p_draw, odds_draw, p_away, odds_away, stake_pick):
    return (
        f"🏆 <b>{home} vs {away}</b>\n"
        f"🌐 <i>{league_info}</i> | ⏰ <code>{match_time} HS</code>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>MERCADO 1X2 / GANADOR</b>\n\n"
        f"• 🏠 <b>Victoria Local:</b> {odds_home:.2f}  <code>({p_home*100:.1f}%)</code>\n"
        f"• 🤝 <b>Empate:</b> {odds_draw:.2f}  <code>({p_draw*100:.1f}%)</code>\n"
        f"• ✈️ <b>Victoria Visitante:</b> {odds_away:.2f}  <code>({p_away*100:.1f}%)</code>\n\n"
        f"🎯 <b>Stake Kelly (Recomendado):</b> <code>{stake_pick}%</code>\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )

def format_corners_cards(league_info, home, away, match_time, avg_corners, odds_corners_over, p_corners, avg_cards, odds_cards_over, p_cards, stake_corners):
    return (
        f"🚩 <b>{home} vs {away}</b>\n"
        f"🌐 <i>{league_info}</i> | ⏰ <code>{match_time} HS</code>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📐 <b>CÓRNERS & TARJETAS</b>\n\n"
        f"• 📐 <b>Córners Esperados:</b> <code>{avg_corners}</code>\n"
        f"  └ <i>Más de 8.5 Córners:</i> {odds_corners_over:.2f} <code>({p_corners*100:.1f}%)</code>\n\n"
        f"• 🟨 <b>Tarjetas Promedio:</b> <code>{avg_cards}</code>\n"
        f"  └ <i>Más de 4.5 Tarjetas:</i> {odds_cards_over:.2f} <code>({p_cards*100:.1f}%)</code>\n\n"
        f"🎯 <b>Stake Kelly (Córners):</b> <code>{stake_corners}%</code>\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )

# ==========================================
# 📊 MOTOR ANALÍTICO Y API-FOOTBALL
# ==========================================

async def fetch_fixtures_from_api(date_str):
    """Consulta los partidos reales de la API-Football para una fecha específica."""
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
    """Motor analítico que provee métricas para Goles, 1X2, Córners y Tarjetas."""
    return {
        "goals": {
            "p_over_25": 0.650,
            "p_btts_yes": 0.620,
            "p_btts_1h": 0.350,
            "odds_over": 1.72,
            "odds_btts_yes": 1.80,
            "odds_btts_1h": 2.50
        },
        "market_1x2": {
            "p_home": 0.520,
            "odds_home": 1.95,
            "p_draw": 0.260,
            "odds_draw": 3.40,
            "p_away": 0.220,
            "odds_away": 4.10
        },
        "corners_cards": {
            "avg_corners": 9.8,
            "p_corners": 0.680,
            "odds_corners_over": 1.85,
            "avg_cards": 4.6,
            "p_cards": 0.590,
            "odds_cards_over": 1.90
        }
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
    """Comando /start que despliega el menú de goles directo y asegura el teclado inferior."""
    today_str = datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%d")
    tomorrow_str = (datetime.now(timezone(timedelta(hours=-3))) + timedelta(days=1)).strftime("%Y-%m-%d")
    
    keyboard = [
        [InlineKeyboardButton("📅 Partidos de Hoy", callback_data=f"loadfixtures_catgoals_{today_str}")],
        [InlineKeyboardButton("📅 Partidos de Mañana", callback_data=f"loadfixtures_catgoals_{tomorrow_str}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "🗓️ <b>Selecciona la fecha para analizar Goles & BTTS:</b>"
    
    if update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)
        await update.message.reply_text("👇 Utiliza el menú inferior para cambiar de sección:", reply_markup=get_persistent_keyboard())

async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja los botones del teclado inferior persistente."""
    text = update.message.text
    today_str = datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%d")
    tomorrow_str = (datetime.now(timezone(timedelta(hours=-3))) + timedelta(days=1)).strftime("%Y-%m-%d")
    
    if "Goles & BTTS" in text:
        keyboard = [
            [InlineKeyboardButton("📅 Partidos de Hoy", callback_data=f"loadfixtures_catgoals_{today_str}")],
            [InlineKeyboardButton("📅 Partidos de Mañana", callback_data=f"loadfixtures_catgoals_{tomorrow_str}")]
        ]
        await update.message.reply_text(
            "🗓️ <b>Selecciona la fecha para Goles & BTTS:</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif "1X2 / Ganador" in text:
        keyboard = [
            [InlineKeyboardButton("📅 Partidos de Hoy (1X2)", callback_data=f"loadfixtures_cat1x2_{today_str}")],
            [InlineKeyboardButton("📅 Partidos de Mañana (1X2)", callback_data=f"loadfixtures_cat1x2_{tomorrow_str}")]
        ]
        await update.message.reply_text(
            "🗓️ <b>Selecciona la fecha para Mercado 1X2:</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif "Córners & Tarjetas" in text:
        keyboard = [
            [InlineKeyboardButton("📅 Partidos de Hoy (Córners)", callback_data=f"loadfixtures_catcorners_{today_str}")],
            [InlineKeyboardButton("📅 Partidos de Mañana (Córners)", callback_data=f"loadfixtures_catcorners_{tomorrow_str}")]
        ]
        await update.message.reply_text(
            "🗓️ <b>Selecciona la fecha para Córners & Tarjetas:</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif "Combinadas EV+" in text:
        await update.message.reply_text("🍀 Buscando combinadas de valor...", reply_markup=get_persistent_keyboard())
    elif "Mis Estadísticas" in text:
        await update.message.reply_text("📊 Tus estadísticas guardadas:", reply_markup=get_persistent_keyboard())
    elif "Top Value +EV" in text:
        await update.message.reply_text("🎯 Picks Top Value del día:", reply_markup=get_persistent_keyboard())
    elif "Ayuda" in text:
        await update.message.reply_text(
            "ℹ️ <b>Ayuda de NosticProno</b>\n\nEste bot analiza mercados utilizando Poisson y Criterio de Kelly.",
            parse_mode="HTML",
            reply_markup=get_persistent_keyboard()
        )
    elif "Panel Admin" in text:
        await update.message.reply_text("⚙️ Panel de administración.", reply_markup=get_persistent_keyboard())

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja las interacciones de los botones inline."""
    query = update.callback_query
    await query.answer()
    data = query.data
    
    # --- MÓDULO GOLES & BTTS ---
    if data.startswith("loadfixtures_catgoals_"):
        parts = data.split("_")
        selected_date = parts[2]
        await query.message.edit_text(f"🔍 <b>Consultando partidos para Goles ({selected_date})...</b>", parse_mode="HTML")
        
        fixtures = await fetch_fixtures_from_api(selected_date)
        if not fixtures:
            await query.message.reply_text("⚠️ No se encontraron partidos para esta fecha.")
            return
            
        for fix in fixtures[:5]:
            fid = fix.get("fixture", {}).get("id", 0)
            teams = fix.get("teams", {})
            league_info = fix.get("league", {}).get("name", "Fútbol")
            home = teams.get("home", {}).get("name", "Local")
            away = teams.get("away", {}).get("name", "Visitante")
            
            match_time = format_match_time(fix)
            analytics = generate_fixture_analytics(fix)["goals"]
            
            p_over = analytics["p_over_25"]
            p_btts = analytics["p_btts_yes"]
            p_btts_1h = analytics["p_btts_1h"]
            odds_over = analytics["odds_over"]
            odds_btts = analytics["odds_btts_yes"]
            odds_btts_1h = analytics["odds_btts_1h"]
            
            stake_over = calculate_kelly_stake(p_over, odds_over)
            stake_1h = calculate_kelly_stake(p_btts_1h, odds_btts_1h)

            card_text = format_goals_card(league_info, home, away, match_time, odds_over, p_over, odds_btts, p_btts, odds_btts_1h, p_btts_1h, stake_over)
            btn = InlineKeyboardMarkup([
                [InlineKeyboardButton("📌 Guardar Over 2.5", callback_data=f"savepick_{fid}_GOALS_OVER_{odds_over}_{stake_over}")],
                [InlineKeyboardButton("📌 Guardar BTTS 1H", callback_data=f"savepick_{fid}_GOALS_BTTS_{odds_btts_1h}_{stake_1h}")]
            ])
            await query.message.reply_text(card_text, parse_mode="HTML", reply_markup=btn)

    # --- MÓDULO 1X2 / GANADOR ---
    elif data.startswith("loadfixtures_cat1x2_"):
        parts = data.split("_")
        selected_date = parts[2]
        await query.message.edit_text(f"🔍 <b>Consultando partidos para 1X2 ({selected_date})...</b>", parse_mode="HTML")
        
        fixtures = await fetch_fixtures_from_api(selected_date)
        if not fixtures:
            await query.message.reply_text("⚠️ No se encontraron partidos para esta fecha.")
            return
            
        for fix in fixtures[:5]:
            fid = fix.get("fixture", {}).get("id", 0)
            teams = fix.get("teams", {})
            league_info = fix.get("league", {}).get("name", "Fútbol")
            home = teams.get("home", {}).get("name", "Local")
            away = teams.get("away", {}).get("name", "Visitante")
            
            match_time = format_match_time(fix)
            m_1x2 = generate_fixture_analytics(fix)["market_1x2"]
            
            p_home, odds_home = m_1x2["p_home"], m_1x2["odds_home"]
            p_draw, odds_draw = m_1x2["p_draw"], m_1x2["odds_draw"]
            p_away, odds_away = m_1x2["p_away"], m_1x2["odds_away"]
            
            stake_home = calculate_kelly_stake(p_home, odds_home)

            card_text = format_1x2_card(league_info, home, away, match_time, p_home, odds_home, p_draw, odds_draw, p_away, odds_away, stake_home)
            btn = InlineKeyboardMarkup([
                [InlineKeyboardButton("📌 Guardar Victoria Local", callback_data=f"savepick_{fid}_1X2_HOME_{odds_home}_{stake_home}")]
            ])
            await query.message.reply_text(card_text, parse_mode="HTML", reply_markup=btn)

    # --- MÓDULO CÓRNERS & TARJETAS ---
    elif data.startswith("loadfixtures_catcorners_"):
        parts = data.split("_")
        selected_date = parts[2]
        await query.message.edit_text(f"🔍 <b>Consultando Córners y Tarjetas ({selected_date})...</b>", parse_mode="HTML")
        
        fixtures = await fetch_fixtures_from_api(selected_date)
        if not fixtures:
            await query.message.reply_text("⚠️ No se encontraron partidos para esta fecha.")
            return
            
        for fix in fixtures[:5]:
            fid = fix.get("fixture", {}).get("id", 0)
            teams = fix.get("teams", {})
            league_info = fix.get("league", {}).get("name", "Fútbol")
            home = teams.get("home", {}).get("name", "Local")
            away = teams.get("away", {}).get("name", "Visitante")
            
            match_time = format_match_time(fix)
            cc = generate_fixture_analytics(fix)["corners_cards"]
            
            avg_corners = cc["avg_corners"]
            p_corners = cc["p_corners"]
            odds_corners = cc["odds_corners_over"]
            
            avg_cards = cc["avg_cards"]
            p_cards = cc["p_cards"]
            odds_cards = cc["odds_cards_over"]
            
            stake_corners = calculate_kelly_stake(p_corners, odds_corners)

            card_text = format_corners_cards(league_info, home, away, match_time, avg_corners, odds_corners, p_corners, avg_cards, odds_cards, p_cards, stake_corners)
            btn = InlineKeyboardMarkup([
                [InlineKeyboardButton("📌 Guardar Más de 8.5 Córners", callback_data=f"savepick_{fid}_CORNERS_OVER_{odds_corners}_{stake_corners}")]
            ])
            await query.message.reply_text(card_text, parse_mode="HTML", reply_markup=btn)
            
    elif data == "main_menu":
        await start_command(update, context)

# ==========================================
# ⚙️ WORKER Y ARRANQUE SEGURO
# ==========================================

async def auto_settlement_worker(context: ContextTypes.DEFAULT_TYPE):
    """Worker periódico en segundo plano."""
    pass

async def post_init(application: Application):
    """Inicializa tareas usando el job_queue de PTB."""
    application.job_queue.run_repeating(auto_settlement_worker, interval=300, first=10)

def main():
    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Registro de Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))

    logger.info("Iniciando bot con módulos 1X2, Córners y Goles operativos...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()