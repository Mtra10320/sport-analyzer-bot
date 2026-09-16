import os
from contextlib import asynccontextmanager

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn


TOKEN = os.getenv("TELEGRAM_TOKEN")
PORT = int(os.getenv("PORT", "10000"))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")


telegram_app = None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Sport Analyzer Bot\n\n"
        "Bot connecté.\n\n"
        "Commandes disponibles :\n"
        "/start - Démarrer\n"
        "/help - Aide\n"
        "/match - Matchs du jour\n"
        "/analyse - Analyse d'un match\n"
        "/buteur - Buteurs\n"
        "/buts - Probabilités de buts\n"
        "/probabilite - Probabilités"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📊 Sport Analyzer\n\n"
        "Analyse sportive automatisée.\n\n"
        "Utilise /match pour voir les matchs du jour."
    )


async def match_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚽ Matchs du jour\n\n"
        "Le module des matchs sera connecté à l'API football prochainement."
    )


async def analyse_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📊 Analyse\n\n"
        "Le moteur d'analyse sera bientôt connecté aux données football."
    )


async def webhook(request: Request):
    global telegram_app

    try:
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)

        return JSONResponse({"ok": True})

    except Exception as e:
        print(f"Webhook error: {e}")
        return JSONResponse({"ok": False}, status_code=500)


async def health(request: Request):
    return JSONResponse({
        "status": "ok",
        "bot": "sport-analyzer-bot"
    })


@asynccontextmanager
async def lifespan(app):
    global telegram_app

    if not TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN est manquant.")

    if not RENDER_URL:
        raise RuntimeError("RENDER_EXTERNAL_URL est manquant.")

    print("Initialisation du bot Telegram...")

    telegram_app = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("help", help_command))
    telegram_app.add_handler(CommandHandler("match", match_command))
    telegram_app.add_handler(CommandHandler("analyse", analyse_command))

    await telegram_app.initialize()
    await telegram_app.start()

    webhook_url = f"{RENDER_URL}/telegram"

    await telegram_app.bot.set_webhook(webhook_url)

    print(f"Webhook Telegram configuré : {webhook_url}")
    print("Bot démarré.")

    yield

    print("Arrêt du bot...")

    await telegram_app.stop()
    await telegram_app.shutdown()


routes = [
    Route("/telegram", webhook, methods=["POST"]),
    Route("/health", health, methods=["GET"]),
]


app = Starlette(
    routes=routes,
    lifespan=lifespan
)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT
    )
