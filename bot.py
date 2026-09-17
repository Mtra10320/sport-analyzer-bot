
import os
import math
from datetime import datetime
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn

TOKEN = os.getenv("TELEGRAM_TOKEN")
API_KEY = os.getenv("API_FOOTBALL_KEY")
PORT = int(os.getenv("PORT", "10000"))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")

PARIS = ZoneInfo("Europe/Paris")
API_BASE = "https://v3.football.api-sports.io"

telegram_app = None

ESSENTIAL_LEAGUES = {
    "England": {"Premier League", "Championship"},
    "Spain": {"La Liga", "Segunda División"},
    "Italy": {"Serie A", "Serie B"},
    "Germany": {"Bundesliga", "2. Bundesliga"},
    "France": {"Ligue 1", "Ligue 2"},
    "Portugal": {"Primeira Liga", "Segunda Liga", "Liga Portugal 2"},
    "Netherlands": {"Eredivisie", "Eerste Divisie"},
    "Belgium": {"Jupiler Pro League", "Challenger Pro League"},
    "Turkey": {"Süper Lig", "1. Lig"},
    "Poland": {"Ekstraklasa", "I Liga"},
    "Austria": {"Bundesliga", "2. Liga"},
    "Switzerland": {"Super League", "Challenge League"},
    "Greece": {"Super League 1", "Super League 2"},
    "Denmark": {"Superliga", "1. Division"},
    "Sweden": {"Allsvenskan", "Superettan"},
    "Norway": {"Eliteserien", "1. Division", "OBOS-ligaen"},
    "Czech-Republic": {"Czech Liga", "FNL"},
    "Serbia": {"Super Liga", "Prva Liga"},
    "Croatia": {"HNL", "First NL"},
    "Romania": {"Liga I", "Liga II", "SuperLiga"},
    "Ukraine": {"Premier League", "First League"},
    "Russia": {"Premier League", "First League"},
    "Israel": {"Premier League", "Liga Leumit"},
    "Scotland": {"Premiership", "Championship"},
    "Ireland": {"Premier Division", "First Division"},
    "Finland": {"Veikkausliiga", "Ykkösliiga"},
    "Hungary": {"NB I", "NB II"},
    "Slovakia": {"Super Liga", "2. liga", "Niké Liga"},
    "Slovenia": {"1. SNL", "2. SNL", "PrvaLiga"},
    "Bulgaria": {"First League", "Second League"},
    "Cyprus": {"1. Division", "Second Division"},
    "Bosnia-Herzegovina": {
        "Premijer Liga",
        "1st League - FBiH",
        "1st League - RS",
    },
    "Albania": {
        "Superliga",
        "1st Division",
        "Kategoria Superiore",
        "Kategoria e Parë",
    },
    "Iceland": {"Úrvalsdeild", "1. Deild", "Besta deild"},
    "Luxembourg": {
        "National Division",
        "Division 2",
        "Nationaldivisioun",
        "Éierepromotioun",
    },
    "Malta": {"Premier League", "Challenge League"},
}

# Cache mémoire pour éviter de répéter inutilement les appels.
CACHE = {}


def today_paris():
    return datetime.now(PARIS).strftime("%Y-%m-%d")


def pct(value):
    try:
        return float(str(value).replace("%", "").replace(",", "."))
    except Exception:
        return None


def fmt_pct(value):
    number = pct(value)
    return f"{number:.1f}%" if number is not None else "N/D"


def safe_num(value):
    try:
        return float(value)
    except Exception:
        return None


def poisson_pmf(k, lam):
    if lam is None or lam < 0:
        return None
    try:
        return math.exp(-lam) * (lam ** k) / math.factorial(k)
    except Exception:
        return None


