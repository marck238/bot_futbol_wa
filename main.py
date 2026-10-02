import os
from fastapi import FastAPI
from contextlib import asynccontextmanager
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from sqlalchemy import select
from database import init_db, AsyncSessionLocal, User, Filter
from analytics import calculate_kelly_stake, poisson_over_under

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")

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

# 1. Comando /start
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    if ADMIN_ID and str(telegram_id) != str(ADMIN_ID):
        await update.message.reply_text("⛔ Acceso denegado. Este bot es de uso privado.")
        return

    user = await get_or_create_user(telegram_id)
    await update.message.reply_text(
        f"⚽ **Motor de Apuestas Activo**\n\n"
        f"👤 ID: `{user.telegram_id}` | Rol: `{user.role}`\n\n"
        f"Comandos disponibles:\n"
        f"📊 `/kelly <prob_%> <cuota> <banca_opcional>`\n"
        f"🎲 `/poisson <media_eventos> <linea>`\n"
        f"⚙️ `/crear_filtro <nombre> <mercado> <min_appm> <min_odd>`\n"
        f"📋 `/mis_filtros`",
        parse_mode="Markdown"
    )

# 2. Comando /kelly (Calculadora de Stake)
async def kelly_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Uso: `/kelly <probabilidad_%> <cuota> [banca]`\nEjemplo: `/kelly 65 1.85 1000`", parse_mode="Markdown")
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
            f"📈 **Criterio de Kelly (1/4 Fractional)**\n\n"
            f"🔹 Valor Esperado (EV): `+{res['expected_value_pct']}%`\n"
            f"🔹 Kelly Teórico: `{res['raw_kelly_pct']}%` de la banca\n"
            f"🎯 **Stake Sugerido (Seguro):** `{res['recommended_stake_pct']}%`\n"
            f"💵 **Monto a Apostar:** `${res['recommended_amount']}` (de ${bankroll:.0f})"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Parámetros inválidos. Asegúrate de ingresar números.")

# 3. Comando /poisson (Probabilidades Over/Under)
async def poisson_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Uso: `/poisson <media_historica> <linea_corte>`\nEjemplo: `/poisson 10.5 9.5`", parse_mode="Markdown")
        return

    try:
        lmbda = float(context.args[0])
        threshold = float(context.args[1])
        res = poisson_over_under(lmbda, threshold)

        msg = (
            f"🎲 **Análisis de Poisson** (Media: {lmbda})\n\n"
            f"🔥 **Over {threshold}:** `{res['prob_over']}%` (Cuota Justa: `{res['fair_odd_over']}`)\n"
            f"🛡️ **Under {threshold}:** `{res['prob_under']}%` (Cuota Justa: `{res['fair_odd_under']}`)"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Ingresa números válidos.")

# 4. Comandos para Filtros Multi-tenant
async def crear_filtro_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    if len(context.args) < 4:
        await update.message.reply_text(
            "Uso: `/crear_filtro <nombre> <mercado> <min_appm> <min_odd>`\n"
            "Ejemplo: `/crear_filtro PresionCorners CORNERS 1.2 1.80`",
            parse_mode="Markdown"
        )
        return

    name = context.args[0]
    market = context.args[1].upper()
    try:
        min_appm = float(context.args[2])
        min_odd = float(context.args[3])

        async with AsyncSessionLocal() as session:
            result = await session.execute(select(User).where(User.telegram_id == telegram_id))
            user = result.scalar_one_or_none()
            if user:
                new_filter = Filter(user_id=user.id, name=name, market=market, min_appm=min_appm, min_odd=min_odd)
                session.add(new_filter)
                await session.commit()
                await update.message.reply_text(f"✅ Filtro **{name}** guardado con éxito en Neon DB.", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Error al procesar los valores numéricos de APPM o Cuota.")

async def mis_filtros_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user:
            res_filters = await session.execute(select(Filter).where(Filter.user_id == user.id))
            filters = res_filters.scalars().all()

            if not filters:
                await update.message.reply_text("📋 No tienes filtros guardados. Usa `/crear_filtro` para registrar uno.")
                return

            msg = "📋 **Tus Filtros Registrados:**\n\n"
            for f in filters:
                msg += f"🔹 **{f.name}** [{f.market}]\n  • Min APPM: `{f.min_appm}` | Min Cuota: `{f.min_odd}` | Estado: `{'🟢 Activo' if f.is_active else '🔴 Inactivo'}`\n\n"
            await update.message.reply_text(msg, parse_mode="Markdown")

# Construcción app
telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CommandHandler("kelly", kelly_command))
telegram_app.add_handler(CommandHandler("poisson", poisson_command))
telegram_app.add_handler(CommandHandler("crear_filtro", crear_filtro_command))
telegram_app.add_handler(CommandHandler("mis_filtros", mis_filtros_command))

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    print(">>> Tablas inicializadas/actualizadas en Neon <<<")
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=True)
    print(">>> Bot de Telegram iniciado con Motor de Apuestas <<<")
    yield
    await telegram_app.updater.stop()
    await telegram_app.stop()
    await telegram_app.shutdown()

app = FastAPI(lifespan=lifespan)

@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/health", methods=["GET", "HEAD"])
def health_check():
    return {"status": "ok", "bot": "online"}