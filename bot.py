import os
import httpx
from datetime import datetime
from zoneinfo import ZoneInfo
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
    api_key = os.getenv("API_FOOTBALL_KEY")

    if not api_key:
        await update.message.reply_text(
            "❌ Clé API-Football absente."
        )
        return

    paris = ZoneInfo("Europe/Paris")
    today = datetime.now(paris).strftime("%Y-%m-%d")

    url = "https://v3.football.api-sports.io/fixtures"

    headers = {
        "x-apisports-key": api_key
    }

    params = {
        "date": today,
        "timezone": "Europe/Paris"
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                url,
                headers=headers,
                params=params
            )

        if response.status_code != 200:
            await update.message.reply_text(
                f"❌ API-Football : erreur HTTP {response.status_code}"
            )
            return

        data = response.json()

        if data.get("errors"):
            await update.message.reply_text(
                f"❌ API-Football : {data['errors']}"
            )
            return

        fixtures = data.get("response", [])

        if not fixtures:
            await update.message.reply_text(
                f"⚽ Aucun match trouvé pour le {today}."
            )
            return

        message = f"⚽ MATCHS DU {today}\n\n"

        for item in fixtures[:30]:
            fixture = item.get("fixture", {})
            league = item.get("league", {})
            teams = item.get("teams", {})

            date = fixture.get("date", "")
            status = fixture.get("status", {}).get("short", "N/A")

            home = teams.get("home", {}).get("name", "Inconnu")
            away = teams.get("away", {}).get("name", "Inconnu")
            competition = league.get("name", "Compétition inconnue")

            try:
                match_time = datetime.fromisoformat(
                    date.replace("Z", "+00:00")
                ).astimezone(paris).strftime("%H:%M")
            except Exception:
                match_time = "??:??"

            message += (
                f"🏆 {competition}\n"
                f"🕐 {match_time}\n"
                f"⚽ {home} - {away}\n"
                f"📌 {status}\n\n"
            )

        if len(fixtures) > 30:
            message += (
                f"📋 {len(fixtures)} matchs trouvés.\n"
                "Affichage limité aux 30 premiers."
            )

        await update.message.reply_text(message)

    except Exception as e:
        print(f"API-Football error: {e}")

        await update.message.reply_text(
            "❌ Impossible de contacter API-Football."
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