def poisson_goal_probs(home_xg, away_xg, max_goals=7):
    if home_xg is None or away_xg is None:
        return None

    matrix = {}
    total = 0.0

    for home_goals in range(max_goals + 1):
        home_probability = poisson_pmf(home_goals, home_xg)

        for away_goals in range(max_goals + 1):
            away_probability = poisson_pmf(away_goals, away_xg)
            probability = (home_probability or 0) * (away_probability or 0)
            matrix[(home_goals, away_goals)] = probability
            total += probability

    if total <= 0:
        return None

    for key in matrix:
        matrix[key] /= total

    return matrix


def poisson_markets(matrix):
    if not matrix:
        return {}

    home = 0.0
    draw = 0.0
    away = 0.0
    btts = 0.0
    over15 = 0.0
    over25 = 0.0
    under35 = 0.0

    for (home_goals, away_goals), probability in matrix.items():
        if home_goals > away_goals:
            home += probability
        elif home_goals == away_goals:
            draw += probability
        else:
            away += probability

        if home_goals > 0 and away_goals > 0:
            btts += probability

        if home_goals + away_goals >= 2:
            over15 += probability

        if home_goals + away_goals >= 3:
            over25 += probability

        if home_goals + away_goals <= 3:
            under35 += probability

    return {
        "home": home * 100,
        "draw": draw * 100,
        "away": away * 100,
        "btts": btts * 100,
        "over15": over15 * 100,
        "over25": over25 * 100,
        "under35": under35 * 100,
    }


def most_likely_scores(matrix, limit=3):
    if not matrix:
        return []

    ordered = sorted(
        matrix.items(),
        key=lambda item: item[1],
        reverse=True,
    )

    return [
        (home_goals, away_goals, probability * 100)
        for (home_goals, away_goals), probability in ordered[:limit]
    ]


def get_team_name(item, side):
    return (
        item.get("teams", {})
        .get(side, {})
        .get("name", "Inconnu")
    )


def status_label(status):
    return {
        "NS": "À venir",
        "TBD": "Horaire à confirmer",
        "1H": "1ère mi-temps",
        "HT": "Mi-temps",
        "2H": "2ème mi-temps",
        "ET": "Prolongation",
        "P": "Tirs au but",
        "FT": "Terminé",
        "AET": "Terminé après prolongation",
        "PST": "Reporté",
        "CANC": "Annulé",
        "SUSP": "Suspendu",
    }.get(status, status or "N/D")


def fixture_datetime(item):
    raw = item.get("fixture", {}).get("date", "")

    try:
        return datetime.fromisoformat(
            raw.replace("Z", "+00:00")
        ).astimezone(PARIS)
    except Exception:
        return None


def league_allowed(item):
    league = item.get("league", {})
    country = league.get("country", "")
    league_name = league.get("name", "")

    return league_name in ESSENTIAL_LEAGUES.get(country, set())


def extract_remaining(response):
    value = response.headers.get(
        "x-ratelimit-requests-remaining"
    )

    if value is None:
        value = response.headers.get(
            "X-Ratelimit-Requests-Remaining"
        )

    try:
        return int(value)
    except Exception:
        return None


async def api_get(client, endpoint, params=None):
    if not API_KEY:
        return None, "Clé API-Football absente.", None

    try:
        response = await client.get(
            f"{API_BASE}/{endpoint}",
            headers={"x-apisports-key": API_KEY},
            params=params or {},
        )
    except Exception as exc:
        return None, f"Erreur réseau API-Football : {exc}", None

    remaining = extract_remaining(response)

    if response.status_code != 200:
        return (
            None,
            f"API-Football HTTP {response.status_code}",
            remaining,
        )

    try:
        data = response.json()
    except Exception:
        return None, "Réponse API-Football invalide.", remaining

    if data.get("errors"):
        return (
            None,
            f"API-Football : {data['errors']}",
            remaining,
        )

    return data, None, remaining


