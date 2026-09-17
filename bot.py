

import os
import math
import sqlite3
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
import io

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BufferedInputFile
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ============================================================
# SPORT ANALYZER V13
# Primary source: Football-Data.org
# Fallback source: TheSportsDB
# API-Football is intentionally not used by this version.
# ============================================================

TOKEN = os.getenv("TELEGRAM_TOKEN")
FOOTBALL_DATA_KEY = os.getenv("FOOTBALL_DATA_KEY")
PORT = int(os.getenv("PORT", "10000"))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")
DB_PATH = os.getenv("DB_PATH", "sport_analyzer.db")

PARIS = ZoneInfo("Europe/Paris")
FD_BASE = "https://api.football-data.org/v4"
TSDB_BASE = "https://www.thesportsdb.com/api/v1/json/123"

CACHE = {}
telegram_app = None

# Football-Data.org free tier currently exposes these 12 competitions.
FREE_COMPETITIONS = {
    "CL": "Ligue des champions",
    "PPL": "Primeira Liga",
    "PL": "Premier League",
    "DED": "Eredivisie",
    "BL1": "Bundesliga",
    "FL1": "Ligue 1",
    "SA": "Serie A",
    "PD": "La Liga",
    "ELC": "Championship",
    "BSA": "Serie A Brésil",
    "WC": "Coupe du monde",
    "EC": "Euro",
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
        "home": 0.0,
        "draw": 0.0,
        "away": 0.0,
        "btts": 0.0,
        "over15": 0.0,
        "over25": 0.0,
        "over35": 0.0,
        "under25": 0.0,
        "under35": 0.0,
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


def fd_status(status):
    return {
        "SCHEDULED": "À venir",
        "TIMED": "Programmé",
        "IN_PLAY": "En direct",
        "PAUSED": "Mi-temps",
        "FINISHED": "Terminé",
        "POSTPONED": "Reporté",
        "SUSPENDED": "Suspendu",
        "CANCELLED": "Annulé",
        "AWARDED": "Attribué",
    }.get(status, status or "N/D")


def fd_dt(item):
    raw = item.get("utcDate", "")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(PARIS)
    except Exception:
        return None


def match_names(item):
    return (
        item.get("homeTeam", {}).get("name", "Inconnu"),
        item.get("awayTeam", {}).get("name", "Inconnu"),
    )


def score_pair(item):
    score = item.get("score", {})
    full = score.get("fullTime", {}) if isinstance(score, dict) else {}
    return full.get("home"), full.get("away")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            fixture_id TEXT NOT NULL,
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
            user_id INTEGER NOT NULL,
            fixture_id TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            market TEXT NOT NULL,
            selection TEXT NOT NULL,
            probability REAL,
            predicted_at TEXT NOT NULL,
            outcome TEXT,
            settled_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def db_add_bet(user_id, fixture_id, market, selection, stake, odds=None, probability=None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        """INSERT INTO bets
        (user_id, fixture_id, market, selection, stake, odds, probability, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, str(fixture_id), market, selection, stake, odds, probability, now_iso()),
    )
    bet_id = cur.lastrowid
    conn.commit()
    conn.close()
    return bet_id


def db_settle_bet(user_id, bet_id, result):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT stake, odds, result FROM bets WHERE id=? AND user_id=?",
        (bet_id, user_id),
    ).fetchone()
    if not row or row[2] is not None:
        conn.close()
        return None
    stake, odds, _ = row
    if result == "win":
        profit = stake * ((odds or 1.0) - 1.0)
    elif result == "loss":
        profit = -stake
    else:
        profit = 0.0
    conn.execute(
        "UPDATE bets SET result=?, profit=?, settled_at=? WHERE id=? AND user_id=?",
        (result, profit, now_iso(), bet_id, user_id),
    )
    conn.commit()
    conn.close()
    return profit


def db_summary(user_id):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT stake, result, profit FROM bets WHERE user_id=?",
        (user_id,),
    ).fetchall()
    conn.close()
    total = sum(r[0] or 0 for r in rows)
    profit = sum(r[2] or 0 for r in rows)
    settled = [r for r in rows if r[1] in {"win", "loss", "void"}]
    wins = sum(r[1] == "win" for r in settled)
    roi = profit / total * 100 if total else 0.0
    return total, profit, len(rows), wins, roi



def db_add_prediction(user_id, fixture_id, home_team, away_team, market, selection, probability):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        """INSERT INTO predictions
        (user_id, fixture_id, home_team, away_team, market, selection, probability, predicted_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id,
            str(fixture_id),
            home_team,
            away_team,
            market,
            selection,
            probability,
            now_iso(),
        ),
    )
    prediction_id = cur.lastrowid
    conn.commit()
    conn.close()
    return prediction_id


def prediction_outcome(market, selection, home_goals, away_goals):
    if home_goals is None or away_goals is None:
        return None

    total = home_goals + away_goals
    if market == "1X2":
        actual = "1" if home_goals > away_goals else "X" if home_goals == away_goals else "2"
    elif market == "BTTS":
        actual = "Oui" if home_goals > 0 and away_goals > 0 else "Non"
    elif market == "O2.5":
        actual = "Oui" if total >= 3 else "Non"
    elif market == "O1.5":
        actual = "Oui" if total >= 2 else "Non"
    elif market == "1X":
        actual = "Oui" if home_goals >= away_goals else "Non"
    elif market == "X2":
        actual = "Oui" if away_goals >= home_goals else "Non"
    elif market == "12":
        actual = "Oui" if home_goals != away_goals else "Non"
    else:
        return None
    return "win" if str(selection).lower() == str(actual).lower() else "loss"


def db_performance(user_id=None):
    conn = sqlite3.connect(DB_PATH)
    if user_id is None:
        rows = conn.execute(
            "SELECT market, probability, outcome FROM predictions WHERE outcome IS NOT NULL"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT market, probability, outcome FROM predictions WHERE user_id=? AND outcome IS NOT NULL",
            (user_id,),
        ).fetchall()
    conn.close()

    by_market = {}
    for market, probability, outcome in rows:
        d = by_market.setdefault(
            market, {"total": 0, "wins": 0, "prob_sum": 0.0, "prob_n": 0}
        )
        d["total"] += 1
        d["wins"] += 1 if outcome == "win" else 0
        if probability is not None:
            d["prob_sum"] += float(probability)
            d["prob_n"] += 1

    total = len(rows)
    wins = sum(1 for _, _, o in rows if o == "win")
    return {
        "total": total,
        "wins": wins,
        "accuracy": (wins / total * 100) if total else None,
        "markets": by_market,
    }


async def validate_predictions():
    conn = sqlite3.connect(DB_PATH)
    pending = conn.execute(
        """SELECT id, fixture_id, market, selection
           FROM predictions
           WHERE outcome IS NULL AND fixture_id NOT LIKE 'TSDB-%'
           ORDER BY id ASC
           LIMIT 30"""
    ).fetchall()
    conn.close()

    if not pending or not FOOTBALL_DATA_KEY:
        return 0

    checked = 0
    async with httpx.AsyncClient(timeout=20) as client:
        for prediction_id, fixture_id, market, selection in pending:
            try:
                match, error = await find_fd_match(client, fixture_id)
                if error or not match:
                    continue
                status = match.get("status")
                if status not in {"FINISHED", "AWARDED"}:
                    continue
                hg, ag = score_pair(match)
                outcome = prediction_outcome(market, selection, hg, ag)
                if outcome is None:
                    continue

                conn = sqlite3.connect(DB_PATH)
                conn.execute(
                    "UPDATE predictions SET outcome=?, settled_at=? WHERE id=? AND outcome IS NULL",
                    (outcome, now_iso(), prediction_id),
                )
                conn.commit()
                conn.close()
                checked += 1
            except Exception as exc:
                print(f"Validation prediction {prediction_id}: {exc}")
    return checked


def record_model_predictions(user_id, data):
    model = data.get("model")
    match = data.get("match", {})
    if not model or not match:
        return 0

    fixture_id = match.get("id")
    home = match.get("homeTeam", {}).get("name", "Domicile")
    away = match.get("awayTeam", {}).get("name", "Extérieur")
    h, d, a = model["final"]
    markets = model["markets"]

    candidates = [
        ("1X2", "1", h),
        ("1X2", "X", d),
        ("1X2", "2", a),
        ("BTTS", "Oui" if markets["btts"] >= 50 else "Non",
         max(markets["btts"], 100 - markets["btts"])),
        ("O2.5", "Oui" if markets["over25"] >= 50 else "Non",
         max(markets["over25"], 100 - markets["over25"])),
        ("O1.5", "Oui" if markets["over15"] >= 50 else "Non",
         max(markets["over15"], 100 - markets["over15"])),
        ("1X", "Oui" if h + d >= 50 else "Non", max(h + d, 100 - (h + d))),
        ("X2", "Oui" if d + a >= 50 else "Non", max(d + a, 100 - (d + a))),
        ("12", "Oui" if h + a >= 50 else "Non", max(h + a, 100 - (h + a))),
    ]

    # One prediction per market for this user/match.
    conn = sqlite3.connect(DB_PATH)
    existing = {
        (r[0], r[1])
        for r in conn.execute(
            "SELECT market, selection FROM predictions WHERE user_id=? AND fixture_id=?",
            (user_id, str(fixture_id)),
        ).fetchall()
    }
    conn.close()

    count = 0
    for market, selection, probability in candidates:
        key = (market, selection)
        if key in existing:
            continue
        db_add_prediction(
            user_id, fixture_id, home, away, market, selection, float(probability)
        )
        count += 1
    return count


async def performance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await validate_predictions()
    perf = db_performance(update.effective_user.id)
    if not perf["total"]:
        await update.message.reply_text(
            "📊 PERFORMANCE DU MODÈLE\n\n"
            "Aucune prédiction terminée n'est encore disponible."
        )
        return

    msg = (
        "📊 PERFORMANCE DU MODÈLE\n\n"
        f"Prédictions validées : {perf['total']}\n"
        f"Correctes : {perf['wins']}\n"
        f"Taux de réussite : {perf['accuracy']:.1f}%\n\n"
        "PAR MARCHÉ\n"
    )
    for market, d in sorted(perf["markets"].items()):
        acc = d["wins"] / d["total"] * 100 if d["total"] else 0
        avg_prob = d["prob_sum"] / d["prob_n"] if d["prob_n"] else None
        msg += f"• {market} : {acc:.1f}% ({d['wins']}/{d['total']})"
        if avg_prob is not None:
            msg += f" | prob. moy. {avg_prob:.1f}%"
        msg += "\n"
    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=match_keyboard(fixture_id))


