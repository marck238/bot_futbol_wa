import os
from datetime import datetime, timedelta
from fastapi import FastAPI
from contextlib import asynccontextmanager
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes
)
from sqlalchemy import select
from database import init_db, AsyncSessionLocal, User, Filter
from analytics import analyze_pre_match_event, calculate_kelly_stake

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")

# --- MENÚ PRINCIPAL CON BOTONES ---
def get_main_menu_keyboard():
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
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if not user:
            role = "ADMIN" if str(telegram_id) == str(ADMIN_ID) else "USER"
            user = User(telegram_id=telegram_id, role=role, status="ACTIVE")
            session.add(user)
            await session.commit()
            await session.refresh(user)
        return user

# Command /start -> Muestra el menú con botones
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    if ADMIN_ID and str(telegram_id) != str(ADMIN_ID):
        await update.message.reply_text("⛔ Acceso denegado. Este bot es de uso privado.")
        return

    await get_or_create_user(telegram_id)
    
    welcome_text = (
        "⚽ **Panel de Pronósticos Pre-Partido**\n\n"
        "Selecciona una opción del menú para escanear partidos en tiempo real "
        "y encontrar apuestas con Valor Esperado Positivo (EV+):"
    )
    
    await update.message.reply_text(
        welcome_text,
        parse_mode="Markdown",
        reply_markup=get_main_menu_keyboard()
    )

# --- MANEJADOR DE CLICS EN BOTONES ---
async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # Confirma la recepción del clic a Telegram
    
    data = query.data

    # Respuestas según el botón presionado
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
            "Para usar la calculadora directamente envía:\n"
            "`/kelly <probabilidad_%> <cuota_casa> [tu_banca]`\n\n"
            "Ejemplo:\n`/kelly 65 1.90 1000`"
        )
        back_button = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data="main_menu")]])
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=back_button)

    elif data == "main_menu":
        welcome_text = "⚽ **Panel de Pronósticos Pre-Partido**\n\nSelecciona una opción:"
        await query.edit_message_text(welcome_text, parse_mode="Markdown", reply_markup=get_main_menu_keyboard())

# --- GENERADOR DE PRONÓSTICOS FORMATEADOS ---
async def render_predictions(query, title: str, hours: int = None, days_offset: int = None, is_weekend: bool = False, min_ev_filter: float = 0.0):
    # Botón para regresar siempre al menú
    back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú Principal", callback_data="main_menu")]])
    
    # Texto de cabecera
    response = f"📊 **{title}**\n\n"
    
    # Mock / Estructura visual de los picks procesados
    # Cuando conectemos la API de partidos, esta función filtrará los datos reales
    sample_picks = [
        {
            "match": "Real Madrid vs Valencia",
            "league": "🇪🇸 LaLiga",
            "time": "18:00 Hs",
            "market": "Over 2.5 Goles",
            "prob": 68.5,
            "fair_odd": 1.46,
            "bookmaker_odd": 1.85,
            "ev": 26.7,
            "stake": 6.6
        },
        {
            "match": "Arsenal vs Chelsea",
            "league": "🏴󠁧󠁢󠁥󠁮󠁧󠁿 Premier League",
            "time": "20:30 Hs",
            "market": "Ambos Anotan (BTTS)",
            "prob": 62.0,
            "fair_odd": 1.61,
            "bookmaker_odd": 1.90,
            "ev": 17.8,
            "stake": 4.9
        }
    ]

    filtered_picks = [p for p in sample_picks if p["ev"] >= min_ev_filter]

    if not filtered_picks:
        response += "⚠️ No se encontraron partidos con Valor Esperado suficiente para este rango horario."
    else:
        for idx, item in enumerate(filtered_picks, start=1):
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

# --- VER FILTROS GUARDADOS ---
async def show_user_filters(query):
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
                "Para añadir uno envía el comando:\n"
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

# Command /kelly para cálculo manual rápido
async def kelly_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Uso: `/kelly <probabilidad_%> <cuota> [banca]`\nEjemplo: `/kelly 62 1.90 1000`", parse_mode="Markdown")
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

# App Builder
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
    print(">>> Bot con Menú de Botones iniciado correctamente <<<")
    yield
    await telegram_app.updater.stop()
    await telegram_app.stop()
    await telegram_app.shutdown()

app = FastAPI(lifespan=lifespan)

@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/health", methods=["GET", "HEAD"])
def health_check():
    return {"status": "ok", "bot": "online"}