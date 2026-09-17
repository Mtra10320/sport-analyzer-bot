

import os
import math
import sqlite3
import time
from datetime import datetime, timezone
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

# ============================================================
# SPORT ANALYZER V3
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


def build_analysis_message(data):
    match = data["match"]
    home = match["homeTeam"]["name"]
    away = match["awayTeam"]["name"]
    comp = match.get("competition", {}).get("name", "Compétition")
    dt = fd_dt(match)
    date_text = dt.strftime("%d/%m/%Y %H:%M") if dt else "Horaire N/D"
    model = data["model"]
    sh = data["standings_home"]
    sa = data["standings_away"]

    msg = (
        "🔎 ANALYSE SPORT ANALYZER V3\n\n"
        f"⚽ {home} - {away}\n"
        f"🏆 {comp}\n"
        f"📅 {date_text}\n"
        f"📊 Statut : {fd_status(match.get('status'))}\n"
        f"🆔 ID : {match.get('id')}\n\n"
        "📋 CLASSEMENT\n"
    )
    if sh:
        msg += f"• {home} : {sh.get('position', 'N/D')}e | {sh.get('points', 'N/D')} pts | {sh.get('gf', 'N/D')}-{sh.get('ga', 'N/D')}\n"
    else:
        msg += f"• {home} : N/D\n"
    if sa:
        msg += f"• {away} : {sa.get('position', 'N/D')}e | {sa.get('points', 'N/D')} pts | {sa.get('gf', 'N/D')}-{sa.get('ga', 'N/D')}\n"
    else:
        msg += f"• {away} : N/D\n"

    hf = data["home_form"]
    af = data["away_form"]
    msg += "\n📈 FORME 5 DERNIERS\n"
    msg += f"• {home} : " + (" ".join(x["result"] for x in hf) if hf else "N/D") + "\n"
    msg += f"• {away} : " + (" ".join(x["result"] for x in af) if af else "N/D") + "\n"

    if model:
        ph, pd, pa = model["final"]
        m = model["markets"]
        msg += (
            "\n🎯 PROBABILITÉS MODÈLE\n"
            f"• 1 : {ph:.1f}%\n"
            f"• X : {pd:.1f}%\n"
            f"• 2 : {pa:.1f}%\n"
            f"• 1X : {ph + pd:.1f}%\n"
            f"• X2 : {pd + pa:.1f}%\n"
            f"• 12 : {ph + pa:.1f}%\n\n"
            "⚽ MARCHÉS DE BUTS\n"
            f"• BTTS Oui : {m['btts']:.1f}%\n"
            f"• Over 1.5 : {m['over15']:.1f}%\n"
            f"• Over 2.5 : {m['over25']:.1f}%\n"
            f"• Over 3.5 : {m['over35']:.1f}%\n"
            f"• Under 2.5 : {m['under25']:.1f}%\n"
            f"• xG modèle : {model['home_xg']:.2f} - {model['away_xg']:.2f}\n"
        )
        scores = likely_scores(model["matrix"], 3)
        if scores:
            msg += "\n🔢 SCORES LES PLUS PROBABLES\n"
            for h, a, p in scores:
                msg += f"• {h}-{a} : {p:.1f}%\n"
        msg += f"\n🧠 Confiance : {confidence_label(model['final'], data['quality'])}\n"
    else:
        msg += "\n⚠️ Données insuffisantes pour calculer le modèle de buts.\n"

    h2h = data["h2h"]
    if h2h:
        wins_h = wins_a = draws = 0
        for item in h2h:
            hg, ag = score_pair(item)
            if hg is None or ag is None:
                continue
            hn = item.get("homeTeam", {}).get("name", "")
            an = item.get("awayTeam", {}).get("name", "")
            if hg == ag:
                draws += 1
            elif (hn == home and hg > ag) or (an == home and ag > hg):
                wins_h += 1
            else:
                wins_a += 1
        msg += (
            "\n🤝 H2H\n"
            f"• Matchs disponibles : {len(h2h)}\n"
            f"• {home} : {wins_h}\n"
            f"• Nuls : {draws}\n"
            f"• {away} : {wins_a}\n"
        )
    else:
        msg += "\n🤝 H2H : N/D\n"

    msg += (
        "\n━━━━━━━━━━━━━━━━━━\n"
        "ℹ️ Les probabilités sont des estimations statistiques calculées à partir des données disponibles."
    )
    return msg


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 SPORT ANALYZER V3\n\n"
        "Football-Data.org + TheSportsDB.\n\n"
        "/match\n"
        "/analyse ID\n"
        "/cotes ID\n"
        "/buteur ID\n"
        "/buts ID\n"
        "/probabilite ID\n"
        "/mise ID marché sélection montant [cote]\n"
        "/resultat ID_BET win|loss|void\n"
        "/bankroll\n"
        "/statusapi\n"
        "/help"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📊 SPORT ANALYZER V3\n\n"
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
        "🔧 /statusapi = état des sources"
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
    await update.message.reply_text(msg[:3900])


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
    await update.message.reply_text("🔎 Analyse en cours...")
    data, error = await full_analysis(fixture_id)
    if error:
        await update.message.reply_text(f"❌ {error}")
        return
    msg = build_analysis_message(data)
    if len(msg) <= 3900:
        await update.message.reply_text(msg)
    else:
        for i in range(0, len(msg), 3800):
            await update.message.reply_text(msg[i:i + 3800])


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
        f"1 {home} : {h:.1f}%\n"
        f"X Nul : {d:.1f}%\n"
        f"2 {away} : {a:.1f}%\n\n"
        f"1X : {h+d:.1f}%\n"
        f"X2 : {d+a:.1f}%\n"
        f"12 : {h+a:.1f}%"
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
    return JSONResponse({"status": "ok", "bot": "sport-analyzer-bot-v3"})


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
