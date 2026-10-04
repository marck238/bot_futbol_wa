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

# Configuración de Entorno (Render / Base de Datos)
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
    """Crea una conexión a la base de datos PostgreSQL."""
    if not DATABASE_URL:
        return None
    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        return conn
    except Exception as e:
        logger.error(f"Error conectando a PostgreSQL: {e}")
        return None

def init_db():
    """Inicializa la tabla de picks para el sistema de aprendizaje y estadísticas."""
    conn = get_db_connection()
    if not conn:
        logger.warning("DATABASE_URL no configurada. El almacenamiento y aprendizaje persistente estarán desactivados.")
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
            conn.commit()
            logger.info("Base de datos inicializada correctamente.")
    except Exception as e:
        logger.error(f"Error al crear tablas: {e}")
    finally:
        conn.close()

# ==========================================
# ⌨️ TECLADO INFERIOR (REPLY KEYBOARD)
# ==========================================

def get_persistent_keyboard():
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
    if p_home >= 0.50 and odds_home >= 1.70:
        recommendation = f"✅ **Apostar por la Victoria Local ({home})**\n💡 <i>La probabilidad matemática supera el 50% con cuota de valor.</i>"
    elif p_away >= 0.40 and odds_away >= 2.00:
        recommendation = f"✅ **Apostar por la Victoria Visitante ({away})**\n💡 <i>Cuota atractiva para arriesgar al visitante.</i>"
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
    if p_over >= 0.60:
        recommendation = "✅ **Entrar a Más de 2.5 Goles**\n💡 <i>Alta tendencia estadística de partidos abiertos.</i>"
    elif p_btts >= 0.60:
        recommendation = "✅ **Entrar a Ambos Anotan (BTTS)**\n💡 <i>Ambos equipos muestran capacidad ofensiva constante.</i>"
    else:
        recommendation = "⚠️ **Mercado de Goles Reservado**\n💡 <i>Partido cerrado o de bajo margen previsible.</i>"

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

def format_corners_cards(league_info, home, away, match_time, avg_corners, odds_corners_over, p_corners, avg_cards, odds_cards_over, p_cards, stake_corners):
    if p_corners >= 0.65:
        recommendation = "✅ **Entrar a Más de 8.5 Córners**\n💡 <i>Estilos de juego con alta generación de saques de esquina.</i>"
    else:
        recommendation = "⚠️ **Baja intensidad estimada en córners**\n💡 <i>Es preferible buscar líneas más bajas o evitar.</i>"

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

# ==========================================
# 🧠 MOTOR ANALÍTICO Y APRENDIZAJE ESTADÍSTICO
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
                data = response.json().get("response", {})
                return data
    except Exception as e:
        logger.error(f"Error obteniendo estadísticas para el equipo {team_id}: {e}")
    return {}

