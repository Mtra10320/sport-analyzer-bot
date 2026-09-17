

import os
import math
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor

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
DB_PATH = os.getenv("DB_PATH", "sport_analyzer.db")

telegram_app = None
CACHE = {}

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
        "Premijer Liga", "1st League - FBiH", "1st League - RS"
    },
    "Albania": {
        "Superliga", "1st Division", "Kategoria Superiore", "Kategoria e Parë"
    },
    "Iceland": {"Úrvalsdeild", "1. Deild", "Besta deild"},
    "Luxembourg": {
        "National Division", "Division 2", "Nationaldivisioun", "Éierepromotioun"
    },
    "Malta": {"Premier League", "Challenge League"},
}

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def today_paris():
    return datetime.now(PARIS).strftime("%Y-%m-%d")

def pct(value):
    try:
        return float(str(value).replace("%", "").replace(",", "."))
    except Exception:
        return None

def fmt_pct(value):
    x = pct(value)
    return f"{x:.1f}%" if x is not None else "N/D"

def num(value):
    try:
        return float(value)
    except Exception:
        return None

def clamp(value, low=0.0, high=100.0):
    return max(low, min(high, value))

def normalize3(a, b, c):
    vals = [max(0.0, float(a)), max(0.0, float(b)), max(0.0, float(c))]
    total = sum(vals)
    if total <= 0:
        return 33.33, 33.34, 33.33
    return tuple(v * 100 / total for v in vals)

def poisson_pmf(k, lam):
    if lam is None or lam < 0:
        return None
    try:
        return math.exp(-lam) * (lam ** k) / math.factorial(k)
    except Exception:
        return None

def poisson_matrix(home_xg, away_xg, max_goals=7):
    if home_xg is None or away_xg is None:
        return None
    matrix = {}
    total = 0.0
    for h in range(max_goals + 1):
        ph = poisson_pmf(h, home_xg)
        for a in range(max_goals + 1):
            pa = poisson_pmf(a, away_xg)
            p = (ph or 0) * (pa or 0)
            matrix[(h, a)] = p
            total += p
    if total <= 0:
        return None
    return {k: v / total for k, v in matrix.items()}

def poisson_markets(matrix):
    if not matrix:
        return {}
    out = {
        "home": 0.0, "draw": 0.0, "away": 0.0,
        "btts": 0.0, "over15": 0.0, "over25": 0.0,
        "over35": 0.0, "under25": 0.0, "under35": 0.0
    }
    for (h, a), p in matrix.items():
        if h > a:
            out["home"] += p
        elif h == a:
            out["draw"] += p
        else:
            out["away"] += p
        if h > 0 and a > 0:
            out["btts"] += p
        if h + a >= 2:
            out["over15"] += p
        if h + a >= 3:
            out["over25"] += p
        if h + a >= 4:
            out["over35"] += p
        if h + a <= 2:
            out["under25"] += p
        if h + a <= 3:
            out["under35"] += p
    return {k: v * 100 for k, v in out.items()}

def likely_scores(matrix, limit=5):
    if not matrix:
        return []
    rows = sorted(matrix.items(), key=lambda x: x[1], reverse=True)
    return [(h, a, p * 100) for (h, a), p in rows[:limit]]

def status_label(code):
    return {
        "NS": "À venir", "TBD": "Horaire à confirmer",
        "1H": "1ère mi-temps", "HT": "Mi-temps",
        "2H": "2ème mi-temps", "ET": "Prolongation",
        "P": "Tirs au but", "FT": "Terminé", "AET": "Terminé",
        "PST": "Reporté", "CANC": "Annulé", "SUSP": "Suspendu"
    }.get(code, code or "N/D")

def fixture_dt(item):
    raw = item.get("fixture", {}).get("date", "")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(PARIS)
    except Exception:
        return None

def league_allowed(item):
    league = item.get("league", {})
    return league.get("name", "") in ESSENTIAL_LEAGUES.get(
        league.get("country", ""), set()
    )

def home_name(item):
    return item.get("teams", {}).get("home", {}).get("name", "Inconnu")

def away_name(item):
    return item.get("teams", {}).get("away", {}).get("name", "Inconnu")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            fixture_id INTEGER NOT NULL,
            market TEXT NOT NULL,
            selection TEXT NOT NULL,
            stake REAL NOT NULL,
            odds REAL,
            probability REAL,
            result TEXT,
            profit REAL,
            created_at TEXT NOT NULL,
            settled_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fixture_id INTEGER NOT NULL,
            market TEXT NOT NULL,
            selection TEXT NOT NULL,
            probability REAL,
            odds REAL,
            created_at TEXT NOT NULL,
            result TEXT
        )
    """)
    conn.commit()
    conn.close()

def db_add_bet(user_id, fixture_id, market, selection, stake, odds=None, probability=None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("""
        INSERT INTO bets
        (user_id, fixture_id, market, selection, stake, odds, probability, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, fixture_id, market, selection, stake, odds, probability, now_iso()))
    bet_id = cur.lastrowid
    conn.commit()
    conn.close()
    return bet_id

