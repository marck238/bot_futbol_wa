import os
from fastapi import FastAPI
from contextlib import asynccontextmanager
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from sqlalchemy import select
from database import init_db, AsyncSessionLocal, User, Filter
from analytics import analyze_pre_match_event, calculate_kelly_stake

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

# 1. /start
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    if ADMIN_ID and str(telegram_id) != str(ADMIN_ID):
        await update.message.reply_text("⛔ Acceso denegado. Este bot es de uso privado.")
        return

    user = await get_or_create_user(telegram_id)
    msg = (
        f"⚽ **Bot Pre-Partido de Apuestas**\n\n"
        f"👤 ID: `{user.telegram_id}` | Rol: `{user.role}`\n\n"
        f"📌 **Comandos de Análisis Pre-Partido:**\n"
        f"🔍 `/analizar <media_gol_local> <media_gol_visit> <cuota_actual> [linea]`\n"
        f"📊 `/kelly <prob_%> <cuota> [banca]`\n"
        f"⚙️ `/crear_filtro <nombre> <mercado> <min_prob_%> <min_cuota> <min_ev_%>`\n"
        f"📋 `/mis_filtros`\n"
        f"🗓️ `/proximos` (Escanea próximos encuentros que cumplen tus filtros)"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

# 2. /analizar (Evaluador pre-partido completo)
async def analizar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text(
            "Uso: `/analizar <media_local> <media_visitante> <cuota_casa> [linea_corte]`\n"
            "Ejemplo: `/analizar 1.65 1.20 1.85 2.5`",
            parse_mode="Markdown"
        )
        return

    try:
        home_exp = float(context.args[0])
        away_exp = float(context.args[1])
        bookmaker_odd = float(context.args[2])
        line = float(context.args[3]) if len(context.args) >= 4 else 2.5

        analysis = analyze_pre_match_event(home_exp, away_exp, line)
        kelly = calculate_kelly_stake(analysis["prob_over_pct"], bookmaker_odd)

        val_status = "🔥 **¡APUESTA CON VALOR (EV+)!**" if kelly.get("has_value") else "⚠️ **SIN VALOR ESPERADO**"

        msg = (
            f"📋 **Análisis Pre-Partido (Línea Over {line})**\n\n"
            f"⚽ Promedio Esperado Total: `{analysis['expected_total']}` goles\n"
            f"🎯 Probabilidad Poisson: `{analysis['prob_over_pct']}%`\n"
            f"📉 Cuota Justa Teórica: `{analysis['fair_odd_over']}`\n"
            f"🏛️ Cuota de la Casa: `{bookmaker_odd}`\n\n"
            f"{val_status}\n"
            f"📊 EV: `+{kelly.get('expected_value_pct', 0)}%`\n"
            f"💵 Stake Recomendado: `{kelly.get('recommended_stake_pct', 0)}%` del bankroll"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Ingresa números válidos.")

# 3. /kelly
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

# 4. /crear_filtro
async def crear_filtro_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    if len(context.args) < 5:
        await update.message.reply_text(
            "Uso: `/crear_filtro <nombre> <mercado> <min_prob_%> <min_cuota> <min_ev_%>`\n"
            "Ejemplo: `/crear_filtro Over25Espana OVER_2.5 60 1.75 4`",
            parse_mode="Markdown"
        )
        return

    name = context.args[0]
    market = context.args[1].upper()
    try:
        min_prob = float(context.args[2])
        min_odd = float(context.args[3])
        min_ev = float(context.args[4])

        async with AsyncSessionLocal() as session:
            result = await session.execute(select(User).where(User.telegram_id == telegram_id))
            user = result.scalar_one_or_none()
            if user:
                new_filter = Filter(
                    user_id=user.id,
                    name=name,
                    market=market,
                    min_expected_prob=min_prob,
                    min_odd=min_odd,
                    min_ev=min_ev
                )
                session.add(new_filter)
                await session.commit()
                await update.message.reply_text(f"✅ Filtro Pre-Partido **{name}** guardado en Neon DB.", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Parámetros numéricos inválidos.")

# 5. /mis_filtros
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

            msg = "📋 **Tus Filtros Pre-Partido:**\n\n"
            for f in filters:
                msg += (
                    f"🔹 **{f.name}** [{f.market}]\n"
                    f"  • Prob. Mínima: `{f.min_expected_prob}%` | Cuota Mínima: `{f.min_odd}`\n"
                    f"  • EV Mínimo: `+{f.min_ev}%` | Estado: `{'🟢 Activo' if f.is_active else '🔴 Inactivo'}`\n\n"
                )
            await update.message.reply_text(msg, parse_mode="Markdown")

# 6. /proximos (Simulación/Escáner de próximos partidos)
async def proximos_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🗓️ **Escáner Pre-Partido**\n\n"
        "Buscando próximos encuentros que inician hoy y coinciden con tus filtros de probabilidad EV+...\n\n"
        "*(Próximamente conectaremos la API de fixtures previas para automatizar el barrido)*",
        parse_mode="Markdown"
    )

# App Builder
telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CommandHandler("analizar", analizar_command))
telegram_app.add_handler(CommandHandler("kelly", kelly_command))
telegram_app.add_handler(CommandHandler("crear_filtro", crear_filtro_command))
telegram_app.add_handler(CommandHandler("mis_filtros", mis_filtros_command))
telegram_app.add_handler(CommandHandler("proximos", proximos_command))

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    print(">>> Tablas Pre-Partido inicializadas en Neon DB <<<")
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=True)
    print(">>> Bot Pre-Partido iniciado correctamente en Render <<<")
    yield
    await telegram_app.updater.stop()
    await telegram_app.stop()
    await telegram_app.shutdown()

app = FastAPI(lifespan=lifespan)

@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/health", methods=["GET", "HEAD"])
def health_check():
    return {"status": "ok", "bot": "online"}