async def generate_fixture_analytics_real(fix):
    calibration = get_learning_calibration_factor()
    
    teams = fix.get("teams", {})
    league = fix.get("league", {})
    
    home_team = teams.get("home", {})
    away_team = teams.get("away", {})
    
    home_id = home_team.get("id", 0)
    away_id = away_team.get("id", 0)
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

    margin = 1.05
    odds_home = round(margin / max(0.05, p_home), 2)
    odds_draw = round(margin / max(0.05, p_draw), 2)
    odds_away = round(margin / max(0.05, p_away), 2)
    odds_over = round(margin / max(0.05, p_over), 2)
    odds_btts = round(margin / max(0.05, p_btts), 2)

    # --- CÁLCULO DINÁMICO DE CÓRNERS & TARJETAS BASADO EN INTENSIDAD ---
    # Usamos la suma de lambdas ofensivas para estimar córners de forma variable por encuentro
    offensive_intensity = lambda_home + lambda_away
    avg_corners = round(8.0 + (offensive_intensity * 1.2), 1)
    
    # Probabilidad variable según los córners estimados
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
    text = update.message.text
    today_str = datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%d")
    tomorrow_str = (datetime.now(timezone(timedelta(hours=-3))) + timedelta(days=1)).strftime("%Y-%m-%d")
    
    if "Goles & BTTS" in text:
        keyboard = [
            [InlineKeyboardButton("📅 Partidos de Hoy", callback_data=f"loadfixtures_catgoals_{today_str}")],
            [InlineKeyboardButton("📅 Partidos de Mañana", callback_data=f"loadfixtures_catgoals_{tomorrow_str}")]
        ]
        await update.message.reply_text("🗓️ <b>Selecciona la fecha para Goles & BTTS:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    elif "1X2 / Ganador" in text:
        keyboard = [
            [InlineKeyboardButton("📅 Partidos de Hoy (1X2)", callback_data=f"loadfixtures_cat1x2_{today_str}")],
            [InlineKeyboardButton("📅 Partidos de Mañana (1X2)", callback_data=f"loadfixtures_cat1x2_{tomorrow_str}")]
        ]
        await update.message.reply_text("🗓️ <b>Selecciona la fecha para Mercado 1X2:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    elif "Córners & Tarjetas" in text:
        keyboard = [
            [InlineKeyboardButton("📅 Partidos de Hoy (Córners)", callback_data=f"loadfixtures_catcorners_{today_str}")],
            [InlineKeyboardButton("📅 Partidos de Mañana (Córners)", callback_data=f"loadfixtures_catcorners_{tomorrow_str}")]
        ]
        await update.message.reply_text("🗓 <b>Selecciona la fecha para Córners & Tarjetas:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    elif "Mis Estadísticas" in text:
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor() as cur:
                    # Estadísticas Generales
                    cur.execute("SELECT COUNT(*) as total, SUM(CASE WHEN status='WON' THEN 1 ELSE 0 END) as wins FROM user_picks WHERE status IN ('WON', 'LOST');")
                    res = cur.fetchone()
                    total = res['total'] or 0
                    wins = res['wins'] or 0
                    hit_rate = (wins / total * 100) if total > 0 else 0
                    factor = get_learning_calibration_factor()

                    # Desglose Avanzado por Mercado
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

                    await update.message.reply_text(
                        f"📊 <b>ESTADÍSTICAS & RENDIMIENTO AVANZADO</b>\n\n"
                        f"• Apuestas Totales Liquidadas: <code>{total}</code>\n"
                        f"• Aciertos Globales: <code>{wins} ({hit_rate:.1f}%)</code>\n"
                        f"• Factor Calibración IA: <code>{factor:.2f}x</code>\n\n"
                        f"📈 <b>Desglose por Mercado:</b>\n{breakdown_text}",
                        parse_mode="HTML", reply_markup=get_persistent_keyboard()
                    )
            except Exception as e:
                logger.error(f"Error generando estadísticas avanzadas: {e}")
                await update.message.reply_text("❌ Error al consultar las estadísticas.", reply_markup=get_persistent_keyboard())
            finally:
                conn.close()
        else:
            await update.message.reply_text("📊 Base de datos no conectada.", reply_markup=get_persistent_keyboard())
    elif "Combinadas EV+" in text:
        await update.message.reply_text("🍀 Buscando combinadas de valor...", reply_markup=get_persistent_keyboard())
    elif "Top Value +EV" in text:
        await update.message.reply_text("🎯 Picks Top Value del día:", reply_markup=get_persistent_keyboard())
    elif "Ayuda" in text:
        await update.message.reply_text("ℹ️ <b>Ayuda de NosticProno</b>\n\nBot con auto-aprendizaje y calibración por Poisson.", parse_mode="HTML", reply_markup=get_persistent_keyboard())
    elif "Panel Admin" in text:
        await update.message.reply_text("⚙ Panel de administración.", reply_markup=get_persistent_keyboard())

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data.startswith("savepick_"):
        parts = data.split("_")
        fid = int(parts[1])
        market = parts[2]
        odds = float(parts[3])
        stake = float(parts[4])
        
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO user_picks (fixture_id, market_type, odds, probability, stake) VALUES (%s, %s, %s, %s, %s)",
                        (fid, market, odds, 0.65, stake)
                    )
                    conn.commit()
                await query.answer("✅ ¡Pick guardado con éxito para seguimiento y auto-aprendizaje!", show_alert=True)
            except Exception as e:
                logger.error(f"Error guardando pick: {e}")
                await query.answer("❌ Error al guardar el pick.", show_alert=True)
            finally:
                conn.close()
        else:
            await query.answer("⚠ Base de datos no disponible.", show_alert=True)
        return

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
            
            # FILTRO DE VALOR: Solo mostrar si la cuota es >= 1.55 o probabilidad >= 55%
            if analytics["odds_over"] < 1.55:
                continue

            shown_count += 1
            card_text = format_goals_card(league_info, home, away, match_time, analytics["odds_over"], analytics["p_over_25"], analytics["odds_btts_yes"], analytics["p_btts_yes"], analytics["odds_btts_1h"], analytics["p_btts_1h"], calculate_kelly_stake(analytics["p_over_25"], analytics["odds_over"]))
            btn = InlineKeyboardMarkup([
                [InlineKeyboardButton("📌 Guardar Over 2.5", callback_data=f"savepick_{fid}_GOALS_OVER_{analytics['odds_over']}_{calculate_kelly_stake(analytics['p_over_25'], analytics['odds_over'])}")]
            ])
            await query.message.reply_text(card_text, parse_mode="HTML", reply_markup=btn)
        
        if shown_count == 0:
            await query.message.reply_text("ℹ️️ No se encontraron partidos que cumplan con el filtro estricto de cuotas mínimas (+EV) para esta fecha.")

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
            
            # FILTRO DE VALOR: Solo mostrar si alguna cuota principal es >= 1.60
            if m_1x2["odds_home"] < 1.60 and m_1x2["odds_away"] < 1.60:
                continue

            shown_count += 1
            card_text = format_1x2_card(league_info, home, away, match_time, m_1x2["p_home"], m_1x2["odds_home"], m_1x2["p_draw"], m_1x2["odds_draw"], m_1x2["p_away"], m_1x2["odds_away"], calculate_kelly_stake(m_1x2["p_home"], m_1x2["odds_home"]))
            btn = InlineKeyboardMarkup([
                [InlineKeyboardButton("📌 Guardar Victoria Local", callback_data=f"savepick_{fid}_1X2_HOME_{m_1x2['odds_home']}_{calculate_kelly_stake(m_1x2['p_home'], m_1x2['odds_home'])}")]
            ])
            await query.message.reply_text(card_text, parse_mode="HTML", reply_markup=btn)
        
        if shown_count == 0:
            await query.message.reply_text("ℹ️ No se encontraron partidos con cuotas de valor mínimo para este mercado en esta fecha.")

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
                [InlineKeyboardButton("📌 Guardar Córners Over", callback_data=f"savepick_{fid}_CORNERS_OVER_{cc['odds_corners_over']}_{calculate_kelly_stake(cc['p_corners'], cc['odds_corners_over'])}")]
            ])
            await query.message.reply_text(card_text, parse_mode="HTML", reply_markup=btn)
        
        if shown_count == 0:
            await query.message.reply_text("ℹ️ No se encontraron partidos con filtros óptimos para córners en esta fecha.")

# ==========================================
# ⚙️ WORKER INDEPENDIENTE DE AUTO-LIQUIDACIÓN
# ==========================================

async def auto_settlement_background_task():
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
                    conn.close()
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
                                    if "GOALS_OVER" in market and total_goals > 2.5:
                                        won = True
                                    elif "1X2_HOME" in market and goals_home > goals_away:
                                        won = True
                                    elif "CORNERS_OVER" in market:
                                        won = True
                                    
                                    new_status = 'WON' if won else 'LOST'
                                    cur.execute("UPDATE user_picks SET status = %s WHERE id = %s", (new_status, pick_id))
                                    conn.commit()
                                    logger.info(f"Pick ID {pick_id} liquidado automáticamente como: {new_status}")
        except Exception as e:
            logger.error(f"Error en bucle de auto-liquidación: {e}")
        finally:
            conn.close()

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

    logger.info("Iniciando bot con worker asíncrono y PostgreSQL...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()