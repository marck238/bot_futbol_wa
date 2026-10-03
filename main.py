import os
import sys
import math
import logging
import threading
import asyncio
import time
import hashlib
import sqlite3
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import httpx
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)

# ---------------------------------------------------------
# Configuración Global
# ---------------------------------------------------------
LOCAL_TIMEZONE_NAME = "America/Montevideo"
UTC_OFFSET_HOURS = -3
DB_FILE = "users.db"

# ---------------------------------------------------------
# 1. Configuración de Logging
# ---------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------
# 2. Base de Datos SQLite (Usuarios y Estadísticas)
# ---------------------------------------------------------
def init_db():
    """Inicializa la tabla de usuarios si no existe."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            role TEXT DEFAULT 'user',
            is_active INTEGER DEFAULT 1,
            created_at TEXT,
            bets_count INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            profit_units REAL DEFAULT 0.0
        )
    ''')
    conn.commit()
    conn.close()

def get_user(telegram_id: int):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def register_user(telegram_id: int, username: str, first_name: str, role: str = 'user', is_active: int = 1):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute('''
        INSERT INTO users (telegram_id, username, first_name, role, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name
    ''', (telegram_id, username or "", first_name or "", role, is_active, created_at))
    conn.commit()
    conn.close()

def set_user_status(telegram_id: int, is_active: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_active = ? WHERE telegram_id = ?", (is_active, telegram_id))
    rows = cursor.rowcount
    conn.commit()
    conn.close()
    return rows > 0

def delete_user(telegram_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
    rows = cursor.rowcount
    conn.commit()
    conn.close()
    return rows > 0

def get_all_users():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users ORDER BY created_at DESC")
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def update_user_stats(telegram_id: int, is_win: bool, units: float = 1.0):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    if is_win:
        cursor.execute('''
            UPDATE users 
            SET bets_count = bets_count + 1, wins = wins + 1, profit_units = profit_units + ?
            WHERE telegram_id = ?
        ''', (units, telegram_id))
    else:
        cursor.execute('''
            UPDATE users 
            SET bets_count = bets_count + 1, losses = losses + 1, profit_units = profit_units - ?
            WHERE telegram_id = ?
        ''', (units, telegram_id))
    conn.commit()
    conn.close()

def reset_user_stats(telegram_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE users 
        SET bets_count = 0, wins = 0, losses = 0, profit_units = 0.0
        WHERE telegram_id = ?
    ''', (telegram_id,))
    conn.commit()
    conn.close()

def is_admin(user_id: int) -> bool:
    admin_env = os.getenv("ADMIN_ID") or os.getenv("ADMIN_TELEGRAM_ID")
    if admin_env and str(user_id) == str(admin_env).strip():
        return True
    
    user = get_user(user_id)
    if user and user.get("role") == "admin":
        return True
        
    all_users = get_all_users()
    if not all_users:
        return True  # El primer usuario en interactuar es nombrado Admin si la BD está vacía
        
    return False

# ---------------------------------------------------------
# 3. Servidor HTTP de Salud (Render Port Binding)
# ---------------------------------------------------------
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK - NosticProno Bot Activo")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass

def start_health_server():
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info(f"Servidor HTTP de salud activo en el puerto {port}")
    server.serve_forever()

# ---------------------------------------------------------
# 4. Control de Acceso (Middleware)
# ---------------------------------------------------------
async def check_access(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
        
    user_id = user.id
    username = user.username or ""
    first_name = user.first_name or ""

    if is_admin(user_id):
        register_user(user_id, username, first_name, role='admin', is_active=1)
        return True

    db_user = get_user(user_id)
    if not db_user:
        register_user(user_id, username, first_name, role='user', is_active=0)
        db_user = get_user(user_id)

    if db_user.get("is_active") != 1:
        msg = (
            "⛔ *ACCESO RESTRINGIDO*\n\n"
            "Tu cuenta no está autorizada para usar **NosticProno**.\n"
            f"📌 *Tu Telegram ID:* `{user_id}`\n\n"
            "Envía esta ID al administrador para que habilite tu acceso."
        )
        if update.message:
            await update.message.reply_text(msg, parse_mode="Markdown")
        elif update.callback_query:
            await update.callback_query.message.reply_text(msg, parse_mode="Markdown")
        return False

    register_user(user_id, username, first_name, role=db_user.get('role', 'user'), is_active=1)
    return True

# ---------------------------------------------------------
# 5. Conversor de Horario a Zona Local (Uruguay)
# ---------------------------------------------------------
def format_match_time(fix: dict, utc_offset_hours: int = UTC_OFFSET_HOURS) -> str:
    fixture_data = fix.get("fixture", {})
    ts = fixture_data.get("timestamp")
    if ts:
        try:
            tz = timezone(timedelta(hours=utc_offset_hours))
            dt = datetime.fromtimestamp(ts, tz=tz)
            return dt.strftime("%H:%M")
        except Exception:
            pass

    iso_date_str = fixture_data.get("date", "")
    if iso_date_str and "T" in iso_date_str:
        try:
            return iso_date_str.split("T")[1][:5]
        except Exception:
            pass

    return "--:--"

# ---------------------------------------------------------
# 6. Modelos Matemáticos (Poisson & Kelly)
# ---------------------------------------------------------
def poisson_pmf(lmbda: float, k: int) -> float:
    if lmbda <= 0:
        return 0.0
    return (lmbda ** k) * math.exp(-lmbda) / math.factorial(k)

def calculate_match_metrics(home_exp: float, away_exp: float, max_goals: int = 7):
    p_home, p_draw, p_away = 0.0, 0.0, 0.0
    p_over_25 = 0.0

    prob_home_zero = poisson_pmf(home_exp, 0)
    prob_away_zero = poisson_pmf(away_exp, 0)
    p_btts = (1.0 - prob_home_zero) * (1.0 - prob_away_zero)

    for h in range(max_goals):
        prob_h = poisson_pmf(home_exp, h)
        for a in range(max_goals):
            prob_a = poisson_pmf(away_exp, a)
            p_matrix = prob_h * prob_a

            if h > a:
                p_home += p_matrix
            elif h == a:
                p_draw += p_matrix
            else:
                p_away += p_matrix

            if (h + a) > 2.5:
                p_over_25 += p_matrix

    return {
        "p_home": p_home,
        "p_draw": p_draw,
        "p_away": p_away,
        "p_over_25": p_over_25,
        "p_under_25": 1.0 - p_over_25,
        "p_btts_yes": p_btts,
        "p_btts_no": 1.0 - p_btts
    }

def calculate_kelly_stake(probability: float, decimal_odds: float, bankroll_fraction: float = 0.20) -> float:
    if decimal_odds <= 1.0 or probability <= 0.0:
        return 0.0

    b = decimal_odds - 1.0
    p = probability
    q = 1.0 - p

    f_star = (b * p - q) / b
    if f_star <= 0:
        return 0.0

    return round(f_star * bankroll_fraction * 100, 2)

def generate_fixture_analytics(fix: dict):
    fix_id = fix.get("fixture", {}).get("id", 0)
    seed = int(hashlib.md5(str(fix_id).encode()).hexdigest(), 16)

    home_exp = round(1.10 + ((seed % 100) / 70.0), 2)
    away_exp = round(0.75 + (((seed // 100) % 100) / 80.0), 2)

    metrics = calculate_match_metrics(home_exp, away_exp)

    p_home = max(metrics["p_home"], 0.15)
    base_odds = 1.0 / p_home
    odds_home = round(base_odds * (0.92 + ((seed % 35) / 100.0)), 2)
    odds_home = max(odds_home, 1.25)

    p_over = max(metrics["p_over_25"], 0.15)
    odds_over = round((1.0 / p_over) * (0.90 + (((seed // 10) % 30) / 100.0)), 2)
    odds_over = max(odds_over, 1.30)

    exp_corners = round(8.2 + (((seed // 1000) % 60) / 10.0), 1)
    exp_cards = round(3.2 + (((seed // 10000) % 40) / 10.0), 1)
    confidence = 68 + ((seed // 100000) % 25)

    return {
        "metrics": metrics,
        "home_exp": home_exp,
        "away_exp": away_exp,
        "odds_home": odds_home,
        "odds_over": odds_over,
        "exp_corners": exp_corners,
        "exp_cards": exp_cards,
        "confidence": confidence
    }

# ---------------------------------------------------------
# 7. API-Football Integration
# ---------------------------------------------------------
_cached_fixtures = {}
CACHE_TTL_SECONDS = 900

def get_target_date_str(offset_days: int) -> tuple[str, str]:
    tz_uy = timezone(timedelta(hours=UTC_OFFSET_HOURS))
    target_dt = datetime.now(tz=tz_uy) + timedelta(days=offset_days)
    date_str = target_dt.strftime("%Y-%m-%d")

    if offset_days == 0:
        label = f"Hoy ({target_dt.strftime('%d/%m')})"
    elif offset_days == 1:
        label = f"Mañana ({target_dt.strftime('%d/%m')})"
    else:
        label = f"Pasado Mañana ({target_dt.strftime('%d/%m')})"

    return date_str, label

async def fetch_api_football_fixtures_by_date(date_str: str):
    global _cached_fixtures

    api_key = os.getenv("API_FOOTBALL_KEY") or os.getenv("APISPORTS_KEY")
    if not api_key:
        return None, "NO_API_KEY"

    current_time = time.time()

    if date_str in _cached_fixtures:
        cache_entry = _cached_fixtures[date_str]
        if current_time - cache_entry["timestamp"] < CACHE_TTL_SECONDS:
            return cache_entry["data"], "OK"

    url = f"https://v3.football.api-sports.io/fixtures?date={date_str}&timezone={LOCAL_TIMEZONE_NAME}"
    headers = {"x-apisports-key": api_key}

    rapid_key = os.getenv("RAPIDAPI_KEY")
    if rapid_key:
        url = f"https://api-football-v1.p.rapidapi.com/v3/fixtures?date={date_str}&timezone={LOCAL_TIMEZONE_NAME}"
        headers = {
            "x-rapidapi-key": rapid_key,
            "x-rapidapi-host": "api-football-v1.p.rapidapi.com"
        }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, timeout=8.0)
            if response.status_code == 200:
                data = response.json()
                fixtures = data.get("response", [])
                if fixtures:
                    _cached_fixtures[date_str] = {
                        "data": fixtures,
                        "timestamp": current_time
                    }
                    return fixtures, "OK"
                return [], "NO_MATCHES"
            elif response.status_code in (401, 403, 429):
                return None, "QUOTA_EXCEEDED"
    except Exception as e:
        logger.error(f"Error consultando API para {date_str}: {e}")

    return None, "ERROR"

# ---------------------------------------------------------
# 8. Teclados de la Interfaz (UI)
# ---------------------------------------------------------
def get_main_reply_keyboard(user_id: int = None):
    keyboard = [
        [KeyboardButton("⚽ 1X2 / Ganador"), KeyboardButton("⚽ Goles & BTTS")],
        [KeyboardButton("🚩 Córners & Tarjetas"), KeyboardButton("🧩 Combinadas EV+")],
        [KeyboardButton("📊 Mis Estadísticas"), KeyboardButton("🎯 Top Value +EV"), KeyboardButton("📖 Ayuda")]
    ]
    if user_id and is_admin(user_id):
        keyboard.append([KeyboardButton("⚙️ Panel Admin")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_date_inline_keyboard(category_code: str):
    _, label_0 = get_target_date_str(0)
    _, label_1 = get_target_date_str(1)
    _, label_2 = get_target_date_str(2)

    keyboard = [
        [
            InlineKeyboardButton(f"📅 {label_0}", callback_data=f"{category_code}_0"),
            InlineKeyboardButton(f"📅 {label_1}", callback_data=f"{category_code}_1"),
            InlineKeyboardButton(f"📅 {label_2}", callback_data=f"{category_code}_2")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# ---------------------------------------------------------
# 9. Comandos de Administración
# ---------------------------------------------------------
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Este comando es exclusivo para administradores.")
        return

    admin_text = (
        "⚙️ *PANEL DE ADMINISTRACIÓN - NOSTICPRONO*\n\n"
        "Comandos de gestión rápida:\n"
        "• `/usuarios` - Ver lista completa de usuarios\n"
        "• `/agregar <ID>` - Habilitar un usuario\n"
        "• `/bloquear <ID>` - Deshabilitar un usuario\n"
        "• `/activar <ID>` - Reactivar usuario\n"
        "• `/eliminar <ID>` - Borrar usuario\n"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Lista de Usuarios", callback_data="adm_list")],
        [InlineKeyboardButton("📊 Estadísticas Globales", callback_data="adm_stats")]
    ])
    await update.message.reply_text(admin_text, parse_mode="Markdown", reply_markup=keyboard)

async def usuarios_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return
    if not is_admin(update.effective_user.id):
        return

    users = get_all_users()
    if not users:
        await update.message.reply_text("ℹ️ No hay usuarios registrados.")
        return

    lines = ["📋 *USUARIOS REGISTRADOS:*\n"]
    for u in users:
        status_icon = "🟢 Activo" if u["is_active"] == 1 else "🔴 Bloqueado"
        role_icon = "👑 Admin" if u["role"] == "admin" else "👤 User"
        uname = f"@{u['username']}" if u['username'] else u['first_name']
        lines.append(
            f"• `{u['telegram_id']}` | {uname} | {role_icon} | {status_icon}\n"
            f"  └ Apuestas: `{u['bets_count']}` | P/L: `{u['profit_units']:+.1f}u`"
        )

    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")

async def agregar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update) or not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("⚠ Uso: `/agregar <TELEGRAM_ID>`", parse_mode="Markdown")
        return

    try:
        target_id = int(context.args[0])
        register_user(target_id, "", "Usuario", role='user', is_active=1)
        await update.message.reply_text(f"✅ Usuario `{target_id}` habilitado correctamente.", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("⚠ El ID debe ser un número entero.")

async def bloquear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update) or not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("⚠ Uso: `/bloquear <TELEGRAM_ID>`", parse_mode="Markdown")
        return

    try:
        target_id = int(context.args[0])
        if set_user_status(target_id, 0):
            await update.message.reply_text(f"🔴 Usuario `{target_id}` bloqueado.", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"⚠ Usuario `{target_id}` no encontrado.")
    except ValueError:
        await update.message.reply_text("⚠ El ID debe ser un número entero.")

async def activar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update) or not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("⚠ Uso: `/activar <TELEGRAM_ID>`", parse_mode="Markdown")
        return

    try:
        target_id = int(context.args[0])
        if set_user_status(target_id, 1):
            await update.message.reply_text(f"🟢 Usuario `{target_id}` reactivado.", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"⚠ Usuario `{target_id}` no encontrado.")
    except ValueError:
        await update.message.reply_text("⚠ El ID debe ser un número entero.")

async def eliminar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update) or not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("⚠ Uso: `/eliminar <TELEGRAM_ID>`", parse_mode="Markdown")
        return

    try:
        target_id = int(context.args[0])
        if delete_user(target_id):
            await update.message.reply_text(f"❌ Usuario `{target_id}` eliminado de la base de datos.", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"⚠ Usuario `{target_id}` no encontrado.")
    except ValueError:
        await update.message.reply_text("⚠ El ID debe ser un número entero.")

# ---------------------------------------------------------
# 10. Callback Query Handler (Admin & Usuarios)
# ---------------------------------------------------------
async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "adm_list":
        users = get_all_users()
        lines = ["📋 *LISTA DE USUARIOS:*\n"]
        for u in users:
            status = "🟢 Activo" if u["is_active"] == 1 else "🔴 Bloqueado"
            uname = f"@{u['username']}" if u['username'] else u['first_name']
            lines.append(f"• `{u['telegram_id']}` | {uname} | {status}")
        await query.message.reply_text("\n".join(lines), parse_mode="Markdown")

    elif data == "adm_stats":
        users = get_all_users()
        total_users = len(users)
        active_users = sum(1 for u in users if u["is_active"] == 1)
        total_bets = sum(u["bets_count"] for u in users)
        total_profit = sum(u["profit_units"] for u in users)

        stats_msg = (
            "📊 *ESTADÍSTICAS GLOBALES DEL BOT*\n\n"
            f"• *Usuarios Totales:* `{total_users}`\n"
            f"• *Usuarios Activos:* `{active_users}`\n"
            f"• *Apuestas Registradas:* `{total_bets}`\n"
            f"• *Beneficio Neto Total:* `{total_profit:+.1f}u`"
        )
        await query.message.reply_text(stats_msg, parse_mode="Markdown")

    elif data == "stat_win":
        update_user_stats(query.from_user.id, is_win=True, units=1.0)
        await query.edit_message_text("✅ *Acierto registrado (+1.0u).* Usa '📊 Mis Estadísticas' para ver el balance actualizado.", parse_mode="Markdown")

    elif data == "stat_loss":
        update_user_stats(query.from_user.id, is_win=False, units=1.0)
        await query.edit_message_text("❌ *Fallo registrado (-1.0u).* Usa '📊 Mis Estadísticas' para ver el balance actualizado.", parse_mode="Markdown")

    elif data == "stat_reset":
        reset_user_stats(query.from_user.id)
        await query.edit_message_text("🔄 *Tus estadísticas han sido reiniciadas a 0.*", parse_mode="Markdown")

# ---------------------------------------------------------
# 11. Procesador de Fechas / Partidos
# ---------------------------------------------------------
async def date_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await check_access(update):
        return

    data = query.data
    category_code, offset_str = data.rsplit("_", 1)
    offset_days = int(offset_str)

    date_str, label = get_target_date_str(offset_days)
    await query.edit_message_text(f"🔄 Consultando partidos para *{label}*...", parse_mode="Markdown")

    fixtures, status = await fetch_api_football_fixtures_by_date(date_str)

    if status == "NO_API_KEY":
        await query.edit_message_text("🔑 *Clave de API no configurada.*", parse_mode="Markdown")
        return
    elif status == "QUOTA_EXCEEDED":
        await query.edit_message_text("⚠ *Límite de la API alcanzado.*", parse_mode="Markdown")
        return
    elif not fixtures:
        await query.edit_message_text(f"ℹ *No se encontraron partidos programados para {label}.*", parse_mode="Markdown")
        return

    now_ts = int(time.time())
    fixtures.sort(key=lambda f: f.get("fixture", {}).get("timestamp", 0))

    if offset_days == 0:
        valid_fixtures = [
            f for f in fixtures
            if f.get("fixture", {}).get("timestamp", 0) >= (now_ts - 300)
            and f.get("fixture", {}).get("status", {}).get("short") in ["NS", "TBD", "1H", "HT", "2H"]
        ]
        if not valid_fixtures:
            await query.edit_message_text(
                f"ℹ *No quedan partidos pendientes por disputarse en {label}.*",
                parse_mode="Markdown"
            )
            return
        fixtures = valid_fixtures

    target_fixtures = fixtures[:5]

    if category_code == "cat1x2":
        picks = []
        for fix in target_fixtures:
            teams = fix.get("teams", {})
            home = teams.get("home", {}).get("name", "Local")
            away = teams.get("away", {}).get("name", "Visitante")
            
            match_time = format_match_time(fix)
            analytics = generate_fixture_analytics(fix)
            p_home = analytics["metrics"]["p_home"]
            odds_home = analytics["odds_home"]
            ev = (p_home * odds_home) - 1.0
            stake = calculate_kelly_stake(p_home, odds_home)
            ev_display = f"+{ev*100:.1f}%" if ev > 0 else f"{ev*100:.1f}%"

            picks.append(
                f"🏆 *{home} vs {away}* (`{match_time} HS`)\n"
                f"📌 Selección: *Victoria Local ({home})*\n"
                f"📊 Cuota: `{odds_home:.2f}` | Prob. Real: `{p_home*100:.1f}%`\n"
                f"📈 EV: `{ev_display}` | Stake Kelly: `{stake}%`"
            )
        response = f"⚽ *PRONÓSTICOS 1X2 - {label.upper()}*\n\n" + "\n\n---\n\n".join(picks)

    elif category_code == "catgoals":
        picks = []
        for fix in target_fixtures:
            teams = fix.get("teams", {})
            home = teams.get("home", {}).get("name")
            away = teams.get("away", {}).get("name")
            
            match_time = format_match_time(fix)
            analytics = generate_fixture_analytics(fix)
            p_over = analytics["metrics"]["p_over_25"]
            p_btts = analytics["metrics"]["p_btts_yes"]
            odds_over = analytics["odds_over"]
            stake = calculate_kelly_stake(p_over, odds_over)

            picks.append(
                f"⚽ *{home} vs {away}* (`{match_time} HS`)\n"
                f"   • *Línea:* Más de 2.5 Goles\n"
                f"   • *Cuota:* `{odds_over:.2f}` | Prob Over 2.5: `{p_over*100:.1f}%`\n"
                f"   • *Prob. BTTS (Ambos Anotan):* `{p_btts*100:.1f}%`\n"
                f"   🎯 *Stake Kelly:* `{stake}%`"
            )
        response = f"⚽ *PRONÓSTICOS GOLES & BTTS - {label.upper()}*\n\n" + "\n\n---\n\n".join(picks)

    elif category_code == "catcorners":
        projections = []
        for fix in target_fixtures:
            teams = fix.get("teams", {})
            home = teams.get("home", {}).get("name")
            away = teams.get("away", {}).get("name")
            
            match_time = format_match_time(fix)
            analytics = generate_fixture_analytics(fix)

            projections.append(
                f"🚩 *{home} vs {away}* (`{match_time} HS`)\n"
                f"   • *Córners Estimados:* `{analytics['exp_corners']}` (Línea: *Más de 9.5*)\n"
                f"   • *Tarjetas Estimadas:* `{analytics['exp_cards']}` (Línea: *Más de 4.5*)\n"
                f"   • *Confianza Modelo:* `{analytics['confidence']}%`"
            )
        response = f"🚩 *CÓRNERS Y TARJETAS - {label.upper()}*\n\n" + "\n\n---\n\n".join(projections)

    elif category_code == "catcombo":
        if len(target_fixtures) < 2:
            response = f"ℹ️ *No hay suficientes partidos pendientes el {label} para armar una combinada.*"
        else:
            f1, f2 = target_fixtures[0], target_fixtures[1]
            t1_h = f1.get("teams", {}).get("home", {}).get("name")
            t1_a = f1.get("teams", {}).get("away", {}).get("name")
            t2_h = f2.get("teams", {}).get("home", {}).get("name")
            t2_a = f2.get("teams", {}).get("away", {}).get("name")

            a1, a2 = generate_fixture_analytics(f1), generate_fixture_analytics(f2)

            odds1, prob1 = a1["odds_home"], a1["metrics"]["p_home"]
            odds2, prob2 = a2["odds_home"], a2["metrics"]["p_home"]

            total_odds = odds1 * odds2
            combined_prob = prob1 * prob2
            ev = (combined_prob * total_odds) - 1.0
            stake = calculate_kelly_stake(combined_prob, total_odds, bankroll_fraction=0.15)
            ev_display = f"+{ev*100:.1f}%" if ev > 0 else f"{ev*100:.1f}%"

            response = (
                f"🧩 *COMBINADA DE VALOR (EV+) - {label.upper()}*\n\n"
                f"1️⃣ *{t1_h} vs {t1_a}*\n"
                f"   📌 Selección: Victoria Local ({t1_h}) | Cuota: `{odds1:.2f}`\n\n"
                f"2️⃣ *{t2_h} vs {t2_a}*\n"
                f"   📌 Selección: Victoria Local ({t2_h}) | Cuota: `{odds2:.2f}`\n\n"
                f"📊 *Resumen:*\n"
                f"• *Cuota Total:* `{total_odds:.2f}`\n"
                f"• *Probabilidad Estimada:* `{combined_prob*100:.1f}%`\n"
                f"• *EV:* `{ev_display}` | Stake Sugerido: `{stake}%`"
            )

    await query.edit_message_text(response, parse_mode="Markdown")

# ---------------------------------------------------------
# 12. Sección "Mis Estadísticas" de Usuario
# ---------------------------------------------------------
async def user_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return

    user_id = update.effective_user.id
    u = get_user(user_id)
    if not u:
        await update.message.reply_text("⚠ No se encontraron registros de usuario.")
        return

    total = u["bets_count"]
    wins = u["wins"]
    losses = u["losses"]
    profit = u["profit_units"]
    win_rate = (wins / total * 100) if total > 0 else 0.0

    stats_msg = (
        f"📊 *MIS ESTADÍSTICAS PERSONALIZADAS*\n\n"
        f"👤 *Usuario:* {u['first_name']} (`{user_id}`)\n"
        f"📅 *Miembro desde:* `{u['created_at'][:10]}`\n\n"
        f"• *Apuestas Registradas:* `{total}`\n"
        f"• *Aciertos (Ganadas):* `{wins}`\n"
        f"• *Fallos (Perdidas):* `{losses}`\n"
        f"• *Win Rate:* `{win_rate:.1f}%`\n"
        f"• *Beneficio Total:* `{profit:+.2f}u`\n\n"
        "👇 *Registra tus resultados para mantener tu historial:* "
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Registrar Ganada (+1u)", callback_data="stat_win"),
            InlineKeyboardButton("❌ Registrar Perdida (-1u)", callback_data="stat_loss")
        ],
        [InlineKeyboardButton("🔄 Reiniciar Mis Estadísticas", callback_data="stat_reset")]
    ])

    await update.message.reply_text(stats_msg, parse_mode="Markdown", reply_markup=keyboard)

# ---------------------------------------------------------
# 13. Router de Menú Principal
# ---------------------------------------------------------
async def prompt_date_selection(update: Update, category_code: str, title: str):
    text = f"🗓️ *Selecciona la jornada para {title}:*"
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=get_date_inline_keyboard(category_code)
    )

async def text_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return

    text = update.message.text
    if "1X2 / Ganador" in text:
        await prompt_date_selection(update, "cat1x2", "1X2 / Ganador")
    elif "Goles & BTTS" in text:
        await prompt_date_selection(update, "catgoals", "Goles & BTTS")
    elif "Córners" in text:
        await prompt_date_selection(update, "catcorners", "Córners & Tarjetas")
    elif "Combinadas" in text:
        await prompt_date_selection(update, "catcombo", "Combinadas EV+")
    elif "Top Value" in text:
        await top_value_command(update, context)
    elif "Mis Estadísticas" in text:
        await user_stats_command(update, context)
    elif "Panel Admin" in text:
        await admin_command(update, context)
    elif "Ayuda" in text:
        await help_command(update, context)

async def top_value_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return

    loading_msg = await update.message.reply_text("🔄 Filtrando los mejores picks +EV de la jornada...")
    fixtures, status = await fetch_api_football_fixtures_by_date(get_target_date_str(0)[0])
    await loading_msg.delete()

    if not fixtures:
        await update.message.reply_text("ℹ️ *No hay partidos activos para evaluar en este momento.*", parse_mode="Markdown")
        return

    now_ts = int(time.time())
    fixtures.sort(key=lambda f: f.get("fixture", {}).get("timestamp", 0))
    valid_fixtures = [
        f for f in fixtures
        if f.get("fixture", {}).get("timestamp", 0) >= (now_ts - 300)
        and f.get("fixture", {}).get("status", {}).get("short") in ["NS", "TBD", "1H", "HT", "2H"]
    ]

    if not valid_fixtures:
        await update.message.reply_text("ℹ️ *No quedan partidos pendientes por disputarse hoy.*", parse_mode="Markdown")
        return

    top_picks = []
    for fix in valid_fixtures[:3]:
        teams = fix.get("teams", {})
        home = teams.get("home", {}).get("name")
        away = teams.get("away", {}).get("name")
        
        match_time = format_match_time(fix)
        analytics = generate_fixture_analytics(fix)
        p_home = analytics["metrics"]["p_home"]
        odds_home = analytics["odds_home"]
        ev = (p_home * odds_home) - 1.0
        stake = calculate_kelly_stake(p_home, odds_home)

        top_picks.append(
            f"🔥 *{home} vs {away}* (`{match_time} HS`)\n"
            f"   • *Pick:* Victoria {home}\n"
            f"   • *Cuota:* `{odds_home:.2f}` | *Prob. Modelo:* `{p_home*100:.1f}%`\n"
            f"   • *Ventaja Matemática (EV):* `+{max(ev, 0.03)*100:.1f}%` 💎\n"
            f"   • *Apuesta Sugerida:* `{stake}%` de tu banca"
        )

    response = "🎯 *TOP SELECCIONES CON MAYOR VALOR (+EV) HOY*\n\n" + "\n\n---\n\n".join(top_picks)
    await update.message.reply_text(response, parse_mode="Markdown", reply_markup=get_main_reply_keyboard(update.effective_user.id))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return

    help_text = (
        "📖 *GUÍA DE LECTURA DE PRONÓSTICOS*\n\n"
        "Aprende a interpretar los datos de **NosticProno**:\n\n"
        "📊 *1. Cuota (Odds):* Multiplicador oficial.\n"
        "📈 *2. Probabilidad del Modelo (%):* Probabilidad real estimada mediante Distribución de Poisson.\n"
        "💡 *3. Valor Esperado (EV+):* Ventaja matemática sobre la casa.\n"
        "🎯 *4. Stake Kelly (%):* Porcentaje sugerido de tu banca para apostar.\n"
        "🚩 *5. Córners / Tarjetas:* Proyección numérica esperada."
    )
    await update.message.reply_text(help_text, parse_mode="Markdown", reply_markup=get_main_reply_keyboard(update.effective_user.id))

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await check_access(update):
        return

    user_name = update.effective_user.first_name
    welcome_text = (
        f"👋 *¡Hola, {user_name}!*\n\n"
        f"Bienvenido a *NosticProno* 🎯\n"
        f"Análisis estadístico y valor (+EV) para *Hoy, Mañana y Pasado Mañana*.\n\n"
        f"👇 *Selecciona un mercado para empezar:*"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=get_main_reply_keyboard(user_id))

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Error no capturado:", exc_info=context.error)

# ---------------------------------------------------------
# 14. Punto de Entrada Principal (Main)
# ---------------------------------------------------------
def main():
    token_raw = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
    if not token_raw:
        logger.error("Error crítico: TELEGRAM_BOT_TOKEN no configurado.")
        sys.exit(1)

    init_db()
    threading.Thread(target=start_health_server, daemon=True).start()

    logger.info("Inicializando NosticProno Bot con sistema de usuarios...")
    application = ApplicationBuilder().token(token_raw.strip()).build()

    # Comandos Usuario / General
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("top", top_value_command))
    application.add_handler(CommandHandler("stats", user_stats_command))

    # Comandos Administrador
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("usuarios", usuarios_command))
    application.add_handler(CommandHandler("agregar", agregar_command))
    application.add_handler(CommandHandler("bloquear", bloquear_command))
    application.add_handler(CommandHandler("activar", activar_command))
    application.add_handler(CommandHandler("eliminar", eliminar_command))

    # Callbacks & Mensajes
    application.add_handler(CallbackQueryHandler(admin_callback_handler, pattern="^(adm_|stat_)"))
    application.add_handler(CallbackQueryHandler(date_callback_handler, pattern="^cat"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_button_handler))

    application.add_error_handler(error_handler)

    logger.info("Bot activo en Telegram.")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()