async def get_today_fixtures(client):
    cache_key = f"fixtures:{today_paris()}"

    if cache_key in CACHE:
        return CACHE[cache_key], None

    data, error, _ = await api_get(
        client,
        "fixtures",
        {
            "date": today_paris(),
            "timezone": "Europe/Paris",
        },
    )

    if error:
        return None, error

    fixtures = data.get("response", [])
    CACHE[cache_key] = fixtures

    return fixtures, None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 SPORT ANALYZER\n\n"
        "Analyse football automatisée.\n\n"
        "Commandes :\n"
        "/match - Matchs prioritaires du jour\n"
        "/analyse ID - Analyse complète\n"
        "/buteur ID - Buteurs disponibles\n"
        "/buts ID - Probabilités de buts\n"
        "/probabilite ID - Probabilités principales\n"
        "/help - Aide"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📊 SPORT ANALYZER\n\n"
        "/match\n"
        "Liste les matchs prioritaires du jour.\n\n"
        "/analyse ID\n"
        "Analyse complète d'un match.\n\n"
        "/buteur ID\n"
        "Affiche les buteurs disponibles.\n\n"
        "/buts ID\n"
        "Calcule les probabilités de buts disponibles.\n\n"
        "/probabilite ID\n"
        "Affiche les probabilités principales."
    )


async def match_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not API_KEY:
        await update.message.reply_text(
            "❌ Clé API-Football absente."
        )
        return

    async with httpx.AsyncClient(timeout=20) as client:
        fixtures, error = await get_today_fixtures(client)

    if error:
        await update.message.reply_text(f"❌ {error}")
        return

    essential = [
        fixture
        for fixture in fixtures
        if league_allowed(fixture)
    ]

    if not essential:
        await update.message.reply_text(
            f"⚽ Aucun match prioritaire trouvé pour le "
            f"{today_paris()}."
        )
        return

    essential.sort(
        key=lambda fixture: (
            fixture_datetime(fixture)
            or datetime.max.replace(tzinfo=PARIS)
        )
    )

    message = (
        "⚽ MATCHS PRIORITAIRES\n"
        f"📅 {today_paris()}\n"
        f"📊 {len(essential)} matchs\n\n"
    )

    current_country = None

    for item in essential:
        league = item.get("league", {})
        fixture = item.get("fixture", {})

        home = get_team_name(item, "home")
        away = get_team_name(item, "away")

        country = league.get("country", "Inconnu")
        competition = league.get(
            "name",
            "Compétition inconnue",
        )

        fixture_id = fixture.get("id", "N/D")
        match_datetime = fixture_datetime(item)

        time_text = (
            match_datetime.strftime("%H:%M")
            if match_datetime
            else "??:??"
        )

        status = status_label(
            fixture.get("status", {}).get("short")
        )

        if country != current_country:
            message += f"🌍 {country.upper()}\n\n"
            current_country = country

        message += (
            f"🏆 {competition}\n"
            f"🕐 {time_text}\n"
            f"⚽ {home} - {away}\n"
            f"📌 {status}\n"
            f"🆔 {fixture_id}\n\n"
        )

        if len(message) > 3800:
            await update.message.reply_text(message)
            message = "━━━━━━━━━━━━━━━━━━\n\n"

    message += (
        "━━━━━━━━━━━━━━━━━━\n"
        "📊 Analyse : /analyse ID"
    )

    await update.message.reply_text(message)


async def find_fixture_by_id(client, fixture_id):
    fixtures, error = await get_today_fixtures(client)

    if error:
        return None, error

    for item in fixtures:
        current_id = item.get("fixture", {}).get("id")

        if str(current_id) == str(fixture_id):
            return item, None

    return None, "Match introuvable dans les matchs du jour."


def build_team_stats_line(name, stats):
    if not stats:
        return ""

    fixtures = stats.get("fixtures", {})
    wins = fixtures.get("wins", {}).get("total")
    draws = fixtures.get("draws", {}).get("total")
    losses = fixtures.get("loses", {}).get("total")
    played = fixtures.get("played", {}).get("total")

    goals = stats.get("goals", {})
    goals_for = (
        goals.get("for", {})
        .get("total", {})
        .get("total")
    )
    goals_against = (
        goals.get("against", {})
        .get("total", {})
        .get("total")
    )

    return (
        f"• {name} : {played if played is not None else 'N/D'} matchs, "
        f"{wins or 0}V {draws or 0}N {losses or 0}D, "
        f"buts {goals_for if goals_for is not None else 'N/D'}-"
        f"{goals_against if goals_against is not None else 'N/D'}\n"
    )


