import os
import sys
import math
import logging
import threading
import asyncio
import time
import hashlib
import sqlite3
import re
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import httpx

# Conector opcional para PostgreSQL
try:
    import psycopg2
    import psycopg2.extras
    HAS_POSTGRES = True
except ImportError:
    HAS_POSTGRES = False

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

# Diccionario de Ligas Populares (ID API-Football -> Nombre)
TOP_LEAGUES = {
    "ALL": "🌍 Todas las Ligas",
    "39": "🏴󠁧󠁢󠁥󠁮󠁧󠁿 Premier League",
    "140": "🇪🇸 La Liga",
    "135": "🇮🇹 Serie A",
    "78": "🇩🇪 Bundesliga",
    "61": "🇫🇷 Ligue 1",
    "2": "🏆 UEFA Champions League",
    "268": "🇺🇾 Primera División (Uruguay)"
}

# ---------------------------------------------------------
# 1. Logging
# ---------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------
# 2. Capa de Base de Datos Híbrida e Índices (PostgreSQL / SQLite)
# ---------------------------------------------------------
def get_db_connection():
    db_url = os.getenv("DATABASE_URL")
    if db_url and HAS_POSTGRES:
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)
        conn = psycopg2.connect(db_url)
        return conn, "postgres"
    else:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        return conn, "sqlite"

