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
from odds_api import get_upcoming_ev_picks

# Configuración de variables de entorno
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")


def get_main_menu_keyboard():
    """Genera la botonera principal con opciones de navegación."""
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
            InlineKeyboardButton("⚙️ Mis Filtros", callback_data="menu_filters"),
            InlineKeyboardButton("📊 Calculadora Kelly", callback_data="calc_kelly"),
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
    """Manejador del comando /start. Muestra la bienvenida y el menú interactivo."""
    telegram_id = update.effective_user.id

    # Control de acceso opcional para modo privado
    if ADMIN_ID and str(telegram_id) != str(ADMIN_ID):
        await update.message.reply_text("⛔ Acceso denegado. Este bot es de uso privado.")
        return

    await get_or_create_user(telegram_id)
    welcome_text = (
        "⚽ **Panel de Pronósticos Pre-Partido — NosticProno**\n\n"
        "Selecciona un rango de tiempo para escanear los partidos programados, "
        "las cuotas reales y encontrar pronósticos con Valor Esperado Positivo (EV+):"
    )
    await update.message.reply_text(
        welcome_text,
        parse_mode="Markdown",
        reply_markup=get_main_menu_keyboard()
    )


async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manejador global de eventos al presionar cualquier botón interactivo."""
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
        # 1. Remueve el botón del mensaje de pronósticos para dejarlo limpio en el historial
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        # 2. Envía la botonera principal como un MENSAJE NUEVO abajo
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
    """Escanea eventos reales en The Odds API y renderiza los resultados en el chat."""
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
            "Para recibir pronósticos con cuotas reales en tiempo real, agrega la variable `ODDS_API_KEY` en Render.\n\n"
            "📌 *Consíguela gratis en [the-odds-api.com](https://the-odds-api.com)*"
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
                f"⏰ Hora: `{item['time']}`\n"
                f"🎯 Mercado: `{item['market']}`\n"
                f"📈 Prob. Estimada: `{item['prob']}%` | Cuota Justa: `{item['fair_odd']}`\n"
                f"🏛️ Cuota Casa: `{item['bookmaker_odd']}`\n"
                f"🔥 **EV (Valor Esperado): `+{item['ev']}%`**\n"
                f"💵 **Stake Recomendado: `{item['stake']}%` de banca**\n"
                f"───────────────\n"
            )

    await query.edit_message_text(response, parse_mode="Markdown", reply_markup=back_keyboard)


async def show_user_filters(query):
    """Consulta y muestra los filtros personalizados del usuario desde Neon DB."""
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
            msg = (
                "⚙️ **Mis Filtros Guardados**\n\n"
                "No tienes filtros configurados.\n"
                "Para añadir uno envía:\n"
                "`/crear_filtro <nombre> <mercado> <min_prob_%> <min_cuota> <min_ev_%>`\n\n"
                "Ejemplo:\n`/crear_filtro MisGoles OVER_2.5 60 1.75 4`"
            )
            await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=back_keyboard)
            return

        msg = "⚙️ **Tus Filtros Pre-Partido Activos:**\n\n"
        for f in filters:
            msg += (
                f"🔹 **{f.name}** [{f.market}]\n"
                f"  • Prob. Mínima: `{f.min_expected_prob}%` | Cuota Mínima: `{f.min_odd}`\n"
                f"  • EV Mínimo: `+{f.min_ev}%` | Estado: `{'🟢 Activo' if f.is_active else '🔴 Inactivo'}`\n\n"
            )
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=back_keyboard)


async def kelly_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /kelly para calcular el stake óptimo sobre cualquier evento."""
    if len(context.args) < 2:
        await update.message.reply_text(
            "Uso: `/kelly <probabilidad_%> <cuota> [banca]`\nEjemplo: `/kelly 62 1.90 1000`",
            parse_mode="Markdown"
        )
        return

    try:
        prob = float(context.args[0])
        odd = float(context.args[1])
        bankroll = float(context.args[2]) if len(context.args) >= 3 else 1000.0

        res = calculate_kelly_stake(prob, odd, bankroll)
        if not res.get("has_value"):
            await update.message.reply_text(f"⚠️ {res['message']}")
            return

        msg = (
            f"📈 **Criterio de Kelly Pre-Partido**\n\n"
            f"🔹 Valor Esperado (EV): `+{res['expected_value_pct']}%`\n"
            f"🎯 Stake Sugerido (1/4 Kelly): `{res['recommended_stake_pct']}%`\n"
            f"💵 Monto a Apostar: `${res['recommended_amount']}`"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Ingresa valores numéricos válidos.")


# Configuración del bot de Telegram
telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CommandHandler("kelly", kelly_command))
telegram_app.add_handler(CallbackQueryHandler(button_callback_handler))


# Ciclo de vida de FastAPI para Render
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    print(">>> Tablas Pre-Partido inicializadas en Neon DB <<<")
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=True)
    print(">>> NosticProno listo con menú, cuotas en tiempo real e historial activo <<<")
    yield
    await telegram_app.updater.stop()
    await telegram_app.stop()
    await telegram_app.shutdown()


app = FastAPI(lifespan=lifespan)


@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/health", methods=["GET", "HEAD"])
def health_check():
    return {"status": "ok", "bot": "NosticProno", "state": "online"}