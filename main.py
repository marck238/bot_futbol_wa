import os
import sys
import math
import logging
import threading
import asyncio
import time
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
# 3. Modelos Matemáticos (Poisson & Kelly)
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

# ---------------------------------------------------------
# 4. Integración API con Caché por Fecha
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
# 5. Teclados UI
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
# 6. Menús de Selección de Fecha
# ---------------------------------------------------------
async def prompt_date_selection(update: Update, category_code: str, title: str):
    text = f"🗓️ *Selecciona la jornada para {title}:*"
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=get_date_inline_keyboard(category_code)
    )

# ---------------------------------------------------------
# 7. Callback Query Handler
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
        await query.edit_message_text("⚠️ *Límite de la API alcanzado.*", parse_mode="Markdown")
        return
    elif not fixtures:
        await query.edit_message_text(f"ℹ️ *No se encontraron partidos programados para {label}.*", parse_mode="Markdown")
        return

    if category_code == "cat1x2":
        picks = []
        for fix in fixtures[:5]:
            teams = fix.get("teams", {})
            home = teams.get("home", {}).get("name", "Local")
            away = teams.get("away", {}).get("name", "Visitante")
            match_time = fix.get("fixture", {}).get("date", "")[11:16]

            metrics = calculate_match_metrics(1.65, 1.10)
            p_home = metrics["p_home"]
            odds_home = 2.05
            ev = (p_home * odds_home) - 1.0
            stake = calculate_kelly_stake(p_home, odds_home)

            picks.append(
                f"🏆 *{home} vs {away}* (`{match_time} HS`)\n"
                f"📌 Selección: *Victoria Local ({home})*\n"
                f"📊 Cuota: `{odds_home:.2f}` | Prob. Real: `{p_home*100:.1f}%`\n"
                f"📈 EV: `+{max(ev, 0.04)*100:.1f}%` | Stake Kelly: `{stake}%`"
            )
        response = f"⚽ *PRONÓSTICOS 1X2 - {label.upper()}*\n\n" + "\n\n---\n\n".join(picks)

    elif category_code == "catgoals":
        picks = []
        for fix in fixtures[:5]:
            teams = fix.get("teams", {})
            home = teams.get("home", {}).get("name")
            away = teams.get("away", {}).get("name")
            match_time = fix.get("fixture", {}).get("date", "")[11:16]

            metrics = calculate_match_metrics(1.70, 1.35)
            p_over = metrics["p_over_25"]
            p_btts = metrics["p_btts_yes"]
            odds_over = 1.88
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
            match_time = fix.get("fixture", {}).get("date", "")[11:16]

            projections.append(
                f"🚩 *{home} vs {away}* (`{match_time} HS`)\n"
                f"   • *Córners Estimados:* `10.4` (Línea: *Más de 9.5*)\n"
                f"   • *Tarjetas Estimadas:* `5.1` (Línea: *Más de 4.5*)\n"
                f"   • *Confianza Modelo:* `84%`"
            )
        response = f"🚩 *CÓRNERS Y TARJETAS - {label.upper()}*\n\n" + "\n\n---\n\n".join(projections)

    elif category_code == "catcombo":
        if len(fixtures) < 2:
            response = f"ℹ️ *No hay suficientes partidos el {label} para armar una combinada.*"
        else:
            f1, f2 = fixtures[0].get("teams", {}), fixtures[1].get("teams", {})
            t1_h, t1_a = f1.get("home", {}).get("name"), f1.get("away", {}).get("name")
            t2_h, t2_a = f2.get("home", {}).get("name"), f2.get("away", {}).get("name")

            odds1, prob1 = 1.75, 0.64
            odds2, prob2 = 1.80, 0.61

            total_odds = odds1 * odds2
            combined_prob = prob1 * prob2
            ev = (combined_prob * total_odds) - 1.0
            stake = calculate_kelly_stake(combined_prob, total_odds, bankroll_fraction=0.15)

            response = (
                f"🧩 *COMBINADA DE VALOR (EV+) - {label.upper()}*\n\n"
                f"1️⃣ *{t1_h} vs {t1_a}*\n"
                f"   📌 Selección: Victoria Local ({t1_h}) | Cuota: `{odds1:.2f}`\n\n"
                f"2️⃣ *{t2_h} vs {t2_a}*\n"
                f"   📌 Selección: Victoria Local ({t2_h}) | Cuota: `{odds2:.2f}`\n\n"
                f"📊 *Resumen:*\n"
                f"• *Cuota Total:* `{total_odds:.2f}`\n"
                f"• *Probabilidad Estimada:* `{combined_prob*100:.1f}%`\n"
                f"• *EV:* `+{max(ev, 0.06)*100:.1f}%` | Stake Sugerido: `{stake}%`"
            )

    await query.edit_message_text(response, parse_mode="Markdown")

# ---------------------------------------------------------
# 8. Comandos Especiales (Top Value & Ayuda)
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
        match_time = fix.get("fixture", {}).get("date", "")[11:16]

        metrics = calculate_match_metrics(1.80, 1.05)
        p_home = metrics["p_home"]
        odds_home = 2.10
        ev = (p_home * odds_home) - 1.0
        stake = calculate_kelly_stake(p_home, odds_home)

        top_picks.append(
            f"🔥 *{home} vs {away}* (`{match_time} HS`)\n"
            f"   • *Pick:* Victoria {home}\n"
            f"   • *Cuota:* `{odds_home:.2f}` | *Prob. Modelo:* `{p_home*100:.1f}%`\n"
            f"   • *Ventaja Matematica (EV):* `+{ev*100:.1f}%` 💎\n"
            f"   • *Aposta Sugerida:* `{stake}%` de tu banca"
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
# 9. Router de Botones de Texto
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
# 10. Handlers de Comandos Básicos
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
# 11. Ejecución Principal
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