def db_settle_bet(user_id, bet_id, result):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT stake, odds FROM bets WHERE id=? AND user_id=? AND result IS NULL",
        (bet_id, user_id)
    ).fetchone()
    if not row:
        conn.close()
        return None
    stake, odds = row
    if result == "win":
        profit = stake * ((odds or 1.0) - 1.0)
    elif result == "loss":
        profit = -stake
    elif result == "void":
        profit = 0.0
    else:
        conn.close()
        return None
    conn.execute(
        "UPDATE bets SET result=?, profit=?, settled_at=? WHERE id=? AND user_id=?",
        (result, profit, now_iso(), bet_id, user_id)
    )
    conn.commit()
    conn.close()
    return profit

def db_summary(user_id):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("""
        SELECT
            COALESCE(SUM(stake),0),
            COALESCE(SUM(CASE WHEN result='win' THEN stake ELSE 0 END),0),
            COALESCE(SUM(profit),0),
            COUNT(*),
            SUM(CASE WHEN result='win' THEN 1 ELSE 0 END)
        FROM bets WHERE user_id=?
    """, (user_id,)).fetchone()
    conn.close()
    total_staked, winning_stakes, profit, count, wins = row
    wins = wins or 0
    roi = (profit / total_staked * 100) if total_staked else 0.0
    return total_staked, profit, count, wins, roi

def extract_remaining(response):
    for key in (
        "x-ratelimit-requests-remaining",
        "X-Ratelimit-Requests-Remaining",
    ):
        if key in response.headers:
            try:
                return int(response.headers[key])
            except Exception:
                pass
    return None

async def api_get(client, endpoint, params=None, cache_key=None, ttl=60):
    if not API_KEY:
        return None, "Clé API-Football absente.", None

    if cache_key:
        cached = CACHE.get(cache_key)
        if cached and cached["expires"] > datetime.now(timezone.utc).timestamp():
            return cached["data"], None, cached.get("remaining")

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
        return None, f"API-Football HTTP {response.status_code}", remaining

    try:
        data = response.json()
    except Exception:
        return None, "Réponse API-Football invalide.", remaining

    if data.get("errors"):
        return None, f"API-Football : {data['errors']}", remaining

    if cache_key:
        CACHE[cache_key] = {
            "data": data,
            "expires": datetime.now(timezone.utc).timestamp() + ttl,
            "remaining": remaining,
        }

    return data, None, remaining

async def get_today_fixtures(client):
    data, error, remaining = await api_get(
        client, "fixtures",
        {"date": today_paris(), "timezone": "Europe/Paris"},
        cache_key=f"today:{today_paris()}", ttl=90
    )
    if error:
        return None, error, remaining
    return data.get("response", []), None, remaining

async def find_today_fixture(client, fixture_id):
    fixtures, error, remaining = await get_today_fixtures(client)
    if error:
        return None, error, remaining
    for item in fixtures:
        if str(item.get("fixture", {}).get("id")) == str(fixture_id):
            return item, None, remaining
    return None, "Match introuvable dans les matchs du jour.", remaining

def parse_form(fixtures, team_id):
    rows = []
    for item in fixtures or []:
        fixture = item.get("fixture", {})
        status = fixture.get("status", {}).get("short")
        if status not in {"FT", "AET", "P"}:
            continue
        teams = item.get("teams", {})
        goals = item.get("goals", {})
        h_id = teams.get("home", {}).get("id")
        a_id = teams.get("away", {}).get("id")
        hg = goals.get("home")
        ag = goals.get("away")
        if hg is None or ag is None:
            continue
        if team_id == h_id:
            gf, ga = hg, ag
            opponent = teams.get("away", {}).get("name", "?")
            result = "V" if gf > ga else "N" if gf == ga else "D"
        elif team_id == a_id:
            gf, ga = ag, hg
            opponent = teams.get("home", {}).get("name", "?")
            result = "V" if gf > ga else "N" if gf == ga else "D"
        else:
            continue
        rows.append({"gf": gf, "ga": ga, "result": result, "opponent": opponent})
    return rows[:5]

def form_metrics(form):
    if not form:
        return {}
    gf = sum(x["gf"] for x in form)
    ga = sum(x["ga"] for x in form)
    wins = sum(x["result"] == "V" for x in form)
    draws = sum(x["result"] == "N" for x in form)
    losses = sum(x["result"] == "D" for x in form)
    n = len(form)
    return {
        "matches": n,
        "gf_avg": gf / n,
        "ga_avg": ga / n,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "points_avg": (wins * 3 + draws) / n,
    }

