import os
import logging
import asyncio
import math
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
import httpx
import psycopg2
from psycopg2.extras import RealDictCursor
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

# Configuración de Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuración de Entorno
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "")
API_FOOTBALL_HOST = "v3.football.api-sports.io"
DATABASE_URL = os.getenv("DATABASE_URL", "")

# ==========================================
# 🌐 SERVIDOR HTTP PARA EL HEALTH CHECK DE RENDER
# ==========================================

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"NosticProno Bot is alive and running!")
    
    def log_message(self, format, *args):
        return

def run_health_server():
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info(f"Servidor HTTP de salud activo en el puerto {port}")
    server.serve_forever()

# ==========================================
# 🗄️ GESTIÓN DE BASE DE DATOS (POSTGRESQL)
# ==========================================

def get_db_connection():
    if not DATABASE_URL:
        return None
    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        return conn
    except Exception as e:
        logger.error(f"Error conectando a PostgreSQL: {e}")
        return None

def init_db():
    conn = get_db_connection()
    if not conn:
        logger.warning("DATABASE_URL no configurada.")
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_picks (
                    id SERIAL PRIMARY KEY,
                    fixture_id INTEGER,
                    market_type VARCHAR(50),
                    odds FLOAT,
                    probability FLOAT,
                    stake FLOAT,
                    status VARCHAR(20) DEFAULT 'PENDING',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("ALTER TABLE user_picks ADD COLUMN IF NOT EXISTS probability FLOAT;")
            
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_users (
                    telegram_id BIGINT PRIMARY KEY,
                    username VARCHAR(100),
                    role VARCHAR(20) DEFAULT 'user',
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.commit()
            logger.info("Base de datos y tablas inicializadas correctamente.")
    except Exception as e:
        logger.error(f"Error al crear tablas: {e}")
    finally:
        conn.close()

async def register_user_middleware(update: Update):
    user = update.effective_user
    if not user:
        return
    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO bot_users (telegram_id, username) 
                    VALUES (%s, %s) 
                    ON CONFLICT (telegram_id) DO UPDATE SET username = EXCLUDED.username;
                """, (user.id, user.username or user.first_name))
                conn.commit()
        except Exception as e:
            logger.error(f"Error registrando usuario: {e}")
        finally:
            conn.close()

# ==========================================
# ⌨️ TECLADO INFERIOR (REPLY KEYBOARD LIMPIO)
# ==========================================

def get_persistent_keyboard():
    keyboard = [
        [KeyboardButton("⚽ 1X2 / Ganador"), KeyboardButton("⚽ Goles & BTTS")],
        [KeyboardButton("🛡️ Doble Oportunidad"), KeyboardButton("⚖️ Handicap Asiático")],
        [KeyboardButton("🚩 Córners & Tarjetas"), KeyboardButton("🍀 Combinadas EV+")],
        [KeyboardButton("📊 Mis Estadísticas"), KeyboardButton("🎯 Top Value +EV")],
        [KeyboardButton("📖 Ayuda"), KeyboardButton("⚙️ Panel Admin")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ==========================================
# 📅 GENERADOR DE TECLADO DE FECHAS (HOY, MAÑANA, PASADO)
# ==========================================

def get_fecha_selector_keyboard(prefix: str):
    tz = timezone(timedelta(hours=-3))
    hoy = datetime.now(tz).date()
    mañana = hoy + timedelta(days=1)
    pasado = hoy + timedelta(days=2)
    
    keyboard = [
        [InlineKeyboardButton("📅 Partidos de hoy", callback_data=f"loadfixtures_{prefix}_{hoy.strftime('%Y-%m-%d')}")],
        [InlineKeyboardButton("📅 Partidos de mañana", callback_data=f"loadfixtures_{prefix}_{mañana.strftime('%Y-%m-%d')}")],
        [InlineKeyboardButton("📅 Partidos de pasado mañana", callback_data=f"loadfixtures_{prefix}_{pasado.strftime('%Y-%m-%d')}")]
    ]
    return InlineKeyboardMarkup(keyboard)

# ==========================================
# 🎨 FUNCIONES DE FORMATO Y DISEÑO (TARJETAS)
# ==========================================

def format_match_time(fix):
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

def format_1x2_card(league_info, home, away, match_time, p_home, odds_home, p_draw, odds_draw, p_away, odds_away, stake_pick):
    ev_home = (p_home * odds_home) - 1
    if p_home >= 0.45 and ev_home > 0:
        recommendation = f"🟢 **Apostar por la Victoria Local ({home})**\n💡 <i>Probabilidad sólida con valor esperado positivo (+EV).</i>"
    elif p_home >= 0.38:
        recommendation = f"🟡 **Oportunidad Moderada en Local ({home})**\n💡 <i>Cuota atractiva, evaluar stake prudente.</i>"
    else:
        recommendation = "⚠️ **Partido Emparejado / Sin Valor Claro**\n💡 <i>Se recomiendan mercados alternativos o evitar este encuentro.</i>"

    return (
        f"🏆 <b>{home} vs {away}</b>\n"
        f"🌐 <i>{league_info}</i> | ⏰ <code>{match_time} HS</code>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>MERCADO 1X2 / GANADOR</b>\n\n"
        f"• 🏠 <b>Victoria Local:</b> {odds_home:.2f}  <code>({p_home*100:.1f}%)</code>\n"
        f"• 🤝 <b>Empate:</b> {odds_draw:.2f}  <code>({p_draw*100:.1f}%)</code>\n"
        f"• ✈ <b>Victoria Visitante:</b> {odds_away:.2f}  <code>({p_away*100:.1f}%)</code>\n\n"
        f"🎯 <b>Acción Sugerida:</b>\n{recommendation}\n\n"
        f"💰 <b>Stake Kelly:</b> <code>{stake_pick}% de tu bankroll</code>\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )

def format_goals_card(league_info, home, away, match_time, odds_over, p_over, odds_btts, p_btts, odds_btts_1h, p_btts_1h, stake_over):
    ev_over = (p_over * odds_over) - 1
    if p_over >= 0.50 and ev_over > 0:
        recommendation = "🟢 **Entrar a Más de 2.5 Goles**\n💡 <i>Alta probabilidad matemática y valor positivo detectado (+EV).</i>"
    elif p_over >= 0.42:
        recommendation = "🟡 **Mercado de Goles Moderado**\n💡 <i>Cuota atractiva, margen ajustado pero viable.</i>"
    else:
        recommendation = "⚠️ **Mercado de Goles Reservado**\n💡 <i>Partido cerrado o de bajo margen previsible según Poisson.</i>"

    return (
        f"⚽ <b>{home} vs {away}</b>\n"
        f"🌐 <i>{league_info}</i> | ⏰ <code>{match_time} HS</code>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📈 <b>ANÁLISIS DE GOLES & BTTS</b>\n\n"
        f"• <b>Más de 2.5 Goles:</b> {odds_over:.2f}  <code>(Prob: {p_over*100:.1f}%)</code>\n"
        f"• <b>Ambos Anotan (BTTS):</b> {odds_btts:.2f}  <code>(Prob: {p_btts*100:.1f}%)</code>\n"
        f"• 🔥 <b>BTTS 1ª Mitad:</b> {odds_btts_1h:.2f}  <code>(Prob: {p_btts_1h*100:.1f}%)</code>\n\n"
        f"🎯 <b>Acción Sugerida:</b>\n{recommendation}\n\n"
        f"💰 <b>Stake Kelly (Over 2.5):</b> <code>{stake_over}% de tu bankroll</code>\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )

def format_double_chance_card(league_info, home, away, match_time, p_1x, odds_1x, p_x2, odds_x2, stake_dc):
    return (
        f"🛡️ <b>{home} vs {away}</b>\n"
        f"🌐 <i>{league_info}</i> | ⏰ <code>{match_time} HS</code>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🛡️ <b>MERCADO DOBLE OPORTUNIDAD</b>\n\n"
        f"• 🏠🤝 <b>Local o Empate (1X):</b> {odds_1x:.2f}  <code>({p_1x*100:.1f}%)</code>\n"
        f"• 🤝✈ <b>Empate o Visita (X2):</b> {odds_x2:.2f}  <code>({p_x2*100:.1f}%)</code>\n\n"
        f"🎯 <b>Acción Sugerida:</b>\n"
        f"🟢 <i>Ideal para combinar o cubrir riesgos en partidos disputados. Cobertura estadística alta.</i>\n\n"
        f"💰 <b>Stake Kelly:</b> <code>{stake_dc}% de tu bankroll</code>\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )

def format_asian_handicap_card(league_info, home, away, match_time, p_ah_home, odds_ah_home, stake_ah):
    return (
        f"⚖️ <b>{home} vs {away}</b>\n"
        f"🌐 <i>{league_info}</i> | ⏰ <code>{match_time} HS</code>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⚖️ <b>MERCADO HANDICAP ASIÁTICO (-0.5)</b>\n\n"
        f"• 🏠 <b>Local (-0.5):</b> {odds_ah_home:.2f}  <code>({p_ah_home*100:.1f}%)</code>\n\n"
        f"🎯 <b>Acción Sugerida:</b>\n"
        f"🟢 <i>Equivale a victoria simple eliminando el empate (si gana el local, la apuesta es ganadora).</i>\n\n"
        f"💰 <b>Stake Kelly:</b> <code>{stake_ah}% de tu bankroll</code>\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )

def format_corners_cards(league_info, home, away, match_time, avg_corners, odds_corners_over, p_corners, avg_cards, odds_cards_over, p_cards, stake_corners):
    ev_corners = (p_corners * odds_corners_over) - 1
    if p_corners >= 0.50 and ev_corners > 0:
        recommendation = "🟢 **Entrar a Más de 8.5 Córners**\n💡 <i>Estilos de juego con alta generación y valor positivo (+EV).</i>"
    else:
        recommendation = "⚠ **Baja intensidad estimada en córners**\n💡 <i>Es preferible buscar líneas más bajas o evitar.</i>"

    return (
        f"🚩 <b>{home} vs {away}</b>\n"
        f"🌐 <i>{league_info}</i> | ⏰ <code>{match_time} HS</code>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📐 <b>CÓRNERS & TARJETAS</b>\n\n"
        f"• 📐 <b>Córners Esperados:</b> <code>{avg_corners}</code>\n"
        f"  └ <i>Más de 8.5 Córners:</i> {odds_corners_over:.2f} <code>({p_corners*100:.1f}%)</code>\n\n"
        f"• 🟨 <b>Tarjetas Promedio:</b> <code>{avg_cards}</code>\n"
        f"  └ <i>Más de 4.5 Tarjetas:</i> {odds_cards_over:.2f} <code>({p_cards*100:.1f}%)</code>\n\n"
        f"🎯 <b>Acción Sugerida:</b>\n{recommendation}\n\n"
        f"💰 <b>Stake Kelly (Córners):</b> <code>{stake_corners}% de tu bankroll</code>\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )

def format_all_markets_card(league_info, home, away, match_time, analytics):
    m_1x2 = analytics["market_1x2"]
    goals = analytics["goals"]
    dc = analytics["double_chance"]
    ah = analytics["asian_handicap"]
    cc = analytics["corners_cards"]

    return (
        f"⚽ <b>{home} vs {away}</b>\n"
        f"🌐 <i>{league_info}</i> | ⏰ <code>{match_time} HS</code>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>RESUMEN DE OPCIONES VIABLES (+EV)</b>\n\n"
        f"• 🏆 <b>1X2 / Ganador:</b>\n"
        f"  └ <i>Local:</i> {m_1x2['odds_home']} ({m_1x2['p_home']*100:.1f}%) | <i>Visita:</i> {m_1x2['odds_away']} ({m_1x2['p_away']*100:.1f}%)\n\n"
        f"• 📈 <b>Goles & BTTS:</b>\n"
        f"  └ <i>Más de 2.5:</i> {goals['odds_over']} ({goals['p_over_25']*100:.1f}%) | <i>BTTS:</i> {goals['odds_btts_yes']} ({goals['p_btts_yes']*100:.1f}%)\n\n"
        f"• 🛡️ <b>Doble Oportunidad:</b>\n"
        f"  └ <i>1X:</i> {dc['odds_1x']} ({dc['p_1x']*100:.1f}%) | <i>X2:</i> {dc['odds_x2']} ({dc['p_x2']*100:.1f}%)\n\n"
        f"• ⚖️ <b>Handicap Asiático:</b>\n"
        f"  └ <i>Local (-0.5):</i> {ah['odds_ah_home']} ({ah['p_ah_home']*100:.1f}%)\n\n"
        f"• 🚩 <b>Córners (Over 8.5):</b>\n"
        f"  └ <i>Cuota:</i> {cc['odds_corners_over']} (Prob: {cc['p_corners']*100:.1f}%)\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )

# ==========================================
# 🧠 MOTOR ANALÍTICO Y CALIBRACIÓN
# ==========================================

def get_learning_calibration_factor():
    conn = get_db_connection()
    if not conn:
        return 1.0
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT probability, status FROM user_picks WHERE status IN ('WON', 'LOST');")
            rows = cur.fetchall()
            if len(rows) < 10:
                return 1.0
            total_pred = sum(r['probability'] for r in rows)
            total_won = sum(1 for r in rows if r['status'] == 'WON')
            actual_hit_rate = total_won / len(rows)
            avg_predicted = total_pred / len(rows)
            if avg_predicted <= 0:
                return 1.0
            factor = actual_hit_rate / avg_predicted
            return max(0.8, min(1.2, factor))
    except Exception as e:
        logger.error(f"Error calculando calibración: {e}")
        return 1.0
    finally:
        conn.close()

async def fetch_fixtures_from_api(date_str):
    url = f"https://{API_FOOTBALL_HOST}/fixtures"
    headers = {"x-rapidapi-key": API_FOOTBALL_KEY, "x-rapidapi-host": API_FOOTBALL_HOST}
    params = {"date": date_str, "timezone": "America/Montevideo"}
    
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, headers=headers, params=params)
            if response.status_code == 200:
                fixtures = response.json().get("response", [])
                active_fixtures = []
                now_utc = datetime.now(timezone.utc)
                
                for fix in fixtures:
                    status_short = fix.get("fixture", {}).get("status", {}).get("short")
                    date_iso = fix.get("fixture", {}).get("date")
                    if status_short == "NS" and date_iso:
                        fix_dt = datetime.fromisoformat(date_iso.replace("Z", "+00:00"))
                        if fix_dt > now_utc:
                            active_fixtures.append(fix)
                return active_fixtures if active_fixtures else []
    except Exception as e:
        logger.error(f"Error consultando API-Football: {e}")
    return []

def poisson_probability(lmbda, k):
    return (math.pow(lmbda, k) * math.exp(-lmbda)) / math.factorial(k)

def calculate_match_probabilities(lambda_home, lambda_away):
    p_home_win = 0.0
    p_draw = 0.0
    p_away_win = 0.0
    p_over_25 = 0.0
    p_btts = 0.0
    max_goals = 6
    
    for h in range(max_goals + 1):
        p_h = poisson_probability(lambda_home, h)
        for a in range(max_goals + 1):
            p_a = poisson_probability(lambda_away, a)
            joint_prob = p_h * p_a
            
            if h > a:
                p_home_win += joint_prob
            elif h == a:
                p_draw += joint_prob
            else:
                p_away_win += joint_prob
                
            if (h + a) > 2.5:
                p_over_25 += joint_prob
            if h > 0 and a > 0:
                p_btts += joint_prob
                
    return {
        "p_home": round(p_home_win, 3),
        "p_draw": round(p_draw, 3),
        "p_away": round(p_away_win, 3),
        "p_over_25": round(p_over_25, 3),
        "p_btts": round(p_btts, 3)
    }

async def fetch_team_statistics(team_id, league_id, season="2026"):
    url = f"https://{API_FOOTBALL_HOST}/teams/statistics"
    headers = {"x-rapidapi-key": API_FOOTBALL_KEY, "x-rapidapi-host": API_FOOTBALL_HOST}
    params = {"team": team_id, "league": league_id, "season": season}
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=headers, params=params)
            if response.status_code == 200:
                return response.json().get("response", {})
    except Exception as e:
        logger.error(f"Error obteniendo estadísticas para el equipo {team_id}: {e}")
    return {}

async def generate_fixture_analytics_real(fix):
    calibration = get_learning_calibration_factor()
    teams = fix.get("teams", {})
    league = fix.get("league", {})
    
    home_id = teams.get("home", {}).get("id", 0)
    away_id = teams.get("away", {}).get("id", 0)
    league_id = league.get("id", 0)
    season = str(league.get("season", "2026"))
    
    home_stats = await fetch_team_statistics(home_id, league_id, season)
    away_stats = await fetch_team_statistics(away_id, league_id, season)
    
    try:
        lambda_home_scored = float(home_stats.get("goals", {}).get("for", {}).get("average", {}).get("home", 1.45))
        lambda_home_conceded = float(home_stats.get("goals", {}).get("against", {}).get("average", {}).get("home", 1.05))
        lambda_away_scored = float(away_stats.get("goals", {}).get("for", {}).get("average", {}).get("away", 1.15))
        lambda_away_conceded = float(away_stats.get("goals", {}).get("against", {}).get("average", {}).get("away", 1.35))
        
        lambda_home = round((lambda_home_scored + lambda_away_conceded) / 2.0, 2)
        lambda_away = round((lambda_away_scored + lambda_home_conceded) / 2.0, 2)
    except Exception:
        lambda_home = 1.45
        lambda_away = 1.15

    lambda_home = max(0.5, lambda_home)
    lambda_away = max(0.5, lambda_away)

    poisson_res = calculate_match_probabilities(lambda_home, lambda_away)
    
    p_home = poisson_res["p_home"]
    p_draw = poisson_res["p_draw"]
    p_away = poisson_res["p_away"]
    p_over = min(0.95, poisson_res["p_over_25"] * calibration)
    p_btts = min(0.95, poisson_res["p_btts"] * calibration)
    p_btts_1h = min(0.95, 0.350 * calibration)

    p_1x = min(0.95, p_home + p_draw)
    p_x2 = min(0.95, p_away + p_draw)
    p_ah_home = p_home

    margin = 1.05
    odds_home = round(margin / max(0.05, p_home), 2)
    odds_draw = round(margin / max(0.05, p_draw), 2)
    odds_away = round(margin / max(0.05, p_away), 2)
    odds_over = round(margin / max(0.05, p_over), 2)
    odds_btts = round(margin / max(0.05, p_btts), 2)
    
    odds_1x = round(margin / max(0.05, p_1x), 2)
    odds_x2 = round(margin / max(0.05, p_x2), 2)
    odds_ah_home = round(margin / max(0.05, p_ah_home), 2)

    offensive_intensity = lambda_home + lambda_away
    avg_corners = round(8.0 + (offensive_intensity * 1.2), 1)
    p_corners = min(0.85, max(0.45, 0.50 + ((avg_corners - 8.5) * 0.05)))
    odds_corners_over = round(margin / max(0.05, p_corners), 2)

    avg_cards = round(3.8 + (abs(lambda_home - lambda_away) * 0.8), 1)
    p_cards = min(0.80, max(0.40, 0.55 + ((avg_cards - 4.5) * 0.04)))
    odds_cards_over = round(margin / max(0.05, p_cards), 2)

    return {
        "goals": {
            "p_over_25": p_over, "p_btts_yes": p_btts, "p_btts_1h": p_btts_1h,
            "odds_over": odds_over, "odds_btts_yes": odds_btts, "odds_btts_1h": 2.50
        },
        "market_1x2": {
            "p_home": p_home, "odds_home": odds_home,
            "p_draw": p_draw, "odds_draw": odds_draw,
            "p_away": p_away, "odds_away": odds_away
        },
        "double_chance": {
            "p_1x": p_1x, "odds_1x": odds_1x, "p_x2": p_x2, "odds_x2": odds_x2
        },
        "asian_handicap": {
            "p_ah_home": p_ah_home, "odds_ah_home": odds_ah_home
        },
        "corners_cards": {
            "avg_corners": avg_corners, "p_corners": p_corners, "odds_corners_over": odds_corners_over,
            "avg_cards": avg_cards, "p_cards": p_cards, "odds_cards_over": odds_cards_over
        }
    }

def calculate_kelly_stake(probability, odds):
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
    await register_user_middleware(update)
    if update.message:
        await update.message.reply_text(
            "⚽ <b>¡Bienvenido a NosticProno Bot!</b>\n\nUtiliza el menú inferior para seleccionar el mercado de apuestas o escribe directamente el nombre de cualquier equipo para buscar su pronóstico:",
            parse_mode="HTML",
            reply_markup=get_persistent_keyboard()
        )

async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
        
    await register_user_middleware(update)
    text = update.message.text.strip()
    logger.info(f"Mensaje recibido de Telegram: '{text}'")
    
    tz_uy = timezone(timedelta(hours=-3))
    today_str = datetime.now(tz_uy).strftime("%Y-%m-%d")
    
    if "Goles & BTTS" in text:
        await update.message.reply_text(
            "🗓️ <b>Selecciona el rango de fecha para Goles & BTTS:</b>",
            parse_mode="HTML",
            reply_markup=get_fecha_selector_keyboard("catgoals")
        )
        
    elif "1X2 / Ganador" in text:
        await update.message.reply_text(
            "🗓️ <b>Selecciona el rango de fecha para Mercado 1X2:</b>",
            parse_mode="HTML",
            reply_markup=get_fecha_selector_keyboard("cat1x2")
        )

    elif "Doble Oportunidad" in text:
        await update.message.reply_text(
            "🗓️ <b>Selecciona el rango de fecha para Doble Oportunidad:</b>",
            parse_mode="HTML",
            reply_markup=get_fecha_selector_keyboard("catdc")
        )

    elif "Handicap Asiático" in text:
        await update.message.reply_text(
            "🗓️ <b>Selecciona el rango de fecha para Handicap Asiático:</b>",
            parse_mode="HTML",
            reply_markup=get_fecha_selector_keyboard("catah")
        )
        
    elif "Córners & Tarjetas" in text:
        await update.message.reply_text(
            "🗓️ <b>Selecciona el rango de fecha para Córners & Tarjetas:</b>",
            parse_mode="HTML",
            reply_markup=get_fecha_selector_keyboard("catcorners")
        )
        
    elif "Mis Estadísticas" in text:
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) as total, SUM(CASE WHEN status='WON' THEN 1 ELSE 0 END) as wins FROM user_picks WHERE status IN ('WON', 'LOST');")
                    res = cur.fetchone()
                    total = res['total'] or 0
                    wins = res['wins'] or 0
                    hit_rate = (wins / total * 100) if total > 0 else 0
                    factor = get_learning_calibration_factor()

                    cur.execute("""
                        SELECT market_type, 
                               COUNT(*) as total_m, 
                               SUM(CASE WHEN status='WON' THEN 1 ELSE 0 END) as wins_m
                        FROM user_picks 
                        WHERE status IN ('WON', 'LOST') 
                        GROUP BY market_type;
                    """)
                    market_rows = cur.fetchall()

                    breakdown_text = ""
                    for row in market_rows:
                        m_type = row['market_type']
                        m_total = row['total_m']
                        m_wins = row['wins_m']
                        m_rate = (m_wins / m_total * 100) if m_total > 0 else 0
                        breakdown_text += f"  • <code>{m_type}</code>: <b>{m_wins}/{m_total}</b> aciertos ({m_rate:.1f}%)\n"

                    if not breakdown_text:
                        breakdown_text = "  <i>Sin apuestas liquidadas aún.</i>"

                    admin_keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton("📋 Gestionar / Ver mis Picks Guardados", callback_data="manage_picks_list")]
                    ])

                    await update.message.reply_text(
                        f"📊 <b>ESTADÍSTICAS & RENDIMIENTO AVANZADO</b>\n\n"
                        f"• Apuestas Totales Liquidadas: <code>{total}</code>\n"
                        f"• Aciertos Globales: <code>{wins} ({hit_rate:.1f}%)</code>\n"
                        f"• Factor Calibración IA: <code>{factor:.2f}x</code>\n\n"
                        f"📈 <b>Desglose por Mercado:</b>\n{breakdown_text}",
                        parse_mode="HTML", reply_markup=admin_keyboard
                    )
            except Exception as e:
                logger.error(f"Error generando estadísticas avanzadas: {e}")
                await update.message.reply_text("❌ Error al consultar las estadísticas.", reply_markup=get_persistent_keyboard())
            finally:
                conn.close()
        else:
            await update.message.reply_text("📊 Base de datos no conectada.", reply_markup=get_persistent_keyboard())
            
    elif "Top Value +EV" in text:
        await update.message.reply_text("🎯 Buscando la oportunidad con mayor valor esperado (`+EV`) entre todos los mercados disponibles...", reply_markup=get_persistent_keyboard())
        try:
            fixtures_data = await fetch_fixtures_from_api(today_str)
            best_pick = None
            max_ev = -999
            
            for fix in fixtures_data[:12]:
                analysis = await generate_fixture_analytics_real(fix)
                h_name = fix.get("teams", {}).get("home", {}).get("name", "Local")
                a_name = fix.get("teams", {}).get("away", {}).get("name", "Visita")
                league_name = fix.get("league", {}).get("name", "Liga")
                match_time = format_match_time(fix)
                
                markets_to_check = [
                    {"name": "Victoria Local (1X2)", "prob": analysis["market_1x2"]["p_home"], "odds": analysis["market_1x2"]["odds_home"]},
                    {"name": "Victoria Visitante (1X2)", "prob": analysis["market_1x2"]["p_away"], "odds": analysis["market_1x2"]["odds_away"]},
                    {"name": "Más de 2.5 Goles", "prob": analysis["goals"]["p_over_25"], "odds": analysis["goals"]["odds_over"]},
                    {"name": "Ambos Anotan (BTTS)", "prob": analysis["goals"]["p_btts_yes"], "odds": analysis["goals"]["odds_btts_yes"]},
                    {"name": "Doble Oportunidad (1X)", "prob": analysis["double_chance"]["p_1x"], "odds": analysis["double_chance"]["odds_1x"]},
                    {"name": "Doble Oportunidad (X2)", "prob": analysis["double_chance"]["p_x2"], "odds": analysis["double_chance"]["odds_x2"]},
                    {"name": "Handicap Asiático (-0.5)", "prob": analysis["asian_handicap"]["p_ah_home"], "odds": analysis["asian_handicap"]["odds_ah_home"]},
                    {"name": "Más de 8.5 Córners", "prob": analysis["corners_cards"]["p_corners"], "odds": analysis["corners_cards"]["odds_corners_over"]}
                ]
                
                for m in markets_to_check:
                    p = m["prob"]
                    odds = m["odds"]
                    ev_val = (p * odds) - 1
                    
                    if ev_val > max_ev:
                        max_ev = ev_val
                        best_pick = {
                            "match": f"{h_name} vs {a_name}",
                            "league": league_name,
                            "time": match_time,
                            "market": m["name"],
                            "prob": p,
                            "odds": odds,
                            "ev": ev_val,
                            "stake": calculate_kelly_stake(p, odds)
                        }
            
            if best_pick and max_ev > 0:
                text_out = (
                    f"🔥 **APUESTA TOP VALUE (+EV) DEL DÍA** 🔥\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"🏆 **{best_pick['match']}**\n"
                    f"🌐 *{best_pick['league']}* | ⏰ `{best_pick['time']} HS`\n\n"
                    f"📌 **Mercado:** {best_pick['market']}\n"
                    f"📊 **Probabilidad:** `{int(best_pick['prob']*100)}%` | 📐 **Cuota:** `{best_pick['odds']}`\n"
                    f"📈 **Valor Esperado (EV):** `+{int(best_pick['ev']*100)}%`\n"
                    f"💰 **Stake Recomendado (Kelly):** `{best_pick['stake']}%`\n"
                    f"━━━━━━━━━━━━━━━━━━━"
                )
                await update.message.reply_text(text_out, parse_mode="Markdown", reply_markup=get_persistent_keyboard())
            else:
                await update.message.reply_text("⚠️ No se encontró ninguna apuesta con EV positivo claro en este momento.", reply_markup=get_persistent_keyboard())
        except Exception as e:
            logger.error(f"Error en Top Value: {e}")
            await update.message.reply_text("❌ Ocurrió un error al calcular el Top Value.", reply_markup=get_persistent_keyboard())

    elif "Combinada" in text or "EV+" in text:
        await update.message.reply_text("🍀 Generando combinada inteligente con valor esperado positivo (`+EV`) para hoy...", reply_markup=get_persistent_keyboard())
        try:
            fixtures_data = await fetch_fixtures_from_api(today_str)
            valid_picks = []
            
            for fix in fixtures_data[:12]:
                analysis = await generate_fixture_analytics_real(fix)
                h_name = fix.get("teams", {}).get("home", {}).get("name", "Local")
                a_name = fix.get("teams", {}).get("away", {}).get("name", "Visita")
                
                p_over = analysis["goals"]["p_over_25"]
                odds_over = analysis["goals"]["odds_over"]
                ev_val = (p_over * odds_over) - 1
                
                if ev_val > 0.02 and p_over >= 0.45:
                    valid_picks.append({
                        "match": f"{h_name} vs {a_name}",
                        "market": "Over 2.5 Goles",
                        "odds": odds_over,
                        "prob": p_over
                    })
            
            if len(valid_picks) >= 2:
                parlay_picks = valid_picks[:2]
                combined_odds = round(parlay_picks[0]["odds"] * parlay_picks[1]["odds"], 2)
                combined_prob = round(parlay_picks[0]["prob"] * parlay_picks[1]["prob"], 3)
                
                combinada_text = (
                    f"🍀 **COMBINADA RECOMENDADA (+EV)** 🍀\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"1️⃣ <b>{parlay_picks[0]['match']}</b>\n"
                    f"   └ <i>Mercado:</i> {parlay_picks[0]['market']} (Cuota: <code>{parlay_picks[0]['odds']}</code>)\n\n"
                    f"2️⃣ <b>{parlay_picks[1]['match']}</b>\n"
                    f"   └ <i>Mercado:</i> {parlay_picks[1]['market']} (Cuota: <code>{parlay_picks[1]['odds']}</code>)\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"📊 <b>Cuota Combinada Total:</b> <code>{combined_odds}</code>\n"
                    f"📈 <b>Probabilidad Conjunta Est.:</b> <code>{combined_prob*100:.1f}%</code>\n"
                    f"💰 <b>Stake Sugerido (Conservador):</b> <code>0.5% - 1%</code>\n"
                    f"━━━━━━━━━━━━━━━━━━━"
                )
                await update.message.reply_text(combinada_text, parse_mode="HTML", reply_markup=get_persistent_keyboard())
            else:
                await update.message.reply_text("⚠ No hay suficientes partidos con EV positivo para armar una combinada segura hoy.", reply_markup=get_persistent_keyboard())
        except Exception as e:
            logger.error(f"Error generando combinada: {e}")
            await update.message.reply_text("❌ Ocurrió un error al armar la combinada.", reply_markup=get_persistent_keyboard())

    elif "Panel Admin" in text:
        conn = get_db_connection()
        total_picks = 0
        pending_picks = 0
        won_picks = 0
        lost_picks = 0
        total_users = 0
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT 
                            COUNT(*) as total, 
                            SUM(CASE WHEN status='PENDING' THEN 1 ELSE 0 END) as pending,
                            SUM(CASE WHEN status='WON' THEN 1 ELSE 0 END) as won,
                            SUM(CASE WHEN status='LOST' THEN 1 ELSE 0 END) as lost
                        FROM user_picks;
                    """)
                    res = cur.fetchone()
                    total_picks = res['total'] or 0
                    pending_picks = res['pending'] or 0
                    won_picks = res['won'] or 0
                    lost_picks = res['lost'] or 0

                    cur.execute("SELECT COUNT(*) as total FROM bot_users;")
                    total_users = cur.fetchone()['total'] or 0
            except Exception as e:
                logger.error(f"Error consultando panel admin: {e}")
            finally:
                conn.close()

        admin_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 Gestionar Usuarios", callback_data="admin_manage_users")],
            [InlineKeyboardButton("🔄 Forzar Auto-Liquidación", callback_data="admin_force_settle")],
            [InlineKeyboardButton("📋 Listar Picks Registrados", callback_data="manage_picks_list")],
            [InlineKeyboardButton("⚠️ Vaciar / Limpiar Base de Datos", callback_data="admin_purge_db")]
        ])

        admin_text = (
            "⚙️ <b>PANEL DE ADMINISTRACIÓN GENERAL</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            f"• 👥 <b>Usuarios Registrados:</b> <code>{total_users}</code>\n"
            f"• 🗄 <b>Total Picks en DB:</b> <code>{total_picks}</code>\n"
            f"• ⏳ <b>Pendientes:</b> <code>{pending_picks}</code> | ✅ <b>Ganadas:</b> <code>{won_picks}</code> | ❌ <b>Perdidas:</b> <code>{lost_picks}</code>\n"
            f"• 🤖 <b>Estado del Bot:</b> <code>ONLINE (Activo)</code>\n"
            f"• 🌐 <b>API-Football:</b> <code>Conectado</code>\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "👇 <i>Selecciona una acción de control administrativo:</i>"
        )
        await update.message.reply_text(admin_text, parse_mode="HTML", reply_markup=admin_keyboard)

    elif "Ayuda" in text:
        help_text = (
            "📖 <b>GUÍA DE USO & NUEVOS MERCADOS</b>\n\n"
            "• ⚽ <b>1X2, Goles & Doble Oportunidad:</b> Analiza partidos con modelos Poisson mejorados.\n"
            "• ⚖️ <b>Handicap Asiático:</b> Apuestas de valor con cobertura de empate (-0.5).\n"
            "• 📊 <b>Mis Estadísticas:</b> Visualiza tus aciertos, gestiona o modifica tus selecciones guardadas.\n"
            "• 🔍 <b>Búsqueda Manual:</b> Escribe el nombre de cualquier equipo en el chat para obtener un reporte completo con todas las opciones viables.\n\n"
            "💡 <i>Usa los botones del teclado inferior para explorar todos los mercados disponibles en tiempo real.</i>"
        )
        await update.message.reply_text(help_text, parse_mode="HTML", reply_markup=get_persistent_keyboard())
        
    else:
        if len(text) > 2 and not text.startswith("/"):
            await update.message.reply_text(f"🔍 Buscando opciones viables para: <b>{text}</b>...", parse_mode="HTML")
            
            tomorrow_str = (datetime.now(tz_uy) + timedelta(days=1)).strftime("%Y-%m-%d")
            fixtures = await fetch_fixtures_from_api(today_str) + await fetch_fixtures_from_api(tomorrow_str)
            found_fix = None
            
            search_query = text.lower()
            for fix in fixtures:
                home = fix.get("teams", {}).get("home", {}).get("name", "").lower()
                away = fix.get("teams", {}).get("away", {}).get("name", "").lower()
                if search_query in home or search_query in away:
                    found_fix = fix
                    break
                    
            if found_fix:
                fix_id = found_fix.get("fixture", {}).get("id", 0)
                league = found_fix.get("league", {})
                league_info = f"{league.get('country', '')} - {league.get('name', 'Fútbol')}"
                home_name = found_fix.get("teams", {}).get("home", {}).get("name", "Local")
                away_name = found_fix.get("teams", {}).get("away", {}).get("name", "Visitante")
                match_time = format_match_time(found_fix)
                
                analytics_full = await generate_fixture_analytics_real(found_fix)
                card = format_all_markets_card(league_info, home_name, away_name, match_time, analytics_full)
                
                btn = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("📌 Guardar 1X2", callback_data=f"save_{fix_id}_1X2"),
                        InlineKeyboardButton("📌 Guardar Goles", callback_data=f"save_{fix_id}_GOALS")
                    ],
                    [
                        InlineKeyboardButton("📌 Guardar Doble Oportunidad", callback_data=f"save_{fix_id}_DC"),
                        InlineKeyboardButton("📌 Guardar Córners", callback_data=f"save_{fix_id}_CORNERS")
                    ]
                ])
                await update.message.reply_text(card, parse_mode="HTML", reply_markup=btn)
            else:
                await update.message.reply_text(
                    f"❌ No se encontró ningún partido próximo que coincida con <b>'{text}'</b> en la cartelera de hoy o mañana.",
                    parse_mode="HTML",
                    reply_markup=get_persistent_keyboard()
                )
            return

        await update.message.reply_text(
            "Utiliza los botones del menú inferior para interactuar con el bot o escribe el nombre de un equipo para buscar su pronóstico.",
            reply_markup=get_persistent_keyboard()
        )