async def validate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    checked = await validate_predictions()
    perf = db_performance(update.effective_user.id)
    await update.message.reply_text(
        "🔄 VALIDATION\n\n"
        f"Prédictions nouvellement validées : {checked}\n"
        f"Prédictions terminées dans ton historique : {perf['total']}\n"
        f"Taux de réussite : "
        f"{perf['accuracy']:.1f}%" if perf["accuracy"] is not None else
        "🔄 VALIDATION\n\nAucune prédiction terminée dans ton historique."
    )


async def cached_get(client, url, headers=None, params=None, cache_key=None, ttl=60):
    if cache_key:
        cached = CACHE.get(cache_key)
        if cached and cached["expires"] > time.time():
            return cached["data"], None
    try:
        response = await client.get(url, headers=headers or {}, params=params or {})
    except Exception as exc:
        return None, f"Erreur réseau : {exc}"
    if response.status_code != 200:
        try:
            detail = response.json()
        except Exception:
            detail = response.text[:300]
        return None, f"HTTP {response.status_code} : {detail}"
    try:
        data = response.json()
    except Exception:
        return None, "Réponse JSON invalide."
    if cache_key:
        CACHE[cache_key] = {"data": data, "expires": time.time() + ttl}
    return data, None


async def fd_get(client, path, params=None, cache_key=None, ttl=60):
    if not FOOTBALL_DATA_KEY:
        return None, "Clé FOOTBALL_DATA_KEY absente."
    return await cached_get(
        client,
        f"{FD_BASE}{path}",
        headers={"X-Auth-Token": FOOTBALL_DATA_KEY},
        params=params,
        cache_key=cache_key,
        ttl=ttl,
    )


async def tsdb_get(client, path, params=None, cache_key=None, ttl=60):
    return await cached_get(
        client,
        f"{TSDB_BASE}/{path}",
        params=params,
        cache_key=cache_key,
        ttl=ttl,
    )


async def fd_today_matches(client):
    data, error = await fd_get(
        client,
        "/matches",
        {"date": today_paris()},
        cache_key=f"fd:today:{today_paris()}",
        ttl=120,
    )
    if error:
        return [], error
    matches = data.get("matches", [])
    return [m for m in matches if m.get("competition", {}).get("code") in FREE_COMPETITIONS], None


async def tsdb_today_events(client):
    data, error = await tsdb_get(
        client,
        "eventsday.php",
        {"d": today_paris(), "s": "Soccer"},
        cache_key=f"tsdb:today:{today_paris()}",
        ttl=120,
    )
    if error:
        return [], error
    return data.get("events", []) or [], None


def tsdb_to_match(event):
    raw_date = event.get("strTimestamp") or event.get("dateEvent") or ""
    raw_time = event.get("strTime") or "12:00:00"
    try:
        if "T" not in raw_date:
            raw_date = f"{raw_date}T{raw_time}"
        dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        dt = None
    return {
        "id": f"TSDB-{event.get('idEvent', '0')}",
        "utcDate": dt.astimezone(timezone.utc).isoformat() if dt else "",
        "status": "FINISHED" if event.get("strStatus") in {"Match Finished", "FT"} else "SCHEDULED",
        "competition": {"name": event.get("strLeague", "Soccer"), "code": "TSDB"},
        "homeTeam": {"id": event.get("idHomeTeam"), "name": event.get("strHomeTeam", "Inconnu")},
        "awayTeam": {"id": event.get("idAwayTeam"), "name": event.get("strAwayTeam", "Inconnu")},
        "score": {
            "fullTime": {
                "home": num(event.get("intHomeScore")),
                "away": num(event.get("intAwayScore")),
            }
        },
        "venue": event.get("strVenue"),
        "_tsdb": event,
    }


def parse_form(matches, team_id):
    rows = []
    for item in matches or []:
        home_id = item.get("homeTeam", {}).get("id")
        away_id = item.get("awayTeam", {}).get("id")
        hg, ag = score_pair(item)
        if hg is None or ag is None:
            continue
        status = item.get("status")
        if status not in {"FINISHED", "AWARDED"}:
            continue
        if str(team_id) == str(home_id):
            gf, ga = hg, ag
            result = "V" if gf > ga else "N" if gf == ga else "D"
            opponent = item.get("awayTeam", {}).get("name", "?")
        elif str(team_id) == str(away_id):
            gf, ga = ag, hg
            result = "V" if gf > ga else "N" if gf == ga else "D"
            opponent = item.get("homeTeam", {}).get("name", "?")
        else:
            continue
        rows.append({"gf": gf, "ga": ga, "result": result, "opponent": opponent})
    return rows[:5]


def form_metrics(form):
    if not form:
        return {}
    n = len(form)
    gf = sum(x["gf"] for x in form)
    ga = sum(x["ga"] for x in form)
    wins = sum(x["result"] == "V" for x in form)
    draws = sum(x["result"] == "N" for x in form)
    losses = sum(x["result"] == "D" for x in form)
    return {
        "matches": n,
        "gf_avg": gf / n,
        "ga_avg": ga / n,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "points_avg": (wins * 3 + draws) / n,
    }


def standings_metrics(standings, team_id):
    for table in standings or []:
        for row in table.get("table", []):
            team = row.get("team", {})
            if str(team.get("id")) == str(team_id):
                return {
                    "position": row.get("position"),
                    "played": row.get("playedGames"),
                    "won": row.get("won"),
                    "draw": row.get("draw"),
                    "lost": row.get("lost"),
                    "gf": row.get("goalsFor"),
                    "ga": row.get("goalsAgainst"),
                    "points": row.get("points"),
                }
    return {}


def build_model(home_form, away_form):
    hf = form_metrics(home_form)
    af = form_metrics(away_form)
    if not hf or not af:
        return None

    home_attack = hf.get("gf_avg")
    away_attack = af.get("gf_avg")
    home_defense = hf.get("ga_avg")
    away_defense = af.get("ga_avg")

    home_xg = (home_attack + away_defense) / 2 * 1.08
    away_xg = (away_attack + home_defense) / 2
    home_xg = clamp(home_xg, 0.15, 4.0)
    away_xg = clamp(away_xg, 0.10, 4.0)

    matrix = poisson_matrix(home_xg, away_xg)
    markets = poisson_markets(matrix)
    final = normalize3(markets["home"], markets["draw"], markets["away"])
    return {
        "home_xg": home_xg,
        "away_xg": away_xg,
        "matrix": matrix,
        "markets": markets,
        "final": final,
        "form_home": hf,
        "form_away": af,
    }


def fair_odds(probability):
    return round(100 / probability, 2) if probability and probability > 0 else None


def confidence_label(probs, quality):
    spread = max(probs) - min(probs)
    if quality >= 0.8 and spread >= 25:
        return "Élevée"
    if quality >= 0.55 and spread >= 12:
        return "Moyenne"
    return "Faible"


def h2h_rows(detail):
    h2h = detail.get("head2head", {}) if isinstance(detail, dict) else {}
    return h2h.get("matches", []) if isinstance(h2h, dict) else []


async def find_fd_match(client, fixture_id):
    if str(fixture_id).startswith("TSDB-"):
        return None, "Cet ID appartient à TheSportsDB et ne peut pas être analysé par Football-Data.org."
    try:
        fid = int(fixture_id)
    except ValueError:
        return None, "ID de match invalide."
    data, error = await fd_get(
        client,
        f"/matches/{fid}",
        {"head2head": 10},
        cache_key=f"fd:match:{fid}",
        ttl=120,
    )
    if error:
        return None, error
    if not data or not data.get("id"):
        return None, "Match introuvable."
    return data, None


async def team_recent(client, team_id):
    data, error = await fd_get(
        client,
        f"/teams/{team_id}/matches",
        {"status": "FINISHED", "limit": 5},
        cache_key=f"fd:team:{team_id}:recent",
        ttl=300,
    )
    if error:
        return [], error
    return data.get("matches", []), None


async def competition_standings(client, code):
    data, error = await fd_get(
        client,
        f"/competitions/{code}/standings",
        {},
        cache_key=f"fd:standings:{code}",
        ttl=600,
    )
    if error:
        return [], error
    return data.get("standings", []), None


async def full_analysis(fixture_id):
    async with httpx.AsyncClient(timeout=20) as client:
        match, error = await find_fd_match(client, fixture_id)
        if error:
            return None, error

        home = match.get("homeTeam", {})
        away = match.get("awayTeam", {})
        competition = match.get("competition", {})
        code = competition.get("code")

        home_recent, e1 = await team_recent(client, home.get("id"))
        away_recent, e2 = await team_recent(client, away.get("id"))
        standings, e3 = await competition_standings(client, code)

    if e1 or e2:
        return None, e1 or e2

    home_form = parse_form(home_recent, home.get("id"))
    away_form = parse_form(away_recent, away.get("id"))
    model = build_model(home_form, away_form)

    h2h = h2h_rows(match)
    quality_parts = [bool(home_form), bool(away_form), bool(standings), bool(h2h)]
    quality = sum(quality_parts) / len(quality_parts)

    return {
        "match": match,
        "home_form": home_form,
        "away_form": away_form,
        "standings": standings,
        "h2h": h2h,
        "model": model,
        "quality": quality,
        "standings_home": standings_metrics(standings, home.get("id")),
        "standings_away": standings_metrics(standings, away.get("id")),
        "standings_error": e3,
    }, None