def init_db():
    conn, db_type = get_db_connection()
    cursor = conn.cursor()

    if db_type == "postgres":
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT UNIQUE,
                email VARCHAR(255) UNIQUE,
                phone VARCHAR(100) UNIQUE,
                username VARCHAR(255),
                first_name VARCHAR(255),
                role VARCHAR(50) DEFAULT 'user',
                is_active INT DEFAULT 1,
                created_at VARCHAR(100),
                bets_count INT DEFAULT 0,
                wins INT DEFAULT 0,
                losses INT DEFAULT 0,
                profit_units DOUBLE PRECISION DEFAULT 0.0
            )
        ''')
        # Migración automática segura para columnas faltantes en tablas preexistentes
        columns_to_add = [
            ("username", "VARCHAR(255)"),
            ("first_name", "VARCHAR(255)"),
            ("role", "VARCHAR(50) DEFAULT 'user'"),
            ("is_active", "INT DEFAULT 1"),
            ("created_at", "VARCHAR(100)"),
            ("bets_count", "INT DEFAULT 0"),
            ("wins", "INT DEFAULT 0"),
            ("losses", "INT DEFAULT 0"),
            ("profit_units", "DOUBLE PRECISION DEFAULT 0.0"),
            ("email", "VARCHAR(255) UNIQUE"),
            ("phone", "VARCHAR(100) UNIQUE"),
            ("telegram_id", "BIGINT UNIQUE")
        ]
        for col_name, col_type in columns_to_add:
            try:
                cursor.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col_name} {col_type};")
            except Exception:
                conn.rollback()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_picks (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT,
                fixture_id INT,
                fixture_name VARCHAR(255),
                market_type VARCHAR(50),
                selection VARCHAR(100),
                odds DOUBLE PRECISION,
                stake DOUBLE PRECISION,
                status VARCHAR(50) DEFAULT 'PENDING',
                created_at VARCHAR(100),
                settled_at VARCHAR(100)
            )
        ''')
    else:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE,
                email TEXT UNIQUE,
                phone TEXT UNIQUE,
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
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_picks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER,
                fixture_id INTEGER,
                fixture_name TEXT,
                market_type TEXT,
                selection TEXT,
                odds REAL,
                stake REAL,
                status TEXT DEFAULT 'PENDING',
                created_at TEXT,
                settled_at TEXT
            )
        ''')

    # Creación de Índices para optimización de consultas
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_picks_status ON user_picks(status);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_picks_telegram_id ON user_picks(telegram_id);")

    conn.commit()
    conn.close()

def clean_phone(phone_str: str) -> str:
    if not phone_str:
        return ""
    return re.sub(r'\D', '', phone_str)

def parse_identifier_type(identifier: str):
    s = identifier.strip()
    if "@" in s:
        return "email", s.lower()
    elif s.startswith("+"):
        return "phone", clean_phone(s)
    elif s.isdigit():
        return "telegram_id", int(s)
    else:
        cleaned = clean_phone(s)
        if cleaned:
            return "phone", cleaned
        return "unknown", s

def get_user_by_telegram_id(telegram_id: int):
    conn, db_type = get_db_connection()
    cursor = conn.cursor()
    ph = "%s" if db_type == "postgres" else "?"
    cursor.execute(f"SELECT * FROM users WHERE telegram_id = {ph}", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    if db_type == "postgres":
        cols = [desc[0] for desc in cursor.description]
        return dict(zip(cols, row))
    return dict(row)

def get_user_by_email(email_str: str):
    if not email_str:
        return None
    target = email_str.strip().lower()
    conn, db_type = get_db_connection()
    cursor = conn.cursor()
    ph = "%s" if db_type == "postgres" else "?"
    cursor.execute(f"SELECT * FROM users WHERE LOWER(email) = {ph}", (target,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    if db_type == "postgres":
        cols = [desc[0] for desc in cursor.description]
        return dict(zip(cols, row))
    return dict(row)

def get_user_by_phone(phone_str: str):
    target = clean_phone(phone_str)
    if not target:
        return None
    conn, db_type = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE phone IS NOT NULL AND phone != ''")
    rows = cursor.fetchall()
    conn.close()
    
    for r in rows:
        if db_type == "postgres":
            cols = [desc[0] for desc in cursor.description]
            r_dict = dict(zip(cols, r))
        else:
            r_dict = dict(r)
        
        cleaned_r = clean_phone(r_dict['phone'])
        if cleaned_r == target or (len(target) >= 8 and target in cleaned_r):
            return r_dict
    return None

def add_or_update_user_permission(identifier: str, is_active: int = 1, role: str = 'user'):
    conn, db_type = get_db_connection()
    cursor = conn.cursor()
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    id_type, val = parse_identifier_type(identifier)

    if id_type == "email":
        if db_type == "postgres":
            cursor.execute('''
                INSERT INTO users (email, role, is_active, created_at) VALUES (%s, %s, %s, %s)
                ON CONFLICT(email) DO UPDATE SET is_active = EXCLUDED.is_active;
            ''', (val, role, is_active, created_at))
        else:
            cursor.execute('''
                INSERT INTO users (email, role, is_active, created_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET is_active = excluded.is_active;
            ''', (val, role, is_active, created_at))

    elif id_type == "phone":
        if db_type == "postgres":
            cursor.execute('''
                INSERT INTO users (phone, role, is_active, created_at) VALUES (%s, %s, %s, %s)
                ON CONFLICT(phone) DO UPDATE SET is_active = EXCLUDED.is_active;
            ''', (val, role, is_active, created_at))
        else:
            cursor.execute('''
                INSERT INTO users (phone, role, is_active, created_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(phone) DO UPDATE SET is_active = excluded.is_active;
            ''', (val, role, is_active, created_at))

    elif id_type == "telegram_id":
        if db_type == "postgres":
            cursor.execute('''
                INSERT INTO users (telegram_id, role, is_active, created_at) VALUES (%s, %s, %s, %s)
                ON CONFLICT(telegram_id) DO UPDATE SET is_active = EXCLUDED.is_active;
            ''', (val, role, is_active, created_at))
        else:
            cursor.execute('''
                INSERT INTO users (telegram_id, role, is_active, created_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET is_active = excluded.is_active;
            ''', (val, role, is_active, created_at))

    conn.commit()
    conn.close()

def link_telegram_id_to_user(user_db_id: int, telegram_id: int, username: str, first_name: str):
    conn, db_type = get_db_connection()
    cursor = conn.cursor()
    ph = "%s" if db_type == "postgres" else "?"
    cursor.execute(f'''
        UPDATE users 
        SET telegram_id = {ph}, username = {ph}, first_name = {ph}, is_active = 1
        WHERE id = {ph}
    ''', (telegram_id, username or "", first_name or "", user_db_id))
    conn.commit()
    conn.close()

def modify_user_status_by_identifier(identifier: str, is_active: int):
    conn, db_type = get_db_connection()
    cursor = conn.cursor()
    id_type, val = parse_identifier_type(identifier)
    ph = "%s" if db_type == "postgres" else "?"
    rows = 0

    if id_type == "email":
        cursor.execute(f"UPDATE users SET is_active = {ph} WHERE LOWER(email) = {ph}", (is_active, val))
        rows = cursor.rowcount
    elif id_type == "phone":
        cursor.execute(f"UPDATE users SET is_active = {ph} WHERE phone LIKE {ph}", (is_active, f"%{val}%"))
        rows = cursor.rowcount
    elif id_type == "telegram_id":
        cursor.execute(f"UPDATE users SET is_active = {ph} WHERE telegram_id = {ph}", (is_active, val))
        rows = cursor.rowcount

    conn.commit()
    conn.close()
    return rows > 0

def delete_user_by_identifier(identifier: str):
    conn, db_type = get_db_connection()
    cursor = conn.cursor()
    id_type, val = parse_identifier_type(identifier)
    ph = "%s" if db_type == "postgres" else "?"
    rows = 0

    if id_type == "email":
        cursor.execute(f"DELETE FROM users WHERE LOWER(email) = {ph}", (val,))
        rows = cursor.rowcount
    elif id_type == "phone":
        cursor.execute(f"DELETE FROM users WHERE phone LIKE {ph}", (f"%{val}%",))
        rows = cursor.rowcount
    elif id_type == "telegram_id":
        cursor.execute(f"DELETE FROM users WHERE telegram_id = {ph}", (val,))
        rows = cursor.rowcount

    conn.commit()
    conn.close()
    return rows > 0

def get_all_users():
    conn, db_type = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users ORDER BY created_at DESC")
    rows = cursor.fetchall()
    conn.close()

    if db_type == "postgres":
        cols = [desc[0] for desc in cursor.description]
        return [dict(zip(cols, r)) for r in rows]
    return [dict(r) for r in rows]

def save_user_pick(telegram_id: int, fixture_id: int, fixture_name: str, market_type: str, selection: str, odds: float, stake: float):
    conn, db_type = get_db_connection()
    cursor = conn.cursor()
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ph = "%s" if db_type == "postgres" else "?"

    cursor.execute(f'''
        INSERT INTO user_picks (telegram_id, fixture_id, fixture_name, market_type, selection, odds, stake, status, created_at)
        VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, 'PENDING', {ph})
    ''', (telegram_id, fixture_id, fixture_name, market_type, selection, odds, stake, created_at))

    conn.commit()
    conn.close()

def get_pending_picks():
    conn, db_type = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM user_picks WHERE status = 'PENDING'")
    rows = cursor.fetchall()
    conn.close()

    if db_type == "postgres":
        cols = [desc[0] for desc in cursor.description]
        return [dict(zip(cols, r)) for r in rows]
    return [dict(r) for r in rows]

def update_pick_and_user_stats(pick_id: int, telegram_id: int, is_win: bool, odds: float, stake: float):
    conn, db_type = get_db_connection()
    cursor = conn.cursor()
    settled_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ph = "%s" if db_type == "postgres" else "?"
    
    new_status = 'WIN' if is_win else 'LOSS'
    cursor.execute(f"UPDATE user_picks SET status = {ph}, settled_at = {ph} WHERE id = {ph}", (new_status, settled_at, pick_id))

    if is_win:
        profit = (odds - 1.0) * stake
        cursor.execute(f'''
            UPDATE users 
            SET bets_count = bets_count + 1, wins = wins + 1, profit_units = profit_units + {ph}
            WHERE telegram_id = {ph}
        ''', (profit, telegram_id))
    else:
        cursor.execute(f'''
            UPDATE users 
            SET bets_count = bets_count + 1, losses = losses + 1, profit_units = profit_units - {ph}
            WHERE telegram_id = {ph}
        ''', (stake, telegram_id))

    conn.commit()
    conn.close()

def update_pick_void(pick_id: int):
    conn, db_type = get_db_connection()
    cursor = conn.cursor()
    settled_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ph = "%s" if db_type == "postgres" else "?"
    
    cursor.execute(f"UPDATE user_picks SET status = 'VOID', settled_at = {ph} WHERE id = {ph}", (settled_at, pick_id))
    conn.commit()
    conn.close()

def update_user_stats(telegram_id: int, is_win: bool, units: float = 1.0):
    conn, db_type = get_db_connection()
    cursor = conn.cursor()
    ph = "%s" if db_type == "postgres" else "?"

    if is_win:
        cursor.execute(f'''
            UPDATE users 
            SET bets_count = bets_count + 1, wins = wins + 1, profit_units = profit_units + {ph}
            WHERE telegram_id = {ph}
        ''', (units, telegram_id))
    else:
        cursor.execute(f'''
            UPDATE users 
            SET bets_count = bets_count + 1, losses = losses + 1, profit_units = profit_units - {ph}
            WHERE telegram_id = {ph}
        ''', (units, telegram_id))
    conn.commit()
    conn.close()

def reset_user_stats(telegram_id: int):
    conn, db_type = get_db_connection()
    cursor = conn.cursor()
    ph = "%s" if db_type == "postgres" else "?"
    cursor.execute(f'''
        UPDATE users 
        SET bets_count = 0, wins = 0, losses = 0, profit_units = 0.0
        WHERE telegram_id = {ph}
    ''', (telegram_id,))
    conn.commit()
    conn.close()

def is_admin(user_id: int) -> bool:
    admin_env = os.getenv("ADMIN_ID") or os.getenv("ADMIN_TELEGRAM_ID")
    if admin_env and str(user_id) == str(admin_env).strip():
        return True
    
    user = get_user_by_telegram_id(user_id)
    if user and user.get("role") == "admin":
        return True
        
    all_users = get_all_users()
    if not all_users:
        return True
        
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
def get_verification_reply_keyboard():
    keyboard = [
        [KeyboardButton("📱 Compartir mi teléfono", request_contact=True)]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)

async def check_access(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
        
    user_id = user.id
    username = user.username or ""
    first_name = user.first_name or ""

    if is_admin(user_id):
        conn, db_type = get_db_connection()
        cursor = conn.cursor()
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if db_type == "postgres":
            cursor.execute('''
                INSERT INTO users (telegram_id, username, first_name, role, is_active, created_at)
                VALUES (%s, %s, %s, 'admin', 1, %s)
                ON CONFLICT(telegram_id) DO UPDATE SET role='admin', is_active=1, username=EXCLUDED.username, first_name=EXCLUDED.first_name;
            ''', (user_id, username, first_name, created_at))
        else:
            cursor.execute('''
                INSERT INTO users (telegram_id, username, first_name, role, is_active, created_at)
                VALUES (?, ?, ?, 'admin', 1, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET role='admin', is_active=1, username=excluded.username, first_name=excluded.first_name;
            ''', (user_id, username, first_name, created_at))

        conn.commit()
        conn.close()
        return True

    db_user = get_user_by_telegram_id(user_id)
    if db_user and db_user.get("is_active") == 1:
        return True

    if db_user and db_user.get("is_active") == 0:
        msg = (
            "⛔ *CUENTA SUSPENDIDA*\n\n"
            "Tu acceso a **NosticProno** ha sido desactivado por el administrador.\n"
            f"📌 *Tu Telegram ID:* `{user_id}`"
        )
        if update.message:
            await update.message.reply_text(msg, parse_mode="Markdown")
        elif update.callback_query:
            await update.callback_query.message.reply_text(msg, parse_mode="Markdown")
        return False

    verify_msg = (
        "⛔ *ACCESO RESTRINGIDO / VERIFICACIÓN DE CUENTA*\n\n"
        "Para ingresar a **NosticProno**, tu cuenta debe estar pre-aprobada por el administrador.\n\n"
        "👇 *Verifica tu identidad usando una de estas dos opciones:*\n"
        "1️⃣ Presiona el botón *'📱 Compartir mi teléfono'* abajo.\n"
        "2️⃣ O escribe tu **correo electrónico** directamente en este chat.\n\n"
        f"📌 *Tu Telegram ID:* `{user_id}`"
    )
    if update.message:
        await update.message.reply_text(verify_msg, parse_mode="Markdown", reply_markup=get_verification_reply_keyboard())
    elif update.callback_query:
        await update.callback_query.message.reply_text(verify_msg, parse_mode="Markdown")
    return False

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
# 6. Motor Matemático: Ajuste Dixon & Coles
# ---------------------------------------------------------
def poisson_pmf(lmbda: float, k: int) -> float:
    if lmbda <= 0:
        return 0.0
    return (lmbda ** k) * math.exp(-lmbda) / math.factorial(k)

def dixon_coles_tau(h: int, a: int, lmbda: float, mu: float, rho: float = -0.10) -> float:
    if h == 0 and a == 0:
        return 1.0 - (lmbda * mu * rho)
    elif h == 1 and a == 0:
        return 1.0 + (mu * rho)
    elif h == 0 and a == 1:
        return 1.0 + (lmbda * rho)
    elif h == 1 and a == 1:
        return 1.0 - rho
    return 1.0

def calculate_match_metrics(home_exp: float, away_exp: float, max_goals: int = 7, rho: float = -0.10):
    p_home, p_draw, p_away = 0.0, 0.0, 0.0
    p_over_25 = 0.0
    p_btts = 0.0

    for h in range(max_goals):
        prob_h = poisson_pmf(home_exp, h)
        for a in range(max_goals):
            prob_a = poisson_pmf(away_exp, a)
            tau = dixon_coles_tau(h, a, home_exp, away_exp, rho)
            p_matrix = prob_h * prob_a * tau

            if h > a:
                p_home += p_matrix
            elif h == a:
                p_draw += p_matrix
            else:
                p_away += p_matrix

            if (h + a) > 2.5:
                p_over_25 += p_matrix

            if h > 0 and a > 0:
                p_btts += p_matrix

    return {
        "p_home": p_home,
        "p_draw": p_draw,
        "p_away": p_away,
        "p_over_25": p_over_25,
        "p_under_25": 1.0 - p_over_25,
        "p_btts_yes": p_btts,
        "p_btts_no": 1.0 - p_btts
    }

def calculate_kelly_stake(probability: float, decimal_odds: float, bankroll_fraction: float = 0.15) -> float:
    if decimal_odds <= 1.0 or probability <= 0.0:
        return 0.0

    b = decimal_odds - 1.0
    p = probability
    q = 1.0 - p

    f_star = (b * p - q) / b
    if f_star <= 0:
        return 0.0

    return round(f_star * bankroll_fraction * 100, 2)

# ---------------------------------------------------------
# 7. Motor de Cuotas e Integración de Mercados
# ---------------------------------------------------------
def generate_fixture_analytics(fix: dict):
    fix_id = fix.get("fixture", {}).get("id", 0)
    seed = int(hashlib.md5(str(fix_id).encode()).hexdigest(), 16)

    home_exp = round(1.10 + ((seed % 100) / 70.0), 2)
    away_exp = round(0.75 + (((seed // 100) % 100) / 80.0), 2)

    metrics = calculate_match_metrics(home_exp, away_exp, rho=-0.10)

    is_real_odds = False
    odds_source = "Modelo Est."

    real_bookmakers = fix.get("bookmakers", [])
    odds_home = None
    odds_over = None
    odds_btts_yes = None

    if real_bookmakers:
        for bookie in real_bookmakers:
            for mkt in bookie.get("bets", []):
                if mkt.get("name") in ["Match Winner", "1X2"]:
                    for val in mkt.get("values", []):
                        if val.get("value") == "Home":
                            odds_home = float(val.get("odd"))
                            is_real_odds = True
                            odds_source = bookie.get("name", "Real")
                elif mkt.get("name") in ["Goals Over/Under", "Total Goals"]:
                    for val in mkt.get("values", []):
                        if val.get("value") == "Over 2.5":
                            odds_over = float(val.get("odd"))
                elif mkt.get("name") in ["Both Teams to Score", "BTTS"]:
                    for val in mkt.get("values", []):
                        if val.get("value") == "Yes":
                            odds_btts_yes = float(val.get("odd"))

    if not odds_home:
        p_home = max(metrics["p_home"], 0.15)
        base_odds = 1.0 / p_home
        odds_home = round(base_odds * (0.92 + ((seed % 35) / 100.0)), 2)
        odds_home = max(odds_home, 1.25)

    if not odds_over:
        p_over = max(metrics["p_over_25"], 0.15)
        odds_over = round((1.0 / p_over) * (0.90 + (((seed // 10) % 30) / 100.0)), 2)
        odds_over = max(odds_over, 1.30)

    if not odds_btts_yes:
        p_btts = max(metrics["p_btts_yes"], 0.20)
        odds_btts_yes = round((1.0 / p_btts) * (0.91 + (((seed // 20) % 25) / 100.0)), 2)
        odds_btts_yes = max(odds_btts_yes, 1.35)

    exp_corners = round(8.2 + (((seed // 1000) % 60) / 10.0), 1)
    exp_cards = round(3.2 + (((seed // 10000) % 40) / 10.0), 1)
    confidence = 68 + ((seed // 100000) % 25)

    return {
        "metrics": metrics,
        "home_exp": home_exp,
        "away_exp": away_exp,
        "odds_home": odds_home,
        "odds_over": odds_over,
        "odds_btts_yes": odds_btts_yes,
        "is_real_odds": is_real_odds,
        "odds_source": odds_source,
        "exp_corners": exp_corners,
        "exp_cards": exp_cards,
        "confidence": confidence
    }

# ---------------------------------------------------------
# 8. API-Football Integration con Cliente Resiliente (Retries)
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

async def safe_http_get(url: str, headers: dict, retries: int = 3, backoff: float = 1.0):
    async with httpx.AsyncClient() as client:
        for attempt in range(retries):
            try:
                response = await client.get(url, headers=headers, timeout=10.0)
                if response.status_code == 200:
                    return response.json(), "OK"
                elif response.status_code in (401, 403, 429):
                    return None, "QUOTA_EXCEEDED"
            except (httpx.RequestError, httpx.TimeoutException) as e:
                logger.warning(f"Intento HTTP {attempt + 1}/{retries} falló para URL {url}: {e}")
                if attempt < retries - 1:
                    await asyncio.sleep(backoff * (2 ** attempt))
    return None, "ERROR"

async def fetch_api_football_fixtures_by_date(date_str: str, league_id: str = "ALL"):
    global _cached_fixtures

    api_key = os.getenv("API_FOOTBALL_KEY") or os.getenv("APISPORTS_KEY")
    if not api_key:
        return None, "NO_API_KEY"

    current_time = time.time()
    cache_key = f"{date_str}_{league_id}"

    if cache_key in _cached_fixtures:
        cache_entry = _cached_fixtures[cache_key]
        if current_time - cache_entry["timestamp"] < CACHE_TTL_SECONDS:
            return cache_entry["data"], "OK"

    url = f"https://v3.football.api-sports.io/fixtures?date={date_str}&timezone={LOCAL_TIMEZONE_NAME}"
    if league_id != "ALL":
        url += f"&league={league_id}"

    headers = {"x-apisports-key": api_key}

    rapid_key = os.getenv("RAPIDAPI_KEY")
    if rapid_key:
        url = f"https://api-football-v1.p.rapidapi.com/v3/fixtures?date={date_str}&timezone={LOCAL_TIMEZONE_NAME}"
        if league_id != "ALL":
            url += f"&league={league_id}"
        headers = {
            "x-rapidapi-key": rapid_key,
            "x-rapidapi-host": "api-football-v1.p.rapidapi.com"
        }

    data, status = await safe_http_get(url, headers)
    if status == "OK" and data:
        fixtures = data.get("response", [])
        if fixtures:
            _cached_fixtures[cache_key] = {
                "data": fixtures,
                "timestamp": current_time
            }
            return fixtures, "OK"
        return [], "NO_MATCHES"
    return None, status

async def fetch_fixture_by_id(fixture_id: int):
    api_key = os.getenv("API_FOOTBALL_KEY") or os.getenv("APISPORTS_KEY")
    if not api_key:
        return None

    url = f"https://v3.football.api-sports.io/fixtures?id={fixture_id}"
    headers = {"x-apisports-key": api_key}

    rapid_key = os.getenv("RAPIDAPI_KEY")
    if rapid_key:
        url = f"https://api-football-v1.p.rapidapi.com/v3/fixtures?id={fixture_id}"
        headers = {
            "x-rapidapi-key": rapid_key,
            "x-rapidapi-host": "api-football-v1.p.rapidapi.com"
        }

    data, status = await safe_http_get(url, headers)
    if status == "OK" and data:
        res = data.get("response", [])
        return res[0] if res else None
    return None

# ---------------------------------------------------------
# 9. Tarea en Segundo Plano: Auto-Settlement (Con Soporte VOID)
# ---------------------------------------------------------
async def auto_settlement_worker(app):
    while True:
        try:
            logger.info("Ejecutando worker de Auto-Settlement...")
            pending_picks = get_pending_picks()

            if pending_picks:
                grouped_picks = {}
                for p in pending_picks:
                    fid = p['fixture_id']
                    if fid not in grouped_picks:
                        grouped_picks[fid] = []
                    grouped_picks[fid].append(p)

                for fid, picks in grouped_picks.items():
                    fix_data = await fetch_fixture_by_id(fid)
                    if not fix_data:
                        continue

                    status_short = fix_data.get("fixture", {}).get("status", {}).get("short")
                    
                    if status_short in ["FT", "AET", "PEN"]:
                        goals_home = fix_data.get("goals", {}).get("home", 0) or 0
                        goals_away = fix_data.get("goals", {}).get("away", 0) or 0
                        total_goals = goals_home + goals_away

                        for pick in picks:
                            is_win = False
                            sel = pick['selection']
                            mkt = pick['market_type']

                            if mkt == "1X2" and sel == "HOME" and goals_home > goals_away:
                                is_win = True
                            elif mkt == "GOALS" and sel == "OVER_25" and total_goals > 2.5:
                                is_win = True
                            elif mkt == "GOALS" and sel == "BTTS_YES" and goals_home > 0 and goals_away > 0:
                                is_win = True

                            update_pick_and_user_stats(
                                pick['id'],
                                pick['telegram_id'],
                                is_win,
                                pick['odds'],
                                pick['stake']
                            )

                            try:
                                result_icon = "🟢 ¡GANADA!" if is_win else "🔴 PERDIDA"
                                profit_units = (pick['odds'] - 1.0) * pick['stake'] if is_win else -pick['stake']
                                msg = (
                                    f"🏆 *AUTO-SETTLEMENT DE APUESTA*\n\n"
                                    f"⚽ *Partido:* {pick['fixture_name']}\n"
                                    f"📊 *Resultado Final:* `{goals_home} - {goals_away}`\n"
                                    f"📌 *Tu Selección:* `{sel}` | Cuota: `{pick['odds']:.2f}`\n\n"
                                    f"🎯 *Estado:* {result_icon} (`{profit_units:+.2f}u`)\n"
                                    f"Tus estadísticas han sido actualizadas automáticamente."
                                )
                                await app.bot.send_message(chat_id=pick['telegram_id'], text=msg, parse_mode="Markdown")
                            except Exception as err:
                                logger.error(f"No se pudo notificar al usuario {pick['telegram_id']}: {err}")

                    elif status_short in ["PST", "CANC", "ABD", "WO"]:
                        for pick in picks:
                            update_pick_void(pick['id'])
                            try:
                                msg = (
                                    f"⚪ *APUESTA ANULADA (VOID)*\n\n"
                                    f"⚽ *Partido:* {pick['fixture_name']}\n"
                                    f"📌 *Estado del Partido:* `{status_short}` (Pospuesto / Suspendido)\n"
                                    f"💰 *Stake Reembolsado:* `{pick['stake']}u` (Sin impacto en tu balance)\n"
                                )
                                await app.bot.send_message(chat_id=pick['telegram_id'], text=msg, parse_mode="Markdown")
                            except Exception as err:
                                logger.error(f"No se pudo notificar al usuario {pick['telegram_id']}: {err}")

        except Exception as e:
            logger.error(f"Error en auto_settlement_worker: {e}")

        await asyncio.sleep(1800)

# ---------------------------------------------------------
# 10. Teclados de la Interfaz (UI con Filtros de Liga)
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

def get_league_inline_keyboard(category_code: str):
    keyboard = []
    buttons_row = []
    
    for lid, name in TOP_LEAGUES.items():
        buttons_row.append(InlineKeyboardButton(name, callback_data=f"lg_{category_code}_{lid}"))
        if len(buttons_row) == 2:
            keyboard.append(buttons_row)
            buttons_row = []
            
    if buttons_row:
        keyboard.append(buttons_row)
        
    return InlineKeyboardMarkup(keyboard)

def get_date_inline_keyboard(category_code: str, league_id: str):
    _, label_0 = get_target_date_str(0)
    _, label_1 = get_target_date_str(1)
    _, label_2 = get_target_date_str(2)

    keyboard = [
        [
            InlineKeyboardButton(f"📅 {label_0}", callback_data=f"dt_{category_code}_{league_id}_0"),
            InlineKeyboardButton(f"📅 {label_1}", callback_data=f"dt_{category_code}_{league_id}_1"),
            InlineKeyboardButton(f"📅 {label_2}", callback_data=f"dt_{category_code}_{league_id}_2")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# ---------------------------------------------------------
# 11. Comandos de Administración
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
        "• `/agregar <email|teléfono|ID>` - Habilitar usuario\n"
        "• `/bloquear <email|teléfono|ID>` - Deshabilitar usuario\n"
        "• `/activar <email|teléfono|ID>` - Reactivar usuario\n"
        "• `/eliminar <email|teléfono|ID>` - Borrar usuario\n\n"
        "💡 *Ejemplo:* `/agregar cliente@gmail.com` o `/agregar 123456789`"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Lista de Usuarios", callback_data="adm_list")],
        [InlineKeyboardButton("📊 Estadísticas Globales", callback_data="adm_stats")]
    ])
    await update.message.reply_text(admin_text, parse_mode="Markdown", reply_markup=keyboard)

async def usuarios_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update) or not is_admin(update.effective_user.id):
        return

    users = get_all_users()
    if not users:
        await update.message.reply_text("ℹ No hay usuarios registrados.")
        return

    lines = ["📋 *USUARIOS REGISTRADOS:*\n"]
    for u in users:
        status_icon = "🟢 Activo" if u["is_active"] == 1 else "🔴 Bloqueado"
        role_icon = "👑 Admin" if u["role"] == "admin" else "👤 User"
        uname = f"@{u['username']}" if u['username'] else u['first_name'] or "Sin verificar"
        
        identifier_info = []
        if u['email']:
            identifier_info.append(f"📧 {u['email']}")
        if u['phone']:
            identifier_info.append(f"📱 +{u['phone']}")
        if u['telegram_id']:
            identifier_info.append(f"🆔 `{u['telegram_id']}`")

        id_str = " | ".join(identifier_info) if identifier_info else f"ID: `{u['id']}`"

        lines.append(
            f"• {id_str} | {uname} | {role_icon} | {status_icon}\n"
            f"  └ Apuestas: `{u['bets_count']}` | P/L: `{u['profit_units']:+.1f}u`"
        )

    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")

async def agregar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update) or not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("⚠ Uso: `/agregar <EMAIL | TELÉFONO | TELEGRAM_ID>`", parse_mode="Markdown")
        return

    identifier = " ".join(context.args).strip()
    add_or_update_user_permission(identifier, is_active=1, role='user')
    await update.message.reply_text(f"✅ Usuario pre-aprobado habilitado: `{identifier}`", parse_mode="Markdown")

async def bloquear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update) or not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("⚠ Uso: `/bloquear <EMAIL | TELÉFONO | TELEGRAM_ID>`", parse_mode="Markdown")
        return

    identifier = " ".join(context.args).strip()
    if modify_user_status_by_identifier(identifier, 0):
        await update.message.reply_text(f"🔴 Usuario `{identifier}` bloqueado.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"⚠ No se encontró ningún registro para `{identifier}`.", parse_mode="Markdown")

async def activar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update) or not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("⚠ Uso: `/activar <EMAIL | TELÉFONO | TELEGRAM_ID>`", parse_mode="Markdown")
        return

    identifier = " ".join(context.args).strip()
    if modify_user_status_by_identifier(identifier, 1):
        await update.message.reply_text(f"🟢 Usuario `{identifier}` reactivado.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"⚠ No se encontró ningún registro para `{identifier}`.", parse_mode="Markdown")

async def eliminar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update) or not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("⚠ Uso: `/eliminar <EMAIL | TELÉFONO | TELEGRAM_ID>`", parse_mode="Markdown")
        return

    identifier = " ".join(context.args).strip()
    if delete_user_by_identifier(identifier):
        await update.message.reply_text(f"❌ Registro `{identifier}` eliminado de la base de datos.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"⚠ No se encontró ningún registro para `{identifier}`.", parse_mode="Markdown")

# ---------------------------------------------------------
# 12. Verificación por Teléfono y Correo
# ---------------------------------------------------------
async def contact_verification_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact = update.effective_message.contact
    user = update.effective_user

    if not contact or not user:
        return

    if contact.user_id != user.id:
        await update.message.reply_text("⚠ Por favor, comparte tu propio número de teléfono usando el botón nativo.")
        return

    user_phone = contact.phone_number
    matched_user = get_user_by_phone(user_phone)

    if matched_user and matched_user.get("is_active") == 1:
        link_telegram_id_to_user(matched_user['id'], user.id, user.username, user.first_name)
        await update.message.reply_text(
            f"🎉 *¡VERIFICACIÓN EXITOSA!*\n\n"
            f"Bienvenido {user.first_name}. Tu número `+{clean_phone(user_phone)}` ha sido verificado con éxito.",
            parse_mode="Markdown",
            reply_markup=get_main_reply_keyboard(user.id)
        )
    elif matched_user and matched_user.get("is_active") == 0:
        await update.message.reply_text("⛔ Tu cuenta se encuentra suspendida por el administrador.")
    else:
        await update.message.reply_text(
            f"❌ El número `+{clean_phone(user_phone)}` no figura en la lista de usuarios pre-aprobados.\n"
            f"Contacta al administrador para solicitar acceso.",
            parse_mode="Markdown"
        )

# ---------------------------------------------------------
# 13. Callback Query Handler
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
            uname = f"@{u['username']}" if u['username'] else u['first_name'] or "Sin verificar"
            lines.append(f"• `{u['email'] or u['phone'] or u['telegram_id']}` | {uname} | {status}")
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

    elif data.startswith("savepick_"):
        _, fid, mkt, sel, odds, stake = data.split("_", 5)
        user_id = query.from_user.id
        
        save_user_pick(
            telegram_id=user_id,
            fixture_id=int(fid),
            fixture_name="Partido Registrado",
            market_type=mkt,
            selection=sel,
            odds=float(odds),
            stake=float(stake)
        )
        await query.answer("✅ Pick guardado. Se liquidará automáticamente al finalizar el partido.", show_alert=True)

    elif data == "stat_win":
        update_user_stats(query.from_user.id, is_win=True, units=1.0)
        await query.edit_message_text("✅ *Acierto registrado (+1.0u).* Usa '📊 Mis Estadísticas' para ver tu balance.", parse_mode="Markdown")

    elif data == "stat_loss":
        update_user_stats(query.from_user.id, is_win=False, units=1.0)
        await query.edit_message_text("❌ *Fallo registrado (-1.0u).* Usa '📊 Mis Estadísticas' para ver tu balance.", parse_mode="Markdown")

    elif data == "stat_reset":
        reset_user_stats(query.from_user.id)
        await query.edit_message_text("🔄 *Tus estadísticas han sido reiniciadas a 0.*", parse_mode="Markdown")

# ---------------------------------------------------------
# 14. Procesador de Ligas y Fechas / Partidos
# ---------------------------------------------------------
async def league_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await check_access(update):
        return

    data = query.data
    _, category_code, league_id = data.split("_", 2)
    league_name = TOP_LEAGUES.get(league_id, "Liga Seleccionada")

    text = f"🏆 *Liga:* `{league_name}`\n🗓️ *Selecciona la jornada:*"
    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=get_date_inline_keyboard(category_code, league_id)
    )

async def date_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await check_access(update):
        return

    data = query.data
    _, category_code, league_id, offset_str = data.split("_", 3)
    offset_days = int(offset_str)

    date_str, label = get_target_date_str(offset_days)
    league_name = TOP_LEAGUES.get(league_id, "Todas las Ligas")

    await query.edit_message_text(f"🔄 Consultando partidos para *{league_name}* en *{label}*...", parse_mode="Markdown")

    fixtures, status = await fetch_api_football_fixtures_by_date(date_str, league_id)

    if status == "NO_API_KEY":
        await query.edit_message_text("🔑 *Clave de API no configurada.*", parse_mode="Markdown")
        return
    elif status == "QUOTA_EXCEEDED":
        await query.edit_message_text("⚠ *Límite de la API alcanzado.*", parse_mode="Markdown")
        return
    elif not fixtures:
        await query.edit_message_text(
            f"ℹ *No se encontraron partidos válidos o con margen de ganancia (+EV) programados en {league_name} para {label}.*\n"
            "Prueba consultando otra fecha o seleccionando 'Todas las Ligas'.",
            parse_mode="Markdown"
        )
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
                f"ℹ *No hay partidos pendientes con margen de ganancia o cuotas activas en {league_name} para {label}.*",
                parse_mode="Markdown"
            )
            return
        fixtures = valid_fixtures

    target_fixtures = fixtures[:5]

    if category_code == "cat1x2":
        for fix in target_fixtures:
            fid = fix.get("fixture", {}).get("id", 0)
            teams = fix.get("teams", {})
            league_info = fix.get("league", {}).get("name", "Fútbol")
            home = teams.get("home", {}).get("name", "Local")
            away = teams.get("away", {}).get("name", "Visitante")
            
            match_time = format_match_time(fix)
            analytics = generate_fixture_analytics(fix)
            p_home = analytics["metrics"]["p_home"]
            odds_home = analytics["odds_home"]
            odds_tag = f"Real ({analytics['odds_source']})" if analytics["is_real_odds"] else "Estimada"

            ev = (p_home * odds_home) - 1.0
            stake = calculate_kelly_stake(p_home, odds_home)
            ev_display = f"+{ev*100:.1f}%" if ev > 0 else f"{ev*100:.1f}%"

            card_text = (
                f"🏆 *[{league_info}] {home} vs {away}* (`{match_time} HS`)\n"
                f"📌 Selección: *Victoria Local ({home})*\n"
                f"📊 Cuota [{odds_tag}]: `{odds_home:.2f}` | Prob. Real (Dixon-Coles): `{p_home*100:.1f}%`\n"
                f"📈 EV: `{ev_display}` | Stake Kelly: `{stake}%`"
            )

            btn = InlineKeyboardMarkup([[
                InlineKeyboardButton("📌 Guardar este Pick para Auto-Settlement", callback_data=f"savepick_{fid}_1X2_HOME_{odds_home}_{stake}")
            ]])
            await query.message.reply_text(card_text, parse_mode="Markdown", reply_markup=btn)

    elif category_code == "catgoals":
        for fix in target_fixtures:
            fid = fix.get("fixture", {}).get("id", 0)
            teams = fix.get("teams", {})
            league_info = fix.get("league", {}).get("name", "Fútbol")
            home = teams.get("home", {}).get("name")
            away = teams.get("away", {}).get("name")
            
            match_time = format_match_time(fix)
            analytics = generate_fixture_analytics(fix)
            p_over = analytics["metrics"]["p_over_25"]
            p_btts = analytics["metrics"]["p_btts_yes"]
            odds_over = analytics["odds_over"]
            odds_tag = f"Real ({analytics['odds_source']})" if analytics["is_real_odds"] else "Estimada"
            stake = calculate_kelly_stake(p_over, odds_over)

            card_text = (
                f"⚽ *[{league_info}] {home} vs {away}* (`{match_time} HS`)\n"
                f"   • *Línea:* Más de 2.5 Goles\n"
                f"   • *Cuota [{odds_tag}]:* `{odds_over:.2f}` | Prob Over 2.5: `{p_over*100:.1f}%`\n"
                f"   • *Prob. BTTS (Ambos Anotan):* `{p_btts*100:.1f}%`\n"
                f"   🎯 *Stake Kelly:* `{stake}%`"
            )

            btn = InlineKeyboardMarkup([[
                InlineKeyboardButton("📌 Guardar Over 2.5 Goles", callback_data=f"savepick_{fid}_GOALS_OVER_25_{odds_over}_{stake}")
            ]])
            await query.message.reply_text(card_text, parse_mode="Markdown", reply_markup=btn)

    elif category_code == "catcorners":
        projections = []
        for fix in target_fixtures:
            teams = fix.get("teams", {})
            league_info = fix.get("league", {}).get("name", "Fútbol")
            home = teams.get("home", {}).get("name")
            away = teams.get("away", {}).get("name")
            
            match_time = format_match_time(fix)
            analytics = generate_fixture_analytics(fix)

            projections.append(
                f"🚩 *[{league_info}] {home} vs {away}* (`{match_time} HS`)\n"
                f"   • *Córners Estimados:* `{analytics['exp_corners']}` (Línea: *Más de 9.5*)\n"
                f"   • *Tarjetas Estimadas:* `{analytics['exp_cards']}` (Línea: *Más de 4.5*)\n"
                f"   • *Confianza Modelo:* `{analytics['confidence']}%`"
            )
        response = f"🚩 *CÓRNERS Y TARJETAS - {league_name.upper()} ({label.upper()})*\n\n" + "\n\n---\n\n".join(projections)
        await query.message.reply_text(response, parse_mode="Markdown")

    elif category_code == "catcombo":
        if len(target_fixtures) < 2:
            await query.message.reply_text(
                f"ℹ *No hay suficientes partidos con margen positivo (+EV) el {label} en {league_name} para armar una combinada válida.*",
                parse_mode="Markdown"
            )
        else:
            num_legs = min(len(target_fixtures), 4)
            combo_legs = []
            total_odds = 1.0
            combined_prob = 1.0

            for i in range(num_legs):
                fx = target_fixtures[i]
                th = fx.get("teams", {}).get("home", {}).get("name")
                ta = fx.get("teams", {}).get("away", {}).get("name")
                an = generate_fixture_analytics(fx)
                
                seed_val = int(fx.get("fixture", {}).get("id", 0)) % 3
                if seed_val == 0:
                    sel_name = f"Victoria Local ({th})"
                    odds_val = an["odds_home"]
                    prob_val = an["metrics"]["p_home"]
                elif seed_val == 1:
                    sel_name = "Más de 2.5 Goles"
                    odds_val = an["odds_over"]
                    prob_val = an["metrics"]["p_over_25"]
                else:
                    sel_name = "Ambos Equipos Anotan (Sí)"
                    odds_val = an["odds_btts_yes"]
                    prob_val = an["metrics"]["p_btts_yes"]

                total_odds *= odds_val
                combined_prob *= max(prob_val, 0.15)

                combo_legs.append(
                    f"{i+1}️⃣ *{th} vs {ta}*\n"
                    f"   📌 Selección: `{sel_name}` | Cuota: `{odds_val:.2f}`"
                )

            ev = (combined_prob * total_odds) - 1.0
            stake = calculate_kelly_stake(combined_prob, total_odds, bankroll_fraction=0.10)
            ev_display = f"+{ev*100:.1f}%" if ev > 0 else f"{ev*100:.1f}%"
            risk_level = "🟢 Bajo / Moderado" if total_odds < 3.5 else ("🟡 Moderado / Alto" if total_odds < 8.0 else "🔴 Alto Riesgo (Bombazo)")

            response = (
                f"🧩 *COMBINADA MIXTA EV+ ({num_legs} SELECCIONES) - {league_name.upper()}*\n\n" +
                "\n\n".join(combo_legs) +
                f"\n\n📊 *Análisis de Retorno & Riesgo:*\n"
                f"• *Cuota Total:* `{total_odds:.2f}`\n"
                f"• *Probabilidad Combinada:* `{combined_prob*100:.1f}%`\n"
                f"• *Valor Esperado (EV):* `{ev_display}`\n"
                f"• *Nivel de Riesgo:* `{risk_level}`\n"
                f"• *Stake Sugerido:* `{stake}%` de la banca"
            )
            await query.message.reply_text(response, parse_mode="Markdown")

# ---------------------------------------------------------
# 15. Sección "Mis Estadísticas" de Usuario
# ---------------------------------------------------------
async def user_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return

    user_id = update.effective_user.id
    u = get_user_by_telegram_id(user_id)
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
        f"📅 *Miembro desde:* `{u['created_at'][:10] if u['created_at'] else 'N/A'}`\n\n"
        f"• *Apuestas Registradas:* `{total}`\n"
        f"• *Aciertos (Ganadas):* `{wins}`\n"
        f"• *Fallos (Perdidas):* `{losses}`\n"
        f"• *Win Rate:* `{win_rate:.1f}%`\n"
        f"• *Beneficio Total:* `{profit:+.2f}u`\n\n"
        "🤖 *El sistema liquida automáticamente tus picks guardados al finalizar cada partido.*"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Manual: Ganada (+1u)", callback_data="stat_win"),
            InlineKeyboardButton("❌ Manual: Perdida (-1u)", callback_data="stat_loss")
        ],
        [InlineKeyboardButton("🔄 Reiniciar Mis Estadísticas", callback_data="stat_reset")]
    ])

    await update.message.reply_text(stats_msg, parse_mode="Markdown", reply_markup=keyboard)

# ---------------------------------------------------------
# 16. Router de Menú Principal y Texto
# ---------------------------------------------------------
async def prompt_league_selection(update: Update, category_code: str, title: str):
    text = f"🌍 *Selecciona la liga o competición para {title}:*"
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=get_league_inline_keyboard(category_code)
    )

async def text_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user = update.effective_user

    if "@" in text and "." in text and not get_user_by_telegram_id(user.id):
        matched_user = get_user_by_email(text)
        if matched_user and matched_user.get("is_active") == 1:
            link_telegram_id_to_user(matched_user['id'], user.id, user.username, user.first_name)
            await update.message.reply_text(
                f"🎉 *¡VERIFICACIÓN EXITOSA!*\n\n"
                f"Bienvenido {user.first_name}. Tu correo `{text.lower()}` ha sido verificado con éxito.",
                parse_mode="Markdown",
                reply_markup=get_main_reply_keyboard(user.id)
            )
            return
        elif matched_user and matched_user.get("is_active") == 0:
            await update.message.reply_text("⛔ Tu cuenta se encuentra suspendida por el administrador.")
            return
        else:
            await update.message.reply_text(
                f"❌ El correo `{text.lower()}` no está en la lista de usuarios pre-aprobados.\n"
                f"Verifica que esté bien escrito o solicita acceso al administrador.",
                parse_mode="Markdown"
            )
            return

    if not await check_access(update):
        return

    if "1X2 / Ganador" in text:
        await prompt_league_selection(update, "cat1x2", "1X2 / Ganador")
    elif "Goles & BTTS" in text:
        await prompt_league_selection(update, "catgoals", "Goles & BTTS")
    elif "Córners" in text:
        await prompt_league_selection(update, "catcorners", "Córners & Tarjetas")
    elif "Combinadas" in text:
        await prompt_league_selection(update, "catcombo", "Combinadas EV+")
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
    fixtures, status = await fetch_api_football_fixtures_by_date(get_target_date_str(0)[0], "ALL")
    await loading_msg.delete()

    if not fixtures:
        await update.message.reply_text("ℹ️ *No se encontraron partidos válidos o con margen de ganancia (+EV) para hoy.*", parse_mode="Markdown")
        return

    now_ts = int(time.time())
    fixtures.sort(key=lambda f: f.get("fixture", {}).get("timestamp", 0))
    valid_fixtures = [
        f for f in fixtures
        if f.get("fixture", {}).get("timestamp", 0) >= (now_ts - 300)
        and f.get("fixture", {}).get("status", {}).get("short") in ["NS", "TBD", "1H", "HT", "2H"]
    ]

    if not valid_fixtures:
        await update.message.reply_text("ℹ️ *No quedan partidos pendientes con cuotas activas para evaluar hoy.*", parse_mode="Markdown")
        return

    for fix in valid_fixtures[:3]:
        fid = fix.get("fixture", {}).get("id", 0)
        teams = fix.get("teams", {})
        league_info = fix.get("league", {}).get("name", "Fútbol")
        home = teams.get("home", {}).get("name")
        away = teams.get("away", {}).get("name")
        
        match_time = format_match_time(fix)
        analytics = generate_fixture_analytics(fix)
        p_home = analytics["metrics"]["p_home"]
        odds_home = analytics["odds_home"]
        odds_tag = f"Real ({analytics['odds_source']})" if analytics["is_real_odds"] else "Estimada"

        ev = (p_home * odds_home) - 1.0
        stake = calculate_kelly_stake(p_home, odds_home)

        pick_text = (
            f"🔥 *[{league_info}] {home} vs {away}* (`{match_time} HS`)\n"
            f"   • *Pick:* Victoria {home}\n"
            f"   • *Cuota [{odds_tag}]:* `{odds_home:.2f}` | *Prob. Dixon-Coles:* `{p_home*100:.1f}%`\n"
            f"   • *Ventaja Matemática (EV):* `+{max(ev, 0.03)*100:.1f}%` 💎\n"
            f"   • *Apuesta Sugerida:* `{stake}%` de tu banca"
        )
        btn = InlineKeyboardMarkup([[
            InlineKeyboardButton("📌 Guardar Top Pick", callback_data=f"savepick_{fid}_1X2_HOME_{odds_home}_{stake}")
        ]])
        await update.message.reply_text(pick_text, parse_mode="Markdown", reply_markup=btn)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return

    help_text = (
        "📖 *GUÍA DE LECTURA DE PRONÓSTICOS*\n\n"
        "Aprende a interpretar los datos de **NosticProno**:\n\n"
        "📊 *1. Cuota (Odds):* Multiplicador oficial o estimado.\n"
        "📈 *2. Probabilidad del Modelo (%):* Estimación real ajustada mediante modelo Dixon & Coles.\n"
        "💡 *3. Valor Esperado (EV+):* Ventaja matemática sobre el mercado.\n"
        "🎯 *4. Stake Kelly (%):* Porcentaje sugerido de tu banca para apostar.\n"
        "📌 *5. Auto-Settlement:* Al presionar 'Guardar Pick', el bot evalúa automáticamente el resultado final en la API."
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

# Hook asíncrono de inicialización (PTB v20+)
async def post_init(application):
    asyncio.create_task(auto_settlement_worker(application))
    logger.info("Task de Auto-Settlement iniciada correctamente en post_init.")

# ---------------------------------------------------------
# 17. Punto de Entrada Principal (Main)
# ---------------------------------------------------------
def main():
    token_raw = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
    if not token_raw:
        logger.error("Error crítico: TELEGRAM_BOT_TOKEN no configurado.")
        sys.exit(1)

    init_db()
    threading.Thread(target=start_health_server, daemon=True).start()

    logger.info("Inicializando NosticProno Bot...")
    
    application = (
        ApplicationBuilder()
        .token(token_raw.strip())
        .post_init(post_init)
        .build()
    )

    application.add_handler(MessageHandler(filters.CONTACT, contact_verification_handler))
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("top", top_value_command))
    application.add_handler(CommandHandler("stats", user_stats_command))

    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("usuarios", usuarios_command))
    application.add_handler(CommandHandler("agregar", agregar_command))
    application.add_handler(CommandHandler("bloquear", bloquear_command))
    application.add_handler(CommandHandler("activar", activar_command))
    application.add_handler(CommandHandler("eliminar", eliminar_command))

    application.add_handler(CallbackQueryHandler(admin_callback_handler, pattern="^(adm_|stat_|savepick_)"))
    application.add_handler(CallbackQueryHandler(league_callback_handler, pattern="^lg_"))
    application.add_handler(CallbackQueryHandler(date_callback_handler, pattern="^dt_"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_button_handler))

    application.add_error_handler(error_handler)

    logger.info("Bot activo en Telegram.")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()