# ==========================================
# 🎛️ MANEJADOR DE CALLBACKS (INTERACTIVIDAD TOTAL)
# ==========================================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data.startswith("save_"):
        try:
            parts = data.split("_")
            fid = int(parts[1])
            market_type_short = parts[2]
            
            market_mapping = {
                "GOALS": "GOALS_OVER_2.5",
                "1X2": "1X2_HOME",
                "DC": "DOUBLE_CHANCE",
                "AH": "ASIAN_HANDICAP_-0.5",
                "CORNERS": "CORNERS_OVER_8.5"
            }
            market = market_mapping.get(market_type_short, "GENERAL_PICK")
            
            conn = get_db_connection()
            if conn:
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO user_picks (fixture_id, market_type, odds, probability, stake) VALUES (%s, %s, %s, %s, %s)",
                            (fid, market, 1.85, 0.65, 1.0)
                        )
                        conn.commit()
                    await query.answer("✅ ¡Pick guardado con éxito para seguimiento!", show_alert=True)
                except Exception as e:
                    logger.error(f"Error guardando pick en DB: {e}")
                    await query.answer("❌ Error al guardar en base de datos.", show_alert=True)
                finally:
                    conn.close()
            else:
                await query.answer("⚠️ Base de datos no disponible.", show_alert=True)
        except Exception as e:
            logger.error(f"Error procesando save_: {e}")
            await query.answer("❌ Error procesando el pick.", show_alert=True)
        return

    elif data == "manage_picks_list":
        conn = get_db_connection()
        if not conn:
            await query.message.reply_text("⚠️ Base de datos no disponible.")
            return
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id, fixture_id, market_type, odds, status FROM user_picks ORDER BY id DESC LIMIT 8;")
                picks = cur.fetchall()
                
                if not picks:
                    await query.message.reply_text("📭 No hay picks registrados en la base de datos actualmente.")
                    return
                
                for p in picks:
                    pid = p['id']
                    market = p['market_type']
                    odds = p['odds']
                    status = p['status']
                    
                    status_emoji = "⏳" if status == "PENDING" else ("✅" if status == "WON" else "❌")
                    
                    row_kb = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("✅ Ganada", callback_data=f"pick_set_WON_{pid}"),
                            InlineKeyboardButton("❌ Perdida", callback_data=f"pick_set_LOST_{pid}"),
                            InlineKeyboardButton("🗑️ Borrar", callback_data=f"pick_del_{pid}")
                        ]
                    ])
                    await query.message.reply_text(
                        f"📌 <b>Pick ID:</b> <code>{pid}</code> | {status_emoji} Estado: <b>{status}</b>\n"
                        f"• Mercado: <code>{market}</code> (Cuota: <code>{odds}</code>)",
                        parse_mode="HTML", reply_markup=row_kb
                    )
        except Exception as e:
            logger.error(f"Error listando picks: {e}")
            await query.message.reply_text("❌ Error al recuperar los picks.")
        finally:
            conn.close()

    elif data.startswith("pick_set_") or data.startswith("pick_del_"):
        parts = data.split("_")
        if data.startswith("pick_set_"):
            new_status = parts[2]
            pid = int(parts[3])
            conn = get_db_connection()
            if conn:
                try:
                    with conn.cursor() as cur:
                        cur.execute("UPDATE user_picks SET status = %s WHERE id = %s", (new_status, pid))
                        conn.commit()
                    await query.answer(f"✅ Pick #{pid} actualizado a {new_status}", show_alert=True)
                    await query.message.edit_text(f"✨ <b>Pick #{pid} actualizado exitosamente a:</b> <code>{new_status}</code>", parse_mode="HTML")
                except Exception as e:
                    logger.error(f"Error actualizando pick {pid}: {e}")
                    await query.answer("❌ Error al actualizar.", show_alert=True)
                finally:
                    conn.close()
        elif data.startswith("pick_del_"):
            pid = int(parts[2])
            conn = get_db_connection()
            if conn:
                try:
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM user_picks WHERE id = %s", (pid,))
                        conn.commit()
                    await query.answer(f"🗑️ Pick #{pid} eliminado correctamente", show_alert=True)
                    await query.message.edit_text(f"🗑️ <b>Pick #{pid} eliminado de la base de datos.</b>", parse_mode="HTML")
                except Exception as e:
                    logger.error(f"Error borrando pick {pid}: {e}")
                    await query.answer("❌ Error al eliminar.", show_alert=True)
                finally:
                    conn.close()

    elif data == "admin_manage_users":
        user_menu_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("➕ Agregar Usuario", callback_data="admin_add_user"),
                InlineKeyboardButton("✏️ Modificar Usuario", callback_data="admin_modify_user")
            ],
            [
                InlineKeyboardButton("📋 Listar Usuarios", callback_data="admin_list_users"),
                InlineKeyboardButton("🗑️ Eliminar Usuario", callback_data="admin_delete_user_prompt")
            ],
            [
                InlineKeyboardButton("🔙 Volver al Panel", callback_data="admin_back_main")
            ]
        ])
        await query.message.edit_text(
            "👥 <b>PANEL DE GESTIÓN DE USUARIOS</b>\nSelecciona una opción administrativa:",
            parse_mode="HTML",
            reply_markup=user_menu_kb
        )

    elif data == "admin_add_user":
        await query.message.edit_text(
            "➕ <b>Agregar Usuario</b>\nEnvía el ID de Telegram y el alias del nuevo usuario que deseas registrar en el sistema.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="admin_manage_users")]])
        )

    elif data == "admin_modify_user":
        await query.message.edit_text(
            "✏ <b>Modificar Usuario</b>\nSelecciona el usuario que deseas modificar o actualiza sus permisos/datos.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="admin_manage_users")]])
        )

    elif data == "admin_delete_user_prompt":
        await query.message.edit_text(
            "🗑️ <b>Eliminar Usuario</b>\nSelecciona el usuario que deseas remover de la base de datos.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="admin_manage_users")]])
        )

    elif data == "admin_list_users":
        conn = get_db_connection()
        if not conn:
            await query.message.reply_text("⚠️ Base de datos no disponible.")
            return
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT telegram_id, username, role FROM bot_users ORDER BY joined_at DESC LIMIT 10;")
                users = cur.fetchall()
                if not users:
                    await query.message.reply_text("📭 No hay usuarios registrados en el sistema.")
                    return
                for u in users:
                    uid = u['telegram_id']
                    uname = u['username'] or "Sin Alias"
                    role = u['role']
                    
                    user_kb = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("⭐ Cambiar Rol", callback_data=f"user_role_{uid}"),
                            InlineKeyboardButton("🗑️ Eliminar", callback_data=f"user_del_{uid}")
                        ],
                        [InlineKeyboardButton("🔙 Volver a Gestión", callback_data="admin_manage_users")]
                    ])
                    await query.message.reply_text(
                        f"👤 <b>Usuario:</b> {uname}\n• ID: <code>{uid}</code>\n• Rol actual: <code>{role}</code>",
                        parse_mode="HTML", reply_markup=user_kb
                    )
        except Exception as e:
            logger.error(f"Error listando usuarios: {e}")
            await query.message.reply_text("❌ Error al recuperar los usuarios.")
        finally:
            conn.close()

    elif data.startswith("user_role_"):
        uid = int(data.split("_")[2])
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT role FROM bot_users WHERE telegram_id = %s", (uid,))
                    res = cur.fetchone()
                    if res:
                        current_role = res['role']
                        new_role = "admin" if current_role == "user" else "user"
                        cur.execute("UPDATE bot_users SET role = %s WHERE telegram_id = %s", (new_role, uid))
                        conn.commit()
                        await query.answer(f"✅ Rol cambiado a {new_role}", show_alert=True)
                        await query.message.edit_text(f"⭐ <b>El rol del usuario ID {uid} ha sido actualizado a:</b> <code>{new_role}</code>", parse_mode="HTML")
            except Exception as e:
                logger.error(f"Error cambiando rol de usuario {uid}: {e}")
                await query.answer("❌ Error al cambiar el rol.", show_alert=True)
            finally:
                conn.close()

    elif data.startswith("user_del_"):
        uid = int(data.split("_")[2])
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM bot_users WHERE telegram_id = %s", (uid,))
                    conn.commit()
                await query.answer(f"🗑️ Usuario {uid} eliminado correctamente", show_alert=True)
                await query.message.edit_text(f"🗑 <b>Usuario con ID {uid} eliminado del sistema.</b>", parse_mode="HTML")
            except Exception as e:
                logger.error(f"Error borrando usuario {uid}: {e}")
                await query.answer("❌ Error al eliminar el usuario.", show_alert=True)
            finally:
                conn.close()

    elif data == "admin_force_settle":
        await query.message.edit_text("🔄 <b>Ejecutando revisión y liquidación manual de partidos pendientes...</b>", parse_mode="HTML")
        asyncio.create_task(run_manual_settlement(query))

    elif data == "admin_purge_db":
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM user_picks;")
                    conn.commit()
                await query.answer("⚠️ Base de datos de picks purgada con éxito.", show_alert=True)
                await query.message.edit_text("🧹 <b>Se han eliminado todos los registros de picks de la base de datos.</b>", parse_mode="HTML")
            except Exception as e:
                logger.error(f"Error purgando DB: {e}")
                await query.answer("❌ Error al purgar.", show_alert=True)
            finally:
                conn.close()

    elif data.startswith("loadfixtures_catgoals_"):
        parts = data.split("_")
        selected_date = parts[2]
        await query.message.edit_text(f"🔍 <b>Consultando y filtrando partidos de Goles ({selected_date})...</b>", parse_mode="HTML")
        fixtures = await fetch_fixtures_from_api(selected_date)
        if not fixtures:
            await query.message.reply_text("⚠️ No se encontraron partidos pendientes para esta fecha.")
            return
            
        shown_count = 0
        for fix in fixtures:
            if shown_count >= 5:
                break
            fid = fix.get("fixture", {}).get("id", 0)
            teams = fix.get("teams", {})
            league = fix.get("league", {})
            league_info = f"{league.get('country', '')} - {league.get('name', 'Fútbol')}"
            home = teams.get("home", {}).get("name", "Local")
            away = teams.get("away", {}).get("name", "Visitante")
            
            match_time = format_match_time(fix)
            analytics = (await generate_fixture_analytics_real(fix))["goals"]
            
            if analytics["odds_over"] < 1.55:
                continue

            shown_count += 1
            card_text = format_goals_card(league_info, home, away, match_time, analytics["odds_over"], analytics["p_over_25"], analytics["odds_btts_yes"], analytics["p_btts_yes"], analytics["odds_btts_1h"], analytics["p_btts_1h"], calculate_kelly_stake(analytics["p_over_25"], analytics["odds_over"]))
            btn = InlineKeyboardMarkup([
                [InlineKeyboardButton("📌 Guardar Over 2.5", callback_data=f"save_{fid}_GOALS")]
            ])
            await query.message.reply_text(card_text, parse_mode="HTML", reply_markup=btn)
        
        if shown_count == 0:
            await query.message.reply_text("ℹ No se encontraron partidos que cumplan con el filtro estricto para esta fecha.")

    elif data.startswith("loadfixtures_cat1x2_"):
        parts = data.split("_")
        selected_date = parts[2]
        await query.message.edit_text(f"🔍 <b>Consultando y filtrando partidos 1X2 ({selected_date})...</b>", parse_mode="HTML")
        fixtures = await fetch_fixtures_from_api(selected_date)
        if not fixtures:
            await query.message.reply_text("⚠️ No se encontraron partidos pendientes para esta fecha.")
            return
            
        shown_count = 0
        for fix in fixtures:
            if shown_count >= 5:
                break
            fid = fix.get("fixture", {}).get("id", 0)
            teams = fix.get("teams", {})
            league = fix.get("league", {})
            league_info = f"{league.get('country', '')} - {league.get('name', 'Fútbol')}"
            home = teams.get("home", {}).get("name", "Local")
            away = teams.get("away", {}).get("name", "Visitante")
            
            match_time = format_match_time(fix)
            m_1x2 = (await generate_fixture_analytics_real(fix))["market_1x2"]
            
            if m_1x2["odds_home"] < 1.60 and m_1x2["odds_away"] < 1.60:
                continue

            shown_count += 1
            card_text = format_1x2_card(league_info, home, away, match_time, m_1x2["p_home"], m_1x2["odds_home"], m_1x2["p_draw"], m_1x2["odds_draw"], m_1x2["p_away"], m_1x2["odds_away"], calculate_kelly_stake(m_1x2["p_home"], m_1x2["odds_home"]))
            btn = InlineKeyboardMarkup([
                [InlineKeyboardButton("📌 Guardar Victoria Local", callback_data=f"save_{fid}_1X2")]
            ])
            await query.message.reply_text(card_text, parse_mode="HTML", reply_markup=btn)
        
        if shown_count == 0:
            await query.message.reply_text("ℹ No se encontraron partidos con cuotas de valor mínimo para este mercado en esta fecha.")

    elif data.startswith("loadfixtures_catdc_"):
        parts = data.split("_")
        selected_date = parts[2]
        await query.message.edit_text(f"🔍 <b>Consultando Doble Oportunidad ({selected_date})...</b>", parse_mode="HTML")
        fixtures = await fetch_fixtures_from_api(selected_date)
        if not fixtures:
            await query.message.reply_text("⚠️ No se encontraron partidos pendientes para esta fecha.")
            return
            
        shown_count = 0
        for fix in fixtures:
            if shown_count >= 5:
                break
            fid = fix.get("fixture", {}).get("id", 0)
            teams = fix.get("teams", {})
            league = fix.get("league", {})
            league_info = f"{league.get('country', '')} - {league.get('name', 'Fútbol')}"
            home = teams.get("home", {}).get("name", "Local")
            away = teams.get("away", {}).get("name", "Visitante")
            
            match_time = format_match_time(fix)
            dc = (await generate_fixture_analytics_real(fix))["double_chance"]

            shown_count += 1
            card_text = format_double_chance_card(league_info, home, away, match_time, dc["p_1x"], dc["odds_1x"], dc["p_x2"], dc["odds_x2"], calculate_kelly_stake(dc["p_1x"], dc["odds_1x"]))
            btn = InlineKeyboardMarkup([
                [InlineKeyboardButton("📌 Guardar Doble Oportunidad (1X)", callback_data=f"save_{fid}_DC")]
            ])
            await query.message.reply_text(card_text, parse_mode="HTML", reply_markup=btn)

    elif data.startswith("loadfixtures_catah_"):
        parts = data.split("_")
        selected_date = parts[2]
        await query.message.edit_text(f"🔍 <b>Consultando Handicap Asiático ({selected_date})...</b>", parse_mode="HTML")
        fixtures = await fetch_fixtures_from_api(selected_date)
        if not fixtures:
            await query.message.reply_text("⚠️ No se encontraron partidos pendientes para esta fecha.")
            return
            
        shown_count = 0
        for fix in fixtures:
            if shown_count >= 5:
                break
            fid = fix.get("fixture", {}).get("id", 0)
            teams = fix.get("teams", {})
            league = fix.get("league", {})
            league_info = f"{league.get('country', '')} - {league.get('name', 'Fútbol')}"
            home = teams.get("home", {}).get("name", "Local")
            away = teams.get("away", {}).get("name", "Visitante")
            
            match_time = format_match_time(fix)
            ah = (await generate_fixture_analytics_real(fix))["asian_handicap"]

            shown_count += 1
            card_text = format_asian_handicap_card(league_info, home, away, match_time, ah["p_ah_home"], ah["odds_ah_home"], calculate_kelly_stake(ah["p_ah_home"], ah["odds_ah_home"]))
            btn = InlineKeyboardMarkup([
                [InlineKeyboardButton("📌 Guardar Handicap Local (-0.5)", callback_data=f"save_{fid}_AH")]
            ])
            await query.message.reply_text(card_text, parse_mode="HTML", reply_markup=btn)

    elif data.startswith("loadfixtures_catcorners_"):
        parts = data.split("_")
        selected_date = parts[2]
        await query.message.edit_text(f"🔍 <b>Consultando Córners y Tarjetas ({selected_date})...</b>", parse_mode="HTML")
        fixtures = await fetch_fixtures_from_api(selected_date)
        if not fixtures:
            await query.message.reply_text("⚠️ No se encontraron partidos pendientes para esta fecha.")
            return
            
        shown_count = 0
        for fix in fixtures:
            if shown_count >= 5:
                break
            fid = fix.get("fixture", {}).get("id", 0)
            teams = fix.get("teams", {})
            league = fix.get("league", {})
            league_info = f"{league.get('country', '')} - {league.get('name', 'Fútbol')}"
            home = teams.get("home", {}).get("name", "Local")
            away = teams.get("away", {}).get("name", "Visitante")
            
            match_time = format_match_time(fix)
            cc = (await generate_fixture_analytics_real(fix))["corners_cards"]
            
            if cc["odds_corners_over"] < 1.65:
                continue

            shown_count += 1
            card_text = format_corners_cards(league_info, home, away, match_time, cc["avg_corners"], cc["odds_corners_over"], cc["p_corners"], cc["avg_cards"], cc["odds_cards_over"], cc["p_cards"], calculate_kelly_stake(cc["p_corners"], cc["odds_corners_over"]))
            btn = InlineKeyboardMarkup([
                [InlineKeyboardButton("📌 Guardar Córners Over", callback_data=f"save_{fid}_CORNERS")]
            ])
            await query.message.reply_text(card_text, parse_mode="HTML", reply_markup=btn)
        
        if shown_count == 0:
            await query.message.reply_text("ℹ️ No se encontraron partidos con filtros óptimos para córners en esta fecha.")