def build_analysis_cards(data):
    """Dashboard V13 mobile-first, en cartes courtes."""
    match = data["match"]
    home = match["homeTeam"]["name"]
    away = match["awayTeam"]["name"]
    comp = match.get("competition", {}).get("name", "Compétition")
    dt = fd_dt(match)
    date_text = dt.strftime("%d/%m/%Y • %H:%M") if dt else "Horaire N/D"
    status = fd_status(match.get("status"))
    model = data.get("model")
    sh = data.get("standings_home") or {}
    sa = data.get("standings_away") or {}
    hf = data.get("home_form") or []
    af = data.get("away_form") or []

    form_h = " ".join(x.get("result", "?") for x in hf) if hf else "N/D"
    form_a = " ".join(x.get("result", "?") for x in af) if af else "N/D"

    # HERO: très compact, lisible immédiatement sur iPhone.
    card1 = (
        "⚡ <b>SPORT ANALYZER • V13</b>\n"
        "╭────────────────────────╮\n"
        f"│  🏠 <b>{home}</b>\n"
        f"│       <b>VS</b>\n"
        f"│  ✈️ <b>{away}</b>\n"
        "╰────────────────────────╯\n"
        f"🏆 {comp}\n"
        f"🗓 {date_text}   •   🟢 {status}\n"
        f"🆔 <code>{match.get('id')}</code>\n\n"
        "📋 <b>FORME & CLASSEMENT</b>\n"
        f"🏠 <b>{sh.get('position','N/D')}e</b>  {sh.get('points','N/D')} pts  •  {sh.get('gf','N/D')}-{sh.get('ga','N/D')}   {form_h}\n"
        f"✈️ <b>{sa.get('position','N/D')}e</b>  {sa.get('points','N/D')} pts  •  {sa.get('gf','N/D')}-{sa.get('ga','N/D')}   {form_a}"
    )

    cards = [card1]

    if not model:
        cards.append(
            "⚠️ <b>DONNÉES INSUFFISANTES</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Le modèle ne dispose pas de suffisamment de données."
        )
        return cards

    ph, pd, pa = model["final"]
    m = model["markets"]
    scores = likely_scores(model["matrix"], 3)

    # PROBABILITÉS: séparation 1X2 / double chance.
    card2 = (
        "🎯 <b>PRÉVISION CENTRALE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🏆 <b>1X2</b>\n"
        + probability_block([
            ("1", ph), ("X", pd), ("2", pa)
        ], width=8)
        + "\n\n"
        "🔁 <b>DOUBLE CHANCE</b>\n"
        + probability_block([
            ("1X", ph + pd), ("X2", pd + pa), ("12", ph + pa)
        ], width=8)
    )

    # BUTS: carte dédiée, plus respirante.
    card3 = (
        "⚽ <b>LABORATOIRE DES BUTS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        + probability_block([
            ("BTTS Oui", m["btts"]),
            ("BTTS Non", 100 - m["btts"]),
            ("Over 1.5", m["over15"]),
            ("Over 2.5", m["over25"]),
            ("Over 3.5", m["over35"]),
            ("Under 2.5", m["under25"]),
            ("Under 3.5", m["under35"])
        ], width=8)
        + f"\n\n📐 <b>xG</b>   {model['home_xg']:.2f}  •  {model['away_xg']:.2f}"
    )

    score_lines = []
    for h, a, p in scores:
        score_lines.append(f"⚽ <b>{h}-{a}</b>  {prob_bar(p, 8)}  <b>{p:.1f}%</b>")
    score_text = "\n".join(score_lines) if score_lines else "Aucun score disponible."

    confidence = confidence_label(model["final"], data["quality"])
    h2h = data.get("h2h") or []
    h2h_line = f"📊 {len(h2h)} rencontre(s) disponible(s)" if h2h else "📊 Données H2H non disponibles"

    # INTELLIGENCE: scores + confiance + qualité.
    card4 = (
        "🧠 <b>INTELLIGENCE MATCH</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔢 <b>Scores les plus probables</b>\n"
        f"{score_text}\n\n"
        f"🧠 <b>Confiance</b>  {confidence}\n"
        f"🤝 <b>Face à face</b>  {h2h_line}\n"
        f"💾 <b>Qualité données</b>  {data.get('quality','N/D')}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "ℹ️ <i>Estimations statistiques. Aucun résultat n'est garanti.</i>"
    )

    cards.extend([card2, card3, card4])
    return cards

def build_analysis_message(data):
    """Compatibilité avec les anciennes parties du bot."""
    return "\n\n".join(build_analysis_cards(data))

def main_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚽  MATCHS DU JOUR", callback_data="menu:match"),
            InlineKeyboardButton("🎯  ANALYSE", callback_data="menu:tools"),
        ],
        [
            InlineKeyboardButton("📊  PERFORMANCE", callback_data="menu:performance"),
            InlineKeyboardButton("💰  BANKROLL", callback_data="menu:bankroll"),
        ],
        [
            InlineKeyboardButton("🔬  SIMULATEUR", callback_data="menu:sim"),
            InlineKeyboardButton("🔄  VALIDATION", callback_data="menu:validation"),
        ],
        [
            InlineKeyboardButton("🔌  SOURCES", callback_data="menu:status"),
            InlineKeyboardButton("❓  AIDE", callback_data="menu:help"),
        ],
    ])

def match_keyboard(fixture_id):
    fid = str(fixture_id)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔎 ANALYSE", callback_data=f"analyse:{fid}"),
            InlineKeyboardButton("🎯 PROBABILITÉS", callback_data=f"prob:{fid}"),
        ],
        [
            InlineKeyboardButton("⚽ BUTS", callback_data=f"buts:{fid}"),
            InlineKeyboardButton("💰 COTES", callback_data=f"cotes:{fid}"),
        ],
        [
            InlineKeyboardButton("⚽ BUTEURS", callback_data=f"buteur:{fid}"),
            InlineKeyboardButton("🔬 SIMULER", callback_data=f"sim:{fid}"),
        ],
        [
            InlineKeyboardButton("⬅️ MATCHS", callback_data="menu:match"),
            InlineKeyboardButton("🏠 ACCUEIL", callback_data="menu:home"),
        ],
    ])


def match_list_keyboard(matches):
    rows = []
    for item in matches[:20]:
        fid = item.get("id")
        home, away = match_names(item)
        rows.append([
            InlineKeyboardButton(
                f"🔎 {home[:18]} - {away[:18]}",
                callback_data=f"match:{fid}"
            )
        ])
    rows.append([
        InlineKeyboardButton("🔄 ACTUALISER", callback_data="menu:match"),
        InlineKeyboardButton("🏠 ACCUEIL", callback_data="menu:home"),
    ])
    return InlineKeyboardMarkup(rows)



def prob_bar(value, width=10):
    """Barre colorée compatible avec Telegram."""
    value = max(0.0, min(100.0, float(value)))
    filled = round(value / 100.0 * width)

    if value >= 60:
        color = "🟩"
    elif value >= 30:
        color = "🟨"
    else:
        color = "🟥"

    return color * filled + "▫️" * (width - filled)


def probability_line(label, value, width=10):
    value = float(value)
    return f"{label:<12} {prob_bar(value, width)} {value:5.1f}%"


def probability_block(items, width=10):
    return "\n".join(probability_line(label, value, width) for label, value in items)

def simulator_keyboard(fixture_id):
    fid = str(fixture_id)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎯 1X2", callback_data=f"market:{fid}:1X2"),
            InlineKeyboardButton("🔁 Double chance", callback_data=f"market:{fid}:DC"),
        ],
        [
            InlineKeyboardButton("⚽ BTTS", callback_data=f"market:{fid}:BTTS"),
            InlineKeyboardButton("📊 Over/Under", callback_data=f"market:{fid}:OU"),
        ],
        [
            InlineKeyboardButton("🔎 Analyse", callback_data=f"analyse:{fid}"),
            InlineKeyboardButton("⬅️ Match", callback_data=f"match:{fid}"),
        ],
    ])


def stake_keyboard(fixture_id, market, selection):
    fid = str(fixture_id)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("5 €", callback_data=f"stake:{fid}:{market}:{selection}:5"),
            InlineKeyboardButton("10 €", callback_data=f"stake:{fid}:{market}:{selection}:10"),
        ],
        [
            InlineKeyboardButton("20 €", callback_data=f"stake:{fid}:{market}:{selection}:20"),
            InlineKeyboardButton("50 €", callback_data=f"stake:{fid}:{market}:{selection}:50"),
        ],
        [
            InlineKeyboardButton("⬅️ Marchés", callback_data=f"sim:{fid}"),
            InlineKeyboardButton("🏠 Menu", callback_data="menu:home"),
        ],
    ])


def market_keyboard(fixture_id, market):
    fid = str(fixture_id)
    if market == "1X2":
        choices = [("1", "1"), ("X", "X"), ("2", "2")]
    elif market == "DC":
        choices = [("1X", "1X"), ("X2", "X2"), ("12", "12")]
    elif market == "BTTS":
        choices = [("Oui", "BTTSY"), ("Non", "BTTSN")]
    else:
        choices = [
            ("Over 1.5", "O15"), ("Over 2.5", "O25"),
            ("Over 3.5", "O35"), ("Under 2.5", "U25"),
            ("Under 3.5", "U35")
        ]
    rows = []
    row = []
    for label, code in choices:
        row.append(InlineKeyboardButton(
            label, callback_data=f"pick:{fid}:{market}:{code}"
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton("⬅️ Simulateur", callback_data=f"sim:{fid}"),
        InlineKeyboardButton("🏠 Menu", callback_data="menu:home"),
    ])
    return InlineKeyboardMarkup(rows)