def season_metrics(stats):
    if not stats:
        return {}
    s = stats.get("response", {}).get("statistics", {})
    fixtures = s.get("fixtures", {})
    goals = s.get("goals", {})
    played = num(fixtures.get("played", {}).get("total"))
    gf = num(goals.get("for", {}).get("total", {}).get("total"))
    ga = num(goals.get("against", {}).get("total", {}).get("total"))
    return {
        "played": played,
        "gf_avg": gf / played if played and gf is not None else None,
        "ga_avg": ga / played if played and ga is not None else None,
        "gf": gf,
        "ga": ga,
    }

def build_model(home_form, away_form, home_season, away_season, api_probs):
    hf = form_metrics(home_form)
    af = form_metrics(away_form)

    home_attack = hf.get("gf_avg")
    away_attack = af.get("gf_avg")
    home_defense = hf.get("ga_avg")
    away_defense = af.get("ga_avg")

    if home_attack is None:
        home_attack = home_season.get("gf_avg")
    if away_attack is None:
        away_attack = away_season.get("gf_avg")
    if home_defense is None:
        home_defense = home_season.get("ga_avg")
    if away_defense is None:
        away_defense = away_season.get("ga_avg")

    if home_attack is None or away_attack is None:
        return None

    home_xg_parts = [home_attack]
    away_xg_parts = [away_attack]

    if away_defense is not None:
        home_xg_parts.append(away_defense)
    if home_defense is not None:
        away_xg_parts.append(home_defense)

    home_xg = sum(home_xg_parts) / len(home_xg_parts)
    away_xg = sum(away_xg_parts) / len(away_xg_parts)

    # Léger avantage domicile, plafonné pour éviter un biais excessif.
    home_xg *= 1.08
    home_xg = clamp(home_xg, 0.15, 4.0)
    away_xg = clamp(away_xg, 0.10, 4.0)

    matrix = poisson_matrix(home_xg, away_xg)
    markets = poisson_markets(matrix)

    api_h, api_d, api_a = api_probs
    model_h, model_d, model_a = normalize3(
        markets["home"], markets["draw"], markets["away"]
    )

    if api_h is not None and api_d is not None and api_a is not None:
        final_h, final_d, final_a = normalize3(
            0.55 * api_h + 0.45 * model_h,
            0.55 * api_d + 0.45 * model_d,
            0.55 * api_a + 0.45 * model_a,
        )
    else:
        final_h, final_d, final_a = model_h, model_d, model_a

    return {
        "home_xg": home_xg,
        "away_xg": away_xg,
        "matrix": matrix,
        "markets": markets,
        "final": (final_h, final_d, final_a),
        "form_home": hf,
        "form_away": af,
    }

def fair_odds(probability):
    return round(100 / probability, 2) if probability and probability > 0 else None

def value_pct(probability, odds):
    if probability is None or odds is None or odds <= 0:
        return None
    return (probability / 100 * odds - 1) * 100

def odds_from_api(data):
    results = []
    for block in (data or {}).get("response", []):
        for bookmaker in block.get("bookmakers", []):
            bookmaker_name = bookmaker.get("name", "Bookmaker")
            for bet in bookmaker.get("bets", []):
                bet_name = bet.get("name", "")
                values = bet.get("values", [])
                for value in values:
                    odd = num(value.get("odd"))
                    if odd is None:
                        continue
                    results.append({
                        "bookmaker": bookmaker_name,
                        "market": bet_name,
                        "selection": value.get("value", ""),
                        "odd": odd,
                    })
    return results

def find_common_market(odds_rows, market_names):
    for row in odds_rows:
        if any(name.lower() in row["market"].lower() for name in market_names):
            yield row

def confidence_label(final_probs, data_quality):
    spread = max(final_probs) - min(final_probs)
    if data_quality >= 0.75 and spread >= 30:
        return "Élevée"
    if data_quality >= 0.50 and spread >= 15:
        return "Moyenne"
    return "Faible"

async def start(update, context):
    await update.message.reply_text(
        "🤖 SPORT ANALYZER\n\n"
        "Analyse football, probabilités, buts, cotes et suivi.\n\n"
        "/match\n"
        "/analyse ID\n"
        "/cotes ID\n"
        "/buteur ID\n"
        "/buts ID\n"
        "/probabilite ID\n"
        "/mise ID marché sélection montant [cote]\n"
        "/resultat ID_BET win|loss|void\n"
        "/bankroll\n"
        "/help"
    )

