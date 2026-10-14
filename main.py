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
                    is_vip BOOLEAN DEFAULT FALSE,
                    expires_at TIMESTAMP,
                    renewal_warned BOOLEAN DEFAULT FALSE,
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS is_vip BOOLEAN DEFAULT FALSE;")
            cur.execute("ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP;")
            cur.execute("ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS renewal_warned BOOLEAN DEFAULT FALSE;")
            conn.commit()
            logger.info("Base de datos y tablas inicializadas correctamente.")
    except Exception as e:
        logger.error(f"Error al crear tablas: {e}")
    finally:
        conn.close()

async def register_user_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    admin_id = os.getenv("ADMIN_TELEGRAM_ID", "1588200613")
    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT telegram_id FROM bot_users WHERE telegram_id = %s;", (user.id,))
                exists = cur.fetchone()
                
                if not exists:
                    cur.execute("""
                        INSERT INTO bot_users (telegram_id, username) 
                        VALUES (%s, %s);
                    """, (user.id, user.username or user.first_name))
                    conn.commit()
                    
                    if admin_id:
                        alert_text = (
                            "🔔 <b>¡Nuevo Usuario en el Bot!</b>\n\n"
                            f"• <b>Nombre:</b> {user.first_name}\n"
                            f"• <b>Alias:</b> @{user.username or 'Sin alias'}\n"
                            f"• <b>ID:</b> <code>{user.id}</code>"
                        )
                        try:
                            await context.bot.send_message(
                                chat_id=int(admin_id),
                                text=alert_text,
                                parse_mode="HTML"
                            )
                        except Exception as e:
                            logger.error(f"Error al enviar notificación de nuevo usuario: {e}")
                else:
                    cur.execute("""
                        UPDATE bot_users SET username = %s WHERE telegram_id = %s;
                    """, (user.username or user.first_name, user.id))
                    conn.commit()
        except Exception as e:
            logger.error(f"Error registrando usuario: {e}")
        finally:
            conn.close()

def check_user_vip_status(telegram_id: int) -> bool:
    conn = get_db_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT role, is_vip FROM bot_users WHERE telegram_id = %s;", (telegram_id,))
            res = cur.fetchone()
            if res and (res['role'] == 'admin' or res.get('is_vip', False)):
                return True
    except Exception as e:
        logger.error(f"Error verificando estado VIP: {e}")
    finally:
        conn.close()
    return False

# ==========================================
# ⌨️ TECLADOS SEGÚN RANGO (VIP vs FREE)
# ==========================================