def tools_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📈 Performance", callback_data="menu:performance"),
            InlineKeyboardButton("🔄 Validation", callback_data="menu:validation"),
        ],
        [
            InlineKeyboardButton("💶 Bankroll", callback_data="menu:bankroll"),
            InlineKeyboardButton("🔌 Sources", callback_data="menu:status"),
        ],
        [InlineKeyboardButton("🏠 Menu principal", callback_data="menu:home")],
    ])


async def send_simulator_menu(message, fixture_id):
    await message.reply_text(
        "🔬 SIMULATEUR\n\n"
        "Choisis le marché à étudier.\n"
        "Les rendements calculés sont théoriques et utilisent "
        "la probabilité du modèle.",
        reply_markup=simulator_keyboard(fixture_id),
    )


async def send_market_menu(message, fixture_id, market):
    data, error = await full_analysis(fixture_id)
    if error:
        await message.reply_text(f"❌ {error}", reply_markup=main_menu())
        return
    model = data.get("model")
    if not model:
        await message.reply_text("❌ Données insuffisantes.", reply_markup=main_menu())
        return

    h, d, a = model["final"]
    m = model["markets"]
    if market == "1X2":
        body = (
            "🎯 1X2\n\n"
            + probability_block([("1", h), ("X", d), ("2", a)])
            + "\n\nChoisis une sélection :"
        )
    elif market == "DC":
        body = (
            "🔁 DOUBLE CHANCE\n\n"
            + probability_block([("1X", h+d), ("X2", d+a), ("12", h+a)])
            + "\n\nChoisis une sélection :"
        )
    elif market == "BTTS":
        body = (
            "⚽ BTTS\n\n"
            + probability_block([("Oui", m["btts"]), ("Non", 100-m["btts"])])
            + "\n\nChoisis une sélection :"
        )
    else:
        body = (
            "📊 OVER / UNDER\n\n"
            + probability_block([
                ("Over 1.5", m["over15"]),
                ("Over 2.5", m["over25"]),
                ("Over 3.5", m["over35"]),
                ("Under 2.5", m["under25"]),
                ("Under 3.5", m["under35"]),
            ])
            + "\n\nChoisis une sélection :"
        )
    await message.reply_text(body, reply_markup=market_keyboard(fixture_id, market))


async def send_pick_stake_menu(message, fixture_id, market, selection):
    data, error = await full_analysis(fixture_id)
    if error:
        await message.reply_text(f"❌ {error}", reply_markup=main_menu())
        return
    model = data.get("model")
    if not model:
        await message.reply_text("❌ Données insuffisantes.", reply_markup=main_menu())
        return

    h, d, a = model["final"]
    m = model["markets"]
    values = {
        "1": ("1", h), "X": ("X", d), "2": ("2", a),
        "1X": ("1X", h+d), "X2": ("X2", d+a), "12": ("12", h+a),
        "BTTSY": ("BTTS Oui", m["btts"]), "BTTSN": ("BTTS Non", 100-m["btts"]),
        "O15": ("Over 1.5", m["over15"]), "O25": ("Over 2.5", m["over25"]),
        "O35": ("Over 3.5", m["over35"]), "U25": ("Under 2.5", m["under25"]),
        "U35": ("Under 3.5", m["under35"]),
    }
    label, probability = values.get(selection, ("Sélection", 0.0))
    if probability <= 0:
        await message.reply_text("❌ Probabilité indisponible.", reply_markup=main_menu())
        return

    fair = 100.0 / float(probability)
    await message.reply_text(
        "💶 SIMULATION\n\n"
        f"🎯 Sélection : {label}\n"
        f"📊 Probabilité modèle : {probability:.1f}%\n"
        f"📐 Cote juste théorique : {fair:.2f}\n\n"
        "Choisis une mise à simuler :",
        reply_markup=stake_keyboard(fixture_id, market, selection),
    )


async def send_stake_result(message, fixture_id, market, selection, stake):
    data, error = await full_analysis(fixture_id)
    if error:
        await message.reply_text(f"❌ {error}", reply_markup=main_menu())
        return
    model = data.get("model")
    if not model:
        await message.reply_text("❌ Données insuffisantes.", reply_markup=main_menu())
        return

    h, d, a = model["final"]
    m = model["markets"]
    probs = {
        "1": h, "X": d, "2": a,
        "1X": h+d, "X2": d+a, "12": h+a,
        "BTTSY": m["btts"], "BTTSN": 100-m["btts"],
        "O15": m["over15"], "O25": m["over25"], "O35": m["over35"],
        "U25": m["under25"], "U35": m["under35"],
    }
    labels = {
        "1": "1", "X": "X", "2": "2", "1X": "1X", "X2": "X2", "12": "12",
        "BTTSY": "BTTS Oui", "BTTSN": "BTTS Non",
        "O15": "Over 1.5", "O25": "Over 2.5", "O35": "Over 3.5",
        "U25": "Under 2.5", "U35": "Under 3.5",
    }
    probability = float(probs.get(selection, 0))
    if probability <= 0:
        await message.reply_text("❌ Probabilité indisponible.", reply_markup=main_menu())
        return

    fair = 100.0 / probability
    total_return = float(stake) * fair
    profit = total_return - float(stake)

    await message.reply_text(
        "🧮 SIMULATION\n\n"
        f"🎯 {labels.get(selection, selection)}\n"
        f"📊 Probabilité : {probability:.1f}%\n"
        f"💶 Mise : {float(stake):.2f} €\n"
        f"📐 Cote juste théorique : {fair:.2f}\n"
        f"💰 Retour théorique : {total_return:.2f} €\n"
        f"📈 Profit théorique : {profit:+.2f} €\n\n"
        "ℹ️ Simulation statistique. Ce n'est pas une cote bookmaker.",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔬 Autre marché", callback_data=f"sim:{fixture_id}"),
                InlineKeyboardButton("🔎 Analyse", callback_data=f"analyse:{fixture_id}"),
            ],
            [InlineKeyboardButton("🏠 Menu principal", callback_data="menu:home")],
        ]),
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == "menu:home":
        await query.message.reply_text(
            "🤖 SPORT ANALYZER V13\n\nChoisis une fonction :",
            reply_markup=main_menu(),
        )
        return

    if data == "menu:match":
        await send_match_results(query.message)
        return

    if data == "menu:status":
        await send_status_result(query.message)
        return

    if data == "menu:performance":
        await send_performance_result(query.message, update.effective_user.id)
        return

    if data == "menu:validation":
        await send_validation_result(query.message, update.effective_user.id)
        return

    if data == "menu:bankroll":
        await send_bankroll_result(query.message, update.effective_user.id)
        return

    if data == "menu:sim":
        await query.message.reply_text(
            "🔬 SIMULATEUR\n\n"
            "Ouvre un match depuis ⚽ Matchs du jour, "
            "puis clique sur 🔬 Simuler.",
            reply_markup=main_menu(),
        )
        return

    if data == "menu:tools":
        await query.message.reply_text(
            "⚙️ OUTILS\n\nChoisis une fonction :",
            reply_markup=tools_keyboard(),
        )
        return

    if data == "menu:help":
        await query.message.reply_text(
            "📚 AIDE RAPIDE\n\n"
            "⚽ Matchs : rencontres disponibles aujourd'hui.\n"
            "🔎 Analyse : analyse statistique complète.\n"
            "🎯 Probabilités : 1X2 et double chance.\n"
            "⚽ Buts : BTTS, Over/Under et xG.\n"
            "💰 Cotes : informations disponibles.\n"
            "⚽ Buteurs : événements disponibles.\n"            "🔬 Simulateur : marchés et mises théoriques.\n"
            "📊 Performance : historique du modèle.\n"
            "🔄 Validation : validation des prédictions terminées.\n"
            "💶 Bankroll : suivi des mises.",
            reply_markup=main_menu(),
        )
        return

    if ":" not in data:
        return
    action, value = data.split(":", 1)

    if action == "match":
        await send_match_actions(query.message, value)
    elif action == "sim":
        await send_simulator_menu(query.message, value)
    elif action == "market":
        fid, market = value.split(":", 1)
        await send_market_menu(query.message, fid, market)
    elif action == "pick":
        fid, market, selection = value.split(":", 2)
        await send_pick_stake_menu(query.message, fid, market, selection)
    elif action == "stake":
        fid, market, selection, stake = value.split(":", 3)
        await send_stake_result(query.message, fid, market, selection, float(stake))
    elif action == "analyse":
        await run_analysis_dashboard_for_message(query.message, value, update.effective_user.id)
    elif action == "buts":
        await run_buts_for_message(query.message, value)
    elif action == "prob":
        await run_prob_for_message(query.message, value)
    elif action == "cotes":
        await run_cotes_for_message(query.message, value)
    elif action == "buteur":
        await run_buteur_for_message(query.message, value)


async def send_match_results(message):
    async with httpx.AsyncClient(timeout=20) as client:
        matches, error = [], None
        source = "Football-Data.org"
        if FOOTBALL_DATA_KEY:
            matches, error = await fd_today_matches(client)
        if not matches:
            events, ts_error = await tsdb_today_events(
                client, today_paris()
            )
            if events:
                matches = [tsdb_to_match(e) for e in events]
                source = "TheSportsDB"
            elif error:
                await message.reply_text(
                    "❌ Aucune source disponible.\n\n"
                    "Vérifie FOOTBALL_DATA_KEY dans Render.",
                    reply_markup=main_menu(),
                )
                return

    matches.sort(key=lambda x: fd_dt(x) or datetime.max.replace(tzinfo=PARIS))
    matches = matches[:20]
    if not matches:
        await message.reply_text(
            "⚽ Aucun match disponible aujourd'hui.",
            reply_markup=main_menu(),
        )
        return

    await message.reply_text(
        f"⚽ MATCHS DU JOUR\n📅 {today_paris()}\n"
        f"🔌 Source : {source}\n📊 {len(matches)} matchs\n\n"
        "👇 Clique sur un match pour voir ses options.",
        reply_markup=match_list_keyboard(matches),
    )


