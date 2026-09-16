import os
import logging

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

import uvicorn


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

TOKEN = os.getenv("TELEGRAM_TOKEN")
PORT = int(os.getenv("PORT", "10000"))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")

telegram_app = Application.builder().token(TOKEN).build()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton(
                "⚽ Matchs du jour",
                callback_data="matches"
            ),
            InlineKeyboardButton(
                "📊 Analyse",
                callback_data="analysis"
            )
        ],
        [
            InlineKeyboardButton(
                "🎯 Buteurs",
                callback_data="scorers"
            ),
            InlineKeyboardButton(
                "⚽ Buts",
                callback_data="goals"
            )
        ],
        [
            InlineKeyboardButton(
                "🌦️ Météo",
                callback_data="weather"
            ),
            InlineKeyboardButton(
                "📈 Probabilités",
                callback_data="probabilities"
            )
        ]
    ]

    await update.message.reply_text(
        "⚽ SPORT ANALYZER\n\n"
        "Analyse football et statistiques.\n\n"
        "Choisis une option :",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Commandes disponibles :\n\n"
        "/start\n"
        "/match\n"
        "/analyse\n"
        "/buteur\n"
        "/buts\n"
        "/probabilite\n"
        "/miTemps"
    )


async def matches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚽ MATCHS DU JOUR\n\n"
        "Le module football sera connecté prochainement."
    )


async def analyse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📊 ANALYSE\n\n"
        "Le moteur analysera :\n"
        "• Forme récente\n"
        "• Classement\n"
        "• Attaque\n"
        "• Défense\n"
        "• Formations\n"
        "• Absents\n"
        "• Buteurs\n"
        "• Passeurs\n"
        "• Penalties\n"
        "• Météo\n"
        "• Historique\n"
        "• Statistiques avancées"
    )


async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    if query.data == "matches":
        text = "⚽ Matchs du jour\n\nModule en préparation."

    elif query.data == "analysis":
        text = "📊 Analyse\n\nSélectionne ensuite un match."

    elif query.data == "scorers":
        text = "🎯 Buteurs\n\nModule en préparation."

    elif query.data == "goals":
        text = "⚽ Analyse des buts\n\nModule en préparation."

    elif query.data == "weather":
        text = "🌦️ Météo\n\nModule en préparation."

    elif query.data == "probabilities":
        text = (
            "📈 PROBABILITÉS\n\n"
            "Le moteur calculera :\n\n"
            "1 : victoire domicile\n"
            "X : match nul\n"
            "2 : victoire extérieur\n"
            "Over / Under\n"
            "BTTS\n"
            "Double chance\n"
            "Buteur\n"
            "Penalty"
        )

    else:
        text = "Option inconnue."

    await query.edit_message_text(text)


async def webhook(request: Request):
    try:
        data = await request.json()

        update = Update.de_json(
            data,
            telegram_app.bot
        )

        await telegram_app.process_update(update)

        return JSONResponse({"ok": True})

    except Exception as error:
        logging.exception("Erreur webhook : %s", error)
        return JSONResponse(
            {"ok": False},
            status_code=500
        )


async def health(request: Request):
    return JSONResponse({
        "status": "ok",
        "bot": "sport-analyzer"
    })


async def startup():
    if not TOKEN:
        raise RuntimeError(
            "La variable TELEGRAM_TOKEN est absente."
        )

    if not RENDER_URL:
        raise RuntimeError(
            "La variable RENDER_EXTERNAL_URL est absente."
        )

    await telegram_app.initialize()
    await telegram_app.start()

    webhook_url = f"{RENDER_URL}/telegram"

    await telegram_app.bot.set_webhook(
        url=webhook_url
    )

    logging.info(
        "Webhook Telegram configuré : %s",
        webhook_url
    )


async def shutdown():
    await telegram_app.bot.delete_webhook()

    await telegram_app.stop()
    await telegram_app.shutdown()


app = Starlette(
    routes=[
        ("/telegram", webhook),
        ("/health", health),
    ],
    on_startup=[startup],
    on_shutdown=[shutdown],
)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT
    )