async def help_command(update, context):
    await update.message.reply_text(
        "📊 SPORT ANALYZER\n\n"
        "/match = matchs prioritaires du jour\n"
        "/analyse ID = analyse complète\n"
        "/cotes ID = cotes disponibles + value théorique\n"
        "/buteur ID = buteurs/événements du match\n"
        "/buts ID = marchés de buts calculés\n"
        "/probabilite ID = probabilités principales\n\n"
        "💶 SUIVI\n"
        "/mise ID marché sélection montant [cote]\n"
        "Exemple : /mise 1561400 1X FC_Orenburg 10 1.80\n\n"
        "/resultat ID_BET win|loss|void\n"
        "/bankroll = bénéfice, mises et ROI"
    )

async def match_command(update, context):
    if not API_KEY:
        await update.message.reply_text("❌ Clé API-Football absente.")
        return
    async with httpx.AsyncClient(timeout=20) as client:
        fixtures, error, _ = await get_today_fixtures(client)
    if error:
        await update.message.reply_text(f"❌ {error}")
        return

    selected = [x for x in fixtures if league_allowed(x)]
    selected.sort(key=lambda x: fixture_dt(x) or datetime.max.replace(tzinfo=PARIS))

    if not selected:
        await update.message.reply_text("⚽ Aucun match prioritaire aujourd'hui.")
        return

    message = f"⚽ MATCHS PRIORITAIRES\n📅 {today_paris()}\n📊 {len(selected)} matchs\n\n"
    current_country = None

    for item in selected:
        league = item.get("league", {})
        fixture = item.get("fixture", {})
        country = league.get("country", "Inconnu")
        if country != current_country:
            message += f"🌍 {country.upper()}\n\n"
            current_country = country
        dt = fixture_dt(item)
        time_text = dt.strftime("%H:%M") if dt else "??:??"
        message += (
            f"🏆 {league.get('name', 'N/D')}\n"
            f"🕐 {time_text}\n"
            f"⚽ {home_name(item)} - {away_name(item)}\n"
            f"📌 {status_label(fixture.get('status', {}).get('short'))}\n"
            f"🆔 {fixture.get('id', 'N/D')}\n\n"
        )
        if len(message) > 3800:
            await update.message.reply_text(message)
            message = "━━━━━━━━━━━━━━━━━━\n\n"
    await update.message.reply_text(message + "━━━━━━━━━━━━━━━━━━\n📊 /analyse ID")

# Analyse standard: environ 9 appels API maximum sur cache froid.
# Le plan gratuit est limité en volume et en débit, donc les résultats
# stables sont mis en cache et les appels non essentiels sont évités.
async def full_analysis_data(fixture_id):
    async with httpx.AsyncClient(timeout=20) as client:
        item, error, remaining = await find_today_fixture(client, fixture_id)
        if error:
            return None, error

        prediction_data, prediction_error, remaining = await api_get(
            client, "predictions", {"fixture": fixture_id},
            cache_key=f"prediction:{fixture_id}", ttl=120
        )

        league = item.get("league", {})
        teams = item.get("teams", {})
        home_id = teams.get("home", {}).get("id")
        away_id = teams.get("away", {}).get("id")
        league_id = league.get("id")
        season = league.get("season")

        standings_data = None
        if league_id and season:
            standings_data, _, remaining = await api_get(
                client, "standings",
                {"league": league_id, "season": season},
                cache_key=f"standings:{league_id}:{season}", ttl=900
            )

        home_last_data, _, remaining = await api_get(
            client, "fixtures",
            {"team": home_id, "last": 5, "status": "FT"},
            cache_key=f"last5:{home_id}", ttl=900
        ) if home_id else (None, None, remaining)

        away_last_data, _, remaining = await api_get(
            client, "fixtures",
            {"team": away_id, "last": 5, "status": "FT"},
            cache_key=f"last5:{away_id}", ttl=900
        ) if away_id else (None, None, remaining)

        home_stats_data = None
        away_stats_data = None

        odds_data, _, remaining = await api_get(
            client, "odds", {"fixture": fixture_id},
            cache_key=f"odds:{fixture_id}", ttl=300
        )

        home_injuries, _, remaining = await api_get(
            client, "injuries",
            {"team": home_id, "season": season},
            cache_key=f"injuries:{home_id}:{season}", ttl=600
        ) if home_id and season else (None, None, remaining)

        away_injuries, _, remaining = await api_get(
            client, "injuries",
            {"team": away_id, "season": season},
            cache_key=f"injuries:{away_id}:{season}", ttl=600
        ) if away_id and season else (None, None, remaining)

        h2h_data, _, remaining = await api_get(
            client, "fixtures/headtohead",
            {"h2h": f"{home_id}-{away_id}"},
            cache_key=f"h2h:{home_id}:{away_id}", ttl=1800
        ) if home_id and away_id else (None, None, remaining)

    return {
        "item": item,
        "detail": item,
        "prediction": prediction_data,
        "prediction_error": prediction_error,
        "standings": standings_data,
        "home_last": home_last_data,
        "away_last": away_last_data,
        "home_stats": home_stats_data,
        "away_stats": away_stats_data,
        "odds": odds_data,
        "home_injuries": home_injuries,
        "away_injuries": away_injuries,
        "h2h": h2h_data,
        "remaining": remaining,
    }, None

