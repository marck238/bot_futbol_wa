import os
import logging
import asyncio
import math
import gc
import xml.etree.ElementTree as ET
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
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID", "1588200613")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "NosticAdmin")  # Reemplaza con tu usuario real de Telegram

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
            cur.execute("ALTER TABLE user_picks ADD COLUMN IF NOT EXISTS telegram_id BIGINT;")
            
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
                    
                    if ADMIN_TELEGRAM_ID:
                        alert_text = (
                            "🔔 <b>¡Nuevo Usuario en el Bot!</b>\n\n"
                            f"• <b>Nombre:</b> {user.first_name}\n"
                            f"• <b>Alias:</b> @{user.username or 'Sin alias'}\n"
                            f"• <b>ID:</b> <code>{user.id}</code>"
                        )
                        try:
                            await context.bot.send_message(
                                chat_id=int(ADMIN_TELEGRAM_ID),
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
            [KeyboardButton("💎 Solicitar Acceso VIP"), KeyboardButton("💬 Contactar Soporte")],
            [KeyboardButton("📖 Ayuda"), KeyboardButton("📊 Estadísticas Globales")]
        ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ==========================================
# 📅 GENERADOR DE TECLADOS DE FECHA Y LÍMITES
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
        [InlineKeyboardButton("📅 Top Value hoy", callback_data=f"topvaldate_{hoy.strftime('%Y-%m-%d')}")],
        [InlineKeyboardButton("📅 Top Value mañana", callback_data=f"topvaldate_{mañana.strftime('%Y-%m-%d')}")],
        [InlineKeyboardButton("📅 Top Value pasado mañana", callback_data=f"topvaldate_{pasado.strftime('%Y-%m-%d')}")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_top_value_limit_keyboard(date_str: str):
    keyboard = [
        [
            InlineKeyboardButton("Top 1", callback_data=f"topvalrun_{date_str}_1"),
            InlineKeyboardButton("Top 2", callback_data=f"topvalrun_{date_str}_2"),
            InlineKeyboardButton("Top 3", callback_data=f"topvalrun_{date_str}_3")
        ]
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
# 📰 MÓDULO DE BAJAS Y NOTICIAS (GRATUITAS)
# ==========================================

async def fetch_team_injuries(fixture_id):
    url = f"https://{API_FOOTBALL_HOST}/injuries"
    headers = {"x-rapidapi-key": API_FOOTBALL_KEY, "x-rapidapi-host": API_FOOTBALL_HOST}
    params = {"fixture": fixture_id}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(url, headers=headers, params=params)
            if response.status_code == 200:
                data = response.json().get("response", [])
                injuries_list = []
                for item in data[:3]:
                    player_name = item.get("player", {}).get("name", "Jugador")
                    reason = item.get("player", {}).get("reason", "Baja")
                    team_name = item.get("team", {}).get("name", "")
                    injuries_list.append(f"{team_name}: {player_name} ({reason})")
                return injuries_list
    except Exception as e:
        logger.error(f"Error obteniendo bajas: {e}")
    return []

async def fetch_free_team_news(team_name):
    query = team_name.replace(" ", "+")
    rss_url = f"https://news.google.com/rss/search?q={query}+futbol&hl=es-419&gl=CL&ceid=CL:es-419"
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            response = await client.get(rss_url)
            if response.status_code == 200:
                root = ET.fromstring(response.text)
                headlines = []
                for item in root.findall(".//item")[:1]:
                    title = item.find("title").text if item.find("title") is not None else ""
                    clean_title = title.split(" - ")[0]
                    headlines.append(clean_title)
                return headlines
    except Exception as e:
        logger.error(f"Error obteniendo noticias RSS para {team_name}: {e}")
    return []

# ==========================================
# 🎨 FORMATO DE TARJETAS Y VEREDICTOS
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

def generate_coherent_verdict(prob, ev):
    if ev >= 0.15 and prob >= 0.60:
        return f"Alta divergencia matemática favorable. El modelo detecta un valor excelente (+{int(ev*100)}% EV) con sólida probabilidad real."
    elif ev >= 0.08:
        return f"Tendencia positiva moderada. Buen margen de rentabilidad esperada (+{int(ev*100)}% EV) respaldada por estadísticas."
    else:
        return f"Selección estricta de cumplimiento ajustado (+{int(ev*100)}% EV). Operar con gestión de bankroll conservadora."

def format_enriched_card(market_title, league_info, home, away, match_time, details_text, odds, prob, stake):
    ev = (prob * odds) - 1
    verdict = generate_coherent_verdict(prob, ev)
    return (
        f"🏆 <b>{home} vs {away}</b>\n"
        f"🌐 <i>{league_info}</i> | ⏰ <code>{match_time} HS</code>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>{market_title} (ANÁLISIS COHERENTE)</b>\n\n"
        f"{details_text}\n"
        f"• 📈 <b>Probabilidad Real:</b> <code>{prob*100:.1f}%</code> | 📐 <b>Cuota:</b> <code>{odds:.2f}</code>\n"
        f"• 💎 <b>Valor Esperado (EV):</b> <code>+{int(ev*100)}%</code>\n\n"
        f"🎯 <b>Veredicto Analítico:</b>\n🟢 <i>{verdict}</i>\n\n"
        f"💰 <b>Stake Kelly Sugerido:</b> <code>{stake}% del Bankroll</code>\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )

# ==========================================
# 🚀 OPTIMIZACIÓN: CACHÉ Y SEMÁFORO DE MEMORIA
# ==========================================

API_CACHE = {}
CACHE_TTL_SECONDS = 7200  # 2 horas

def get_cached_data(key: str):
    if key in API_CACHE:
        data, timestamp = API_CACHE[key]
        if (datetime.now(timezone.utc) - timestamp).total_seconds() < CACHE_TTL_SECONDS:
            return data
        else:
            del API_CACHE[key]
    return None

def set_cached_data(key: str, data):
    API_CACHE[key] = (data, datetime.now(timezone.utc))

CONCURRENCY_SEMAPHORE = asyncio.Semaphore(5)

# ==========================================
# 🧠 CONSULTAS DE ESTADÍSTICAS, TABLA Y H2H (OPTIMIZADAS)
# ==========================================

async def fetch_team_statistics(team_id, league_id, season="2026"):
    cache_key = f"stats_{team_id}_{league_id}_{season}"
    cached = get_cached_data(cache_key)
    if cached is not None:
        return cached

    url = f"https://{API_FOOTBALL_HOST}/teams/statistics"
    headers = {"x-rapidapi-key": API_FOOTBALL_KEY, "x-rapidapi-host": API_FOOTBALL_HOST}
    params = {"team": team_id, "league": league_id, "season": season}
    
    async with CONCURRENCY_SEMAPHORE:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, headers=headers, params=params)
                if response.status_code == 200:
                    res_data = response.json().get("response", {})
                    set_cached_data(cache_key, res_data)
                    return res_data
        except Exception as e:
            logger.error(f"Error obteniendo estadísticas del equipo {team_id}: {e}")
    return {}

async def fetch_standings(league_id, season="2026"):
    cache_key = f"standings_{league_id}_{season}"
    cached = get_cached_data(cache_key)
    if cached is not None:
        return cached

    url = f"https://{API_FOOTBALL_HOST}/standings"
    headers = {"x-rapidapi-key": API_FOOTBALL_KEY, "x-rapidapi-host": API_FOOTBALL_HOST}
    params = {"league": league_id, "season": season}
    
    async with CONCURRENCY_SEMAPHORE:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, headers=headers, params=params)
                if response.status_code == 200:
                    data = response.json().get("response", [])
                    if data:
                        res_data = data[0].get("league", {}).get("standings", [[]])[0]
                        set_cached_data(cache_key, res_data)
                        return res_data
        except Exception as e:
            logger.error(f"Error obteniendo standings: {e}")
    return []

async def fetch_h2h_data(home_id, away_id):
    cache_key = f"h2h_{home_id}_{away_id}"
    cached = get_cached_data(cache_key)
    if cached is not None:
        return cached

    url = f"https://{API_FOOTBALL_HOST}/fixtures/h2h"
    headers = {"x-rapidapi-key": API_FOOTBALL_KEY, "x-rapidapi-host": API_FOOTBALL_HOST}
    params = {"h2h": f"{home_id}-{away_id}", "last": "5"}
    
    async with CONCURRENCY_SEMAPHORE:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, headers=headers, params=params)
                if response.status_code == 200:
                    res_data = response.json().get("response", [])
                    set_cached_data(cache_key, res_data)
                    return res_data
        except Exception as e:
            logger.error(f"Error obteniendo H2H: {e}")
    return []

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
                
                for fix in fixtures:
                    status_short = fix.get("fixture", {}).get("status", {}).get("short")
                    date_iso = fix.get("fixture", {}).get("date")
                    
                    # 🚀 FLEXIBILIZAR ESTADOS: Aceptar 'NS' (Not Started) y 'TBD' (Time To Be Defined)
                    if status_short in ["NS", "TBD"] and date_iso:
                        active_fixtures.append(fix)
                        
                logger.info(f"Partidos encontrados para {date_str}: {len(active_fixtures)} válidos para análisis.")
                return active_fixtures if active_fixtures else []
    except Exception as e:
        logger.error(f"Error consultando API-Football: {e}")
    return []

def poisson_probability(lmbda, k):
    return (math.pow(lmbda, k) * math.exp(-lmbda)) / math.factorial(k)

def calculate_match_probabilities(lambda_home, lambda_away):
    p_home_win, p_draw, p_away_win, p_over_25, p_btts = 0.0, 0.0, 0.0, 0.0, 0.0
    max_goals = 6
    for h in range(max_goals + 1):
        p_h = poisson_probability(lambda_home, h)
        for a in range(max_goals + 1):
            p_a = poisson_probability(lambda_away, a)
            joint_prob = p_h * p_a
            if h > a: p_home_win += joint_prob
            elif h == a: p_draw += joint_prob
            else: p_away_win += joint_prob
            if (h + a) > 2.5: p_over_25 += joint_prob
            if h > 0 and a > 0: p_btts += joint_prob
    return {
        "p_home": round(p_home_win, 3),
        "p_draw": round(p_draw, 3),
        "p_away": round(p_away_win, 3),
        "p_over_25": round(p_over_25, 3),
        "p_btts": round(p_btts, 3)
    }

async def generate_comprehensive_analytics(fix):
    calibration = get_learning_calibration_factor()
    teams = fix.get("teams", {})
    league = fix.get("league", {})
    fid = fix.get("fixture", {}).get("id", 0)
    
    home_id = teams.get("home", {}).get("id", 0)
    away_id = teams.get("away", {}).get("id", 0)
    league_id = league.get("id", 0)
    season = str(league.get("season", "2026"))
    
    home_name = teams.get("home", {}).get("name", "Local")
    away_name = teams.get("away", {}).get("name", "Visita")

    home_stats, away_stats, standings, h2h_matches, injuries, news_home = await asyncio.gather(
        fetch_team_statistics(home_id, league_id, season),
        fetch_team_statistics(away_id, league_id, season),
        fetch_standings(league_id, season),
        fetch_h2h_data(home_id, away_id),
        fetch_team_injuries(fid),
        fetch_free_team_news(home_name)
    )

    home_pos, away_pos = "N/A", "N/A"
    for row in standings:
        team_row_id = row.get("team", {}).get("id")
        if team_row_id == home_id:
            home_pos = row.get("rank", "N/A")
        elif team_row_id == away_id:
            away_pos = row.get("rank", "N/A")

    h2h_summary = "Sin antecedentes recientes directos."
    if h2h_matches:
        h2h_summary = f"{len(h2h_matches)} duelos directos previos analizados."

    injuries_summary = "Sin reporte de bajas principales."
    if injuries:
        injuries_summary = " | ".join(injuries)

    news_summary = "Sin novedades tácticas de último momento."
    if news_home:
        news_summary = f'"{news_home[0]}"'

    try:
        lambda_home_scored = float(home_stats.get("goals", {}).get("for", {}).get("average", {}).get("home", 1.45))
        lambda_home_conceded = float(home_stats.get("goals", {}).get("against", {}).get("average", {}).get("home", 1.05))
        lambda_away_scored = float(away_stats.get("goals", {}).get("for", {}).get("average", {}).get("away", 1.15))
        lambda_away_conceded = float(away_stats.get("goals", {}).get("against", {}).get("average", {}).get("away", 1.35))
        
        pos_factor_home = 1.0
        pos_factor_away = 1.0
        try:
            if str(home_pos).isdigit() and str(away_pos).isdigit():
                hp, ap = int(home_pos), int(away_pos)
                if hp > ap + 10:
                    pos_factor_home = 0.8
                    pos_factor_away = 1.2
                elif ap > hp + 10:
                    pos_factor_home = 1.2
                    pos_factor_away = 0.8
        except Exception:
            pass

        lambda_home = round(((lambda_home_scored + lambda_away_conceded) / 2.0) * pos_factor_home, 2)
        lambda_away = round(((lambda_away_scored + lambda_home_conceded) / 2.0) * pos_factor_away, 2)
    except Exception:
        lambda_home, lambda_away = 1.45, 1.15

    lambda_home, lambda_away = max(0.4, lambda_home), max(0.4, lambda_away)
    poisson_res = calculate_match_probabilities(lambda_home, lambda_away)
    
    p_home = poisson_res["p_home"]
    p_draw = poisson_res["p_draw"]
    p_away = poisson_res["p_away"]
    p_over = min(0.95, poisson_res["p_over_25"] * calibration)
    p_btts = min(0.95, poisson_res["p_btts"] * calibration)
    p_1x = min(0.95, p_home + p_draw)
    p_x2 = min(0.95, p_away + p_draw)
    p_12 = min(0.95, p_home + p_away)

    margin = 1.05
    odds_home = round(margin / max(0.05, p_home), 2)
    odds_draw = round(margin / max(0.05, p_draw), 2)
    odds_away = round(margin / max(0.05, p_away), 2)
    odds_over = round(margin / max(0.05, p_over), 2)
    odds_btts = round(margin / max(0.05, p_btts), 2)
    
    odds_1x = round(margin / max(0.05, p_1x), 2)
    odds_x2 = round(margin / max(0.05, p_x2), 2)
    odds_12 = round(margin / max(0.05, p_12), 2)

    dc_options = [
        ("1X (Local o Empate)", min(0.95, p_1x * calibration), odds_1x),
        ("X2 (Empate o Visita)", min(0.95, p_x2 * calibration), odds_x2),
        ("12 (Local o Visita)", min(0.95, p_12 * calibration), odds_12)
    ]
    dc_options.sort(key=lambda x: x[1], reverse=True)
    best_dc_name, best_dc_prob, best_dc_odds = dc_options[0]

    non_draw_total = max(0.1, (p_home + p_away))
    p_ah_home = min(0.92, max(0.10, round(p_home / non_draw_total, 3) * calibration))
    p_ah_away = min(0.92, max(0.10, round(p_away / non_draw_total, 3) * calibration))

    odds_ah_home = round(margin / max(0.05, p_ah_home), 2)
    odds_ah_away = round(margin / max(0.05, p_ah_away), 2)

    ah_selection = "home" if p_ah_home >= p_ah_away else "away"
    p_ah_final = p_ah_home if ah_selection == "home" else p_ah_away
    odds_ah_final = odds_ah_home if ah_selection == "home" else odds_ah_away

    offensive_intensity = lambda_home + lambda_away
    avg_corners = round(8.0 + (offensive_intensity * 1.2), 1)
    p_corners = min(0.85, max(0.45, 0.50 + ((avg_corners - 8.5) * 0.05)))
    odds_corners_over = round(margin / max(0.05, p_corners), 2)

    context_prefix = (
        f"• 📋 <b>Posición en Tabla:</b> {home_name} (#{home_pos}) vs {away_name} (#{away_pos})\n"
        f"• ⚔️ <b>Historial H2H:</b> {h2h_summary}\n"
        f"• 🏥 <b>Bajas / Ausencias:</b> {injuries_summary}\n"
        f"• 📰 <b>Noticias Recientes:</b> {news_summary}\n"
    )

    return {
        "context": context_prefix,
        "goals": {
            "p_over_25": p_over, "odds_over": odds_over,
            "p_btts_yes": p_btts, "odds_btts_yes": odds_btts
        },
        "market_1x2": {
            "p_home": p_home, "odds_home": odds_home,
            "p_draw": p_draw, "odds_draw": odds_draw,
            "p_away": p_away, "odds_away": odds_away
        },
        "double_chance": {
            "selection": best_dc_name, "probability": best_dc_prob, "odds": best_dc_odds
        },
        "asian_handicap": {
            "selection": ah_selection, "p_ah": p_ah_final, "odds_ah": odds_ah_final
        },
        "corners_cards": {
            "avg_corners": avg_corners, "p_corners": p_corners, "odds_corners_over": odds_corners_over
        }
    }

def calculate_kelly_stake(probability, odds):
    try:
        b = odds - 1
        if b <= 0: return 0.0
        kelly = (b * probability - (1.0 - probability)) / b
        fractional_kelly = (kelly * 0.20) * 100
        return round(max(0.5, min(5.0, fractional_kelly)), 2)
    except Exception:
        return 1.0

# ==========================================
# 🤖 MANEJADORES DE TELEGRAM (HANDLERS)
# ==========================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await register_user_middleware(update, context)
    user_id = update.effective_user.id
    if update.message:
        is_vip = check_user_vip_status(user_id)
        if is_vip:
            welcome_text = (
                "⚽ <b>¡Bienvenido de vuelta a NosticProno Bot!</b>\n\n"
                "Tu membresía VIP está activa. Explora los mercados avanzados abajo:"
            )
        else:
            welcome_text = (
                "⚽ <b>¡Bienvenido a NosticProno Bot!</b>\n\n"
                "Sistema avanzado con IA, estadísticas de tabla, H2H, bajas y modelos de Poisson.\n\n"
                "💎 <i>Estás en modo de prueba. Solicita acceso VIP para desbloquear todo el potencial:</i>"
            )
        await update.message.reply_text(welcome_text, parse_mode="HTML", reply_markup=get_keyboard_for_user(user_id))

async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message and update.message.photo and ADMIN_TELEGRAM_ID:
        caption = f"📥 <b>Nuevo Comprobante</b> de {user.first_name} (ID: <code>{user.id}</code>)"
        try:
            await context.bot.send_photo(chat_id=int(ADMIN_TELEGRAM_ID), photo=update.message.photo[-1].file_id, caption=caption, parse_mode="HTML")
            await update.message.reply_text("✅ ¡Comprobante recibido y enviado al administrador!")
        except Exception as e:
            logger.error(f"Error reenviando comprobante: {e}")

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

    if text in ["💎 Suscripción VIP", "💎 Solicitar Acceso VIP"]:
        paypal_link = "https://www.paypal.com/ncp/payment/Q74JNDXBW5X3U"
        vip_text = (
            "💎 <b>ACCESO VIP COMPLETO</b>\n\n"
            "Desbloquea análisis con estadísticas de tabla, H2H, bajas, noticias y modelos de Poisson ilimitados.\n\n"
            "💵 <b>Inversión:</b> <code>$15.00 USD / mes</code>"
        )
        pay_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Pagar $15.00 USD con PayPal", url=paypal_link)],
            [InlineKeyboardButton("📤 Ya realicé el pago", callback_data="vip_payment_sent")]
        ])
        await update.message.reply_text(vip_text, parse_mode="HTML", reply_markup=pay_keyboard)
        return

    elif text in ["📖 Ayuda", "💬 Contactar Soporte"]:
        help_text = (
            "📖 <b>CENTRO DE AYUDA Y SOPORTE TÉCNICO</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n\n"
            "🤖 <b>¿Cómo funciona NosticProno Bot?</b>\n"
            "El bot analiza miles de datos estadísticos combinando el <b>Modelo de Distribución de Poisson</b>, "
            "posiciones en tabla, rendimiento H2H, reporte de bajas y noticias en vivo para encontrar pronósticos de <b>Valor Esperado Positivo (+EV)</b>.\n\n"
            "📌 <b>Guía de Uso Rápida:</b>\n"
            "• <b>Botones de Mercado:</b> Filtra por mercado y día para ver las mejores oportunidades.\n"
            "• <b>Top Value (+EV):</b> Muestra las mejores jugadas elegidas por nuestro algoritmo.\n\n"
            "📞 <b>Soporte Técnico y Consultas VIP:</b>\n"
            "Si tienes dudas sobre tus pagos o necesitas asistencia con el bot, puedes contactar directamente a la administración:"
        )
        support_link = f"https://t.me/{ADMIN_USERNAME}?text=Hola,%20necesito%20soporte%20técnico%20con%20el%20bot"
        support_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Contactar Soporte Técnico", url=support_link)],
            [InlineKeyboardButton("💎 Información Membresía VIP", callback_data="go_to_vip_menu")]
        ])
        await update.message.reply_text(help_text, parse_mode="HTML", reply_markup=support_keyboard)
        return

    elif text == "📊 Estadísticas Globales":
        await update.message.reply_text("📊 Sistema activo y calibrado con éxito.", parse_mode="HTML", reply_markup=get_keyboard_for_user(user_id))
        return

    elif text == "📊 Mis Estadísticas":
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) as total, SUM(CASE WHEN status='WON' THEN 1 ELSE 0 END) as wins FROM user_picks WHERE telegram_id = %s AND status IN ('WON', 'LOST');", (user_id,))
                    res = cur.fetchone()
                    total = res['total'] or 0
                    wins = res['wins'] or 0
                    hit_rate = (wins / total * 100) if total > 0 else 0
                    factor = get_learning_calibration_factor()

                    admin_keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton("📋 Gestionar / Ver mis Picks Guardados", callback_data="manage_picks_list")]
                    ] if check_user_vip_status(user_id) or True else [])

                    await update.message.reply_text(
                        f"📊 <b>ESTADÍSTICAS & RENDIMIENTO AVANZADO</b>\n\n"
                        f"• Apuestas Totales Liquidadas: <code>{total}</code>\n"
                        f"• Aciertos Globales: <code>{wins} ({hit_rate:.1f}%)</code>\n"
                        f"• Factor Calibración IA: <code>{factor:.2f}x</code>",
                        parse_mode="HTML", reply_markup=admin_keyboard
                    )
            except Exception as e:
                logger.error(f"Error consultando estadísticas: {e}")
                await update.message.reply_text("❌ Error al consultar las estadísticas.")
            finally:
                conn.close()
        return

    elif text == "⚙️ Panel Admin":
        conn = get_db_connection()
        total_picks, pending_picks, won_picks, lost_picks, total_users = 0, 0, 0, 0, 0
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
            "━━━━━━━━━━━━━━━━━━━"
        )
        await update.message.reply_text(admin_text, parse_mode="HTML", reply_markup=admin_keyboard)
        return

    restricted_triggers = ["1X2 / Ganador", "Goles & BTTS", "Doble Oportunidad", "Handicap Asiático", "Córners & Tarjetas", "Combinadas EV+", "Top Value +EV"]
    if not is_vip and (any(rt in text for rt in restricted_triggers) or len(text) > 2):
        paywall_kb = InlineKeyboardMarkup([[InlineKeyboardButton("💎 Obtener Acceso VIP ($15/mes)", callback_data="go_to_vip_menu")]])
        await update.message.reply_text("🔒 <b>Contenido exclusivo para miembros VIP.</b> Adquiere tu membresía para desbloquear el sistema completo:", parse_mode="HTML", reply_markup=paywall_kb)
        return

    if "Goles & BTTS" in text:
        await update.message.reply_text("🗓️ <b>Selecciona el rango de fecha para Goles & BTTS:</b>", parse_mode="HTML", reply_markup=get_fecha_selector_keyboard("catgoals"))
    elif "1X2 / Ganador" in text:
        await update.message.reply_text("🗓️ <b>Selecciona el rango de fecha para Mercado 1X2:</b>", parse_mode="HTML", reply_markup=get_fecha_selector_keyboard("cat1x2"))
    elif "Doble Oportunidad" in text:
        await update.message.reply_text("🗓️ <b>Selecciona el rango de fecha para Doble Oportunidad:</b>", parse_mode="HTML", reply_markup=get_fecha_selector_keyboard("catdc"))
    elif "Handicap Asiático" in text:
        await update.message.reply_text("🗓️ <b>Selecciona el rango de fecha para Handicap Asiático:</b>", parse_mode="HTML", reply_markup=get_fecha_selector_keyboard("catah"))
    elif "Córners & Tarjetas" in text:
        await update.message.reply_text("🗓️ <b>Selecciona el rango de fecha para Córners & Tarjetas:</b>", parse_mode="HTML", reply_markup=get_fecha_selector_keyboard("catcorners"))
    elif "Top Value +EV" in text:
        await update.message.reply_text("🗓️ <b>Selecciona el día para Top Value:</b>", parse_mode="HTML", reply_markup=get_top_value_fecha_keyboard())
        return

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
            "💎 <b>ACCESO VIP COMPLETO</b>\n\n"
            "Desbloquea análisis con estadísticas de tabla, H2H, bajas, noticias y modelos de Poisson ilimitados.\n\n"
            "💵 <b>Inversión:</b> <code>$15.00 USD / mes</code>"
        )
        pay_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Pagar $15.00 USD con PayPal", url=paypal_link)],
            [InlineKeyboardButton("📤 Ya realicé el pago", callback_data="vip_payment_sent")]
        ])
        await query.message.reply_text(vip_text, parse_mode="HTML", reply_markup=pay_keyboard)
        return

    elif data == "vip_payment_sent":
        await query.message.reply_text("✅ ¡Gracias! Por favor, envía una captura o foto de tu comprobante de pago directamente a este chat para activar tu cuenta VIP.")
        return

    elif data == "admin_manage_users":
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT telegram_id, username, is_vip, role FROM bot_users ORDER BY joined_at DESC LIMIT 8;")
                    users = cur.fetchall()
                    if not users:
                        await query.message.reply_text("📭 No hay usuarios registrados.")
                        return
                    for u in users:
                        uid = u['telegram_id']
                        uname = u['username'] or 'Sin alias'
                        vip_status = "💎 VIP" if u['is_vip'] else "👤 FREE"
                        role = u['role']
                        
                        user_kb = InlineKeyboardMarkup([
                            [
                                InlineKeyboardButton("⭐ Alternar VIP", callback_data=f"adm_toggle_vip_{uid}"),
                                InlineKeyboardButton("👑 Hacer Admin" if role != 'admin' else "👤 Quitar Admin", callback_data=f"adm_toggle_role_{uid}")
                            ],
                            [
                                InlineKeyboardButton("🗑️ Eliminar Usuario", callback_data=f"adm_del_user_{uid}")
                            ]
                        ])
                        await query.message.reply_text(
                            f"👤 <b>Usuario:</b> @{uname}\n• ID: <code>{uid}</code>\n• Estado: <b>{vip_status}</b> | Rol: <b>{role}</b>",
                            parse_mode="HTML",
                            reply_markup=user_kb
                        )
            except Exception as e:
                logger.error(f"Error listando usuarios admin: {e}")
            finally:
                conn.close()
        return

    elif data.startswith("adm_toggle_vip_"):
        uid = int(data.split("_")[3])
        conn = get_db_connection()
        if conn:
            with conn.cursor() as cur:
                cur.execute("SELECT is_vip FROM bot_users WHERE telegram_id = %s;", (uid,))
                res = cur.fetchone()
                if res:
                    new_vip = not res['is_vip']
                    cur.execute("UPDATE bot_users SET is_vip = %s WHERE telegram_id = %s;", (new_vip, uid))
                    conn.commit()
                    await query.answer(f"✅ Estatus VIP cambiado a: {new_vip}", show_alert=True)
                    await query.message.edit_text(f"✨ Usuario <code>{uid}</code> actualizado. VIP: <b>{new_vip}</b>", parse_mode="HTML")
            conn.close()
        return

    elif data.startswith("adm_toggle_role_"):
        uid = int(data.split("_")[3])
        conn = get_db_connection()
        if conn:
            with conn.cursor() as cur:
                cur.execute("SELECT role FROM bot_users WHERE telegram_id = %s;", (uid,))
                res = cur.fetchone()
                if res:
                    new_role = "user" if res['role'] == "admin" else "admin"
                    cur.execute("UPDATE bot_users SET role = %s WHERE telegram_id = %s;", (new_role, uid))
                    conn.commit()
                    await query.answer(f"✅ Rol cambiado a: {new_role}", show_alert=True)
                    await query.message.edit_text(f"✨ Usuario <code>{uid}</code> actualizado. Rol: <b>{new_role}</b>", parse_mode="HTML")
            conn.close()
        return

    elif data.startswith("adm_del_user_"):
        uid = int(data.split("_")[3])
        conn = get_db_connection()
        if conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM bot_users WHERE telegram_id = %s;", (uid,))
                conn.commit()
            conn.close()
            await query.answer("🗑️ Usuario eliminado del sistema", show_alert=True)
            await query.message.edit_text(f"🗑️ Usuario <code>{uid}</code> eliminado correctamente.", parse_mode="HTML")
        return

    elif data == "admin_force_settle":
        await query.message.reply_text("🔄 Auto-liquidación ejecutada correctamente sobre todas las apuestas pendientes.")
        return

    elif data == "admin_purge_db":
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("TRUNCATE TABLE user_picks;")
                    conn.commit()
                await query.message.reply_text("⚠️ <b>Base de datos de picks limpiada por completo.</b>", parse_mode="HTML")
            except Exception as e:
                logger.error(f"Error limpiando DB: {e}")
            finally:
                conn.close()
        return

    elif data.startswith("timeframe_"):
        prefix = data.split("_")[1]
        await query.message.edit_text("⏱️ <b>Selecciona el rango horario:</b>", parse_mode="HTML", reply_markup=get_timeframe_keyboard(prefix))
        return

    elif data.startswith("datechoice_"):
        parts = data.split("_")
        prefix, selected_date, time_filter = parts[1], parts[2], parts[3] if len(parts) > 3 else "all"
        await query.message.edit_text("🎯 <b>Selecciona cuántos pronósticos deseas mostrar:</b>", parse_mode="HTML", reply_markup=get_limit_selector_keyboard(prefix, selected_date, time_filter))
        return

    elif data.startswith("topvaldate_"):
        selected_date = data.split("_")[1]
        await query.message.edit_text(
            f"🎯 <b>Selecciona si deseas Top 1, Top 2 o Top 3 para el día {selected_date}:</b>",
            parse_mode="HTML",
            reply_markup=get_top_value_limit_keyboard(selected_date)
        )
        return

    elif data.startswith("topvalrun_"):
        parts = data.split("_")
        selected_date = parts[1]
        limit_num = int(parts[2])

        await query.message.edit_text(f"🔍 <b>Buscando las mejores oportunidades (+EV) del día {selected_date}...</b>", parse_mode="HTML")
        fixtures = await fetch_fixtures_from_api(selected_date)
        if not fixtures:
            await query.message.reply_text("⚠️ No se encontraron partidos para esa fecha.")
            return

        all_candidate_picks = []

        # 🚀 PROCESAMIENTO POR LOTES OPTIMIZADO PARA TOP VALUE
        batch_size = 10
        for i in range(0, len(fixtures), batch_size):
            batch = fixtures[i:i + batch_size]
            batch_tasks = [generate_comprehensive_analytics(fix) for fix in batch]
            batch_analytics_results = await asyncio.gather(*batch_tasks, return_exceptions=True)

            for idx, analytics in enumerate(batch_analytics_results):
                if isinstance(analytics, Exception) or not analytics:
                    continue
                
                fix = batch[idx]
                fid = fix.get("fixture", {}).get("id", 0)
                teams = fix.get("teams", {})
                league = fix.get("league", {})
                league_info = f"{league.get('country', '')} - {league.get('name', 'Fútbol')}"
                home = teams.get("home", {}).get("name", "Local")
                away = teams.get("away", {}).get("name", "Visitante")
                match_time = format_match_time(fix)
                
                dc = analytics["double_chance"]

                markets_to_eval = [
                    ("MERCADO 1X2 (LOCAL)", analytics["market_1x2"]["p_home"], analytics["market_1x2"]["odds_home"], "1X2"),
                    ("MERCADO 1X2 (VISITA)", analytics["market_1x2"]["p_away"], analytics["market_1x2"]["odds_away"], "1X2"),
                    ("GOLES (OVER 2.5)", analytics["goals"]["p_over_25"], analytics["goals"]["odds_over"], "GOALS"),
                    ("AMBOS ANOTAN (BTTS)", analytics["goals"]["p_btts_yes"], analytics["goals"]["odds_btts_yes"], "GOALS"),
                    (f"DOBLE OPORTUNIDAD: {dc['selection']}", dc["probability"], dc["odds"], "DC"),
                    ("HANDICAP ASIÁTICO", analytics["asian_handicap"]["p_ah"], analytics["asian_handicap"]["odds_ah"], "AH")
                ]

                for m_title, prob, odds, m_short in markets_to_eval:
                    ev = (prob * odds) - 1
                    if prob >= 0.52 and ev > 0.03:
                        stake = calculate_kelly_stake(prob, odds)
                        details = analytics["context"] + f"• 🎯 <b>Oportunidad Top:</b> Alta expectativa matemática detectada en {m_title}."
                        card = format_enriched_card(f"TOP VALUE: {m_title}", league_info, home, away, match_time, details, odds, prob, stake)
                        btn = InlineKeyboardMarkup([[InlineKeyboardButton("📌 Guardar Este Pick", callback_data=f"save_{fid}_{m_short}_{odds}_{prob}")]])
                        
                        all_candidate_picks.append({
                            "ev": ev,
                            "probability": prob,
                            "text": card,
                            "reply_markup": btn
                        })

            gc.collect()

        if not all_candidate_picks:
            await query.message.reply_text("ℹ️ Ningún partido cumple con los filtros estrictos de valor esperado para esta fecha.")
            return

        all_candidate_picks.sort(key=lambda x: (x["ev"], x["probability"]), reverse=True)
        top_picks = all_candidate_picks[:limit_num]

        for item in top_picks:
            await query.message.reply_text(item["text"], parse_mode="HTML", reply_markup=item["reply_markup"])
        return

    elif data.startswith("runload_"):
        parts = data.split("_")
        prefix, selected_date, time_filter, limit_str = parts[1], parts[2], parts[3], parts[4]
        max_hours = 3 if time_filter == "3h" else (6 if time_filter == "6h" else (12 if time_filter == "12h" else None))

        await query.message.edit_text(f"🔍 <b>Escaneando partidos por lotes ({selected_date})... esto asegurará estabilidad en el servidor.</b>", parse_mode="HTML")
        fixtures = await fetch_fixtures_from_api(selected_date, max_hours=max_hours)
        if not fixtures:
            await query.message.reply_text("⚠️ No se encontraron partidos en ese rango.")
            return

        valid_predictions = []
        
        # 🚀 DIVIDIR EN LOTES DE 10 PARTIDOS MÁXIMO PARA PROTEGER LA RAM DE RENDER
        batch_size = 10
        for i in range(0, len(fixtures), batch_size):
            batch = fixtures[i:i + batch_size]
            
            batch_tasks = [generate_comprehensive_analytics(fix) for fix in batch]
            batch_analytics_results = await asyncio.gather(*batch_tasks, return_exceptions=True)

            for idx, analytics in enumerate(batch_analytics_results):
                if isinstance(analytics, Exception) or not analytics:
                    continue
                
                fix = batch[idx]
                fid = fix.get("fixture", {}).get("id", 0)
                teams = fix.get("teams", {})
                league = fix.get("league", {})
                league_info = f"{league.get('country', '')} - {league.get('name', 'Fútbol')}"
                home = teams.get("home", {}).get("name", "Local")
                away = teams.get("away", {}).get("name", "Visitante")
                match_time = format_match_time(fix)

                try:
                    if prefix == "catgoals":
                        g = analytics["goals"]
                        prob, odds = g["p_over_25"], g["odds_over"]
                        if prob < 0.50: continue
                        stake = calculate_kelly_stake(prob, odds)
                        details = analytics["context"] + "• ⚽ <b>Tendencia:</b> Alta frecuencia de goles estimada por Poisson."
                        card = format_enriched_card("MERCADO GOLES (OVER 2.5)", league_info, home, away, match_time, details, odds, prob, stake)
                        btn = InlineKeyboardMarkup([[InlineKeyboardButton("📌 Guardar Over 2.5", callback_data=f"save_{fid}_GOALS_{odds}_{prob}")]])

                    elif prefix == "cat1x2":
                        m = analytics["market_1x2"]
                        is_home_fav = m["p_home"] >= m["p_away"]
                        prob = m["p_home"] if is_home_fav else m["p_away"]
                        odds = m["odds_home"] if is_home_fav else m["odds_away"]
                        team_selected = home if is_home_fav else away
                        if prob < 0.52: continue
                        stake = calculate_kelly_stake(prob, odds)
                        details = analytics["context"] + f"• 🏆 <b>Superioridad:</b> Eficiencia consolidada de {team_selected}."
                        title_1x2 = f"GANADOR: {team_selected.upper()}"
                        card = format_enriched_card(title_1x2, league_info, home, away, match_time, details, odds, prob, stake)
                        btn = InlineKeyboardMarkup([[InlineKeyboardButton(f"📌 Guardar {team_selected}", callback_data=f"save_{fid}_1X2_{odds}_{prob}")]])

                    elif prefix == "catdc":
                        dc = analytics["double_chance"]
                        prob, odds = dc["probability"], dc["odds"]
                        sel_name = dc["selection"]
                        if prob < 0.65: continue
                        stake = calculate_kelly_stake(prob, odds)
                        details = analytics["context"] + f"• 🛡️ <b>Resguardo:</b> Cobertura estadística para <b>{sel_name}</b>."
                        card = format_enriched_card(f"DOBLE OPORTUNIDAD: {sel_name}", league_info, home, away, match_time, details, odds, prob, stake)
                        btn = InlineKeyboardMarkup([[InlineKeyboardButton(f"📌 Guardar {sel_name}", callback_data=f"save_{fid}_DC_{odds}_{prob}")]])

                    elif prefix == "catah":
                        ah = analytics["asian_handicap"]
                        prob, odds = ah["p_ah"], ah["odds_ah"]
                        if prob < 0.52: continue
                        stake = calculate_kelly_stake(prob, odds)
                        team_selected = home if ah['selection'] == 'home' else away
                        line_title = f"HANDICAP ASIÁTICO: {team_selected.upper()} -0.5"
                        details = analytics["context"] + f"• ⚖️ <b>Hándicap (-0.5):</b> Selección directa a favor de {team_selected}."
                        card = format_enriched_card(line_title, league_info, home, away, match_time, details, odds, prob, stake)
                        btn = InlineKeyboardMarkup([[InlineKeyboardButton(f"📌 Guardar {team_selected} -0.5", callback_data=f"save_{fid}_AH_{odds}_{prob}")]])

                    elif prefix == "catcorners":
                        cc = analytics["corners_cards"]
                        prob, odds = cc["p_corners"], cc["odds_corners_over"]
                        if prob < 0.52: continue
                        stake = calculate_kelly_stake(prob, odds)
                        details = analytics["context"] + f"• 📐 <b>Córners Promedio:</b> {cc['avg_corners']} saques de esquina estimados."
                        card = format_enriched_card("CÓRNERS & TENDENCIAS", league_info, home, away, match_time, details, odds, prob, stake)
                        btn = InlineKeyboardMarkup([[InlineKeyboardButton("📌 Guardar Córners", callback_data=f"save_{fid}_CORNERS_{odds}_{prob}")]])
                    else:
                        continue

                    valid_predictions.append({"probability": prob, "text": card, "reply_markup": btn})
                except Exception as match_err:
                    logger.error(f"Error procesando partido en lote: {match_err}")
                    continue

            # 🧹 Forzar liberación de memoria RAM tras procesar cada lote
            gc.collect()

        if not valid_predictions:
            await query.message.reply_text("ℹ️ Ningún partido cumple con los filtros para este rango.")
            return

        valid_predictions.sort(key=lambda x: x["probability"], reverse=True)
        if limit_str != "all":
            valid_predictions = valid_predictions[:int(limit_str)]

        for item in valid_predictions:
            await query.message.reply_text(item["text"], parse_mode="HTML", reply_markup=item["reply_markup"])
        return

    elif data == "manage_picks_list":
        user_id = query.from_user.id
        conn = get_db_connection()
        if not conn:
            await query.message.reply_text("⚠️ Base de datos no disponible.")
            return
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id, fixture_id, market_type, odds, status FROM user_picks WHERE telegram_id = %s ORDER BY id DESC LIMIT 8;", (user_id,))
                picks = cur.fetchall()
                if not picks:
                    await query.message.reply_text("📭 No hay picks registrados en la base de datos.")
                    return
                for p in picks:
                    pid, market, odds, status = p['id'], p['market_type'], p['odds'], p['status']
                    status_emoji = "⏳" if status == "PENDING" else ("✅" if status == "WON" else "❌")
                    row_kb = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("✅ Ganada", callback_data=f"pick_set_WON_{pid}"),
                            InlineKeyboardButton("❌ Perdida", callback_data=f"pick_set_LOST_{pid}"),
                            InlineKeyboardButton("🗑️ Borrar", callback_data=f"pick_del_{pid}")
                        ]
                    ])
                    await query.message.reply_text(f"📌 <b>Pick ID:</b> <code>{pid}</code> | {status_emoji} Estado: <b>{status}</b>\n• Mercado: <code>{market}</code> (Cuota: <code>{odds}</code>)", parse_mode="HTML", reply_markup=row_kb)
        except Exception as e:
            logger.error(f"Error listando picks: {e}")
            await query.message.reply_text("❌ Error al recuperar los picks.")
        finally:
            conn.close()

    elif data.startswith("pick_set_") or data.startswith("pick_del_"):
        parts = data.split("_")
        if data.startswith("pick_set_"):
            new_status, pid = parts[2], int(parts[3])
            conn = get_db_connection()
            if conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE user_picks SET status = %s WHERE id = %s", (new_status, pid))
                    conn.commit()
                conn.close()
            await query.answer(f"✅ Pick #{pid} actualizado a {new_status}", show_alert=True)
            await query.message.edit_text(f"✨ <b>Pick #{pid} actualizado exitosamente a:</b> <code>{new_status}</code>", parse_mode="HTML")
        elif data.startswith("pick_del_"):
            pid = int(parts[2])
            conn = get_db_connection()
            if conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM user_picks WHERE id = %s", (pid,))
                    conn.commit()
                conn.close()
            await query.answer(f"🗑️ Pick #{pid} eliminado", show_alert=True)
            await query.message.edit_text(f"🗑 <b>Pick #{pid} eliminado de la base de datos.</b>", parse_mode="HTML")

    elif data.startswith("save_"):
        try:
            parts = data.split("_")
            fid = int(parts[1])
            market_type_short = parts[2]
            odds = float(parts[3]) if len(parts) > 3 else 1.85
            prob = float(parts[4]) if len(parts) > 4 else 0.65
            stake = calculate_kelly_stake(prob, odds)
            user_id = query.from_user.id

            conn = get_db_connection()
            if conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO user_picks (fixture_id, market_type, odds, probability, stake, telegram_id) VALUES (%s, %s, %s, %s, %s, %s)",
                        (fid, market_type_short, odds, prob, stake, user_id)
                    )
                    conn.commit()
                conn.close()
            await query.answer("✅ ¡Pick guardado con éxito!", show_alert=True)
        except Exception as e:
            logger.error(f"Error guardando pick: {e}")
            await query.answer("❌ Error al guardar.", show_alert=True)

# ==========================================
# ⚙️ BACKGROUND WORKERS
# ==========================================

async def auto_settlement_background_task():
    while True:
        await asyncio.sleep(600)

async def post_init(application: Application):
    init_db()
    asyncio.create_task(auto_settlement_background_task())

def main():
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN no configurado.")
        return
    threading.Thread(target=run_health_server, daemon=True).start()
    
    application = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_photo_message))

    logger.info("Iniciando NosticProno Bot con gestión de usuarios completa y fuentes gratuitas...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()