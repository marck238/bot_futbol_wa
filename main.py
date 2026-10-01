import os
import asyncio
from fastapi import FastAPI
from contextlib import asynccontextmanager
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# Configurar la aplicación de Telegram
telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

# --- AGREGA AQUÍ TUS HANDLERS/COMANDOS ---
# Ejemplo:
# telegram_app.add_handler(CommandHandler("start", start_command))

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Inicializar y arrancar el bot de Telegram en segundo plano
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()
    print(">>> Bot de Telegram iniciado en Render <<<")
    yield
    # Apagar el bot limpiamente al detener el servicio
    await telegram_app.updater.stop()
    await telegram_app.stop()
    await telegram_app.shutdown()

app = FastAPI(lifespan=lifespan)

@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/health", methods=["GET", "HEAD"])
def health_check():
    return {"status": "ok", "bot": "online"}