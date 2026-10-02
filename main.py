import os
from fastapi import FastAPI
from contextlib import asynccontextmanager
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from sqlalchemy import select
from database import init_db, AsyncSessionLocal, User

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")

async def get_or_create_user(telegram_id: int):
    """Obtiene el usuario o lo crea si no existe en la base de datos."""
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

# Handlers con verificación DB
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    
    # Restricción por ADMIN_ID
    if ADMIN_ID and str(telegram_id) != str(ADMIN_ID):
        await update.message.reply_text("⛔ Acceso denegado. Este bot es de uso privado.")
        return

    user = await get_or_create_user(telegram_id)
    await update.message.reply_text(
        f"¡Hola! Bot activo.\n"
        f"👤 ID: {user.telegram_id}\n"
        f"🔑 Rol: {user.role}\n"
        f"🟢 Estado: {user.status}"
    )

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    if ADMIN_ID and str(telegram_id) != str(ADMIN_ID):
        await update.message.reply_text("⛔ Acceso denegado.")
        return

    await update.message.reply_text("📌 Menú principal de opciones.")

# Construcción app
telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CommandHandler("menu", menu_command))

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Inicializar tablas en Neon
    await init_db()
    print(">>> Base de datos inicializada en Neon <<<")
    
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()
    print(">>> Bot de Telegram iniciado en Render <<<")
    
    yield
    
    await telegram_app.updater.stop()
    await telegram_app.stop()
    await telegram_app.shutdown()

app = FastAPI(lifespan=lifespan)

@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/health", methods=["GET", "HEAD"])
def health_check():
    return {"status": "ok", "bot": "online"}