async def analyse_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not API_KEY:
        await update.message.reply_text(
            "❌ Clé API-Football absente."
        )
        return

    if (
        not context.args
        or not context.args[0].isdigit()
    ):
        await update.message.reply_text(
            "📊 ANALYSE D'UN MATCH\n\n"
            "Utilisation : /analyse ID\n\n"
            "Exemple : /analyse 1561400"
        )
        return

    fixture_id = context.args[0]

    async with httpx.AsyncClient(timeout=20) as client:
        item, error = await find_fixture_by_id(
            client,
            fixture_id,
        )

        if error:
            await update.message.reply_text(
                f"❌ {error}"
            )
            return

        prediction_data, prediction_error, remaining = (
            await api_get(
                client,
                "predictions",
                {"fixture": fixture_id},
            )
        )

        prediction_item = {}
        predictions = {}

        if prediction_data:
            response_list = prediction_data.get(
                "response",
                [],
            )

            if response_list:
                prediction_item = response_list[0]
                predictions = (
                    prediction_item.get(
                        "predictions",
                        {},
                    )
                    or {}
                )

        teams = item.get("teams", {})
        home = teams.get("home", {})
        away = teams.get("away", {})

        home_id = home.get("id")
        away_id = away.get("id")

        league = item.get("league", {})
        league_id = league.get("id")
        season = league.get("season")

        h2h_data = None
        standings_data = None
        home_stats = None
        away_stats = None

        if (
            home_id
            and away_id
            and (
                remaining is None
                or remaining >= 8
            )
        ):
            h2h_data, _, remaining = await api_get(
                client,
                "fixtures/headtohead",
                {"h2h": f"{home_id}-{away_id}"},
            )

        if (
            league_id
            and season
            and (
                remaining is None
                or remaining >= 7
            )
        ):
            standings_data, _, remaining = await api_get(
                client,
                "standings",
                {
                    "league": league_id,
                    "season": season,
                },
            )

        if (
            league_id
            and season
            and home_id
            and away_id
            and (
                remaining is None
                or remaining >= 5
            )
        ):
            home_stats, _, remaining = await api_get(
                client,
                "teams/statistics",
                {
                    "league": league_id,
                    "season": season,
                    "team": home_id,
                },
            )

            if (
                remaining is None
                or remaining >= 4
            ):
                away_stats, _, remaining = await api_get(
                    client,
                    "teams/statistics",
                    {
                        "league": league_id,
                        "season": season,
                        "team": away_id,
                    },
                )

    fixture = item.get("fixture", {})
    goals = item.get("goals", {})

    home_name = home.get("name", "Inconnu")
    away_name = away.get("name", "Inconnu")

    country = league.get("country", "Inconnu")
    competition = league.get(
        "name",
        "Compétition inconnue",
    )

    match_datetime = fixture_datetime(item)

    formatted_date = (
        match_datetime.strftime("%d/%m/%Y à %H:%M")
        if match_datetime
        else "Date inconnue"
    )

    status = status_label(
        fixture.get("status", {}).get("short")
    )

    venue = (
        fixture.get("venue", {}).get("name")
        or "Non communiqué"
    )

    message = (
        "📊 SPORT ANALYZER\n\n"
        f"🌍 {country}\n"
        f"🏆 {competition}\n"
        f"⚽ {home_name} - {away_name}\n"
        f"🕐 {formatted_date}\n"
        f"📌 {status}\n"
        f"🏟️ {venue}\n"
        f"🆔 {fixture_id}\n"
    )

    if (
        goals.get("home") is not None
        or goals.get("away") is not None
    ):
        message += (
            f"\n🥅 SCORE : "
            f"{goals.get('home', '?')} - "
            f"{goals.get('away', '?')}\n"
        )

    percent_data = predictions.get(
        "percent",
        {},
    )

    home_pct = percent_data.get("home", "N/D")
    draw_pct = percent_data.get("draw", "N/D")
    away_pct = percent_data.get("away", "N/D")

    message += (
        "\n━━━━━━━━━━━━━━━━━━\n"
        "📈 PROBABILITÉS API\n\n"
        f"1️⃣ {home_name} : {fmt_pct(home_pct)}\n"
        f"⚖️ Nul : {fmt_pct(draw_pct)}\n"
        f"2️⃣ {away_name} : {fmt_pct(away_pct)}\n"
    )

    home_probability = pct(home_pct)
    draw_probability = pct(draw_pct)
    away_probability = pct(away_pct)

    if (
        home_probability is not None
        and draw_probability is not None
        and away_probability is not None
    ):
        message += (
            "\n🔄 DOUBLE CHANCE\n\n"
            f"1X : "
            f"{home_probability + draw_probability:.1f}%\n"
            f"X2 : "
            f"{draw_probability + away_probability:.1f}%\n"
            f"12 : "
            f"{home_probability + away_probability:.1f}%\n"
        )

    winner = predictions.get(
        "winner",
        {},
    )

    if isinstance(winner, dict):
        winner_name = winner.get(
            "name",
            "N/D",
        )
    else:
        winner_name = str(winner or "N/D")

    advice = predictions.get(
        "advice"
    ) or "Non disponible"

    under_over = predictions.get(
        "under_over"
    ) or "Non disponible"

    score = predictions.get(
        "score",
        {},
    )

    if not isinstance(score, dict):
        score = {}

    score_home = score.get("home")
    score_away = score.get("away")

    if (
        score_home is not None
        and score_away is not None
    ):
        score_text = (
            f"{score_home} - {score_away}"
        )
    else:
        score_text = "N/D"

    predicted_goals = predictions.get(
        "goals",
        {},
    )

    if not isinstance(
        predicted_goals,
        dict,
    ):
        predicted_goals = {}

    api_goal_home = predicted_goals.get("home")
    api_goal_away = predicted_goals.get("away")

    message += (
        "\n🎯 PRÉDICTION API\n\n"
        f"🏆 Vainqueur : {winner_name}\n"
        f"🥅 Score prévu : {score_text}\n"
        f"📊 Seuils buts API : "
        f"{api_goal_home if api_goal_home is not None else 'N/D'} / "
        f"{api_goal_away if api_goal_away is not None else 'N/D'}\n"
        f"📈 Over/Under : {under_over}\n"
        f"💡 Conseil API : {advice}\n"
    )

    comparison = prediction_item.get(
        "comparison",
        {},
    )

    if comparison:
        message += (
            "\n📊 COMPARAISON API\n\n"
        )

        labels = {
            "form": "Forme",
            "att": "Attaque",
            "def": "Défense",
            "h2h": "H2H",
            "poisson_distribution": "Poisson",
        }

        for key, label in labels.items():
            value = comparison.get(key)

            if isinstance(value, dict):
                message += (
                    f"• {label} : "
                    f"{value.get('home', 'N/D')} / "
                    f"{value.get('away', 'N/D')}\n"
                )
            elif value is not None:
                message += (
                    f"• {label} : "
                    f"{value}\n"
                )

    # Modèle transparent basé uniquement sur les buts numériques
    # fournis par l'API.
    lambda_home = safe_num(api_goal_home)
    lambda_away = safe_num(api_goal_away)

    matrix = poisson_goal_probs(
        lambda_home,
        lambda_away,
    )

    markets = poisson_markets(matrix)
    likely_scores = most_likely_scores(matrix)

    if markets:
        message += (
            "\n🧮 MODÈLE DE BUTS\n\n"
            f"1️⃣ {home_name} : "
            f"{markets['home']:.1f}%\n"
            f"⚖️ Nul : "
            f"{markets['draw']:.1f}%\n"
            f"2️⃣ {away_name} : "
            f"{markets['away']:.1f}%\n"
            f"⚽ BTTS Oui : "
            f"{markets['btts']:.1f}%\n"
            f"⚽ Over 1.5 : "
            f"{markets['over15']:.1f}%\n"
            f"⚽ Over 2.5 : "
            f"{markets['over25']:.1f}%\n"
            f"⚽ Under 3.5 : "
            f"{markets['under35']:.1f}%\n"
        )

        if likely_scores:
            message += (
                "\n🎯 SCORES LES PLUS PROBABLES\n\n"
            )

            for (
                score_home_value,
                score_away_value,
                score_probability,
            ) in likely_scores:
                message += (
                    f"• {score_home_value}-"
                    f"{score_away_value} : "
                    f"{score_probability:.1f}%\n"
                )
    else:
        message += (
            "\n🧮 MODÈLE DE BUTS\n\n"
            "Données numériques de buts insuffisantes "
            "pour calculer BTTS et Over/Under.\n"
        )

    if standings_data:
        standings_rows = []

        for table in standings_data.get(
            "response",
            [],
        ):
            groups = (
                table.get("league", {})
                .get("standings", [])
            )

            for group in groups:
                if isinstance(group, list):
                    standings_rows.extend(group)

        home_rank = next(
            (
                row
                for row in standings_rows
                if row.get("team", {}).get("id") == home_id
            ),
            None,
        )

        away_rank = next(
            (
                row
                for row in standings_rows
                if row.get("team", {}).get("id") == away_id
            ),
            None,
        )

        if home_rank or away_rank:
            message += (
                "\n🏆 CLASSEMENT\n\n"
            )

            if home_rank:
                message += (
                    f"• {home_name} : "
                    f"{home_rank.get('rank', 'N/D')}e, "
                    f"{home_rank.get('points', 'N/D')} pts\n"
                )

            if away_rank:
                message += (
                    f"• {away_name} : "
                    f"{away_rank.get('rank', 'N/D')}e, "
                    f"{away_rank.get('points', 'N/D')} pts\n"
                )

    if home_stats or away_stats:
        message += (
            "\n📈 STATISTIQUES SAISON\n\n"
        )

        message += build_team_stats_line(
            home_name,
            home_stats,
        )

        message += build_team_stats_line(
            away_name,
            away_stats,
        )

    if h2h_data:
        h2h_matches = h2h_data.get(
            "response",
            [],
        )

        if h2h_matches:
            recent = h2h_matches[:5]
            home_wins = 0
            draws = 0
            away_wins = 0

            for match in recent:
                match_goals = match.get(
                    "goals",
                    {},
                )

                home_goals = match_goals.get(
                    "home"
                )

                away_goals = match_goals.get(
                    "away"
                )

                if (
                    home_goals is None
                    or away_goals is None
                ):
                    continue

                if home_goals > away_goals:
                    home_wins += 1
                elif home_goals == away_goals:
                    draws += 1
                else:
                    away_wins += 1

            message += (
                "\n🤝 H2H RÉCENT\n\n"
                f"Matchs analysés : {len(recent)}\n"
                f"{home_name} : {home_wins}\n"
                f"Nuls : {draws}\n"
                f"{away_name} : {away_wins}\n"
            )

    if prediction_error:
        message += (
            "\n⚠️ Prédiction API indisponible : "
            f"{prediction_error}\n"
        )

    if remaining is not None:
        message += (
            f"\n📦 Quota API restant détecté : "
            f"{remaining}\n"
        )

    message += (
        "\n━━━━━━━━━━━━━━━━━━\n"
        "ℹ️ Les probabilités du modèle sont des "
        "calculs dérivés des données disponibles. "
        "Elles ne garantissent pas un résultat."
    )

    await update.message.reply_text(message)


