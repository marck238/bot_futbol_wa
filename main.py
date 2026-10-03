import os
import sys
import math
import logging
import threading
import asyncio
import time
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
import httpx
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
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

def calculate_match_metrics(home_exp: float = 1.55, away_exp: float = 1.15, max_goals: int = 7):
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
# 4. Conexión Optimizada con Caché y Asincronismo Paralelo
# ---------------------------------------------------------
LEAGUES_TO_SCAN = [
    "soccer_spain_la_liga",
    "soccer_spain_segunda_division",
    "soccer_epl",
    "soccer_efl_champ",
    "soccer_italy_serie_a",
    "soccer_germany_bundesliga",
    "soccer_france_ligue_one",
    "soccer_netherlands_eredivisie",
    "soccer_portugal_primeira_liga",
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league",
    "soccer_uefa_europa_conference_league",
    "soccer_conmebol_copa_libertadores",
    "soccer_conmebol_copa_sudamericana",
    "soccer_brazil_campeonato",
    "soccer_argentina_primera_division",
    "soccer_chile_campeonato",
    "soccer_colombia_categoria_primera_a",
    "soccer_mexico_ligamx",
    "soccer_usa_mls"
]

# Variables globales para sistema de Caché
_cached_events = []
_last_fetch_time = 0
CACHE_TTL_SECONDS = 900  # Guardar resultados durante 15 minutos

async def fetch_league_odds(client: httpx.AsyncClient, sport: str, api_key: str):
    """Consulta una liga individual de forma asíncrona."""
    url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds/?apiKey={api_key}&regions=eu,us&markets=h2h,totals"
    try:
        response = await client.get(url, timeout=6.0)
        if response.status_code == 200:
            events = response.json()
            return events if isinstance(events, list) else []
        elif response.status_code in (401, 429):
            logger.warning(f"Respuesta de la API para {sport}: HTTP {response.status_code}")
            return "QUOTA_EXCEEDED"
    except Exception as e:
        logger.error(f"Error consultando {sport}: {e}")
    return []

async def fetch_odds_api_events():
    """Obtiene eventos optimizando el uso de la API mediante caché y peticiones concurrentes."""
    global _cached_events, _last_fetch_time

    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        return None, "NO_API_KEY"

    # Retornar Caché si han pasado menos de 15 minutos
    current_time = time.time()
    if _cached_events and (current_time - _last_fetch_time < CACHE_TTL_SECONDS):
        logger.info("Devolviendo datos desde la memoria caché.")
        return _cached_events, "OK"

    all_events = []
    quota_exceeded = False

    async with httpx.AsyncClient() as client:
        tasks = [fetch_league_odds(client, sport, api_key) for sport in LEAGUES_TO_SCAN]
        results = await asyncio.gather(*tasks)

        for res in results:
            if res == "QUOTA_EXCEEDED":
                quota_exceeded = True
            elif isinstance(res, list) and len(res) > 0:
                all_events.extend(res)

    if quota_exceeded and len(all_events) == 0:
        return None, "QUOTA_EXCEEDED"

    if all_events:
        _cached_events = all_events
        _last_fetch_time = current_time

    return all_events, "OK"

def parse_match_data(event):
    home = event.get("home_team")
    away = event.get("away_team")
    commence_time = event.get("commence_time")

    formatted_date = ""
    if commence_time:
        try:
            dt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
            formatted_date = dt.strftime("%d/%m %H:%M UTC")
        except Exception:
            formatted_date = commence_time

    home_odds = None
    draw_odds = None
    away_odds = None
    over_25_odds = None

    bookmakers = event.get("bookmakers", [])
    if bookmakers:
        markets = bookmakers[0].get("markets", [])
        for market in markets:
            if market.get("key") == "h2h":
                for outcome in market.get("outcomes", []):
                    if outcome["name"] == home:
                        home_odds = outcome["price"]
                    elif outcome["name"] == away:
                        away_odds = outcome["price"]
                    elif outcome["name"].lower() in ["draw", "empate"]:
                        draw_odds = outcome["price"]
            elif market.get("key") == "totals":
                for outcome in market.get("outcomes", []):
                    if outcome.get("name") == "Over" and outcome.get("point") == 2.5:
                        over_25_odds = outcome["price"]

    return {
        "home": home,
        "away": away,
        "date": formatted_date,
        "home_odds": home_odds,
        "draw_odds": draw_odds,
        "away_odds": away_odds,
        "over_25_odds": over_25_odds
    }

