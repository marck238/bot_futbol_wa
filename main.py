import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from sqlalchemy import select

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes
)

from database import init_db, AsyncSessionLocal, User, Filter
from analytics import calculate_kelly_stake
from odds_api import get_upcoming_ev_picks, get_best_parlays

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")


def get_main_menu_keyboard():
    """Genera la botonera principal del bot."""
    keyboard = [
        [
            InlineKeyboardButton("⏳ Próximas 4 Horas", callback_data="time_4h"),
            InlineKeyboardButton("📅 Hoy", callback_data="time_today"),
        ],
        [
            InlineKeyboardButton("📆 Mañana", callback_data="time_tomorrow"),
            InlineKeyboardButton("📆 Pasado Mañana", callback_data="time_pasado_manana"),
        ],
        [
            InlineKeyboardButton("⚽ Próximo Finde", callback_data="time_weekend"),
        ],
        [
            InlineKeyboardButton("⭐ Las Mejores de Hoy", callback_data="top_today"),
            InlineKeyboardButton("💎 Top 3 Días (EV+)", callback_data="top_3days"),
        ],
        [
            InlineKeyboardButton("🎟️ Combinadas EV+", callback_data="menu_parlays"),
            InlineKeyboardButton("⚙️ Mis Filtros", callback_data="menu_filters"),
        ],
        [
            InlineKeyboardButton("📊 Calculadora Kelly", callback_data="calc_kelly"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_parlays_menu_keyboard():
    """Genera el submenú de opciones para apuestas combinadas."""
    keyboard = [
        [
            InlineKeyboardButton("🎟️ Combinada de Hoy", callback_data="parlay_today"),
            InlineKeyboardButton("🎟️ Combinada de Mañana", callback_data="parlay_tomorrow"),
        ],
        [
            InlineKeyboardButton("🔙 Volver al Menú Principal", callback_data="main_menu")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


async def get_or_create_user(telegram_id: int):
    """Registra o recupera al usuario en la base de datos Neon PostgreSQL."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if not user:
            role = "ADMIN" if ADMIN_ID and str(telegram_id) == str(ADMIN_ID) else "USER"
            user = User(telegram_id=telegram_id, role=role, status="ACTIVE")
            session.add(user)
            await session.commit()
            await session.refresh(user)
        return user


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manejador del comando /start."""
    telegram_id = update.effective_user.id

    if ADMIN_ID and str(telegram_id) != str(ADMIN_ID):
        await update.message.reply_text("⛔ Acceso denegado. Este bot es de uso privado.")
        return

    await get_or_create_user(telegram_id)
    welcome_text = (
        "⚽ **Panel de Pronósticos Pre-Partido — NosticProno**\n\n"
        "Selecciona una opción para escanear eventos individuales o armar **combinadas de valor (EV+)**:"
    )
    await update.message.reply_text(
        welcome_text,
        parse_mode="Markdown",
        reply_markup=get_main_menu_keyboard()
    )


async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manejador central de eventos para los botones interactivos."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "time_4h":
        await render_predictions(query, title="⏳ Partidos en las Próximas 4 Horas", hours=4)
    elif data == "time_today":
        await render_predictions(query, title="📅 Pronósticos para Hoy", hours=24)
    elif data == "time_tomorrow":
        await render_predictions(query, title="📆 Pronósticos para Mañana", days_offset=1)
    elif data == "time_pasado_manana":
        await render_predictions(query, title="📆 Pronósticos para Pasado Mañana", days_offset=2)
    elif data == "time_weekend":
        await render_predictions(query, title="⚽ Pronósticos para el Próximo Fin de Semana", is_weekend=True)
    elif data == "top_today":
        await render_predictions(query, title="⭐ LAS MEJORES APUESTAS DE HOY (EV+ Máximo)", hours=24, min_ev_filter=5.0)
    elif data == "top_3days":
        await render_predictions(query, title="💎 TOP PICKS DE LOS PRÓXIMOS 3 DÍAS", hours=72, min_ev_filter=6.0)
    elif data == "menu_parlays":
        msg = (
            "🎟️ **Panel de Apuestas Combinadas (EV+)**\n\n"
            "El bot selecciona automáticamente las mejores opciones con EV+ positivo "
            "(mínimo 2 y máximo 4 partidos por ticket) y calcula la cuota acumulada y el stake óptimo."
        )
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=get_parlays_menu_keyboard())
    elif data == "parlay_today":
        await render_parlays(query, title="🎟️ Combinada de Hoy", hours=24)
    elif data == "parlay_tomorrow":
        await render_parlays(query, title="🎟️ Combinada de Mañana", days_offset=1)
    elif data == "menu_filters":
        await show_user_filters(query)
    elif data == "calc_kelly":
        msg = (
            "📊 **Calculadora de Criterio de Kelly**\n\n"
            "Para usar la calculadora envía:\n"
            "`/kelly <probabilidad_%> <cuota_casa> [tu_banca]`\n\n"
            "Ejemplo:\n`/kelly 65 1.90 1000`"
        )
        back_button = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data="main_menu")]])
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=back_button)
    elif data == "main_menu":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        welcome_text = (
            "⚽ **Panel de Pronósticos Pre-Partido — NosticProno**\n\n"
            "Selecciona un rango de tiempo para escanear eventos con Valor Esperado Positivo (EV+):"
        )
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=welcome_text,
            parse_mode="Markdown",
            reply_markup=get_main_menu_keyboard()
        )


