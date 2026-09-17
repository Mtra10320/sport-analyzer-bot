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

# ==========================================
# CHAMPIONNATS PRIORITAIRES
# ==========================================

ESSENTIAL_LEAGUES = {
    "England": {
        "Premier League",
        "Championship",
    },
    "Spain": {
        "La Liga",
        "Segunda División",
    },
    "Italy": {
        "Serie A",
        "Serie B",
    },
    "Germany": {
        "Bundesliga",
        "2. Bundesliga",
    },
    "France": {
        "Ligue 1",
        "Ligue 2",
    },
    "Portugal": {
        "Primeira Liga",
        "Segunda Liga",
    },
    "Netherlands": {
        "Eredivisie",
        "Eerste Divisie",
    },
    "Belgium": {
        "Jupiler Pro League",
        "Challenger Pro League",
    },
    "Turkey": {
        "Süper Lig",
        "1. Lig",
    },
    "Poland": {
        "Ekstraklasa",
        "I Liga",
    },
    "Austria": {
        "Bundesliga",
        "2. Liga",
    },
    "Switzerland": {
        "Super League",
        "Challenge League",
    },
    "Greece": {
        "Super League 1",
        "Super League 2",
    },
    "Denmark": {
        "Superliga",
        "1. Division",
    },
    "Sweden": {
        "Allsvenskan",
        "Superettan",
    },
    "Norway": {
        "Eliteserien",
        "1. Division",
        "OBOS-ligaen",
    },
    "Czech-Republic": {
        "Czech Liga",
        "FNL",
    },
    "Serbia": {
        "Super Liga",
        "Prva Liga",
    },
    "Croatia": {
        "HNL",
        "First NL",
    },
    "Romania": {
        "Liga I",
        "Liga II",
    },
    "Ukraine": {
        "Premier League",
        "Persha Liga",
    },
    "Russia": {
        "Premier League",
        "First League",
    },
    "Israel": {
        "Premier League",
        "Liga Leumit",
    },
    "Scotland": {
        "Premiership",
        "Championship",
    },
    "Ireland": {
        "Premier Division",
        "First Division",
    },
    "Finland": {
        "Veikkausliiga",
        "Ykkösliiga",
    },
    "Hungary": {
        "NB I",
        "NB II",
    },
    "Slovakia": {
        "Super Liga",
        "2. liga",
    },
    "Slovenia": {
        "1. SNL",
        "2. SNL",
    },
    "Bulgaria": {
        "First League",
        "Second League",
    },
    "Cyprus": {
        "1. Division",
        "2. Division",
    },
    "Bosnia-Herzegovina": {
        "Premijer Liga",
        "1st League - FBiH",
        "1st League - RS",
    },
    "Albania": {
        "Superliga",
        "1st Division",
    },
    "Iceland": {
        "Úrvalsdeild",
        "1. Deild",
    },
    "Luxembourg": {
        "National Division",
        "Division 2",
    },
    "Malta": {
        "Premier League",
        "Challenge League",
    },
}

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

        # Filtrage des championnats prioritaires
        essential_fixtures = []

        for item in fixtures:
            league = item.get("league", {})

            country = league.get("country", "")
            league_name = league.get("name", "")

            allowed_leagues = ESSENTIAL_LEAGUES.get(country, set())

            if league_name in allowed_leagues:
                essential_fixtures.append(item)

        if not essential_fixtures:
            await update.message.reply_text(
                f"⚽ Aucun match des championnats prioritaires "
                f"pour le {today}."
            )
            return

        message = (
            f"⚽ MATCHS PRIORITAIRES\n"
            f"📅 {today}\n"
            f"📊 {len(essential_fixtures)} matchs\n\n"
        )

        current_country = None

        for item in essential_fixtures:
            fixture = item.get("fixture", {})
            league = item.get("league", {})
            teams = item.get("teams", {})

            fixture_id = fixture.get("id")
            date = fixture.get("date", "")
            status = fixture.get("status", {}).get("short", "N/A")

            country = league.get("country", "Inconnu")
            competition = league.get(
                "name",
                "Compétition inconnue"
            )

            home = teams.get("home", {}).get(
                "name",
                "Inconnu"
            )

            away = teams.get("away", {}).get(
                "name",
                "Inconnu"
            )

            if country != current_country:
                message += (
                    f"🌍 {country.upper()}\n\n"
                )
                current_country = country

            try:
                match_time = datetime.fromisoformat(
                    date.replace("Z", "+00:00")
                ).astimezone(paris).strftime("%H:%M")
            except Exception:
                match_time = "??:??"

            status_display = {
                "NS": "À venir",
                "TBD": "Horaire à confirmer",
                "1H": "1ère mi-temps",
                "HT": "Mi-temps",
                "2H": "2ème mi-temps",
                "ET": "Prolongation",
                "P": "Tirs au but",
                "FT": "Terminé",
                "PST": "Reporté",
                "CANC": "Annulé",
                "SUSP": "Suspendu",
            }.get(status, status)

            message += (
                f"🏆 {competition}\n"
                f"🕐 {match_time}\n"
                f"⚽ {home} - {away}\n"
                f"📌 {status_display}\n"
                f"🆔 {fixture_id}\n\n"
            )

        message += (
            "━━━━━━━━━━━━━━━━━━\n"
            "📊 Pour analyser un match :\n"
            "/analyse ID\n\n"
            "Exemple :\n"
            "/analyse 1234567"
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
