import os
from fastapi import FastAPI
from contextlib import asynccontextmanager
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")  # ID de Telegram del dueño

def is_authorized(user_id: int) -> bool:
    """Verifica si el usuario es el administrador autorizado."""
    if not ADMIN_ID:
        return True  # Permite acceso si la variable no está configurada aún
    return str(user_id) == str(ADMIN_ID)

# 1. Handlers con verificación de acceso
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("⛔ Acceso denegado. Este bot es de uso privado.")
        return

    await update.message.reply_text("¡Hola! El bot está activo y asegurado bajo tu cuenta.")

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("⛔ Acceso denegado.")
        return

    await update.message.reply_text("📌 Menú principal de opciones disponibles.")

# 2. Construcción de la app
telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CommandHandler("menu", menu_command))

# 3. Ciclo de vida FastAPI
@asynccontextmanager
async def lifespan(app: FastAPI):
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()
    print(">>> Bot de Telegram iniciado en Render con control de acceso <<<")
    yield
    await telegram_app.updater.stop()
    await telegram_app.stop()
    await telegram_app.shutdown()

app = FastAPI(lifespan=lifespan)

@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/health", methods=["GET", "HEAD"])
def health_check():
    return {"status": "ok", "bot": "online"}