def standings_position(data, team_id):
    if not data:
        return None
    for table in data.get("response", []):
        for group in table.get("league", {}).get("standings", []):
            if not isinstance(group, list):
                continue
            for row in group:
                if row.get("team", {}).get("id") == team_id:
                    return row
    return None

def injury_summary(data):
    if not data:
        return []
    result = []
    for row in data.get("response", []):
        player = row.get("player", {})
        if player.get("name"):
            result.append(
                (
                    player.get("name"),
                    row.get("type") or row.get("reason") or "Absence"
                )
            )
    return result[:8]

def build_analysis_message(data, fixture_id):
    item = data["item"]
    detail = data["detail"] or item
    prediction_data = data["prediction"] or {}
    prediction_item = (
        prediction_data.get("response", [None])[0]
        if prediction_data.get("response")
        else {}
    ) or {}
    predictions = prediction_item.get("predictions", {}) or {}

    home = item.get("teams", {}).get("home", {})
    away = item.get("teams", {}).get("away", {})
    home_id = home.get("id")
    away_id = away.get("id")
    hn = home.get("name", "Inconnu")
    an = away.get("name", "Inconnu")

    league = item.get("league", {})
    dt = fixture_dt(item)
    date_text = dt.strftime("%d/%m/%Y à %H:%M") if dt else "N/D"

    message = (
        "📊 SPORT ANALYZER\n\n"
        f"🌍 {league.get('country', 'N/D')}\n"
        f"🏆 {league.get('name', 'N/D')}\n"
        f"⚽ {hn} - {an}\n"
        f"🕐 {date_text}\n"
        f"📌 {status_label(item.get('fixture', {}).get('status', {}).get('short'))}\n"
        f"🏟️ {item.get('fixture', {}).get('venue', {}).get('name') or 'N/D'}\n"
        f"🆔 {fixture_id}\n"
    )

    goals = item.get("goals", {})
    if goals.get("home") is not None or goals.get("away") is not None:
        message += f"\n🥅 SCORE : {goals.get('home', '?')} - {goals.get('away', '?')}\n"

    p = predictions.get("percent", {}) or {}
    api_h, api_d, api_a = pct(p.get("home")), pct(p.get("draw")), pct(p.get("away"))

    message += (
        "\n━━━━━━━━━━━━━━━━━━\n"
        "📈 PROBABILITÉS API\n\n"
        f"1️⃣ {hn} : {fmt_pct(api_h)}\n"
        f"⚖️ Nul : {fmt_pct(api_d)}\n"
        f"2️⃣ {an} : {fmt_pct(api_a)}\n"
    )

    if api_h is not None and api_d is not None and api_a is not None:
        message += (
            "\n🔄 DOUBLE CHANCE\n\n"
            f"1X : {api_h + api_d:.1f}%\n"
            f"X2 : {api_d + api_a:.1f}%\n"
            f"12 : {api_h + api_a:.1f}%\n"
        )

    winner = predictions.get("winner", {})
    winner_name = winner.get("name", "N/D") if isinstance(winner, dict) else str(winner or "N/D")
    score = predictions.get("score", {}) or {}
    if score.get("home") is not None and score.get("away") is not None:
        api_score = f"{score['home']} - {score['away']}"
    else:
        api_score = "N/D"

    message += (
        "\n🎯 PRÉDICTION API\n\n"
        f"🏆 Vainqueur : {winner_name}\n"
        f"🥅 Score prévu : {api_score}\n"
        f"💡 Conseil : {predictions.get('advice') or 'N/D'}\n"
    )

    comparison = prediction_item.get("comparison", {}) or {}
    if comparison:
        message += "\n📊 COMPARAISON API\n\n"
        for key, label in {
            "form": "Forme", "att": "Attaque", "def": "Défense",
            "h2h": "H2H", "poisson_distribution": "Poisson"
        }.items():
            v = comparison.get(key)
            if isinstance(v, dict):
                message += f"• {label} : {v.get('home', 'N/D')} / {v.get('away', 'N/D')}\n"

    home_form = parse_form((data["home_last"] or {}).get("response", []), home_id)
    away_form = parse_form((data["away_last"] or {}).get("response", []), away_id)
    hs = season_metrics(data["home_stats"])
    aws = season_metrics(data["away_stats"])

    model = build_model(home_form, away_form, hs, aws, (api_h, api_d, api_a))

    if model:
        fh, fd, fa = model["final"]
        mk = model["markets"]
        conf = confidence_label((fh, fd, fa), min(
            1.0,
            (len(home_form) + len(away_form)) / 10 +
            (0.25 if hs and aws else 0)
        ))

        message += (
            "\n🧠 MODÈLE SPORT ANALYZER\n\n"
            f"1️⃣ {hn} : {fh:.1f}%\n"
            f"⚖️ Nul : {fd:.1f}%\n"
            f"2️⃣ {an} : {fa:.1f}%\n"
            f"🎚️ Confiance : {conf}\n\n"
            f"⚽ BTTS Oui : {mk['btts']:.1f}%\n"
            f"⚽ BTTS Non : {100 - mk['btts']:.1f}%\n"
            f"📈 Over 1.5 : {mk['over15']:.1f}%\n"
            f"📈 Over 2.5 : {mk['over25']:.1f}%\n"
            f"📈 Over 3.5 : {mk['over35']:.1f}%\n"
            f"📉 Under 2.5 : {mk['under25']:.1f}%\n"
            f"📉 Under 3.5 : {mk['under35']:.1f}%\n"
            f"🥅 xG modèle : {model['home_xg']:.2f} - {model['away_xg']:.2f}\n"
        )

        message += "\n🎯 SCORES PROBABLES\n\n"
        for h, a, pr in likely_scores(model["matrix"], 3):
            message += f"• {h}-{a} : {pr:.1f}%\n"

        message += "\n📝 FORME RÉCENTE\n\n"
        message += (
            f"• {hn} : "
            f"{''.join(x['result'] for x in home_form) or 'N/D'} | "
            f"{form_metrics(home_form).get('gf_avg', 0):.2f} buts/match\n"
        )
        message += (
            f"• {an} : "
            f"{''.join(x['result'] for x in away_form) or 'N/D'} | "
            f"{form_metrics(away_form).get('gf_avg', 0):.2f} buts/match\n"
        )
    else:
        message += (
            "\n🧠 MODÈLE SPORT ANALYZER\n\n"
            "Données insuffisantes pour calculer le modèle de buts.\n"
        )

    home_rank = standings_position(data["standings"], home_id)
    away_rank = standings_position(data["standings"], away_id)
    if home_rank or away_rank:
        message += "\n🏆 CLASSEMENT\n\n"
        if home_rank:
            message += f"• {hn} : {home_rank.get('rank', 'N/D')}e | {home_rank.get('points', 'N/D')} pts\n"
        if away_rank:
            message += f"• {an} : {away_rank.get('rank', 'N/D')}e | {away_rank.get('points', 'N/D')} pts\n"

    hi = injury_summary(data["home_injuries"])
    ai = injury_summary(data["away_injuries"])
    if hi or ai:
        message += "\n🤕 ABSENCES DISPONIBLES\n\n"
        if hi:
            message += f"• {hn} : {len(hi)} absence(s)\n"
            for name, reason in hi[:5]:
                message += f"  - {name} ({reason})\n"
        if ai:
            message += f"• {an} : {len(ai)} absence(s)\n"
            for name, reason in ai[:5]:
                message += f"  - {name} ({reason})\n"

    h2h = (data["h2h"] or {}).get("response", [])
    if h2h:
        recent = h2h[:5]
        hw = d = aw = 0
        for m in recent:
            g = m.get("goals", {})
            gh, ga = g.get("home"), g.get("away")
            if gh is None or ga is None:
                continue
            mid = m.get("teams", {}).get("home", {}).get("id")
            if gh == ga:
                d += 1
            elif mid == home_id:
                hw += 1 if gh > ga else 0
                aw += 1 if gh < ga else 0
            else:
                aw += 1 if gh > ga else 0
                hw += 1 if gh < ga else 0
        message += (
            "\n🤝 H2H RÉCENT\n\n"
            f"Matchs analysés : {len(recent)}\n"
            f"{hn} : {hw}\n"
            f"Nuls : {d}\n"
            f"{an} : {aw}\n"
        )

    odds_rows = odds_from_api(data["odds"])
    if odds_rows:
        message += "\n💰 COTES DISPONIBLES\n\n"
        shown = 0
        for row in odds_rows:
            if row["market"].lower() in {"match winner", "1x2", "double chance"}:
                message += (
                    f"• {row['bookmaker']} | {row['market']} | "
                    f"{row['selection']} : {row['odd']:.2f}\n"
                )
                shown += 1
                if shown >= 12:
                    break
        if not shown:
            message += "Cotes disponibles mais marché principal non identifié.\n"
    else:
        message += "\n💰 COTES\n\nNon disponibles pour ce match.\n"

    message += (
        "\n━━━━━━━━━━━━━━━━━━\n"
        "ℹ️ Les probabilités du modèle sont des estimations "
        "calculées à partir des données disponibles. Elles ne "
        "garantissent pas le résultat d'un match."
    )
    return message

