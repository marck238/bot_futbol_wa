import os
import logging
import datetime as dt
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Configuración básica de logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- TECLADO PERSISTENTE ---
def get_persistent_keyboard():
    keyboard = [
        [KeyboardButton("📊 Mis Estadísticas"), KeyboardButton("🍀 Combinadas EV+")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# --- FUNCIÓN PRINCIPAL DE MENSAJES ---
async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
        
    text = update.message.text.strip()
    logger.info(f"Mensaje recibido de Telegram: '{text}'")

    if "Estadísticas" in text:
        await handle_statistics(update, context)
        
    elif "Combinada" in text or "EV+" in text:
        await update.message.reply_text("🍀 Analizando el mercado y buscando las mejores opciones para tu combinada...", reply_markup=get_persistent_keyboard())
        
        try:
            current_date_str = dt.datetime.now(dt.timezone(dt.timedelta(hours=-3))).strftime("%Y-%m-%d")
            
            fixtures_data = await fetch_fixtures_for_today(current_date_str)
            
            candidates = []
            for fix in fixtures_data[:15]:
                analysis = await generate_fixture_analytics_real(fix)
                h_name = fix.get("teams", {}).get("home", {}).get("name", "Local")
                a_name = fix.get("teams", {}).get("away", {}).get("name", "Visita")
                league_name = fix.get("league", {}).get("name", "Liga")
                
                p_over = analysis["goals"]["p_over_25"]
                odds_over = analysis["goals"]["odds_over"]
                if p_over >= 0.58:
                    candidates.append({
                        "match": f"{h_name} vs {a_name}",
                        "league": league_name,
                        "market": "Más de 2.5 Goles",
                        "probability": p_over,
                        "odds": odds_over
                    })
                    
            if len(candidates) >= 2:
                candidates = sorted(candidates, key=lambda x: x["probability"], reverse=True)[:3]
                
                combined_odds = 1.0
                parlay_text = "🍀 **COMBINADA DEL DÍA (+EV)** 🍀\n━━━━━━━━━━━━━━━━━━━\n"
                
                for i, item in enumerate(candidates, 1):
                    combined_odds *= item["odds"]
                    parlay_text += f"**{i}. {item['match']}**\n"
                    parlay_text += f"   📌 *Mercado:* {item['market']}\n"
                    parlay_text += f"   📊 *Prob:* {int(item['probability']*100)}% | *Cuota:* {item['odds']}\n\n"
                
                combined_odds = round(combined_odds, 2)
                suggested_stake = "1% a 2% (Stake bajo por ser combinada)"
                
                parlay_text += f"━━━━━━━━━━━━━━━━━━━\n"
                parlay_text += f"🔥 **Cuota Combinada Total:** `{combined_odds}`\n"
                parlay_text += f"💰 **Stake Sugerido:** {suggested_stake}\n"
                parlay_text += f"💡 *Consejo:* Las combinadas multiplican el riesgo, mantén una gestión de bankroll estricta."
                
                await update.message.reply_text(parlay_text, parse_mode="Markdown", reply_markup=get_persistent_keyboard())
            else:
                await update.message.reply_text(
                    "⚠️ No se encontraron suficientes partidos con alta probabilidad (`+EV`) para armar una combinada sólida en este momento. ¡Inténtalo más tarde!",
                    reply_markup=get_persistent_keyboard()
                )
                
        except Exception as e:
            logger.error(f"Error generando combinada: {str(e)}")
            await update.message.reply_text(
                f"❌ Ocurrió un error al generar la combinada: {str(e)}",
                reply_markup=get_persistent_keyboard()
            )
    else:
        await update.message.reply_text(
            "Utiliza los botones del menú inferior para interactuar con el bot.",
            reply_markup=get_persistent_keyboard()
        )