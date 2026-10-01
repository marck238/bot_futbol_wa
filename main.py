import os
from fastapi import FastAPI
from contextlib import asynccontextmanager
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# 1. Definir la lógica de respuestas para cada comando
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("¡Hola! El bot de fútbol está activo 24/7 en Render.")

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📌 Menú principal de opciones disponibles.")

# 2. Construir la aplicación de Telegram
telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

# 3. REGISTRAR LOS COMANDOS (Obligatorio para que responda)
telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CommandHandler("menu", menu_command))

# 4. Ciclo de vida en FastAPI
@asynccontextmanager
async def lifespan(app: FastAPI):
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