async def analyse_command(update, context):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Utilisation : /analyse ID\nExemple : /analyse 1561400")
        return
    await update.message.reply_text("🔎 Analyse en cours...")
    data, error = await full_analysis_data(context.args[0])
    if error:
        await update.message.reply_text(f"❌ {error}")
        return
    message = build_analysis_message(data, context.args[0])
    # Telegram accepte environ 4096 caractères par message.
    if len(message) <= 3900:
        await update.message.reply_text(message)
        return
    parts = []
    current = ""
    for paragraph in message.split("\n\n"):
        if len(current) + len(paragraph) + 2 > 3800:
            parts.append(current)
            current = paragraph
        else:
            current = (current + "\n\n" + paragraph).strip()
    if current:
        parts.append(current)
    for part in parts:
        await update.message.reply_text(part)

async def cotes_command(update, context):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Utilisation : /cotes ID")
        return
    fixture_id = context.args[0]
    async with httpx.AsyncClient(timeout=20) as client:
        data, error, _ = await api_get(
            client, "odds", {"fixture": fixture_id},
            cache_key=f"odds:{fixture_id}", ttl=300
        )
        prediction_data, _, _ = await api_get(
            client, "predictions", {"fixture": fixture_id},
            cache_key=f"prediction:{fixture_id}", ttl=120
        )
    if error:
        await update.message.reply_text(f"❌ {error}")
        return

    rows = odds_from_api(data)
    p = (prediction_data or {}).get("response", [{}])[0].get("predictions", {})
    pp = p.get("percent", {}) if isinstance(p, dict) else {}
    api_probs = {
        "Home": pct(pp.get("home")),
        "Draw": pct(pp.get("draw")),
        "Away": pct(pp.get("away")),
    }

    message = "💰 COTES & VALUE THÉORIQUE\n\n"
    shown = 0
    for row in rows:
        market = row["market"].lower()
        selection = row["selection"]
        probability = None
        if market in {"match winner", "1x2"}:
            if selection.lower() in {"home", "1"}:
                probability = api_probs["Home"]
            elif selection.lower() in {"draw", "x"}:
                probability = api_probs["Draw"]
            elif selection.lower() in {"away", "2"}:
                probability = api_probs["Away"]
        if probability is None:
            continue
        fair = fair_odds(probability)
        value = value_pct(probability, row["odd"])
        message += (
            f"🏦 {row['bookmaker']}\n"
            f"Marché : {row['market']}\n"
            f"Sélection : {selection}\n"
            f"Cote : {row['odd']:.2f}\n"
            f"Probabilité API : {probability:.1f}%\n"
            f"Cote équitable : {fair:.2f}\n"
            f"Value théorique : {value:+.1f}%\n\n"
        )
        shown += 1
        if shown >= 15:
            break

    if not shown:
        message += (
            "Aucune cote 1X2 exploitable avec une probabilité API "
            "correspondante.\n"
        )
    message += (
        "\n⚠️ La value est une comparaison mathématique avec la "
        "probabilité disponible, pas une garantie de gain."
    )
    await update.message.reply_text(message)