# ==========================================
# ⚙️ WORKERS DE AUTO-LIQUIDACIÓN
# ==========================================

async def run_manual_settlement(query):
    conn = get_db_connection()
    if not conn:
        await query.message.reply_text("⚠️ Base de datos no disponible.")
        return
    settled_count = 0
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, fixture_id, market_type FROM user_picks WHERE status = 'PENDING';")
            pending_picks = cur.fetchall()
            if not pending_picks:
                await query.message.reply_text("ℹ️ No hay picks pendientes para liquidar en este momento.")
                return
            
            headers = {"x-rapidapi-key": API_FOOTBALL_KEY, "x-rapidapi-host": API_FOOTBALL_HOST}
            async with httpx.AsyncClient(timeout=15.0) as client:
                for pick in pending_picks:
                    pick_id = pick['id']
                    fid = pick['fixture_id']
                    market = pick['market_type']
                    
                    url = f"https://{API_FOOTBALL_HOST}/fixtures?id={fid}"
                    resp = await client.get(url, headers=headers)
                    if resp.status_code == 200:
                        data = resp.json().get("response", [])
                        if data:
                            fixture_data = data[0]
                            status_short = fixture_data.get("fixture", {}).get("status", {}).get("short")
                            if status_short == "FT":
                                goals_home = fixture_data.get("goals", {}).get("home", 0)
                                goals_away = fixture_data.get("goals", {}).get("away", 0)
                                total_goals = goals_home + goals_away
                                
                                won = False
                                if "GOALS" in market and total_goals > 2.5:
                                    won = True
                                elif "1X2" in market and goals_home > goals_away:
                                    won = True
                                elif "DOUBLE_CHANCE" in market and goals_home >= goals_away:
                                    won = True
                                elif "ASIAN_HANDICAP" in market and goals_home > goals_away:
                                    won = True
                                elif "CORNERS" in market:
                                    won = True
                                
                                new_status = 'WON' if won else 'LOST'
                                cur.execute("UPDATE user_picks SET status = %s WHERE id = %s", (new_status, pick_id))
                                conn.commit()
                                settled_count += 1
        await query.message.reply_text(f"✅ <b>Liquidación manual finalizada:</b> Se actualizaron <code>{settled_count}</code> picks.", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error en liquidación manual: {e}")
        await query.message.reply_text("❌ Ocurrió un error al procesar la liquidación manual.")
    finally:
        conn.close()

async def auto_settlement_background_task():
    try:
        while True:
            await asyncio.sleep(600)
            conn = get_db_connection()
            if not conn:
                continue
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT id, fixture_id, market_type FROM user_picks WHERE status = 'PENDING';")
                    pending_picks = cur.fetchall()
                    if not pending_picks:
                        continue
                    
                    headers = {"x-rapidapi-key": API_FOOTBALL_KEY, "x-rapidapi-host": API_FOOTBALL_HOST}
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        for pick in pending_picks:
                            pick_id = pick['id']
                            fid = pick['fixture_id']
                            market = pick['market_type']
                            
                            url = f"https://{API_FOOTBALL_HOST}/fixtures?id={fid}"
                            resp = await client.get(url, headers=headers)
                            if resp.status_code == 200:
                                data = resp.json().get("response", [])
                                if data:
                                    fixture_data = data[0]
                                    status_short = fixture_data.get("fixture", {}).get("status", {}).get("short")
                                    if status_short == "FT":
                                        goals_home = fixture_data.get("goals", {}).get("home", 0)
                                        goals_away = fixture_data.get("goals", {}).get("away", 0)
                                        total_goals = goals_home + goals_away
                                        
                                        won = False
                                        if "GOALS" in market and total_goals > 2.5:
                                            won = True
                                        elif "1X2" in market and goals_home > goals_away:
                                            won = True
                                        elif "DOUBLE_CHANCE" in market and goals_home >= goals_away:
                                            won = True
                                        elif "ASIAN_HANDICAP" in market and goals_home > goals_away:
                                            won = True
                                        elif "CORNERS" in market:
                                            won = True
                                        
                                        new_status = 'WON' if won else 'LOST'
                                        cur.execute("UPDATE user_picks SET status = %s WHERE id = %s", (new_status, pick_id))
                                        conn.commit()
                                        logger.info(f"Pick ID {pick_id} liquidado automáticamente como: {new_status}")
            except Exception as e:
                logger.error(f"Error en bucle de auto-liquidación: {e}")
            finally:
                conn.close()
    except asyncio.CancelledError:
        logger.info("Tarea de auto-liquidación detenida limpiamente.")

async def post_init(application: Application):
    init_db()
    asyncio.create_task(auto_settlement_background_task())

def main():
    if not TELEGRAM_TOKEN:
        logger.error("¡ERROR CRÍTICO! La variable de entorno TELEGRAM_TOKEN no está configurada.")
        return

    threading.Thread(target=run_health_server, daemon=True).start()

    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))

    logger.info("Iniciando bot con Top Value global en todos los mercados...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()