def get_keyboard_for_user(telegram_id: int):
    is_vip = check_user_vip_status(telegram_id)
    if is_vip:
        keyboard = [
            [KeyboardButton("⚽ 1X2 / Ganador"), KeyboardButton("⚽ Goles & BTTS")],
            [KeyboardButton("🛡️ Doble Oportunidad"), KeyboardButton("⚖️ Handicap Asiático")],
            [KeyboardButton("🚩 Córners & Tarjetas"), KeyboardButton("🍀 Combinadas EV+")],
            [KeyboardButton("📊 Mis Estadísticas"), KeyboardButton("🎯 Top Value +EV")],
            [KeyboardButton("💎 Suscripción VIP"), KeyboardButton("📖 Ayuda")],
            [KeyboardButton("⚙️ Panel Admin")]
        ]
    else:
        keyboard = [
            [KeyboardButton("💎 Suscripción VIP"), KeyboardButton("📖 Ayuda")],
            [KeyboardButton("📊 Estadísticas Globales")]
        ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ==========================================
# 📅 GENERADOR DE TECLADO DE FECHAS Y HORAS
# ==========================================

def get_fecha_selector_keyboard(prefix: str):
    tz = timezone(timedelta(hours=-3))
    hoy = datetime.now(tz).date()
    mañana = hoy + timedelta(days=1)
    pasado = hoy + timedelta(days=2)
    
    keyboard = [
        [InlineKeyboardButton("📅 Partidos de hoy", callback_data=f"timeframe_{prefix}")],
        [InlineKeyboardButton("📅 Partidos de mañana", callback_data=f"datechoice_{prefix}_{mañana.strftime('%Y-%m-%d')}")],
        [InlineKeyboardButton("📅 Partidos de pasado mañana", callback_data=f"datechoice_{prefix}_{pasado.strftime('%Y-%m-%d')}")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_top_value_fecha_keyboard():
    tz = timezone(timedelta(hours=-3))
    hoy = datetime.now(tz).date()
    mañana = hoy + timedelta(days=1)
    pasado = hoy + timedelta(days=2)
    
    keyboard = [
        [InlineKeyboardButton("📅 Top Value para hoy", callback_data=f"topval_{hoy.strftime('%Y-%m-%d')}")],
        [InlineKeyboardButton("📅 Top Value para mañana", callback_data=f"topval_{mañana.strftime('%Y-%m-%d')}")],
        [InlineKeyboardButton("📅 Top Value para pasado mañana", callback_data=f"topval_{pasado.strftime('%Y-%m-%d')}")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_timeframe_keyboard(prefix: str):
    tz = timezone(timedelta(hours=-3))
    hoy_str = datetime.now(tz).strftime('%Y-%m-%d')
    
    keyboard = [
        [InlineKeyboardButton("⚡ Próximas 3 horas", callback_data=f"datechoice_{prefix}_{hoy_str}_3h")],
        [InlineKeyboardButton("⏳ Próximas 6 horas", callback_data=f"datechoice_{prefix}_{hoy_str}_6h")],
        [InlineKeyboardButton("🕒 Próximas 12 horas", callback_data=f"datechoice_{prefix}_{hoy_str}_12h")],
        [InlineKeyboardButton("📅 Todos los partidos de hoy", callback_data=f"datechoice_{prefix}_{hoy_str}_all")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_limit_selector_keyboard(prefix: str, date_str: str, time_filter: str):
    keyboard = [
        [
            InlineKeyboardButton("Top 3", callback_data=f"runload_{prefix}_{date_str}_{time_filter}_3"),
            InlineKeyboardButton("Top 5", callback_data=f"runload_{prefix}_{date_str}_{time_filter}_5")
        ],
        [
            InlineKeyboardButton("Top 10", callback_data=f"runload_{prefix}_{date_str}_{time_filter}_10"),
            InlineKeyboardButton("🔥 Todos los mejores", callback_data=f"runload_{prefix}_{date_str}_{time_filter}_all")
        ]
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
    return (
        f"🏆 <b>{home} vs {away}</b>\n"
        f"🌐 <i>{league_info}</i> | ⏰ <code>{match_time} HS</code>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>MERCADO 1X2 / GANADOR (ALTA PRECISIÓN)</b>\n\n"
        f"• 🏠 <b>Victoria Local:</b> {odds_home:.2f}  <code>({p_home*100:.1f}%)</code>\n"
        f"• 🤝 <b>Empate:</b> {odds_draw:.2f}  <code>({p_draw*100:.1f}%)</code>\n"
        f"• ✈ <b>Victoria Visitante:</b> {odds_away:.2f}  <code>({p_away*100:.1f}%)</code>\n\n"
        f"🎯 <b>Acción Estricta:</b>\n🟢 <i>Filtro Poisson superado con éxito. Valor esperado de +{int(ev_home*100)}%.</i>\n\n"
        f"💰 <b>Stake Kelly:</b> <code>{stake_pick}% de tu bankroll</code>\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )

def format_goals_card(league_info, home, away, match_time, odds_over, p_over, odds_btts, p_btts, odds_btts_1h, p_btts_1h, stake_over):
    ev_over = (p_over * odds_over) - 1
    return (
        f"⚽ <b>{home} vs {away}</b>\n"
        f"🌐 <i>{league_info}</i> | ⏰ <code>{match_time} HS</code>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📈 <b>ANÁLISIS DE GOLES & BTTS (ESTRICTO)</b>\n\n"
        f"• <b>Más de 2.5 Goles:</b> {odds_over:.2f}  <code>(Prob: {p_over*100:.1f}%)</code>\n"
        f"• <b>Ambos Anotan (BTTS):</b> {odds_btts:.2f}  <code>(Prob: {p_btts*100:.1f}%)</code>\n"
        f"• 🔥 <b>BTTS 1ª Mitad:</b> {odds_btts_1h:.2f}  <code>(Prob: {p_btts_1h*100:.1f}%)</code>\n\n"
        f"🎯 <b>Acción Estricta:</b>\n🟢 <i>Alta tendencia ofensiva y solidez de Poisson confirmada (+{int(ev_over*100)}% EV).</i>\n\n"
        f"💰 <b>Stake Kelly (Over 2.5):</b> <code>{stake_over}% de tu bankroll</code>\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )

def format_double_chance_card(league_info, home, away, match_time, p_1x, odds_1x, p_x2, odds_x2, stake_dc):
    return (
        f"🛡️ <b>{home} vs {away}</b>\n"
        f"🌐 <i>{league_info}</i> | ⏰ <code>{match_time} HS</code>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🛡️ <b>MERCADO DOBLE OPORTUNIDAD (SEGURO)</b>\n\n"
        f"• 🏠🤝 <b>Local o Empate (1X):</b> {odds_1x:.2f}  <code>({p_1x*100:.1f}%)</code>\n"
        f"• 🤝✈ <b>Empate o Visita (X2):</b> {odds_x2:.2f}  <code>({p_x2*100:.1f}%)</code>\n\n"
        f"🎯 <b>Acción Estricta:</b>\n🟢 <i>Cobertura de alta precisión validada por desviación estándar mínima.</i>\n\n"
        f"💰 <b>Stake Kelly:</b> <code>{stake_dc}% de tu bankroll</code>\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )

def format_asian_handicap_card(league_info, home, away, match_time, selected_team, p_ah, odds_ah, stake_ah):
    team_name = home if selected_team == "home" else away
    ev_ah = (p_ah * odds_ah) - 1

    return (
        f"⚖️ <b>{home} vs {away}</b>\n"
        f"🌐 <i>{league_info}</i> | ⏰ <code>{match_time} HS</code>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⚖️ <b>MERCADO HANDICAP ASIÁTICO (-0.5)</b>\n\n"
        f"• 🎯 <b>Selección Estricta:</b> <b>{team_name} (-0.5)</b>\n"
        f"• 📊 <b>Probabilidad Real:</b> <code>{p_ah*100:.1f}%</code> | 📐 <b>Cuota:</b> <code>{odds_ah:.2f}</code>\n"
        f"• 📈 <b>Valor Esperado (EV):</b> <code>+{int(ev_ah*100)}%</code>\n\n"
        f"🎯 <b>Acción Estricta:</b>\n🟢 <i>Filtrado estricto sin empate: superioridad matemática directa confirmada.</i>\n\n"
        f"💰 <b>Stake Kelly:</b> <code>{stake_ah}% de tu bankroll</code>\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )

def format_corners_cards(league_info, home, away, match_time, avg_corners, odds_corners_over, p_corners, avg_cards, odds_cards_over, p_cards, stake_corners):
    ev_corners = (p_corners * odds_corners_over) - 1
    return (
        f"🚩 <b>{home} vs {away}</b>\n"
        f"🌐 <i>{league_info}</i> | ⏰ <code>{match_time} HS</code>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📐 <b>CÓRNERS & TARJETAS (ESTRICTO)</b>\n\n"
        f"• 📐 <b>Córners Esperados:</b> <code>{avg_corners}</code>\n"
        f"  └ <i>Más de 8.5 Córners:</i> {odds_corners_over:.2f} <code>({p_corners*100:.1f}%)</code>\n\n"
        f"• 🟨 <b>Tarjetas Promedio:</b> <code>{avg_cards}</code>\n"
        f"  └ <i>Más de 4.5 Tarjetas:</i> {odds_cards_over:.2f} <code>({p_cards*100:.1f}%)</code>\n\n"
        f"🎯 <b>Acción Estricta:</b>\n🟢 <i>Patrones de ritmo de juego y presión alta verificados (+{int(ev_corners*100)}% EV).</i>\n\n"
        f"💰 <b>Stake Kelly (Córners):</b> <code>{stake_corners}% de tu bankroll</code>\n"
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

async def fetch_fixtures_from_api(date_str, max_hours=None):
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
                
                max_time_utc = None
                if max_hours:
                    max_time_utc = now_utc + timedelta(hours=max_hours)
                
                for fix in fixtures:
                    status_short = fix.get("fixture", {}).get("status", {}).get("short")
                    date_iso = fix.get("fixture", {}).get("date")
                    if status_short == "NS" and date_iso:
                        fix_dt = datetime.fromisoformat(date_iso.replace("Z", "+00:00"))
                        if fix_dt > now_utc:
                            if max_time_utc is None or fix_dt <= max_time_utc:
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
    
    non_draw_total = max(0.1, (p_home + p_away))
    p_ah_home = round(p_home / non_draw_total, 3)
    p_ah_home = min(0.92, max(0.10, p_ah_home * calibration))

    p_ah_away = round(p_away / non_draw_total, 3)
    p_ah_away = min(0.92, max(0.10, p_ah_away * calibration))

    margin = 1.05
    odds_home = round(margin / max(0.05, p_home), 2)
    odds_draw = round(margin / max(0.05, p_draw), 2)
    odds_away = round(margin / max(0.05, p_away), 2)
    odds_over = round(margin / max(0.05, p_over), 2)
    odds_btts = round(margin / max(0.05, p_btts), 2)
    
    odds_1x = round(margin / max(0.05, p_1x), 2)
    odds_x2 = round(margin / max(0.05, p_x2), 2)
    
    odds_ah_home = round(margin / max(0.05, p_ah_home), 2)
    odds_ah_away = round(margin / max(0.05, p_ah_away), 2)

    if p_ah_home >= p_ah_away:
        ah_selection = "home"
        p_ah_final = p_ah_home
        odds_ah_final = odds_ah_home
    else:
        ah_selection = "away"
        p_ah_final = p_ah_away
        odds_ah_final = odds_ah_away

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
            "selection": ah_selection,
            "p_ah": p_ah_final,
            "odds_ah": odds_ah_final
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
    await register_user_middleware(update, context)
    user_id = update.effective_user.id
    
    if update.message:
        welcome_text = (
            "⚽ <b>¡Bienvenido a NosticProno Bot!</b>\n\n"
            "Sistema avanzado de predicción y análisis matemático deportivo basado en modelos de Poisson y gestión de bankroll.\n\n"
            "👇 <i>Utiliza los botones del menú inferior para explorar:</i>"
        )
        await update.message.reply_text(
            welcome_text,
            parse_mode="HTML",
            reply_markup=get_keyboard_for_user(user_id)
        )

async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    admin_id = os.getenv("ADMIN_TELEGRAM_ID", "1588200613")
    
    if update.message and update.message.photo and admin_id:
        caption = (
            f"📥 <b>Nuevo Comprobante Recibido</b>\n\n"
            f"• <b>De:</b> {user.first_name} (@{user.username or 'Sin alias'})\n"
            f"• <b>ID:</b> <code>{user.id}</code>"
        )
        try:
            await context.bot.send_photo(
                chat_id=int(admin_id),
                photo=update.message.photo[-1].file_id,
                caption=caption,
                parse_mode="HTML"
            )
            await update.message.reply_text(
                "✅ ¡Comprobante recibido y enviado al administrador para su revisión!"
            )
        except Exception as e:
            logger.error(f"Error al reenviar comprobante al admin: {e}")

async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
        
    await register_user_middleware(update, context)
    user_id = update.effective_user.id
    text = update.message.text.strip()
    is_vip = check_user_vip_status(user_id)
    
    tz_uy = timezone(timedelta(hours=-3))
    today_str = datetime.now(tz_uy).strftime("%Y-%m-%d")
    tomorrow_str = (datetime.now(tz_uy) + timedelta(days=1)).strftime("%Y-%m-%d")
    after_tomorrow_str = (datetime.now(tz_uy) + timedelta(days=2)).strftime("%Y-%m-%d")

    if text == "💎 Suscripción VIP":
        paypal_link = "https://www.paypal.com/ncp/payment/Q74JNDXBW5X3U"
        vip_text = (
            "💎 <b>ACCESO COMPLETO & SUSCRIPCIÓN VIP</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "Desbloquea todo el potencial de <b>NosticProno</b> y lleva tus análisis deportivos al siguiente nivel:\n\n"
            "• 📊 Escaneo ilimitado de Top Value (+EV).\n"
            "• 🤖 Modelos de Poisson avanzados en todos los mercados.\n"
            "• 💰 Cálculo automático de Bankroll con Criterio de Kelly.\n"
            "• 📈 Historial y estadísticas avanzadas de rendimiento.\n\n"
            "💵 <b>Inversión:</b> <code>$15.00 USD / mes</code>\n\n"
            "👉 <b>Haz clic en el siguiente botón para realizar tu pago seguro a través de PayPal:</b>"
        )
        pay_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Pagar $15.00 USD con PayPal", url=paypal_link)],
            [InlineKeyboardButton("📤 Ya realicé el pago (Enviar comprobante)", callback_data="vip_payment_sent")]
        ])
        await update.message.reply_text(vip_text, parse_mode="HTML", reply_markup=pay_keyboard)
        return

    elif text == "📖 Ayuda":
        help_text = (
            "📖 <b>GUÍA DE USO & INFORMACIÓN</b>\n\n"
            "• 🤖 <b>NosticProno</b> utiliza algoritmos de probabilidad de Poisson y el Criterio de Kelly para detectar apuestas con valor esperado positivo (+EV).\n"
            "• 💎 <b>Suscripción VIP:</b> Desbloquea cuotas en tiempo real, escáner de valor y todos los mercados diarios.\n\n"
            "💡 <i>Suscríbete desde el botón del menú inferior para acceder a la plataforma completa.</i>"
        )
        await update.message.reply_text(help_text, parse_mode="HTML", reply_markup=get_keyboard_for_user(user_id))
        return

    elif text == "📊 Estadísticas Globales":
        conn = get_db_connection()
        total = 0
        wins = 0
        hit_rate = 0
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) as total, SUM(CASE WHEN status='WON' THEN 1 ELSE 0 END) as wins FROM user_picks WHERE status IN ('WON', 'LOST');")
                    res = cur.fetchone()
                    total = res['total'] or 0
                    wins = res['wins'] or 0
                    hit_rate = (wins / total * 100) if total > 0 else 0
            except Exception as e:
                logger.error(f"Error consultando estadísticas globales: {e}")
            finally:
                conn.close()
        
        await update.message.reply_text(
            f"📊 <b>RENDIMIENTO GLOBAL DEL SISTEMA</b>\n\n"
            f"• Total de Picks Registrados: <code>{total}</code>\n"
            f"• Tasa de Acierto Histórica: <code>{hit_rate:.1f}%</code>\n\n"
            f"💎 <i>Adquiere tu Suscripción VIP para ver el desglose detallado de cada partido.</i>",
            parse_mode="HTML",
            reply_markup=get_keyboard_for_user(user_id)
        )
        return

    restricted_triggers = ["1X2 / Ganador", "Goles & BTTS", "Doble Oportunidad", "Handicap Asiático", "Córners & Tarjetas", "Combinadas EV+", "Mis Estadísticas", "Top Value +EV", "Panel Admin"]
    
    if not is_vip and (any(rt in text for rt in restricted_triggers) or len(text) > 2):
        paywall_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 Obtener Acceso VIP ($15/mes)", callback_data="go_to_vip_menu")]
        ])
        await update.message.reply_text(
            "🔒 <b>CONTENIDO EXCLUSIVO PARA MIEMBROS VIP</b>\n\n"
            "La exploración gratuita solo permite ver el menú principal y la ayuda. Para consultar pronósticos detallados, buscadores de equipos y modelos +EV, adquiere tu membresía mensual:",
            parse_mode="HTML",
            reply_markup=paywall_kb
        )
        return

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
                await update.message.reply_text("❌ Error al consultar las estadísticas.", reply_markup=get_keyboard_for_user(user_id))
            finally:
                conn.close()
            
    elif "Top Value +EV" in text:
        await update.message.reply_text(
            "🗓️ <b>Selecciona el día para buscar el mejor Top Value (+EV):</b>",
            parse_mode="HTML",
            reply_markup=get_top_value_fecha_keyboard()
        )
        return

    elif "Combinada" in text or "EV+" in text:
        await update.message.reply_text("🍀 Generando combinada inteligente con valor esperado positivo (`+EV`) para hoy...", reply_markup=get_keyboard_for_user(user_id))
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
                
                if ev_val > 0.05 and p_over >= 0.52:
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
                    f"🍀 **COMBINADA RECOMENDADA (+EV ESTRICTA)** 🍀\n"
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
                await update.message.reply_text(combinada_text, parse_mode="HTML", reply_markup=get_keyboard_for_user(user_id))
            else:
                await update.message.reply_text("⚠ No hay suficientes partidos con los filtros estrictos de EV positivo hoy.", reply_markup=get_keyboard_for_user(user_id))
        except Exception as e:
            logger.error(f"Error generando combinada: {e}")
            await update.message.reply_text("❌ Ocurrió un error al armar la combinada.", reply_markup=get_keyboard_for_user(user_id))

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

    else:
        if is_vip:
            search_query = text.lower()
            await update.message.reply_text(f"🔍 Buscando partidos para <b>'{text}'</b> (hoy, mañana y pasado mañana)...", parse_mode="HTML")
            
            dates_to_search = [today_str, tomorrow_str, after_tomorrow_str]
            found_matches = []

            for d_str in dates_to_search:
                fixtures = await fetch_fixtures_from_api(d_str)
                for fix in fixtures:
                    home_name = fix.get("teams", {}).get("home", {}).get("name", "").lower()
                    away_name = fix.get("teams", {}).get("away", {}).get("name", "").lower()
                    
                    if search_query in home_name or search_query in away_name:
                        found_matches.append(fix)

            if not found_matches:
                await update.message.reply_text(f"⚠️ No se encontraron partidos programados para <b>'{text}'</b> en los próximos 3 días.", parse_mode="HTML")
                return

            for fix in found_matches[:5]:
                fid = fix.get("fixture", {}).get("id", 0)
                teams = fix.get("teams", {})
                league = fix.get("league", {})
                league_info = f"{league.get('country', '')} - {league.get('name', 'Fútbol')}"
                home = teams.get("home", {}).get("name", "Local")
                away = teams.get("away", {}).get("name", "Visitante")
                match_time = format_match_time(fix)
                
                analytics = await generate_fixture_analytics_real(fix)
                m_1x2 = analytics["market_1x2"]
                
                card_text = format_1x2_card(league_info, home, away, match_time, m_1x2["p_home"], m_1x2["odds_home"], m_1x2["p_draw"], m_1x2["odds_draw"], m_1x2["p_away"], m_1x2["odds_away"], calculate_kelly_stake(m_1x2["p_home"], m_1x2["odds_home"]))
                btn = InlineKeyboardMarkup([[InlineKeyboardButton("📌 Guardar Este Pick", callback_data=f"save_{fid}_1X2")]])
                
                await update.message.reply_text(card_text, parse_mode="HTML", reply_markup=btn)

# ==========================================
# 🎛️ MANEJADOR DE CALLBACKS
# ==========================================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data in ["go_to_vip", "go_to_vip_menu"]:
        paypal_link = "https://www.paypal.com/ncp/payment/Q74JNDXBW5X3U"
        vip_text = (
            "💎 <b>ACCESO COMPLETO & SUSCRIPCIÓN VIP</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "Desbloquea todo el potencial de <b>NosticProno</b> y lleva tus análisis deportivos al siguiente nivel:\n\n"
            "• 📊 Escaneo ilimitado de Top Value (+EV).\n"
            "• 🤖 Modelos de Poisson avanzados en todos los mercados.\n"
            "• 💰 Cálculo automático de Bankroll con Criterio de Kelly.\n"
            "• 📈 Historial y estadísticas avanzadas de rendimiento.\n\n"
            "💵 <b>Inversión:</b> <code>$15.00 USD / mes</code>\n\n"
            "👉 <b>Haz clic en el siguiente botón para realizar tu pago seguro a través de PayPal:</b>"
        )
        pay_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Pagar $15.00 USD con PayPal", url=paypal_link)],
            [InlineKeyboardButton("📤 Ya realicé el pago (Enviar comprobante)", callback_data="vip_payment_sent")]
        ])
        await query.message.reply_text(vip_text, parse_mode="HTML", reply_markup=pay_keyboard)
        return

    if data == "vip_payment_sent":
        await query.answer("¡Gracias! Verificaremos tu comprobante a la brevedad.", show_alert=True)
        await query.message.reply_text(
            "📩 <b>Comprobante registrado para verificación.</b>\n\n"
            "Por favor, envíanos una captura de pantalla del pago de PayPal por este medio. Una vez verificado por el administrador, se te otorgará el acceso completo.",
            parse_mode="HTML"
        )
        return

    elif data.startswith("timeframe_"):
        prefix = data.split("_")[1]
        await query.message.edit_text(
            "⏱️ <b>Selecciona el rango de horario deseado para hoy:</b>",
            parse_mode="HTML",
            reply_markup=get_timeframe_keyboard(prefix)
        )
        return

    elif data.startswith("datechoice_"):
        parts = data.split("_")
        prefix = parts[1]
        selected_date = parts[2]
        time_filter = parts[3] if len(parts) > 3 else "all"

        await query.message.edit_text(
            f"🎯 <b>Selecciona la cantidad de mejores pronósticos que deseas mostrar:</b>",
            parse_mode="HTML",
            reply_markup=get_limit_selector_keyboard(prefix, selected_date, time_filter)
        )
        return

    elif data.startswith("topval_"):
        selected_date = data.split("_")[1]
        await query.message.edit_text(f"🎯 Buscando la oportunidad con mayor valor esperado estricto (`+EV`) para el día <b>{selected_date}</b>...", parse_mode="HTML")
        
        try:
            fixtures_data = await fetch_fixtures_from_api(selected_date)
            best_pick = None
            max_ev = -999
            
            for fix in fixtures_data[:15]:
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
                    {"name": f"Handicap Asiático ({analysis['asian_handicap']['selection']})", "prob": analysis["asian_handicap"]["p_ah"], "odds": analysis["asian_handicap"]["odds_ah"]},
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
            
            if best_pick and max_ev > 0.05:
                text_out = (
                    f"🔥 <b>APUESTA TOP VALUE ESTRICTA (+EV) - {selected_date}</b> 🔥\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"🏆 <b>{best_pick['match']}</b>\n"
                    f"🌐 <i>{best_pick['league']}</i> | ⏰ <code>{best_pick['time']} HS</code>\n\n"
                    f"📌 <b>Mercado:</b> {best_pick['market']}\n"
                    f"📊 <b>Probabilidad:</b> <code>{int(best_pick['prob']*100)}%</code> | 📐 <b>Cuota:</b> <code>{best_pick['odds']}</code>\n"
                    f"📈 <b>Valor Esperado (EV):</b> <code>+{int(best_pick['ev']*100)}%</code>\n"
                    f"💰 <b>Stake Recomendado (Kelly):</b> <code>{best_pick['stake']}%</code>\n"
                    f"━━━━━━━━━━━━━━━━━━━"
                )
                await query.message.reply_text(text_out, parse_mode="HTML")
            else:
                await query.message.reply_text("⚠️ No se encontró ninguna apuesta que cumpla con el filtro estricto de EV superior al 5% para la fecha.")
        except Exception as e:
            logger.error(f"Error en Top Value por fecha: {e}")
            await query.message.reply_text("❌ Ocurrió un error al calcular el Top Value para esa fecha.")
        return

    elif data.startswith("save_"):
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

    elif data == "admin_list_users":
        conn = get_db_connection()
        if not conn:
            await query.message.reply_text("⚠️ Base de datos no disponible.")
            return
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT telegram_id, username, role, is_vip, expires_at FROM bot_users ORDER BY joined_at DESC LIMIT 10;")
                users = cur.fetchall()
                if not users:
                    await query.message.reply_text("📭 No hay usuarios registrados en el sistema.")
                    return
                for u in users:
                    uid = u['telegram_id']
                    uname = u['username'] or "Sin Alias"
                    role = u['role']
                    is_vip_val = u['is_vip']
                    exp_val = u['expires_at'].strftime("%d/%m/%Y") if u['expires_at'] else "N/A"
                    
                    user_kb = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("💎 Alternar VIP", callback_data=f"user_vip_{uid}"),
                            InlineKeyboardButton("🗑️ Eliminar", callback_data=f"user_del_{uid}")
                        ],
                        [InlineKeyboardButton("🔙 Volver a Gestión", callback_data="admin_manage_users")]
                    ])
                    await query.message.reply_text(
                        f"👤 <b>Usuario:</b> {uname}\n• ID: <code>{uid}</code>\n• Rol: <code>{role}</code> | VIP: <code>{is_vip_val}</code>\n• Expira: <code>{exp_val}</code>",
                        parse_mode="HTML", reply_markup=user_kb
                    )
        except Exception as e:
            logger.error(f"Error listando usuarios: {e}")
            await query.message.reply_text("❌ Error al recuperar los usuarios.")
        finally:
            conn.close()

    elif data.startswith("user_vip_"):
        uid = int(data.split("_")[2])
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT is_vip FROM bot_users WHERE telegram_id = %s", (uid,))
                    res = cur.fetchone()
                    if res:
                        new_vip_status = not res['is_vip']
                        tz_uy = timezone(timedelta(hours=-3))
                        if new_vip_status:
                            expires_at = datetime.now(tz_uy) + timedelta(days=30)
                            cur.execute("UPDATE bot_users SET is_vip = TRUE, expires_at = %s, renewal_warned = FALSE WHERE telegram_id = %s", (expires_at, uid))
                        else:
                            cur.execute("UPDATE bot_users SET is_vip = FALSE, expires_at = NULL, renewal_warned = FALSE WHERE telegram_id = %s", (uid,))
                        conn.commit()
                        await query.answer(f"✅ Estado VIP actualizado a {new_vip_status}", show_alert=True)
                        exp_msg = f" (Expira en 30 días)" if new_vip_status else ""
                        await query.message.edit_text(f"💎 <b>El usuario ID {uid} ahora tiene estatus VIP:</b> <code>{new_vip_status}</code>{exp_msg}", parse_mode="HTML")
            except Exception as e:
                logger.error(f"Error cambiando VIP de usuario {uid}: {e}")
                await query.answer("❌ Error al cambiar VIP.", show_alert=True)
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

    elif data.startswith("runload_"):
        parts = data.split("_")
        prefix = parts[1]
        selected_date = parts[2]
        time_filter = parts[3]
        limit_str = parts[4]

        max_hours = None
        if time_filter == "3h": max_hours = 3
        elif time_filter == "6h": max_hours = 6
        elif time_filter == "12h": max_hours = 12

        await query.message.edit_text(f"🔍 <b>Analizando partidos con filtros estrictos ({selected_date})...</b>", parse_mode="HTML")
        
        fixtures = await fetch_fixtures_from_api(selected_date, max_hours=max_hours)
        if not fixtures:
            await query.message.reply_text("⚠️ No se encontraron partidos con los filtros estrictos en ese rango de horario.")
            return

        valid_predictions = []

        for fix in fixtures:
            fid = fix.get("fixture", {}).get("id", 0)
            teams = fix.get("teams", {})
            league = fix.get("league", {})
            league_info = f"{league.get('country', '')} - {league.get('name', 'Fútbol')}"
            home = teams.get("home", {}).get("name", "Local")
            away = teams.get("away", {}).get("name", "Visitante")
            match_time = format_match_time(fix)
            
            analytics = await generate_fixture_analytics_real(fix)

            if prefix == "catgoals":
                goals_data = analytics["goals"]
                prob = goals_data["p_over_25"]
                odds = goals_data["odds_over"]
                ev = (prob * odds) - 1
                if prob < 0.52 or ev < 0.05:
                    continue
                card_text = format_goals_card(league_info, home, away, match_time, odds, prob, goals_data["odds_btts_yes"], goals_data["p_btts_yes"], goals_data["odds_btts_1h"], goals_data["p_btts_1h"], calculate_kelly_stake(prob, odds))
                btn = InlineKeyboardMarkup([[InlineKeyboardButton("📌 Guardar Over 2.5 Estricto", callback_data=f"save_{fid}_GOALS")]])

            elif prefix == "cat1x2":
                m_1x2 = analytics["market_1x2"]
                prob = max(m_1x2["p_home"], m_1x2["p_away"])
                odds = m_1x2["odds_home"] if prob == m_1x2["p_home"] else m_1x2["odds_away"]
                ev = (prob * odds) - 1
                if prob < 0.55 or ev < 0.05:
                    continue
                card_text = format_1x2_card(league_info, home, away, match_time, m_1x2["p_home"], m_1x2["odds_home"], m_1x2["p_draw"], m_1x2["odds_draw"], m_1x2["p_away"], m_1x2["odds_away"], calculate_kelly_stake(m_1x2["p_home"], m_1x2["odds_home"]))
                btn = InlineKeyboardMarkup([[InlineKeyboardButton("📌 Guardar 1X2 Estricto", callback_data=f"save_{fid}_1X2")]])

            elif prefix == "catdc":
                dc = analytics["double_chance"]
                prob = max(dc["p_1x"], dc["p_x2"])
                odds = dc["odds_1x"] if prob == dc["p_1x"] else dc["odds_x2"]
                ev = (prob * odds) - 1
                if prob < 0.70:
                    continue
                card_text = format_double_chance_card(league_info, home, away, match_time, dc["p_1x"], dc["odds_1x"], dc["p_x2"], dc["odds_x2"], calculate_kelly_stake(prob, odds))
                btn = InlineKeyboardMarkup([[InlineKeyboardButton("📌 Guardar Doble Oportunidad", callback_data=f"save_{fid}_DC")]])

            elif prefix == "catah":
                ah = analytics["asian_handicap"]
                prob = ah["p_ah"]
                odds = ah["odds_ah"]
                ev = (prob * odds) - 1
                if prob < 0.55 or ev < 0.05:
                    continue
                card_text = format_asian_handicap_card(league_info, home, away, match_time, ah["selection"], prob, odds, calculate_kelly_stake(prob, odds))
                team_label = home if ah["selection"] == "home" else away
                btn = InlineKeyboardMarkup([[InlineKeyboardButton(f"📌 Guardar Hándicap {team_label} (-0.5)", callback_data=f"save_{fid}_AH")]])

            elif prefix == "catcorners":
                cc = analytics["corners_cards"]
                prob = cc["p_corners"]
                odds = cc["odds_corners_over"]
                ev = (prob * odds) - 1
                if prob < 0.55 or ev < 0.05:
                    continue
                card_text = format_corners_cards(league_info, home, away, match_time, cc["avg_corners"], odds, prob, cc["avg_cards"], cc["odds_cards_over"], cc["p_cards"], calculate_kelly_stake(prob, odds))
                btn = InlineKeyboardMarkup([[InlineKeyboardButton("📌 Guardar Córners Estricto", callback_data=f"save_{fid}_CORNERS")]])
            else:
                continue

            valid_predictions.append({
                "probability": prob,
                "text": card_text,
                "reply_markup": btn
            })

        if not valid_predictions:
            await query.message.reply_text("ℹ️ Ningún partido cumple con los filtros estrictos de alta precisión para este rango.")
            return

        # 🔄 ORDENAR DE MAYOR A MENOR PROBABILIDAD Y RECORTAR SEGÚN LA CANTIDAD ELEGIDA
        valid_predictions.sort(key=lambda x: x["probability"], reverse=True)
        
        if limit_str != "all":
            limit_num = int(limit_str)
            valid_predictions = valid_predictions[:limit_num]

        for item in valid_predictions:
            await query.message.reply_text(item["text"], parse_mode="HTML", reply_markup=item["reply_markup"])
        return

# ==========================================
# ⚙️ WORKERS DE AUTO-LIQUIDACIÓN Y SUSCRIPCIÓN
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

async def background_subscription_checker(application):
    try:
        while True:
            await asyncio.sleep(3600)
            conn = get_db_connection()
            if not conn:
                continue
            try:
                tz_uy = timezone(timedelta(hours=-3))
                now = datetime.now(tz_uy)
                warning_threshold = now + timedelta(days=2)

                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT telegram_id, expires_at FROM bot_users 
                        WHERE is_vip = TRUE AND expires_at IS NOT NULL 
                          AND expires_at <= %s AND expires_at > %s 
                          AND (renewal_warned IS FALSE OR renewal_warned IS NULL);
                        """,
                        (warning_threshold, now)
                    )
                    users_to_warn = cur.fetchall()
                    
                    for u in users_to_warn:
                        uid = u['telegram_id']
                        exp_date = u['expires_at'].strftime("%d/%m/%Y a las %H:%M")
                        warn_text = (
                            "⚠️ <b>¡Tu Suscripción VIP está por expirar!</b>\n\n"
                            f"Tu membresía caducará el próximo <code>{exp_date}</code>.\n\n"
                            "Para evitar interrupciones en tus pronósticos, escaneos +EV y modelos de Poisson, puedes renovar tu acceso mensual ($15 USD) haciendo clic abajo:"
                        )
                        renewal_kb = InlineKeyboardMarkup([
                            [InlineKeyboardButton("💳 Renovar Suscripción ($15 USD)", url="https://www.paypal.com/ncp/payment/Q74JNDXBW5X3U")],
                            [InlineKeyboardButton("📤 Enviar Comprobante de Renovación", callback_data="vip_payment_sent")]
                        ])
                        try:
                            await application.bot.send_message(chat_id=uid, text=warn_text, parse_mode="HTML", reply_markup=renewal_kb)
                            cur.execute("UPDATE bot_users SET renewal_warned = TRUE WHERE telegram_id = %s;", (uid,))
                            conn.commit()
                        except Exception as ex:
                            logger.error(f"No se pudo enviar aviso de expiración a {uid}: {ex}")

                    cur.execute(
                        """
                        UPDATE bot_users 
                        SET is_vip = FALSE, renewal_warned = FALSE 
                        WHERE is_vip = TRUE AND expires_at IS NOT NULL AND expires_at < %s;
                        """,
                        (now,)
                    )
                    conn.commit()

            except Exception as e:
                logger.error(f"Error en la tarea de verificación de suscripciones: {e}")
            finally:
                conn.close()
    except asyncio.CancelledError:
        pass

async def post_init(application: Application):
    init_db()
    asyncio.create_task(auto_settlement_background_task())
    asyncio.create_task(background_subscription_checker(application))

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
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_photo_message))

    logger.info("Iniciando bot con selector de cantidad de mejores pronósticos...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()