async def buts_command(update, context):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Utilisation : /buts ID")
        return
    data, error = await full_analysis_data(context.args[0])
    if error:
        await update.message.reply_text(f"❌ {error}")
        return
    item = data["item"]
    home_id = item.get("teams", {}).get("home", {}).get("id")
    away_id = item.get("teams", {}).get("away", {}).get("id")
    hf = parse_form((data["home_last"] or {}).get("response", []), home_id)
    af = parse_form((data["away_last"] or {}).get("response", []), away_id)
    model = build_model(
        hf, af,
        season_metrics(data["home_stats"]),
        season_metrics(data["away_stats"]),
        (None, None, None),
    )
    if not model:
        await update.message.reply_text("❌ Données insuffisantes pour calculer les marchés de buts.")
        return
    m = model["markets"]
    await update.message.reply_text(
        "⚽ MARCHÉS DE BUTS\n\n"
        f"BTTS Oui : {m['btts']:.1f}%\n"
        f"BTTS Non : {100-m['btts']:.1f}%\n"
        f"Over 1.5 : {m['over15']:.1f}%\n"
        f"Over 2.5 : {m['over25']:.1f}%\n"
        f"Over 3.5 : {m['over35']:.1f}%\n"
        f"Under 2.5 : {m['under25']:.1f}%\n"
        f"Under 3.5 : {m['under35']:.1f}%\n"
        f"xG modèle : {model['home_xg']:.2f} - {model['away_xg']:.2f}"
    )

async def probabilite_command(update, context):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Utilisation : /probabilite ID")
        return
    await analyse_command(update, context)