async def send_match_actions(message, fixture_id):
    async with httpx.AsyncClient(timeout=20) as client:
        item, error = await find_fd_match(client, fixture_id)
        if (error or not item) and str(fixture_id).startswith("TSDB-"):
            events, error = await tsdb_today_events(client, today_paris())
            item = next(
                (
                    tsdb_to_match(e)
                    for e in (events or [])
                    if f"TSDB-{e.get('idEvent')}" == str(fixture_id)
                ),
                None,
            )

    if error or not item:
        await message.reply_text(
            f"❌ Match introuvable : {fixture_id}",
            reply_markup=main_menu(),
        )
        return

    home, away = match_names(item)
    comp = item.get("competition", {}).get("name", "Football")
    dt = fd_dt(item)
    date_text = dt.strftime("%d/%m/%Y %H:%M") if dt else "N/D"

    await message.reply_text(
        "⚽ <b>MATCH SÉLECTIONNÉ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>{home}</b>\n"
        f"<i>vs</i>\n"
        f"<b>{away}</b>\n\n"
        f"🏆 {comp}\n"
        f"📅 {date_text}\n"
        f"🆔 <code>{fixture_id}</code>\n\n"
        "👇 <b>CHOISIS TON MODULE</b>",
        parse_mode="HTML",
        reply_markup=match_keyboard(fixture_id),
    )



# ============================================================
# DASHBOARD GRAPHIQUE V13
# Rendu PNG 1024x1536 pour reproduire le mockup mobile.
# Telegram affiche ensuite ce PNG avec les boutons réellement cliquables.
# ============================================================

FONT_REG = "/usr/share/fonts/truetype/lato/Lato-Regular.ttf"
FONT_MED = "/usr/share/fonts/truetype/lato/Lato-Medium.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/lato/Lato-Bold.ttf"
FONT_ITALIC = "/usr/share/fonts/truetype/lato/Lato-Italic.ttf"


def _font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def _fit_font(text, max_width, start_size, min_size=14):
    size = start_size
    while size > min_size:
        f = _font(FONT_BOLD, size)
        if f.getlength(str(text)) <= max_width:
            return f
        size -= 1
    return _font(FONT_BOLD, min_size)


