import os
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

TOKEN = os.getenv("TELEGRAM_TOKEN")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("⚽ Matchs du jour", callback_data="matches"),
            InlineKeyboardButton("📊 Analyse", callback_data="analysis")
        ],
        [
            InlineKeyboardButton("🎯 Buteurs", callback_data="scorers"),
            InlineKeyboardButton("⚽ Buts", callback_data="goals")
        ],
        [
            InlineKeyboardButton("🌦️ Météo", callback_data="weather"),
            InlineKeyboardButton("📈 Probabilités", callback_data="probabilities")
        ]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "⚽ SPORT ANALYZER\n\n"
        "Analyse football et statistiques.\n\n"
        "Choisis une option :",
        reply_markup=reply_markup
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
        "Le module football sera connecté prochainement.\n\n"
        "Il affichera :\n"
        "• Matchs du jour\n"
        "• Horaires\n"
        "• Compétitions\n"
        "• Équipes\n"
        "• Cotes"
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


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            "📈 Probabilités\n\n"
            "Le moteur calculera notamment :\n"
            "1 : victoire équipe domicile\n"
            "X : match nul\n"
            "2 : victoire équipe extérieure\n"
            "Over / Under\n"
            "BTTS\n"
            "Double chance\n"
            "Buteur\n"
            "Penalty"
        )

    else:
        text = "Option inconnue."

    await query.edit_message_text(text)


async def main():
    if not TOKEN:
        raise ValueError("La variable TELEGRAM_TOKEN est absente.")

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("match", matches))
    application.add_handler(CommandHandler("analyse", analyse))
    application.add_handler(
        CallbackQueryHandler(button_handler)
    )

    print("Bot démarré")

    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    await application.updater.idle()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