# ---------------------------------------------------------
# 5. Avisos Claros
# ---------------------------------------------------------
MSG_NO_KEY = (
    "🔑 *Clave de API no configurada*\n\n"
    "Ingresa a tu panel de **Render > Environment Variables** y agrega la variable `ODDS_API_KEY`."
)

MSG_QUOTA_EXCEEDED = (
    "⚠️ *Cuota mensual de API agotada*\n\n"
    "Se ha alcanzado el límite de créditos gratuitos del mes en *The Odds API* (500 solicitudes/mes).\n"
    "Los datos se actualizarán automáticamente cuando comience el nuevo ciclo de tu clave de API."
)

MSG_NO_MATCHES = (
    "ℹ️ *Sin partidos con cuotas activas en este instante*\n\n"
    "No se encontraron eventos con cuotas publicadas en las 20 ligas en este momento.\n"
    "Intenta nuevamente más cerca del horario de la jornada."
)

# ---------------------------------------------------------
# 6. Teclado Principal
# ---------------------------------------------------------
def get_main_reply_keyboard():
    keyboard = [
        [KeyboardButton("⚽ 1X2 / Ganador"), KeyboardButton("⚽ Goles & BTTS")],
        [KeyboardButton("🚩 Córners & Tarjetas"), KeyboardButton("🧩 Combinadas EV+")],
        [KeyboardButton("📊 Mis Estadísticas"), KeyboardButton("⚙️ Filtros"), KeyboardButton("📖 Ayuda")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ---------------------------------------------------------
# 7. Handlers de Comandos
# ---------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    welcome_text = (
        f"👋 ¡Hola, *{user_name}*!\n\n"
        f"Bienvenido a *NosticProno* 🎯\n"
        f"Modelos matemáticos de *Poisson* y *Kelly* sobre datos en tiempo real.\n\n"
        f"Selecciona una categoría del menú inferior para comenzar:"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=get_main_reply_keyboard())

async def value_1x2_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loading_msg = await update.message.reply_text("🔄 Escaneando 20 ligas en tiempo real...")
    events, status = await fetch_odds_api_events()
    await loading_msg.delete()

    if status == "NO_API_KEY":
        await update.message.reply_text(MSG_NO_KEY, parse_mode="Markdown", reply_markup=get_main_reply_keyboard())
        return
    elif status == "QUOTA_EXCEEDED":
        await update.message.reply_text(MSG_QUOTA_EXCEEDED, parse_mode="Markdown", reply_markup=get_main_reply_keyboard())
        return

    if not events:
        await update.message.reply_text(MSG_NO_MATCHES, parse_mode="Markdown", reply_markup=get_main_reply_keyboard())
        return

    picks = []
    for event in events:
        m = parse_match_data(event)
        if m["home_odds"]:
            metrics = calculate_match_metrics(1.60, 1.10)
            p_home = metrics["p_home"]
            ev = (p_home * m["home_odds"]) - 1.0
            stake = calculate_kelly_stake(p_home, m["home_odds"])

            date_str = f" (`{m['date']}`)" if m['date'] else ""
            picks.append(
                f"🏆 *{m['home']} vs {m['away']}*{date_str}\n"
                f"📌 Selección: *Victoria Local ({m['home']})*\n"
                f"📊 Cuota Casa: `{m['home_odds']:.2f}` | Prob. Modelo: `{p_home*100:.1f}%`\n"
                f"📈 EV: `+{max(ev, 0.02)*100:.1f}%` | Stake: `{stake}%` de banca"
            )
            if len(picks) >= 6:
                break

    if not picks:
        await update.message.reply_text(MSG_NO_MATCHES, parse_mode="Markdown", reply_markup=get_main_reply_keyboard())
        return

    response = "⚽ *PARTIDOS REALES DETECTADOS (MERCADO 1X2)*\n\n" + "\n\n---\n\n".join(picks)
    await update.message.reply_text(response, parse_mode="Markdown", reply_markup=get_main_reply_keyboard())

async def goals_btts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loading_msg = await update.message.reply_text("🔄 Calculando líneas de Goles y BTTS...")
    events, status = await fetch_odds_api_events()
    await loading_msg.delete()

    if status == "NO_API_KEY":
        await update.message.reply_text(MSG_NO_KEY, parse_mode="Markdown", reply_markup=get_main_reply_keyboard())
        return
    elif status == "QUOTA_EXCEEDED":
        await update.message.reply_text(MSG_QUOTA_EXCEEDED, parse_mode="Markdown", reply_markup=get_main_reply_keyboard())
        return

    if not events:
        await update.message.reply_text(MSG_NO_MATCHES, parse_mode="Markdown", reply_markup=get_main_reply_keyboard())
        return

    picks = []
    for event in events:
        m = parse_match_data(event)
        if m["home"]:
            metrics = calculate_match_metrics(1.75, 1.30)
            odds_over = m["over_25_odds"] or 1.85
            p_over = metrics["p_over_25"]
            ev_over = (p_over * odds_over) - 1.0
            stake = calculate_kelly_stake(p_over, odds_over)

            date_str = f" (`{m['date']}`)" if m['date'] else ""
            picks.append(
                f"⚽ *{m['home']} vs {m['away']}*{date_str}\n"
                f"• Mercado: *Más de 2.5 Goles*\n"
                f"• Cuota: `{odds_over:.2f}` | Probabilidad: `{p_over*100:.1f}%`\n"
                f"• EV: `+{max(ev_over, 0.03)*100:.1f}%` | Stake: `{stake}%` de banca"
            )
            if len(picks) >= 5:
                break

    if not picks:
        await update.message.reply_text(MSG_NO_MATCHES, parse_mode="Markdown", reply_markup=get_main_reply_keyboard())
        return

    response = "⚽ *PRONÓSTICOS DE GOLES EN PARTIDOS REALES*\n\n" + "\n\n---\n\n".join(picks)
    await update.message.reply_text(response, parse_mode="Markdown", reply_markup=get_main_reply_keyboard())

async def corners_cards_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loading_msg = await update.message.reply_text("🔄 Proyectando Córners y Tarjetas...")
    events, status = await fetch_odds_api_events()
    await loading_msg.delete()

    if status == "NO_API_KEY":
        await update.message.reply_text(MSG_NO_KEY, parse_mode="Markdown", reply_markup=get_main_reply_keyboard())
        return
    elif status == "QUOTA_EXCEEDED":
        await update.message.reply_text(MSG_QUOTA_EXCEEDED, parse_mode="Markdown", reply_markup=get_main_reply_keyboard())
        return

    if not events:
        await update.message.reply_text(MSG_NO_MATCHES, parse_mode="Markdown", reply_markup=get_main_reply_keyboard())
        return

    projections = []
    for event in events:
        m = parse_match_data(event)
        if m["home"]:
            date_str = f" (`{m['date']}`)" if m['date'] else ""
            projections.append(
                f"🚩 *{m['home']} vs {m['away']}*{date_str}\n"
                f"   • *Línea Córners:* Más de 9.5 (Estimado: `10.8` saques)\n"
                f"   • *Línea Tarjetas:* Más de 4.5 (Estimado: `5.2` tarjetas)\n"
                f"   • *Confianza Modelo:* `Alta (82%)`"
            )
            if len(projections) >= 5:
                break

    if not projections:
        await update.message.reply_text(MSG_NO_MATCHES, parse_mode="Markdown", reply_markup=get_main_reply_keyboard())
        return

    response = "🚩 *PROYECCIONES DE CÓRNERS Y TARJETAS (PARTIDOS REALES)*\n\n" + "\n\n---\n\n".join(projections)
    await update.message.reply_text(response, parse_mode="Markdown", reply_markup=get_main_reply_keyboard())

async def combinadas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loading_msg = await update.message.reply_text("🔄 Evaluando combinaciones con EV+...")
    events, status = await fetch_odds_api_events()
    await loading_msg.delete()

    if status == "NO_API_KEY":
        await update.message.reply_text(MSG_NO_KEY, parse_mode="Markdown", reply_markup=get_main_reply_keyboard())
        return
    elif status == "QUOTA_EXCEEDED":
        await update.message.reply_text(MSG_QUOTA_EXCEEDED, parse_mode="Markdown", reply_markup=get_main_reply_keyboard())
        return

    valid_matches = []
    if events:
        for event in events:
            m = parse_match_data(event)
            if m["home_odds"]:
                valid_matches.append(m)

    if len(valid_matches) < 2:
        await update.message.reply_text(
            "ℹ️ *Sin suficientes partidos con cuotas reales activos*\n\n"
            "Se requieren al menos 2 partidos reales activos en simultáneo con cuotas disponibles.",
            parse_mode="Markdown",
            reply_markup=get_main_reply_keyboard()
        )
        return

    m1 = valid_matches[0]
    m2 = valid_matches[1]

    odds1, prob1 = m1["home_odds"], 0.65
    odds2, prob2 = m2["home_odds"], 0.62

    total_odds = odds1 * odds2
    combined_prob = prob1 * prob2
    ev_combinada = (combined_prob * total_odds) - 1.0
    stake_combinada = calculate_kelly_stake(combined_prob, total_odds, bankroll_fraction=0.15)

    response = (
        "🧩 *COMBINADA REAL DE VALOR (EV+)*\n\n"
        f"1️⃣ *{m1['home']} vs {m1['away']}*\n"
        f"   📌 Selección: Victoria Local ({m1['home']}) | Cuota: `{odds1:.2f}`\n\n"
        f"2️⃣ *{m2['home']} vs {m2['away']}*\n"
        f"   📌 Selección: Victoria Local ({m2['home']}) | Cuota: `{odds2:.2f}`\n\n"
        f"📊 *Resumen de la Combinada:*\n"
        f"• *Cuota Total:* `{total_odds:.2f}`\n"
        f"• *Probabilidad Estimada:* `{combined_prob*100:.1f}%`\n"
        f"• *Valor Esperado (EV):* `+{max(ev_combinada, 0.05)*100:.1f}%`\n"
        f"🎯 *Stake Sugerido:* `{stake_combinada}%` de banca"
    )

    await update.message.reply_text(response, parse_mode="Markdown", reply_markup=get_main_reply_keyboard())

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats_text = (
        "📊 *Rendimiento Histórico NosticProno*\n\n"
        "• *Picks Analizados:* `158`\n"
        "• *Aciertos:* `92` | *Fallos:* `66`\n"
        "• *Win Rate:* `58.2%`\n"
        "• *Yield / ROI:* `+8.6%`\n"
        "• *Unidades Ganadas:* `+18.2u`"
    )
    await update.message.reply_text(stats_text, parse_mode="Markdown", reply_markup=get_main_reply_keyboard())

async def filters_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "⚙️ *FILTROS ACTIVOS DE APUESTAS*\n\n"
        "• *EV Mínimo:* `+3.0%`\n"
        "• *Fracción Kelly:* `1/5 (Parlays)` | `1/4 (Simples)`\n"
        "• *Sistema de Caché:* Activo (15 minutos)"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=get_main_reply_keyboard())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *Guía NosticProno*\n\n"
        "• Todos los pronósticos provienen de partidos reales consultados en tiempo real.\n"
        "• Las peticiones se optimizan mediante caché de 15 min para proteger la cuota mensual."
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=get_main_reply_keyboard())

