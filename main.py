import os
import asyncio
from fastapi import FastAPI
from contextlib import asynccontextmanager

# Importa aquí las funciones o la instancia de tu bot de Telegram
# Ejemplo: from bot_telegram import application

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Código que se ejecuta al arrancar el servidor
    # Si usas python-telegram-bot v20+:
    # asyncio.create_task(application.start())
    # asyncio.create_task(application.updater.start_polling())
    yield
    # Código que se ejecuta al apagar el servidor
    # await application.stop()

app = FastAPI(lifespan=lifespan)

@app.get("/")
@app.get("/health")
def health_check():
    return {"status": "ok", "bot": "online"}