async def probabilite_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if (
        not context.args
        or not context.args[0].isdigit()
    ):
        await update.message.reply_text(
            "Utilisation : /probabilite ID"
        )
        return

    await analyse_command(
        update,
        context,
    )


async def buts_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if (
        not context.args
        or not context.args[0].isdigit()
    ):
        await update.message.reply_text(
            "Utilisation : /buts ID"
        )
        return

    await analyse_command(
        update,
        context,
    )


async def buteur_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if (
        not context.args
        or not context.args[0].isdigit()
    ):
        await update.message.reply_text(
            "Utilisation : /buteur ID"
        )
        return

    fixture_id = context.args[0]

    async with httpx.AsyncClient(timeout=20) as client:
        data, error, _ = await api_get(
            client,
            "fixtures",
            {"id": fixture_id},
        )

    if error:
        await update.message.reply_text(
            f"❌ {error}"
        )
        return

    response = data.get(
        "response",
        [],
    )

    if not response:
        await update.message.reply_text(
            "❌ Match introuvable."
        )
        return

    item = response[0]
    events = item.get(
        "events",
        [],
    )

    scorers = []

    for event in events:
        if event.get("type") != "Goal":
            continue

        player = (
            event.get("player", {})
            .get("name", "Inconnu")
        )

        assist = (
            event.get("assist", {})
            .get("name")
        )

        detail = event.get(
            "detail",
            "",
        )

        scorers.append(
            (
                player,
                assist,
                detail,
            )
        )

    if not scorers:
        await update.message.reply_text(
            "⚽ Aucun buteur disponible "
            "pour ce match."
        )
        return

    message = (
        "⚽ BUTEURS DU MATCH\n\n"
    )

    for (
        player,
        assist,
        detail,
    ) in scorers:
        assist_text = (
            f" | passe : {assist}"
            if assist
            else ""
        )

        message += (
            f"• {player}"
            f"{assist_text}"
        )

        if detail:
            message += f" ({detail})"

        message += "\n"

    await update.message.reply_text(
        message
    )


