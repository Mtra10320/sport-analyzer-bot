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
    api_key = os.getenv("API_FOOTBALL_KEY")

    if not api_key:
        await update.message.reply_text(
            "❌ Clé API-Football absente."
        )
        return

    if not context.args:
        await update.message.reply_text(
            "📊 ANALYSE D'UN MATCH\n\n"
            "Utilisation :\n"
            "/analyse ID\n\n"
            "Exemple :\n"
            "/analyse 1561400"
        )
        return

    fixture_id = context.args[0]

    if not fixture_id.isdigit():
        await update.message.reply_text(
            "❌ L'ID du match doit être numérique.\n\n"
            "Exemple : /analyse 1561400"
        )
        return

    headers = {
        "x-apisports-key": api_key
    }

    fixture_url = (
        "https://v3.football.api-sports.io/fixtures"
    )

    prediction_url = (
        "https://v3.football.api-sports.io/predictions"
    )

    try:
        async with httpx.AsyncClient(timeout=20) as client:

            fixture_response = await client.get(
                fixture_url,
                headers=headers,
                params={
                    "ids": fixture_id
                }
            )

            prediction_response = await client.get(
                prediction_url,
                headers=headers,
                params={
                    "fixture": fixture_id
                }
            )

        if fixture_response.status_code != 200:
            await update.message.reply_text(
                f"❌ Erreur API-Football : "
                f"{fixture_response.status_code}"
            )
            return

        fixture_data = fixture_response.json()

        if fixture_data.get("errors"):
            await update.message.reply_text(
                f"❌ API-Football : "
                f"{fixture_data['errors']}"
            )
            return

        fixtures = fixture_data.get("response", [])

        if not fixtures:
            await update.message.reply_text(
                "❌ Match introuvable."
            )
            return

        item = fixtures[0]

        fixture = item.get("fixture", {})
        league = item.get("league", {})
        teams = item.get("teams", {})
        goals = item.get("goals", {})

        home = teams.get("home", {})
        away = teams.get("away", {})

        home_name = home.get("name", "Inconnu")
        away_name = away.get("name", "Inconnu")

        competition = league.get(
            "name",
            "Compétition inconnue"
        )

        country = league.get(
            "country",
            "Pays inconnu"
        )

        status = fixture.get(
            "status",
            {}
        ).get(
            "short",
            "N/A"
        )

        venue = fixture.get(
            "venue",
            {}
        ).get(
            "name",
            "Non communiqué"
        )

        paris = ZoneInfo("Europe/Paris")

        match_date = fixture.get("date", "")

        try:
            match_datetime = datetime.fromisoformat(
                match_date.replace("Z", "+00:00")
            ).astimezone(paris)

            formatted_date = match_datetime.strftime(
                "%d/%m/%Y à %H:%M"
            )

        except Exception:
            formatted_date = "Date inconnue"

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
            "SUSP": "Suspendu"
        }.get(status, status)

        message = (
            "📊 SPORT ANALYZER\n\n"
            f"🌍 {country}\n"
            f"🏆 {competition}\n"
            f"⚽ {home_name} - {away_name}\n"
            f"🕐 {formatted_date}\n"
            f"📌 {status_display}\n"
            f"🏟️ {venue}\n"
            f"🆔 {fixture_id}\n"
        )

        # Score si disponible
        home_goals = goals.get("home")
        away_goals = goals.get("away")

        if home_goals is not None or away_goals is not None:
            message += (
                f"\n🥅 SCORE : "
                f"{home_goals} - {away_goals}\n"
            )

        # Analyse API-Football
        if prediction_response.status_code == 200:

            prediction_data = prediction_response.json()

            prediction_response_list = (
                prediction_data.get("response", [])
            )

            if prediction_response_list:

                prediction_item = (
                    prediction_response_list[0]
                )

                predictions = (
                    prediction_item.get(
                        "predictions",
                        {}
                    )
                )

                percent = predictions.get(
                    "percent",
                    {}
                )

                home_pct = percent.get(
                    "home",
                    "N/A"
                )

                draw_pct = percent.get(
                    "draw",
                    "N/A"
                )

                away_pct = percent.get(
                    "away",
                    "N/A"
                )

                winner = predictions.get(
                    "winner",
                    {}
                )

                winner_name = winner.get(
                    "name",
                    "N/A"
                ) if isinstance(
                    winner,
                    dict
                ) else str(winner)

                advice = predictions.get(
                    "advice",
                    "N/A"
                )

                under_over = predictions.get(
                    "under_over",
                    "N/A"
                )

                predicted_goals = predictions.get(
                    "goals",
                    {}
                )

                predicted_home = predicted_goals.get(
                    "home",
                    "N/A"
                ) if isinstance(
                    predicted_goals,
                    dict
                ) else "N/A"

                predicted_away = predicted_goals.get(
                    "away",
                    "N/A"
                ) if isinstance(
                    predicted_goals,
                    dict
                ) else "N/A"

                predicted_score = predictions.get(
                    "score",
                    {}
                )

                score_home = predicted_score.get(
                    "home",
                    "N/A"
                ) if isinstance(
                    predicted_score,
                    dict
                ) else "N/A"

                score_away = predicted_score.get(
                    "away",
                    "N/A"
                ) if isinstance(
                    predicted_score,
                    dict
                ) else "N/A"

                message += (
                    "\n━━━━━━━━━━━━━━━━━━\n"
                    "📈 PROBABILITÉS API\n\n"
                    f"1️⃣ {home_name} : {home_pct}\n"
                    f"⚖️ Nul : {draw_pct}\n"
                    f"2️⃣ {away_name} : {away_pct}\n"
                )

                # Conversion des probabilités
                def percentage(value):
                    try:
                        return float(
                            str(value)
                            .replace("%", "")
                            .replace(",", ".")
                        )
                    except Exception:
                        return None

                h = percentage(home_pct)
                d = percentage(draw_pct)
                a = percentage(away_pct)

                if (
                    h is not None
                    and d is not None
                    and a is not None
                ):
                    message += (
                        "\n🔄 DOUBLE CHANCE\n\n"
                        f"1X : {h + d:.1f}%\n"
                        f"X2 : {d + a:.1f}%\n"
                        f"12 : {h + a:.1f}%\n"
                    )

                message += (
                    "\n🎯 PRÉDICTION API\n\n"
                    f"🏆 Vainqueur : {winner_name}\n"
                    f"🥅 Score prévu : "
                    f"{score_home} - {score_away}\n"
                    f"📊 Buts prévus : "
                    f"{predicted_home} - "
                    f"{predicted_away}\n"
                    f"📈 Over/Under : {under_over}\n"
                    f"💡 Conseil API : {advice}\n"
                )

                comparison = (
                    prediction_item.get(
                        "comparison",
                        {}
                    )
                )

                if comparison:

                    message += (
                        "\n📊 COMPARAISON\n\n"
                    )

                    labels = {
                        "form": "Forme",
                        "att": "Attaque",
                        "def": "Défense",
                        "h2h": "H2H",
                        "poisson_distribution": "Poisson"
                    }

                    for key, label in labels.items():

                        value = comparison.get(key)

                        if isinstance(value, dict):

                            comp_home = value.get(
                                "home",
                                "N/A"
                            )

                            comp_away = value.get(
                                "away",
                                "N/A"
                            )

                            message += (
                                f"• {label} : "
                                f"{comp_home} / "
                                f"{comp_away}\n"
                            )

                        elif value is not None:

                            message += (
                                f"• {label} : "
                                f"{value}\n"
                            )

        else:
            message += (
                "\n⚠️ Prédiction API indisponible "
                "pour ce match.\n"
            )

        message += (
            "\n━━━━━━━━━━━━━━━━━━\n"
            "🔎 Analyse avancée : prochaine étape\n"
            "📋 Classement\n"
            "📈 Forme détaillée\n"
            "🤕 Blessures / absences\n"
            "👥 Compositions\n"
            "⚽ Buteurs\n"
            "📊 BTTS / Over-Under\n"
            "💰 Cotes & Value\n"
        )

        await update.message.reply_text(message)

    except Exception as e:

        print(f"Analyse error: {e}")

        await update.message.reply_text(
            "❌ Impossible d'effectuer l'analyse "
            "de ce match."
        )