async def buteur_command(update, context):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Utilisation : /buteur ID")
        return
    fixture_id = context.args[0]
    async with httpx.AsyncClient(timeout=20) as client:
        data, error, _ = await api_get(
            client, "fixtures", {"id": fixture_id},
            cache_key=f"fixture_detail:{fixture_id}", ttl=120
        )
    if error:
        await update.message.reply_text(f"❌ {error}")
        return
    response = data.get("response", [])
    if not response:
        await update.message.reply_text("❌ Match introuvable.")
        return
    events = response[0].get("events", [])
    goals = [
        e for e in events
        if e.get("type") == "Goal"
    ]
    if not goals:
        await update.message.reply_text("⚽ Aucun buteur disponible pour ce match.")
        return
    message = "⚽ BUTS / BUTEURS\n\n"
    for e in goals:
        player = e.get("player", {}).get("name", "Inconnu")
        assist = e.get("assist", {}).get("name")
        time = e.get("time", {}).get("elapsed")
        detail = e.get("detail") or ""
        message += f"• {time or '?'}' {player}"
        if assist:
            message += f" | passe : {assist}"
        if detail:
            message += f" | {detail}"
        message += "\n"
    await update.message.reply_text(message)

async def mise_command(update, context):
    if len(context.args) < 4:
        await update.message.reply_text(
            "Utilisation : /mise ID marché sélection montant [cote]\n"
            "Exemple : /mise 1561400 1X FC_Orenburg 10 1.80"
        )
        return
    try:
        fixture_id = int(context.args[0])
        market = context.args[1]
        selection = context.args[2]
        stake = float(context.args[3].replace(",", "."))
        odds = float(context.args[4].replace(",", ".")) if len(context.args) >= 5 else None
        if stake <= 0 or (odds is not None and odds <= 1):
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Montant ou cote invalide.")
        return
    bet_id = db_add_bet(
        update.effective_user.id, fixture_id, market,
        selection, stake, odds
    )
    await update.message.reply_text(
        f"💶 Pari enregistré\n\n"
        f"ID : {bet_id}\n"
        f"Match : {fixture_id}\n"
        f"Marché : {market}\n"
        f"Sélection : {selection}\n"
        f"Mise : {stake:.2f} €\n"
        f"Cote : {odds:.2f}" if odds else
        f"💶 Pari enregistré\n\nID : {bet_id}\nMise : {stake:.2f} €"
    )

async def resultat_command(update, context):
    if len(context.args) != 2:
        await update.message.reply_text("Utilisation : /resultat ID_BET win|loss|void")
        return
    try:
        bet_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID de pari invalide.")
        return
    result = context.args[1].lower()
    if result not in {"win", "loss", "void"}:
        await update.message.reply_text("❌ Résultat : win, loss ou void.")
        return
    profit = db_settle_bet(update.effective_user.id, bet_id, result)
    if profit is None:
        await update.message.reply_text("❌ Pari introuvable ou déjà réglé.")
        return
    await update.message.reply_text(
        f"📊 Pari {bet_id} réglé\n"
        f"Résultat : {result}\n"
        f"Profit/perte : {profit:+.2f} €"
    )

async def bankroll_command(update, context):
    total, profit, count, wins, roi = db_summary(update.effective_user.id)
    await update.message.reply_text(
        "💶 BANKROLL\n\n"
        f"Mises enregistrées : {total:.2f} €\n"
        f"Profit/perte : {profit:+.2f} €\n"
        f"Paris : {count}\n"
        f"Paris gagnants : {wins}\n"
        f"ROI : {roi:+.2f}%"
    )

async def webhook(request: Request):
    global telegram_app
    try:
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
        return JSONResponse({"ok": True})
    except Exception as exc:
        print(f"Webhook error: {exc}")
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
    init_db()

    telegram_app = Application.builder().token(TOKEN).build()
    handlers = [
        ("start", start),
        ("help", help_command),
        ("match", match_command),
        ("analyse", analyse_command),
        ("cotes", cotes_command),
        ("buteur", buteur_command),
        ("buts", buts_command),
        ("probabilite", probabilite_command),
        ("mise", mise_command),
        ("resultat", resultat_command),
        ("bankroll", bankroll_command),
    ]
    for name, handler in handlers:
        telegram_app.add_handler(CommandHandler(name, handler))

    await telegram_app.initialize()
    await telegram_app.start()

    webhook_url = f"{RENDER_URL}/telegram"
    await telegram_app.bot.set_webhook(webhook_url)
    print(f"Webhook Telegram configuré : {webhook_url}")
    print("Bot démarré.")

    yield

    await telegram_app.stop()
    await telegram_app.shutdown()

routes = [
    Route("/telegram", webhook, methods=["POST"]),
    Route("/health", health, methods=["GET"]),
]

app = Starlette(routes=routes, lifespan=lifespan)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