async def render_predictions(
    query,
    title: str,
    hours: int = None,
    days_offset: int = None,
    is_weekend: bool = False,
    min_ev_filter: float = 0.0
):
    """Renderiza predicciones de partidos individuales."""
    back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú Principal", callback_data="main_menu")]])
    await query.edit_message_text(f"🔍 *Escaneando eventos y cuotas reales para {title}...*", parse_mode="Markdown")

    api_res = await get_upcoming_ev_picks(
        hours=hours,
        days_offset=days_offset,
        is_weekend=is_weekend,
        min_ev=min_ev_filter
    )

    if api_res["status"] == "NO_API_KEY":
        msg = (
            f"📊 **{title}**\n\n"
            "⚠️ **API Key no detectada**\n"
            "Agrega la variable `ODDS_API_KEY` en Render."
        )
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=back_keyboard)
        return

    picks = api_res["data"]
    response = f"📊 **{title}**\n\n"

    if not picks:
        response += "⚠️ No se encontraron partidos con Valor Esperado positivo (EV+) para este rango de tiempo."
    else:
        for idx, item in enumerate(picks, start=1):
            response += (
                f"**{idx}. {item['match']}** ({item['league']})\n"
                f"⏰ Hora: `{item['time']}` | Mercado: `{item['market']}`\n"
                f"📈 Prob. Estimada: `{item['prob']}%` | Cuota Casa: `{item['bookmaker_odd']}`\n"
                f"🔥 **EV: `+{item['ev']}%`** | Stake Sugerido: `{item['stake']}%`\n"
                f"───────────────\n"
            )

    await query.edit_message_text(response, parse_mode="Markdown", reply_markup=back_keyboard)


