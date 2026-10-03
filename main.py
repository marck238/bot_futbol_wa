import os
import logging
from typing import Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from odds_api import get_upcoming_ev_picks, get_best_parlays

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

main_keyboard = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("⏳ Próximas 4 Hs", callback_data="hours_4"),
        InlineKeyboardButton("📅 Hoy", callback_data="today")
    ],
    [
        InlineKeyboardButton("📆 Mañana", callback_data="tomorrow"),
        InlineKeyboardButton("📆 Pasado Mañana", callback_data="day_after")
    ],
    [
        InlineKeyboardButton("⚽ Fin de Semana", callback_data="weekend"),
        InlineKeyboardButton("🎟️ Combinada EV+", callback_data="parlay")
    ]
])

back_keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton("🔙 Volver al Menú Principal", callback_data="main_menu")]
])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    welcome_text = (
        f"👋 ¡Hola, <b>{user.first_name}</b>!\n\n"
        "🤖 Bienvenid@ a tu bot de <b>Pronósticos de Valor Esperado (EV+)</b>.\n\n"
        "Selecciona una opción del menú para consultar los mejores pronósticos:"
    )
    if update.message:
        await update.message.reply_text(welcome_text, parse_mode="HTML", reply_markup=main_keyboard)


async def render_predictions(
    query,
    title: str,
    hours: Optional[int] = None,
    days_offset: Optional[int] = None,
    is_weekend: bool = False,
    min_ev_filter: float = 0.0
):
    await query.answer()

    api_res = await get_upcoming_ev_picks(
        hours=hours,
        days_offset=days_offset,
        is_weekend=is_weekend,
        min_ev_filter=min_ev_filter
    )

    status = api_res.get("status")

    if status == "NO_API_KEY":
        msg = "❌ <b>API Key No Configurada en Render.</b>"
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=back_keyboard)
        return

    if status == "INVALID_KEY":
        msg = "❌ <b>API Key Inválida en The Odds API.</b>"
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=back_keyboard)
        return

    if status == "QUOTA_EXCEEDED":
        msg = "⚠️ <b>Límite mensual alcanzado en The Odds API.</b>"
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=back_keyboard)
        return

    picks = api_res.get("data", [])
    if not picks:
        msg = (
            f"<b>{title}</b>\n\n"
            "⚠️ No se encontraron partidos con EV+ en este momento.\n\n"
            "💡 <i>Tip: Prueba presionando 📅 Hoy o 📆 Mañana para buscar en un rango más amplio.</i>"
        )
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=back_keyboard)
        return

    text_lines = [f"📊 <b>{title}</b>\n"]
    for idx, pick in enumerate(picks[:10], 1):
        text_lines.append(
            f"{idx}. <b>{pick['match']}</b> ({pick['league']})\n"
            f"⏰ Hora: <b>{pick['time']}</b> | Mercado: <b>{pick['best_pick']}</b>\n"
            f"📈 Prob. Estimada: <b>{pick['prob']}%</b> | Cuota: <b>{pick['odd']}</b> ({pick['bookmaker']})\n"
            f"🔥 EV: <b>+{pick['ev']}%</b> | Stake Sugerido: <b>{pick['stake_pct']}% (${pick['stake_amount']})</b>\n"
            f"───────────────"
        )

    full_text = "\n".join(text_lines)
    await query.edit_message_text(full_text, parse_mode="HTML", reply_markup=back_keyboard)


async def render_parlay(query):
    await query.answer()

    res = await get_best_parlays(hours=48)
    status = res.get("status")

    if status != "SUCCESS":
        msg = f"⚠️ <b>Error al consultar cuotas ({status})</b>"
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=back_keyboard)
        return

    parlay = res.get("parlay")
    if not parlay:
        msg = (
            "🎟️ <b>Combinada Sugerida EV+</b>\n\n"
            "⚠️ No hay suficientes selecciones individuales con EV+ para armar una combinada en este momento."
        )
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=back_keyboard)
        return

    legs_text = []
    for idx, leg in enumerate(parlay["legs"], 1):
        legs_text.append(
            f"  {idx}. <b>{leg['match']}</b>\n"
            f"     • Selección: {leg['best_pick']}\n"
            f"     • Cuota: {leg['odd']} | Prob: {leg['prob']}%"
        )

    legs_str = "\n".join(legs_text)
    msg = (
        f"🎟️ <b>Combinada Sugerida EV+ ({parlay['legs_count']} Selecciones)</b>\n\n"
        f"📋 <b>Legs:</b>\n{legs_str}\n\n"
        f"📊 <b>Totales de la Combinada:</b>\n"
        f"• Cuota Total: <b>{parlay['total_odd']}</b>\n"
        f"• Probabilidad Implícita: <b>{parlay['total_prob_pct']}%</b>\n"
        f"• EV Compuesto: <b>+{parlay['total_ev']}%</b>\n"
        f"• Stake Conservador Sugerido: <b>{parlay['recommended_stake_pct']}%</b>"
    )

    await query.edit_message_text(msg, parse_mode="HTML", reply_markup=back_keyboard)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data == "main_menu":
        await query.answer()
        await query.edit_message_text(
            "🤖 <b>Menú Principal de Pronósticos</b>\n\nSelecciona una opción:",
            parse_mode="HTML",
            reply_markup=main_keyboard
        )
    elif data == "hours_4":
        await render_predictions(query, "Partidos en las Próximas 4 Horas", hours=4)
    elif data == "today":
        await render_predictions(query, "Pronósticos para Hoy", days_offset=0)
    elif data == "tomorrow":
        await render_predictions(query, "Pronósticos para Mañana", days_offset=1)
    elif data == "day_after":
        await render_predictions(query, "Pronósticos para Pasado Mañana", days_offset=2)
    elif data == "weekend":
        await render_predictions(query, "Pronósticos para el Próximo Fin de Semana", is_weekend=True)
    elif data == "parlay":
        await render_parlay(query)


def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("Error: TELEGRAM_BOT_TOKEN no configurado en el entorno.")
        return

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("Bot en marcha y escuchando peticiones...")
    app.run_polling()


if __name__ == "__main__":
    main()