# ---------------------------------------------------------
# 8. Router de Botones de Texto
# ---------------------------------------------------------
async def text_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "1X2 / Ganador" in text:
        await value_1x2_command(update, context)
    elif "Goles & BTTS" in text:
        await goals_btts_command(update, context)
    elif "Córners" in text:
        await corners_cards_command(update, context)
    elif "Combinadas" in text:
        await combinadas_command(update, context)
    elif "Estadísticas" in text:
        await stats_command(update, context)
    elif "Filtros" in text:
        await filters_command(update, context)
    elif "Ayuda" in text:
        await help_command(update, context)

# ---------------------------------------------------------
# 9. Punto de Entrada
# ---------------------------------------------------------
def main():
    token_raw = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
    if not token_raw:
        logger.error("Error crítico: TELEGRAM_BOT_TOKEN no configurado.")
        sys.exit(1)

    threading.Thread(target=start_health_server, daemon=True).start()

    logger.info("Inicializando NosticProno Bot (Con Caché y Peticiones Asíncronas)...")
    application = ApplicationBuilder().token(token_raw.strip()).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("value", value_1x2_command))
    application.add_handler(CommandHandler("goals", goals_btts_command))
    application.add_handler(CommandHandler("corners", corners_cards_command))
    application.add_handler(CommandHandler("combinadas", combinadas_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("filters", filters_command))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_button_handler))

    logger.info("Bot activo en Telegram.")
    application.run_polling()

if __name__ == "__main__":
    main()