def _rounded(draw, box, fill, outline=None, radius=22, width=2):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _gradient_background(w, h):
    img = Image.new("RGB", (w, h), (5, 14, 30))
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        r = int(5 + 5 * t)
        g = int(12 + 9 * t)
        b = int(28 + 18 * t)
        for x in range(w):
            glow = int(8 * max(0, 1 - abs(x - w * .52) / (w * .62)))
            px[x, y] = (r, g + glow // 3, min(55, b + glow))
    return img


def _background_pattern(draw, w, h):
    # Motif discret type ballon / statistiques, volontairement très léger.
    for y in range(35, h, 115):
        for x in range(35, w, 130):
            cx, cy = x, y
            draw.ellipse((cx-16, cy-16, cx+16, cy+16), outline=(18, 54, 83), width=2)
            draw.line((cx-9, cy, cx+9, cy), fill=(14, 48, 76), width=2)
            draw.line((cx, cy-9, cx, cy+9), fill=(14, 48, 76), width=2)


def _draw_title(draw, text, xy, size=25):
    draw.text(xy, text, font=_font(FONT_BOLD, size), fill=(245, 248, 255))


def _draw_bar(draw, x, y, w, h, value, fill_color, label=None, label_x=None):
    value = clamp(float(value))
    _rounded(draw, (x, y, x+w, y+h), (27, 48, 76), radius=h//2)
    fw = max(2, int(w * value / 100)) if value > 0 else 0
    if fw:
        _rounded(draw, (x, y, x+fw, y+h), fill_color, radius=h//2)
    if label is not None:
        draw.text((label_x if label_x is not None else x+w+15, y-3), label,
                  font=_font(FONT_BOLD, 22), fill=fill_color)


def _bar_color(value):
    if value >= 60:
        return (27, 235, 82)
    if value >= 30:
        return (255, 203, 22)
    return (255, 67, 67)


def _draw_prob_card(draw, box, model):
    x1, y1, x2, y2 = box
    _rounded(draw, box, (9, 26, 49), outline=(47, 112, 168), radius=24, width=2)
    _draw_title(draw, "🎯  PROBABILITÉS MODÈLE", (x1+24, y1+20), 25)
    draw.ellipse((x2-53, y1+18, x2-23, y1+48), fill=(31, 67, 101), outline=(86, 137, 177))
    draw.text((x2-43, y1+21), "i", font=_font(FONT_BOLD, 19), fill=(230,240,255))

    home, drawp, away = model["final"]
    rows = [("1", home), ("X", drawp), ("2", away), ("1X", home+drawp), ("X2", drawp+away), ("12", home+away)]
    yy = y1 + 72
    for i, (lab, val) in enumerate(rows):
        if i == 3:
            draw.line((x1+24, yy-15, x2-24, yy-15), fill=(71, 107, 139), width=2)
            yy += 14
        draw.rounded_rectangle((x1+24, yy, x1+62, yy+35), radius=7, fill=(34, 105, 169))
        draw.text((x1+34, yy+3), lab, font=_font(FONT_BOLD, 21), fill=(245,248,255))
        color = (0, 218, 255) if lab == "12" else _bar_color(val)
        _draw_bar(draw, x1+82, yy+5, 260, 26, val, color)
        draw.text((x1+365, yy-1), f"{val:.1f}%", font=_font(FONT_BOLD, 22), fill=color)
        yy += 48


def _draw_goals_card(draw, box, model):
    x1, y1, x2, y2 = box
    _rounded(draw, box, (9, 26, 49), outline=(47, 112, 168), radius=24, width=2)
    _draw_title(draw, "⚽  MARCHÉS DE BUTS", (x1+24, y1+20), 25)
    m = model["markets"]
    rows = [
        ("BTTS Oui", m["btts"]), ("BTTS Non", 100-m["btts"]),
        ("Over 1.5", m["over15"]), ("Over 2.5", m["over25"]),
        ("Over 3.5", m["over35"]), ("Under 2.5", m["under25"]),
        ("Under 3.5", m["under35"]),
    ]
    yy = y1 + 72
    for lab, val in rows:
        color = _bar_color(val)
        draw.text((x1+24, yy-2), lab, font=_font(FONT_MED, 19), fill=(246,248,255))
        _draw_bar(draw, x1+140, yy+1, 215, 25, val, color)
        draw.text((x1+370, yy-2), f"{val:.1f}%", font=_font(FONT_BOLD, 20), fill=color)
        yy += 43
    draw.text((x1+24, y2-49), f"📐  xG estimé : {model['home_xg']:.2f}  —  {model['away_xg']:.2f}",
              font=_font(FONT_BOLD, 19), fill=(238,245,255))


def _draw_scores_card(draw, box, model):
    x1, y1, x2, y2 = box
    _rounded(draw, box, (9, 26, 49), outline=(47, 112, 168), radius=24, width=2)
    _draw_title(draw, "🔢  SCORES LES PLUS PROBABLES", (x1+24, y1+20), 22)
    scores = likely_scores(model["matrix"], 3)
    yy = y1 + 78
    for h, a, p in scores:
        draw.text((x1+40, yy), f"⚽ {h}-{a}", font=_font(FONT_BOLD, 21), fill=(245,248,255))
        _draw_bar(draw, x1+120, yy+3, 235, 25, p, (0, 193, 239))
        draw.text((x1+370, yy-1), f"{p:.1f}%", font=_font(FONT_BOLD, 20), fill=(235,244,255))
        yy += 57


def _draw_status_card(draw, box, data, model):
    x1, y1, x2, y2 = box
    _rounded(draw, box, (9, 26, 49), outline=(47, 112, 168), radius=24, width=2)
    confidence = confidence_label(model["final"], data.get("quality", 0))
    spread = max(model["final"]) - min(model["final"])
    trend = "Équilibré" if spread < 20 else "Tendance marquée"
    reliability = "Bonne" if data.get("quality", 0) >= .65 else ("Moyenne" if data.get("quality", 0) >= .4 else "Faible")
    items = [("🧠", "Confiance", confidence, (255,205,25)),
             ("📊", "Tendance", trend, (115,154,210)),
             ("🛡", "Fiabilité", reliability, (28,221,92))]
    yy = y1 + 27
    for icon, lab, val, color in items:
        draw.text((x1+24, yy), icon, font=_font(FONT_BOLD, 22), fill=(245,248,255))
        draw.text((x1+63, yy+2), lab, font=_font(FONT_MED, 19), fill=(245,248,255))
        vw = 128
        _rounded(draw, (x2-vw-20, yy-3, x2-20, yy+32), color, radius=17)
        vf = _font(FONT_BOLD, 17)
        tw = vf.getlength(val)
        draw.text((x2-vw/2-10-tw/2, yy+2), val, font=vf, fill=(10,18,30))
        yy += 49
    _rounded(draw, (x1+20, y2-78, x2-20, y2-18), (12, 37, 66), outline=(55,133,187), radius=15, width=1)
    draw.text((x1+32, y2-68), "❝", font=_font(FONT_BOLD, 27), fill=(145,205,255))
    draw.multiline_text((x1+66, y2-67),
                        "Les probabilités sont des estimations\nstatistiques calculées à partir des données disponibles.",
                        font=_font(FONT_ITALIC, 14), fill=(205,220,239), spacing=2)


def _paste_logo(base, url, box, fallback_text):
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=5) as response:
            raw = response.read()
        logo = Image.open(io.BytesIO(raw)).convert("RGBA")
        logo.thumbnail((box[2]-box[0], box[3]-box[1]), Image.Resampling.LANCZOS)
        x = box[0] + ((box[2]-box[0])-logo.width)//2
        y = box[1] + ((box[3]-box[1])-logo.height)//2
        base.paste(logo, (x, y), logo)
        return True
    except Exception:
        return False


def render_analysis_dashboard(data, home_logo=None, away_logo=None):
    W, H = 1024, 1536
    img = _gradient_background(W, H)
    draw = ImageDraw.Draw(img)
    _background_pattern(draw, W, H)

    white = (244, 247, 255)
    cyan = (0, 218, 255)
    blue = (24, 95, 170)
    panel = (7, 25, 47)

    match = data["match"]
    home = match.get("homeTeam", {}).get("name", "Domicile")
    away = match.get("awayTeam", {}).get("name", "Extérieur")
    comp = match.get("competition", {}).get("name", "Football")
    dt = fd_dt(match)
    date_text = dt.strftime("%d/%m/%Y  •  %H:%M") if dt else "Horaire N/D"
    status = fd_status(match.get("status"))
    model = data.get("model") or {"final": (33.3,33.4,33.3), "markets": {"btts":50,"over15":50,"over25":50,"over35":50,"under25":50,"under35":50}, "home_xg":1.0,"away_xg":1.0,"matrix":poisson_matrix(1,1)}
    sh = data.get("standings_home") or {}
    sa = data.get("standings_away") or {}
    hf = data.get("home_form") or []
    af = data.get("away_form") or []

    # Header
    _rounded(draw, (42, 32, 982, 132), (5, 23, 45), outline=(35, 190, 235), radius=48, width=2)
    draw.ellipse((63, 47, 123, 107), outline=cyan, width=4, fill=(8, 40, 65))
    draw.text((77, 59), "⚽", font=_font(FONT_BOLD, 31), fill=white)
    draw.text((142, 49), "Sport Analyzer", font=_font(FONT_BOLD, 27), fill=white)
    draw.text((143, 82), "bot", font=_font(FONT_REG, 18), fill=(180,201,224))
    draw.text((690, 55), "▂▅▇▇", font=_font(FONT_BOLD, 25), fill=(0, 218, 255))
    draw.text((755, 53), "Des données\ndes analyses\nd'opportunités", font=_font(FONT_REG, 14), fill=(218,229,245), spacing=1)

    # Hero
    hero = (42, 153, 982, 405)
    _rounded(draw, hero, panel, outline=(0, 202, 255), radius=26, width=2)
    draw.text((63, 174), "🏆", font=_font(FONT_BOLD, 25), fill=(255,203,22))
    draw.text((108, 176), comp.upper(), font=_font(FONT_BOLD, 20), fill=white)
    draw.text((108, 208), f"Journée  •  {date_text}", font=_font(FONT_REG, 17), fill=(195,211,231))
    draw.text((641, 180), f"ID: {match.get('id')}", font=_font(FONT_BOLD, 17), fill=white)
    _rounded(draw, (797, 173, 957, 213), (8, 50, 52), outline=(0, 239, 151), radius=20, width=2)
    draw.text((817, 183), f"🟢 {status}", font=_font(FONT_BOLD, 16), fill=(0,239,151))

    # Team zone
    hf_txt = " ".join(x.get("result", "?") for x in hf[-5:]) if hf else "N/D"
    af_txt = " ".join(x.get("result", "?") for x in af[-5:]) if af else "N/D"
    draw.text((163, 250), home, font=_fit_font(home, 310, 24, 17), fill=white)
    draw.text((721, 250), away, font=_fit_font(away, 210, 23, 16), fill=white)
    draw.text((163, 285), f"{sh.get('position','N/D')}e  •  {sh.get('points','N/D')} pts  •  {sh.get('gf','N/D')}-{sh.get('ga','N/D')}", font=_font(FONT_REG, 16), fill=(199,215,234))
    draw.text((721, 285), f"{sa.get('position','N/D')}e  •  {sa.get('points','N/D')} pts  •  {sa.get('gf','N/D')}-{sa.get('ga','N/D')}", font=_font(FONT_REG, 16), fill=(199,215,234))
    draw.text((481, 251), "VS", font=_font(FONT_BOLD, 28), fill=white)
    draw.text((456, 290), dt.strftime("%d/%m/%Y") if dt else "N/D", font=_font(FONT_REG, 14), fill=(193,210,231))
    draw.text((482, 314), dt.strftime("%H:%M") if dt else "N/D", font=_font(FONT_REG, 16), fill=white)
    draw.text((163, 329), hf_txt, font=_font(FONT_BOLD, 16), fill=(231,240,250))
    draw.text((721, 329), af_txt, font=_font(FONT_BOLD, 16), fill=(231,240,250))
    draw.text((470, 350), "🏟", font=_font(FONT_BOLD, 22), fill=(155,184,215))
    venue = (match.get("venue") or {}).get("name") or "Stade"
    draw.multiline_text((505, 350), venue[:28], font=_font(FONT_REG, 14), fill=(206,220,239), spacing=1)

    # Logos fetched only if URL is supplied by the source.
    if home_logo:
        _paste_logo(img, home_logo, (62, 245, 145, 335), "H")
    else:
        draw.ellipse((70, 245, 145, 320), fill=(20,75,116), outline=cyan, width=2)
        draw.text((93, 263), home[:1].upper(), font=_font(FONT_BOLD, 30), fill=white)
    if away_logo:
        _paste_logo(img, away_logo, (640, 245, 710, 315), "A")
    else:
        draw.ellipse((638, 245, 708, 315), fill=(20,75,116), outline=cyan, width=2)
        draw.text((662, 263), away[:1].upper(), font=_font(FONT_BOLD, 30), fill=white)

    # Tabs visual, same proportions as the reference.
    tabs = [(42, "Vue d'ensemble"), (286, "📊  Statistiques"), (523, "⚽  Face à face"), (761, "⚽  Compos")]
    for i, (x, lab) in enumerate(tabs):
        fill = (35, 69, 126) if i == 0 else (11, 34, 60)
        outline = (70, 181, 255) if i == 0 else (49, 86, 124)
        _rounded(draw, (x, 418, x+224, 475), fill, outline=outline, radius=18, width=2)
        draw.text((x+24, 434), lab, font=_font(FONT_BOLD if i==0 else FONT_MED, 16), fill=white)

    _draw_prob_card(draw, (42, 488, 510, 885), model)
    _draw_goals_card(draw, (532, 488, 982, 885), model)
    _draw_scores_card(draw, (42, 902, 510, 1164), model)
    _draw_status_card(draw, (532, 902, 982, 1164), data, model)

    # Footer buttons are also represented in the image, then duplicated as real Telegram buttons.
    buttons = [
        (42, 1182, 347, 1244, "🔎  Analyse", (73,35,218)),
        (359, 1182, 664, 1244, "🎯  Probabilités", (23,66,126)),
        (677, 1182, 982, 1244, "⚽  Buts", (23,66,126)),
        (42, 1255, 347, 1317, "💰  Cotes", (23,66,126)),
        (359, 1255, 664, 1317, "⚽  Buteurs", (23,66,126)),
        (677, 1255, 982, 1317, "🔬  Simuler", (23,66,126)),
        (42, 1330, 982, 1388, "⬅️  Menu principal", (13,73,151)),
    ]
    for bx1, by1, bx2, by2, lab, fill in buttons:
        _rounded(draw, (bx1,by1,bx2,by2), fill, outline=(0,191,255), radius=16, width=2)
        f = _font(FONT_BOLD, 18)
        tw = f.getlength(lab)
        draw.text(((bx1+bx2-tw)/2, by1+18), lab, font=f, fill=white)

    draw.text((52, 1430), "“Analyse aujourd'hui, de meilleures décisions demain.”", font=_font(FONT_ITALIC, 14), fill=(181,199,222))
    draw.text((824, 1430), "Sport Analyzer V13  ▂▅▇", font=_font(FONT_ITALIC, 13), fill=(181,199,222))
    return img


async def fetch_crest(url):
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(url)
            if r.status_code == 200 and r.content:
                return r.content
    except Exception:
        pass
    return None


async def run_analysis_dashboard_for_message(message, fixture_id, user_id):
    data, error = await full_analysis(fixture_id)
    if error:
        await message.reply_text(f"❌ {error}", reply_markup=main_menu())
        return

    record_model_predictions(user_id, data)
    await validate_predictions()

    home_url = (data.get("match", {}).get("homeTeam", {}) or {}).get("crest")
    away_url = (data.get("match", {}).get("awayTeam", {}) or {}).get("crest")
    home_bytes = await fetch_crest(home_url)
    away_bytes = await fetch_crest(away_url)
    home_logo = away_logo = None
    # PIL accepte directement les bytes. On les conserve dans data temporairement.
    if home_bytes:
        home_logo = "bytes://home"
    if away_bytes:
        away_logo = "bytes://away"

    # Dessin avec logos locaux, sans dépendre d'un navigateur.
    W, H = 1024, 1536
    img = render_analysis_dashboard(data, None, None)
    if home_bytes or away_bytes:
        # Re-rendu avec collage des logos depuis les bytes, pour éviter toute URL réseau dans le renderer.
        img = render_analysis_dashboard(data, None, None)
        if home_bytes:
            try:
                logo = Image.open(io.BytesIO(home_bytes)).convert("RGBA")
                logo.thumbnail((82,82), Image.Resampling.LANCZOS)
                img.paste(logo, (103-logo.width//2, 285-logo.height//2), logo)
            except Exception:
                pass
        if away_bytes:
            try:
                logo = Image.open(io.BytesIO(away_bytes)).convert("RGBA")
                logo.thumbnail((70,70), Image.Resampling.LANCZOS)
                img.paste(logo, (673-logo.width//2, 280-logo.height//2), logo)
            except Exception:
                pass

    output = io.BytesIO()
    output.name = "sport_analyzer_v13.png"
    img.save(output, format="PNG", optimize=True)
    output.seek(0)
    await message.reply_photo(
        photo=BufferedInputFile(output.getvalue(), filename="sport_analyzer_v13.png"),
        caption="⚡ <b>SPORT ANALYZER • V13</b>\nInterface graphique mobile • données dynamiques",
        parse_mode="HTML",
        reply_markup=match_keyboard(fixture_id),
    )

async def run_analysis_for_message(message, fixture_id, user_id):
    await message.reply_text("🔎 Analyse en cours…")
    data, error = await full_analysis(fixture_id)
    if error:
        await message.reply_text(f"❌ {error}", reply_markup=main_menu())
        return
    record_model_predictions(user_id, data)
    await validate_predictions()
    cards = build_analysis_cards(data)
    for i, card in enumerate(cards):
        await message.reply_text(
            card,
            parse_mode="HTML",
            reply_markup=match_keyboard(fixture_id) if i == len(cards) - 1 else None,
        )


async def run_buts_for_message(message, fixture_id):
    data, error = await full_analysis(fixture_id)
    if error:
        await message.reply_text(f"❌ {error}", reply_markup=main_menu())
        return
    model = data.get("model")
    if not model:
        await message.reply_text("❌ Données insuffisantes.", reply_markup=main_menu())
        return
    m = model["markets"]
    await message.reply_text(
        "⚽ MARCHÉS DE BUTS\n\n"
        f"BTTS Oui : {m['btts']:.1f}%\n"
        f"BTTS Non : {100-m['btts']:.1f}%\n"
        f"Over 1.5 : {m['over15']:.1f}%\n"
        f"Over 2.5 : {m['over25']:.1f}%\n"
        f"Over 3.5 : {m['over35']:.1f}%\n"
        f"Under 2.5 : {m['under25']:.1f}%\n"
        f"Under 3.5 : {m['under35']:.1f}%\n"
        f"xG : {model['home_xg']:.2f} - {model['away_xg']:.2f}",
        reply_markup=match_keyboard(fixture_id),
    )


async def run_prob_for_message(message, fixture_id):
    data, error = await full_analysis(fixture_id)
    if error:
        await message.reply_text(f"❌ {error}", reply_markup=main_menu())
        return
    model = data.get("model")
    if not model:
        await message.reply_text("❌ Données insuffisantes.", reply_markup=main_menu())
        return
    h, d, a = model["final"]
    home = data["match"]["homeTeam"]["name"]
    away = data["match"]["awayTeam"]["name"]
    await message.reply_text(
        "🎯 PROBABILITÉS\n\n"
        f"1 {home} : {h:.1f}%\n"
        f"X Nul : {d:.1f}%\n"
        f"2 {away} : {a:.1f}%\n\n"
        f"1X : {h+d:.1f}%\nX2 : {d+a:.1f}%\n12 : {h+a:.1f}%",
        reply_markup=match_keyboard(fixture_id),
    )


async def run_cotes_for_message(message, fixture_id):
    await message.reply_text(
        "💰 COTES\n\n"
        "La source gratuite actuelle ne fournit pas toujours les cotes. "
        "Aucune cote n'est inventée.\n\n"
        "Pour enregistrer une cote manuellement :\n"
        "/mise ID marché sélection montant cote",
        reply_markup=match_keyboard(fixture_id),
    )


async def run_buteur_for_message(message, fixture_id):
    if str(fixture_id).startswith("TSDB-"):
        event_id = str(fixture_id).split("-", 1)[1]
        async with httpx.AsyncClient(timeout=20) as client:
            data, error = await tsdb_get(
                client, "lookuptimeline.php", {"id": event_id},
                cache_key=f"tsdb:timeline:{event_id}", ttl=120
            )
        if error:
            await message.reply_text(f"❌ {error}", reply_markup=main_menu())
            return
        timeline = data.get("timeline", []) or []
        goals = [
            x for x in timeline
            if "goal" in str(x.get("strTimeline", "")).lower()
        ]
        if not goals:
            await message.reply_text(
                "⚽ Aucun événement de but disponible.",
                reply_markup=match_keyboard(fixture_id),
            )
            return
        msg = "⚽ BUTS / ÉVÉNEMENTS\n\n"
        for g in goals[:15]:
            msg += (
                f"• {g.get('strTimeline', 'But')} | "
                f"{g.get('strPlayer', 'Joueur N/D')}\n"
            )
        await message.reply_text(msg, reply_markup=match_keyboard(fixture_id))
        return

    async with httpx.AsyncClient(timeout=20) as client:
        data, error = await find_fd_match(client, fixture_id)
    if error:
        await message.reply_text(f"❌ {error}", reply_markup=main_menu())
        return
    goals = data.get("goals", []) or []
    if not goals:
        await message.reply_text(
            "⚽ Aucun détail de buteur disponible pour ce match.",
            reply_markup=match_keyboard(fixture_id),
        )
        return
    msg = "⚽ BUTEURS\n\n"
    for g in goals[:20]:
        scorer = g.get("scorer", {}) or {}
        assist = g.get("assist", {}) or {}
        msg += f"• {g.get('minute', '?')}' {scorer.get('name', 'N/D')}"
        if assist.get("name"):
            msg += f" | passe : {assist['name']}"
        msg += "\n"
    await message.reply_text(msg, reply_markup=match_keyboard(fixture_id))


async def send_performance_result(message, user_id):
    await validate_predictions()
    perf = db_performance(user_id)
    if not perf["total"]:
        await message.reply_text(
            "📊 PERFORMANCE DU MODÈLE\n\n"
            "Aucune prédiction terminée n'est encore disponible.",
            reply_markup=main_menu(),
        )
        return
    msg = (
        "📊 PERFORMANCE DU MODÈLE\n\n"
        f"Prédictions validées : {perf['total']}\n"
        f"Correctes : {perf['wins']}\n"
        f"Taux de réussite : {perf['accuracy']:.1f}%\n\n"
        "PAR MARCHÉ\n"
    )
    for market, d in sorted(perf["markets"].items()):
        acc = d["wins"] / d["total"] * 100 if d["total"] else 0
        msg += f"• {market} : {acc:.1f}% ({d['wins']}/{d['total']})\n"
    await message.reply_text(msg, reply_markup=main_menu())


async def send_validation_result(message, user_id):
    checked = await validate_predictions()
    perf = db_performance(user_id)
    result = (
        f"🔄 VALIDATION\n\n"
        f"Prédictions nouvellement validées : {checked}\n"
        f"Prédictions terminées : {perf['total']}\n"
        f"Taux de réussite : {perf['accuracy']:.1f}%"
        if perf["accuracy"] is not None
        else "🔄 VALIDATION\n\nAucune prédiction terminée dans ton historique."
    )
    await message.reply_text(result, reply_markup=main_menu())


async def send_bankroll_result(message, user_id):
    total, profit, count, wins, roi = db_summary(user_id)
    await message.reply_text(
        "💶 BANKROLL\n\n"
        f"Mises enregistrées : {total:.2f} €\n"
        f"Profit/perte : {profit:+.2f} €\n"
        f"Paris : {count}\n"
        f"Paris gagnants : {wins}\n"
        f"ROI : {roi:+.2f}%",
        reply_markup=main_menu(),
    )


async def send_status_result(message):
    async with httpx.AsyncClient(timeout=15) as client:
        fd_ok = False
        fd_detail = "Clé absente"
        if FOOTBALL_DATA_KEY:
            data, error = await fd_get(
                client, "/matches", {"date": today_paris()},
                cache_key="fd:status", ttl=30
            )
            fd_ok = data is not None and error is None
            fd_detail = "OK" if fd_ok else str(error)
        ts_data, ts_error = await tsdb_get(
            client, "eventsday.php",
            {"d": today_paris(), "s": "Soccer"},
            cache_key="tsdb:status", ttl=30
        )
        ts_ok = ts_data is not None and ts_error is None
    await message.reply_text(
        "🔌 ÉTAT DES SOURCES\n\n"
        f"Football-Data.org : {'🟢 OK' if fd_ok else '🔴 Indisponible'}\n"
        f"Détail : {fd_detail[:300]}\n\n"
        f"TheSportsDB : {'🟢 OK' if ts_ok else '🔴 Indisponible'}\n"
        "API-Football : ⚪ désactivée dans V5",
        reply_markup=main_menu(),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚡ <b>SPORT ANALYZER • V13</b>\n"
        "╭────────────────────────╮\n"
        "│ ⚽ <b>FOOTBALL INTELLIGENCE</b>\n"
        "│ 🎯 Probabilités  •  ⚽ Buts\n"
        "│ 💰 Cotes  •  🔬 Simulation\n"
        "╰────────────────────────╯\n\n"
        "👇 <b>CHOISIS TON MODULE</b>",
        parse_mode="HTML",
        reply_markup=main_menu(),
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📊 SPORT ANALYZER V13\n\n"
        "/match = matchs du jour\n"
        "/analyse ID = analyse statistique\n"
        "/buts ID = BTTS, Over/Under et xG modèle\n"
        "/probabilite ID = probabilités 1X2\n"
        "/buteur ID = événements/buteurs si la source les fournit\n"
        "/cotes ID = statut des cotes\n\n"
        "💶 SUIVI\n"
        "/mise ID marché sélection montant [cote]\n"
        "/resultat ID_BET win|loss|void\n"
        "/bankroll\n\n"
        "🔧 /statusapi = état des sources\n"
        "📈 /performance = résultats historiques du modèle\n"
        "🔄 /validation = valider les prédictions terminées"
    )


async def match_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with httpx.AsyncClient(timeout=20) as client:
        if FOOTBALL_DATA_KEY:
            matches, error = await fd_today_matches(client)
            source = "Football-Data.org"
            if error:
                matches = []
        else:
            matches, error = [], "Clé Football-Data.org absente"

        if not matches:
            events, ts_error = await tsdb_today_events(client)
            if events:
                matches = [tsdb_to_match(e) for e in events]
                source = "TheSportsDB"
            elif error:
                await update.message.reply_text(
                    "❌ Aucune source disponible.\n\n"
                    "Ajoute FOOTBALL_DATA_KEY dans Render ou réessaie plus tard."
                )
                return

    matches.sort(key=lambda x: fd_dt(x) or datetime.max.replace(tzinfo=PARIS))
    matches = matches[:40]
    msg = f"⚽ MATCHS DU JOUR\n📅 {today_paris()}\n🔌 Source : {source}\n📊 {len(matches)} matchs\n\n"
    for item in matches:
        dt = fd_dt(item)
        time_text = dt.strftime("%H:%M") if dt else "N/D"
        home, away = match_names(item)
        comp = item.get("competition", {}).get("name", "Football")
        msg += f"🕒 {time_text} | {comp}\n{home} - {away}\n🆔 {item.get('id')}\n\n"
    msg += "ℹ️ Les données gratuites varient selon la source et la compétition."
    await update.message.reply_text(msg[:3900], reply_markup=match_list_keyboard(matches))


async def analyse_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Utilisation : /analyse ID\nExemple : /analyse 123456")
        return
    fixture_id = context.args[0]
    if str(fixture_id).startswith("TSDB-"):
        await update.message.reply_text(
            "ℹ️ Cet ID vient de TheSportsDB. Pour l'analyse complète, utilise un ID Football-Data.org."
        )
        return
    await update.message.reply_text("🔎 Génération du dashboard…")
    await run_analysis_dashboard_for_message(update.message, fixture_id, update.effective_user.id)


async def buts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Utilisation : /buts ID")
        return
    data, error = await full_analysis(context.args[0])
    if error:
        await update.message.reply_text(f"❌ {error}")
        return
    model = data.get("model")
    if not model:
        await update.message.reply_text("❌ Données insuffisantes pour le modèle de buts.")
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
        f"xG : {model['home_xg']:.2f} - {model['away_xg']:.2f}"
    )


async def probabilite_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Utilisation : /probabilite ID")
        return
    data, error = await full_analysis(context.args[0])
    if error:
        await update.message.reply_text(f"❌ {error}")
        return
    model = data.get("model")
    if not model:
        await update.message.reply_text("❌ Données insuffisantes.")
        return
    h, d, a = model["final"]
    home = data["match"]["homeTeam"]["name"]
    away = data["match"]["awayTeam"]["name"]
    await update.message.reply_text(
        "🎯 PROBABILITÉS\n\n"
        f"1 {home} : {prob_bar(h)} {h:.1f}%\n"
        f"X Nul : {prob_bar(d)} {d:.1f}%\n"
        f"2 {away} : {prob_bar(a)} {a:.1f}%\n\n"
        f"1X : {prob_bar(h+d)} {h+d:.1f}%\n"
        f"X2 : {prob_bar(d+a)} {d+a:.1f}%\n"
        f"12 : {prob_bar(h+a)} {h+a:.1f}%"
    )


async def cotes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💰 COTES\n\n"
        "La version gratuite de Football-Data.org ne fournit pas les cotes bookmaker. "
        "Le bot ne fabrique donc aucune cote.\n\n"
        "Tu peux quand même saisir une cote manuellement avec /mise."
    )


async def buteur_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Utilisation : /buteur ID")
        return
    fixture_id = context.args[0]
    if str(fixture_id).startswith("TSDB-"):
        event_id = str(fixture_id).split("-", 1)[1]
        async with httpx.AsyncClient(timeout=20) as client:
            data, error = await tsdb_get(
                client,
                "lookuptimeline.php",
                {"id": event_id},
                cache_key=f"tsdb:timeline:{event_id}",
                ttl=120,
            )
        if error:
            await update.message.reply_text(f"❌ {error}")
            return
        timeline = data.get("timeline", []) or []
        goals = [x for x in timeline if "goal" in str(x.get("strTimeline", "")).lower()]
        if not goals:
            await update.message.reply_text("⚽ Aucun événement de but disponible.")
            return
        msg = "⚽ BUTS / ÉVÉNEMENTS\n\n"
        for g in goals[:15]:
            msg += f"• {g.get('strTimeline', 'But')} | {g.get('strPlayer', 'Joueur N/D')}\n"
        await update.message.reply_text(msg)
        return

    async with httpx.AsyncClient(timeout=20) as client:
        data, error = await find_fd_match(client, fixture_id)
    if error:
        await update.message.reply_text(f"❌ {error}")
        return
    goals = data.get("goals", []) or []
    if not goals:
        await update.message.reply_text(
            "⚽ Aucun détail de buteur fourni par Football-Data.org pour ce match dans le niveau gratuit."
        )
        return
    msg = "⚽ BUTEURS\n\n"
    for g in goals[:20]:
        scorer = g.get("scorer", {}) or {}
        assist = g.get("assist", {}) or {}
        msg += f"• {g.get('minute', '?')}' {scorer.get('name', 'N/D')}"
        if assist.get("name"):
            msg += f" | passe : {assist['name']}"
        msg += "\n"
    await update.message.reply_text(msg)


async def mise_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 4:
        await update.message.reply_text(
            "Utilisation : /mise ID marché sélection montant [cote]\n"
            "Exemple : /mise 123456 1X Home 10 1.80"
        )
        return
    try:
        fixture_id = context.args[0]
        market = context.args[1]
        selection = context.args[2]
        stake = float(context.args[3].replace(",", "."))
        odds = float(context.args[4].replace(",", ".")) if len(context.args) >= 5 else None
        if stake <= 0 or (odds is not None and odds <= 1):
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Montant ou cote invalide.")
        return
    bet_id = db_add_bet(update.effective_user.id, fixture_id, market, selection, stake, odds)
    text = (
        "💶 PARI ENREGISTRÉ\n\n"
        f"ID pari : {bet_id}\n"
        f"Match : {fixture_id}\n"
        f"Marché : {market}\n"
        f"Sélection : {selection}\n"
        f"Mise : {stake:.2f} €\n"
    )
    if odds:
        text += f"Cote : {odds:.2f}\n"
    await update.message.reply_text(text)


async def resultat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        f"📊 Pari {bet_id} réglé\nRésultat : {result}\nProfit/perte : {profit:+.2f} €"
    )


async def bankroll_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total, profit, count, wins, roi = db_summary(update.effective_user.id)
    await update.message.reply_text(
        "💶 BANKROLL\n\n"
        f"Mises enregistrées : {total:.2f} €\n"
        f"Profit/perte : {profit:+.2f} €\n"
        f"Paris : {count}\n"
        f"Paris gagnants : {wins}\n"
        f"ROI : {roi:+.2f}%"
    )


async def statusapi_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with httpx.AsyncClient(timeout=15) as client:
        fd_ok = False
        fd_detail = "Clé absente"
        if FOOTBALL_DATA_KEY:
            data, error = await fd_get(client, "/matches", {"date": today_paris()}, cache_key="fd:status", ttl=30)
            fd_ok = data is not None and error is None
            fd_detail = "OK" if fd_ok else str(error)
        ts_data, ts_error = await tsdb_get(
            client,
            "eventsday.php",
            {"d": today_paris(), "s": "Soccer"},
            cache_key="tsdb:status",
            ttl=30,
        )
        ts_ok = ts_data is not None and ts_error is None
    await update.message.reply_text(
        "🔌 ÉTAT DES SOURCES\n\n"
        f"Football-Data.org : {'🟢 OK' if fd_ok else '🔴 Indisponible'}\n"
        f"Détail : {fd_detail[:300]}\n\n"
        f"TheSportsDB : {'🟢 OK' if ts_ok else '🔴 Indisponible'}\n"
        f"API-Football : ⚪ désactivée dans V3"
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
    return JSONResponse({"status": "ok", "bot": "sport-analyzer-bot-v13"})


@asynccontextmanager
async def lifespan(app):
    global telegram_app
    if not TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN est manquant.")
    if not RENDER_URL:
        raise RuntimeError("RENDER_EXTERNAL_URL est manquant.")
    init_db()
    telegram_app = Application.builder().token(TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("help", help_command))
    telegram_app.add_handler(CommandHandler("match", match_command))
    telegram_app.add_handler(CommandHandler("analyse", analyse_command))
    telegram_app.add_handler(CommandHandler("cotes", cotes_command))
    telegram_app.add_handler(CommandHandler("buteur", buteur_command))
    telegram_app.add_handler(CommandHandler("buts", buts_command))
    telegram_app.add_handler(CommandHandler("probabilite", probabilite_command))
    telegram_app.add_handler(CommandHandler("mise", mise_command))
    telegram_app.add_handler(CommandHandler("resultat", resultat_command))
    telegram_app.add_handler(CommandHandler("bankroll", bankroll_command))
    telegram_app.add_handler(CommandHandler("statusapi", statusapi_command))
    telegram_app.add_handler(CommandHandler("performance", performance_command))
    telegram_app.add_handler(CommandHandler("validation", validate_command))
    telegram_app.add_handler(CallbackQueryHandler(callback_handler))

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.bot.set_webhook(f"{RENDER_URL}/telegram")
    try:
        yield
    finally:
        await telegram_app.stop()
        await telegram_app.shutdown()


routes = [
    Route("/health", health, methods=["GET"]),
    Route("/telegram", webhook, methods=["POST"]),
]
app = Starlette(routes=routes, lifespan=lifespan)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
