import os
import sys
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ---------------------------------------------------------
# 1. Configuración de Logging
# ---------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------
# 2. Servidor HTTP de Salud (Render Port Binding)
# ---------------------------------------------------------
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        # Silenciar peticiones continuas de escaneo en los logs
        pass

def start_health_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info(f"Servidor HTTP de salud activo en el puerto {port}")
    server.serve_forever()

# ---------------------------------------------------------
# 3. Comandos del Bot de Telegram
# ---------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responde al comando /start"""
    user_name = update.effective_user.first_name
    await update.message.reply_text(
        f"¡Hola {user_name}! 🤖 El bot está funcionando correctamente en Render."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responde al comando /help"""
    await update.message.reply_text(
        "Comandos disponibles:\n/start - Iniciar el bot\n/help - Ver ayuda"
    )

# ---------------------------------------------------------
# 4. Punto de Entrada Principal
# ---------------------------------------------------------
def main():
    # Detecta TELEGRAM_BOT_TOKEN o TELEGRAM_TOKEN de forma flexible
    token_raw = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
    odds_api_key = os.getenv("ODDS_API_KEY")

    if not token_raw:
        logger.error("Error: No se encontró TELEGRAM_BOT_TOKEN ni TELEGRAM_TOKEN en las variables de entorno.")
        sys.exit(1)

    telegram_token = token_raw.strip()

    if not odds_api_key:
        logger.warning("Advertencia: ODDS_API_KEY no se encuentra configurada en el entorno.")

    # Levanta el servidor HTTP interno en un hilo daemon antes de iniciar el polling
    threading.Thread(target=start_health_server, daemon=True).start()

    # Construye la aplicación de Telegram
    logger.info("Inicializando bot de Telegram...")
    application = ApplicationBuilder().token(telegram_token).build()

    # Registrar handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))

    # Iniciar recepción de mensajes por polling
    logger.info("Bot en marcha y escuchando peticiones...")
    application.run_polling()

if __name__ == "__main__":
    main()