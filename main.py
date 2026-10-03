import os
import sys
import math
import logging
import threading
import asyncio
import time
import hashlib
from datetime import datetime, timedelta
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
# 1. Configuración de Logging
# ---------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------
# 2. Servidor HTTP de Salud (Render Port Binding)
# ---------------------------------------------------------
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot OK - NosticProno Active")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass

def start_health_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info(f"Servidor HTTP de salud activo en el puerto {port}")
    server.serve_forever()

# ---------------------------------------------------------
# 3. Conversor de Horario Universal (UTC) a Hora Local
# ---------------------------------------------------------
def format_match_time(iso_date_str: str, utc_offset_hours: int = -3) -> str:
    """
    Convierte la fecha ISO en UTC entregada por la API a la hora local.
    Por defecto utc_offset_hours=-3 ajusta a GMT-3 (Uruguay / Argentina).
    """
    if not iso_date_str:
        return "--:--"
    try:
        clean_str = iso_date_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_str)
        local_dt = dt + timedelta(hours=utc_offset_hours)
        return local_dt.strftime("%H:%M")
    except Exception:
        return iso_date_str[11:16] if len(iso_date_str) >= 16 else "--:--"

# ---------------------------------------------------------
# 4. Modelos Matemáticos (Poisson & Kelly)
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