async def render_parlays(query, title: str, hours: int = None, days_offset: int = None):
    """Renderiza el ticket de la mejor apuesta combinada detectada."""
    back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú de Combinadas", callback_data="menu_parlays")]])
    await query.edit_message_text(f"🔍 *Calculando la mejor combinada con EV+ para {title}...*", parse_mode="Markdown")

    res = await get_best_parlays(hours=hours, days_offset=days_offset)

    if res["status"] == "NO_API_KEY":
        msg = "⚠️ **API Key no detectada.** Por favor configura `ODDS_API_KEY` en Render."
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=back_keyboard)
        return

    parlay = res.get("parlay")

    if not parlay:
        msg = f"🎟️ **{title}**\n\n⚠️ No hay suficientes partidos independientes con EV+ positivo en este rango para armar un ticket combinado (se requieren al menos 2 partidos distintos)."
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=back_keyboard)
        return

    response = (
        f"🎟️ **{title} ({parlay['legs_count']} Partidos)**\n"
        f"───────────────\n"
    )

    for idx, leg in enumerate(parlay["legs"], start=1):
        response += (
            f"📌 **Leg {idx}: {leg['match']}** ({leg['league']})\n"
            f"⏰ Hora: `{leg['time']}` | Mercado: `{leg['market']}`\n"
            f"📈 Prob. Individual: `{leg['prob']}%` | Cuota: `{leg['bookmaker_odd']}`\n"
            f"───────────────\n"
        )

    response += (
        f"📊 **RESUMEN DE LA COMBINADA:**\n"
        f"🏛️ **Cuota Total Acumulada: `{parlay['total_odd']}`**\n"
        f"📈 **Probabilidad Conjunta: `{parlay['total_prob_pct']}%`**\n"
        f"🔥 **Valor Esperado Acumulado (EV): `+{parlay['total_ev']}%`**\n"
        f"💵 **Stake Recomendado: `{parlay['recommended_stake_pct']}%` de banca**\n\n"
        f"💡 *Nota: Se aplica un Kelly Fraccionado conservador debido a la varianza de apuestas múltiples.*"
    )

    await query.edit_message_text(response, parse_mode="Markdown", reply_markup=back_keyboard)


async def show_user_filters(query):
    """Muestra los filtros guardados del usuario."""
    telegram_id = query.from_user.id
    async with AsyncSessionLocal() as session:
        res_user = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = res_user.scalar_one_or_none()
        back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data="main_menu")]])

        if not user:
            await query.edit_message_text("❌ Usuario no registrado.", reply_markup=back_keyboard)
            return

        res_filters = await session.execute(select(Filter).where(Filter.user_id == user.id))
        filters = res_filters.scalars().all()

        if not filters:
            msg = "⚙️ **Mis Filtros Guardados**\n\nNo tienes filtros configurados."
            await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=back_keyboard)
            return

        msg = "⚙️ **Tus Filtros Pre-Partido Activos:**\n\n"
        for f in filters:
            msg += f"🔹 **{f.name}** [{f.market}] | Min EV: `+{f.min_ev}%`\n"
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=back_keyboard)


async def kelly_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /kelly."""
    if len(context.args) < 2:
        await update.message.reply_text("Uso: `/kelly <probabilidad_%> <cuota> [banca]`", parse_mode="Markdown")
        return

    try:
        prob = float(context.args[0])
        odd = float(context.args[1])
        bankroll = float(context.args[2]) if len(context.args) >= 3 else 1000.0

        res = calculate_kelly_stake(prob, odd, bankroll)
        if not res.get("has_value"):
            await update.message.reply_text(f"⚠️️ {res['message']}")
            return

        msg = (
            f"📈 **Criterio de Kelly Pre-Partido**\n\n"
            f"🔹 EV: `+{res['expected_value_pct']}%`\n"
            f"🎯 Stake Sugerido: `{res['recommended_stake_pct']}%`\n"
            f"💵 Monto: `${res['recommended_amount']}`"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Ingresa valores numéricos válidos.")


# Aplicación Telegram y FastAPI
telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CommandHandler("kelly", kelly_command))
telegram_app.add_handler(CallbackQueryHandler(button_callback_handler))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    print(">>> Tablas Pre-Partido inicializadas en Neon DB <<<")
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=True)
    print(">>> NosticProno listo con módulo de combinadas EV+ <<<")
    yield
    await telegram_app.updater.stop()
    await telegram_app.stop()
    await telegram_app.shutdown()


app = FastAPI(lifespan=lifespan)


@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/health", methods=["GET", "HEAD"])
def health_check():
    return {"status": "ok", "bot": "NosticProno", "state": "online"}