async def webhook(request: Request):
    global telegram_app

    try:
        data = await request.json()

        update = Update.de_json(
            data,
            telegram_app.bot,
        )

        await telegram_app.process_update(
            update
        )

        return JSONResponse(
            {"ok": True}
        )

    except Exception as exc:
        print(
            f"Webhook error: {exc}"
        )

        return JSONResponse(
            {"ok": False},
            status_code=500,
        )


async def health(request: Request):
    return JSONResponse(
        {
            "status": "ok",
            "bot": "sport-analyzer-bot",
        }
    )


@asynccontextmanager
async def lifespan(app):
    global telegram_app

    if not TOKEN:
        raise RuntimeError(
            "TELEGRAM_TOKEN est manquant."
        )

    if not RENDER_URL:
        raise RuntimeError(
            "RENDER_EXTERNAL_URL est manquant."
        )

    telegram_app = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    telegram_app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "match",
            match_command,
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "analyse",
            analyse_command,
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "buteur",
            buteur_command,
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "buts",
            buts_command,
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "probabilite",
            probabilite_command,
        )
    )

    await telegram_app.initialize()
    await telegram_app.start()

    webhook_url = (
        f"{RENDER_URL}/telegram"
    )

    await telegram_app.bot.set_webhook(
        webhook_url
    )

    print(
        f"Webhook Telegram configuré : "
        f"{webhook_url}"
    )

    print(
        "Bot démarré."
    )

    yield

    await telegram_app.stop()
    await telegram_app.shutdown()


routes = [
    Route(
        "/telegram",
        webhook,
        methods=["POST"],
    ),
    Route(
        "/health",
        health,
        methods=["GET"],
    ),
]

app = Starlette(
    routes=routes,
    lifespan=lifespan,
)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
    )
