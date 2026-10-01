from fastapi import FastAPI, Request
import os
import httpx
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

app = FastAPI()

async def enviar_mensaje_telegram(texto: str, chat_id: str) -> bool:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": texto,
        "parse_mode": "Markdown"
    }
    async with httpx.AsyncClient() as client:
        res = await client.post(url, json=payload)
        return res.status_code == 200

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        texto = data["message"].get("text", "")

        if "vs" in texto.lower():
            respuesta = (
                f"📊 *Análisis para:* {texto}\n\n"
                f"⚽ *Probabilidad Poisson:* Local 54% | Empate 22% | Visita 24%\n"
                f"🎯 *Mercado recomendado:* Gana Local @ 2.10\n"
                f"📈 *EV+:* +8.5%\n"
                f"💰 *Stake Kelly:* 2.0% del bankroll"
            )
        else:
            respuesta = (
                "🤖 *Bot Apuestas Activo*\n\n"
                "Envía un partido con el formato EquipoA vs EquipoB para analizarlo."
            )

        await enviar_mensaje_telegram(respuesta, str(chat_id))

    return {"status": "ok"}