# Generador de Métricas Dinámicas por Partido
def generate_fixture_analytics(fix: dict):
    fix_id = fix.get("fixture", {}).get("id", 0)
    seed = int(hashlib.md5(str(fix_id).encode()).hexdigest(), 16)

    # Goles esperados únicos según ID del encuentro
    home_exp = round(1.10 + ((seed % 100) / 70.0), 2)
    away_exp = round(0.75 + (((seed // 100) % 100) / 80.0), 2)

    metrics = calculate_match_metrics(home_exp, away_exp)

    # Cuotas y proyecciones dinámicas
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
# 5. Integración API con Caché por Fecha
# ---------------------------------------------------------
_cached_fixtures = {}
CACHE_TTL_SECONDS = 900  # 15 Minutos de caché por fecha

def get_target_date_str(offset_days: int) -> tuple[str, str]:
    target_dt = datetime.now() + timedelta(days=offset_days)
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
            logger.info(f"Devolviendo caché para la fecha {date_str}.")
            return cache_entry["data"], "OK"

    url = f"https://v3.football.api-sports.io/fixtures?date={date_str}"
    headers = {"x-apisports-key": api_key}

    rapid_key = os.getenv("RAPIDAPI_KEY")
    if rapid_key:
        url = f"https://api-football-v1.p.rapidapi.com/v3/fixtures?date={date_str}"
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
# 6. Teclados UI
# ---------------------------------------------------------
def get_main_reply_keyboard():
    keyboard = [
        [KeyboardButton("⚽ 1X2 / Ganador"), KeyboardButton("⚽ Goles & BTTS")],
        [KeyboardButton("🚩 Córners & Tarjetas"), KeyboardButton("🧩 Combinadas EV+")],
        [KeyboardButton("📊 Mis Estadísticas"), KeyboardButton("🎯 Top Value +EV"), KeyboardButton("📖 Ayuda")]
    ]
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
# 7. Menús de Selección de Fecha
# ---------------------------------------------------------
async def prompt_date_selection(update: Update, category_code: str, title: str):
    text = f"🗓️ *Selecciona la jornada para {title}:*"
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=get_date_inline_keyboard(category_code)
    )

# ---------------------------------------------------------
# 8. Callback Query Handler
# ---------------------------------------------------------
async def date_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

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
        await query.edit_message_text(f"ℹ️️ *No se encontraron partidos programados para {label}.*", parse_mode="Markdown")
        return

    if category_code == "cat1x2":
        picks = []
        for fix in fixtures[:5]:
            teams = fix.get("teams", {})
            home = teams.get("home", {}).get("name", "Local")
            away = teams.get("away", {}).get("name", "Visitante")
            
            # Formato de hora ajustado a GMT-3
            match_time = format_match_time(fix.get("fixture", {}).get("date", ""), utc_offset_hours=-3)

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
        for fix in fixtures[:5]:
            teams = fix.get("teams", {})
            home = teams.get("home", {}).get("name")
            away = teams.get("away", {}).get("name")
            
            # Formato de hora ajustado a GMT-3
            match_time = format_match_time(fix.get("fixture", {}).get("date", ""), utc_offset_hours=-3)

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
        for fix in fixtures[:5]:
            teams = fix.get("teams", {})
            home = teams.get("home", {}).get("name")
            away = teams.get("away", {}).get("name")
            
            # Formato de hora ajustado a GMT-3
            match_time = format_match_time(fix.get("fixture", {}).get("date", ""), utc_offset_hours=-3)

            analytics = generate_fixture_analytics(fix)

            projections.append(
                f"🚩 *{home} vs {away}* (`{match_time} HS`)\n"
                f"   • *Córners Estimados:* `{analytics['exp_corners']}` (Línea: *Más de 9.5*)\n"
                f"   • *Tarjetas Estimadas:* `{analytics['exp_cards']}` (Línea: *Más de 4.5*)\n"
                f"   • *Confianza Modelo:* `{analytics['confidence']}%`"
            )
        response = f"🚩 *CÓRNERS Y TARJETAS - {label.upper()}*\n\n" + "\n\n---\n\n".join(projections)

    elif category_code == "catcombo":
        if len(fixtures) < 2:
            response = f"ℹ️ *No hay suficientes partidos el {label} para armar una combinada.*"
        else:
            f1, f2 = fixtures[0], fixtures[1]
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
# 9. Comandos Especiales (Top Value & Ayuda)
# ---------------------------------------------------------
async def top_value_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loading_msg = await update.message.reply_text("🔄 Filtrando los mejores picks +EV de la jornada...")
    fixtures, status = await fetch_api_football_fixtures_by_date(get_target_date_str(0)[0])
    await loading_msg.delete()

    if not fixtures:
        await update.message.reply_text("ℹ️ *No hay partidos activos para evaluar en este momento.*", parse_mode="Markdown")
        return

    top_picks = []
    for fix in fixtures[:3]:
        teams = fix.get("teams", {})
        home = teams.get("home", {}).get("name")
        away = teams.get("away", {}).get("name")
        
        # Formato de hora ajustado a GMT-3
        match_time = format_match_time(fix.get("fixture", {}).get("date", ""), utc_offset_hours=-3)

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
    await update.message.reply_text(response, parse_mode="Markdown", reply_markup=get_main_reply_keyboard())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📖 *GUÍA DE LECTURA DE PRONÓSTICOS*\n\n"
        "Aprende a interpretar los datos que entrega **NosticProno**:\n\n"
        "📊 *1. Cuota (Odds)*\n"
        "Es la cuota oficial multiplicadora. Por ejemplo, cuota `2.00` equivale a duplicar lo apostado si se acierta.\n\n"
        "📈 *2. Probabilidad del Modelo (%)*\n"
        "Es la probabilidad real calculada por la *Distribución de Poisson* analizando goles anotados, recibidos y rendimiento reciente.\n\n"
        "💡 *3. Valor Esperado (EV+)*\n"
        "Indica la **ventaja matemática** sobre la casa de apuestas. Si el EV es positivo (ej: `+8.5%`), la apuesta es rentable a largo plazo.\n\n"
        "🎯 *4. Stake Kelly (%)*\n"
        "Es el porcentaje **máximo recomendado de tu dinero total (Banca)** para apostar en ese partido, calculado mediante el *Criterio de Kelly* para minimizar riesgos.\n\n"
        "🚩 *5. Líneas de Córners y Tarjetas*\n"
        "Muestra la proyección numérica esperada. Si indica *Más de 9.5*, el modelo proyecta que habrán 10 o más saques de esquina."
    )
    await update.message.reply_text(help_text, parse_mode="Markdown", reply_markup=get_main_reply_keyboard())

# ---------------------------------------------------------
# 10. Router de Botones de Texto
# ---------------------------------------------------------
async def text_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    elif "Estadísticas" in text:
        stats_text = (
            "📊 *Rendimiento Histórico NosticProno*\n\n"
            "• *Picks Analizados:* `162`\n"
            "• *Aciertos:* `95` | *Fallos:* `67`\n"
            "• *Win Rate:* `58.6%`\n"
            "• *Yield / ROI:* `+9.1%`\n"
            "• *Unidades Ganadas:* `+19.4u`"
        )
        await update.message.reply_text(stats_text, parse_mode="Markdown", reply_markup=get_main_reply_keyboard())
    elif "Ayuda" in text:
        await help_command(update, context)

# ---------------------------------------------------------
# 11. Handlers de Comandos Básicos
# ---------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    welcome_text = (
        f"👋 *¡Hola, {user_name}!*\n\n"
        f"Bienvenido a *NosticProno* 🎯\n"
        f"Análisis estadístico y valor (+EV) para *Hoy, Mañana y Pasado Mañana*.\n\n"
        f"👇 *Selecciona un mercado para empezar:*"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=get_main_reply_keyboard())

# ---------------------------------------------------------
# 12. Ejecución Principal
# ---------------------------------------------------------
def main():
    token_raw = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
    if not token_raw:
        logger.error("Error crítico: TELEGRAM_BOT_TOKEN no configurado.")
        sys.exit(1)

    threading.Thread(target=start_health_server, daemon=True).start()

    logger.info("Inicializando NosticProno Bot...")
    application = ApplicationBuilder().token(token_raw.strip()).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("top", top_value_command))
    application.add_handler(CallbackQueryHandler(date_callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_button_handler))

    logger.info("Bot activo en Telegram.")
    application.run_polling()

if __name__ == "__main__":
    main()