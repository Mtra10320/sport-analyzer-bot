
import os
import math
import sqlite3
import time
import io
import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn
from PIL import Image, ImageDraw, ImageFont

# ============================================================
# SPORT ANALYZER V13.1
# Optimized mobile dashboard
# Sources: Football-Data.org + TheSportsDB fallback
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
CACHE_LOCK = asyncio.Lock()
telegram_app = None

FREE_COMPETITIONS = {
    "CL": "Ligue des champions", "PPL": "Primeira Liga", "PL": "Premier League",
    "DED": "Eredivisie", "BL1": "Bundesliga", "FL1": "Ligue 1",
    "SA": "Serie A", "PD": "La Liga", "ELC": "Championship",
    "BSA": "Serie A Brésil", "WC": "Coupe du monde", "EC": "Euro",
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def today_paris():
    return datetime.now(PARIS).strftime("%Y-%m-%d")


def clamp(value, low=0.0, high=100.0):
    return max(low, min(high, float(value)))


def num(value):
    try:
        return float(value)
    except Exception:
        return None


def normalize3(a, b, c):
    vals = [max(0.0, float(a)), max(0.0, float(b)), max(0.0, float(c))]
    total = sum(vals)
    if total <= 0:
        return 33.33, 33.34, 33.33
    return tuple(v * 100.0 / total for v in vals)


def poisson_pmf(k, lam):
    if lam is None or lam < 0:
        return None
    try:
        return math.exp(-lam) * lam**k / math.factorial(k)
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
            p = (ph or 0.0) * (pa or 0.0)
            matrix[(h, a)] = p
            total += p
    if total <= 0:
        return None
    return {k: v / total for k, v in matrix.items()}


def poisson_markets(matrix):
    if not matrix:
        return {}
    out = {"home": 0.0, "draw": 0.0, "away": 0.0, "btts": 0.0,
           "over15": 0.0, "over25": 0.0, "over35": 0.0,
           "under25": 0.0, "under35": 0.0}
    for (h, a), p in matrix.items():
        if h > a: out["home"] += p
        elif h == a: out["draw"] += p
        else: out["away"] += p
        if h > 0 and a > 0: out["btts"] += p
        if h + a >= 2: out["over15"] += p
        if h + a >= 3: out["over25"] += p
        if h + a >= 4: out["over35"] += p
        if h + a <= 2: out["under25"] += p
        if h + a <= 3: out["under35"] += p
    return {k: v * 100.0 for k, v in out.items()}


def likely_scores(matrix, limit=5):
    if not matrix:
        return []
    rows = sorted(matrix.items(), key=lambda x: x[1], reverse=True)
    return [(h, a, p * 100.0) for (h, a), p in rows[:limit]]


def fd_status(status):
    return {"SCHEDULED": "À venir", "TIMED": "Programmé", "IN_PLAY": "En direct",
            "PAUSED": "Mi-temps", "FINISHED": "Terminé", "POSTPONED": "Reporté",
            "SUSPENDED": "Suspendu", "CANCELLED": "Annulé", "AWARDED": "Attribué"}.get(
        status, status or "N/D")


def fd_dt(item):
    try:
        return datetime.fromisoformat(item.get("utcDate", "").replace("Z", "+00:00")).astimezone(PARIS)
    except Exception:
        return None


def match_names(item):
    return (item.get("homeTeam", {}).get("name", "Inconnu"),
            item.get("awayTeam", {}).get("name", "Inconnu"))


def score_pair(item):
    score = item.get("score", {})
    full = score.get("fullTime", {}) if isinstance(score, dict) else {}
    return full.get("home"), full.get("away")

# -------------------------
# Database
# -------------------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS bets (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
        fixture_id TEXT NOT NULL, market TEXT NOT NULL, selection TEXT NOT NULL,
        stake REAL NOT NULL, odds REAL, probability REAL, result TEXT, profit REAL,
        created_at TEXT NOT NULL, settled_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
        fixture_id TEXT NOT NULL, home_team TEXT NOT NULL, away_team TEXT NOT NULL,
        market TEXT NOT NULL, selection TEXT NOT NULL, probability REAL,
        predicted_at TEXT NOT NULL, outcome TEXT, settled_at TEXT)""")
    conn.commit(); conn.close()


def db_add_bet(user_id, fixture_id, market, selection, stake, odds=None, probability=None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("""INSERT INTO bets
        (user_id, fixture_id, market, selection, stake, odds, probability, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, str(fixture_id), market, selection, stake, odds, probability, now_iso()))
    bet_id = cur.lastrowid; conn.commit(); conn.close(); return bet_id


def db_settle_bet(user_id, bet_id, result):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT stake, odds, result FROM bets WHERE id=? AND user_id=?", (bet_id, user_id)).fetchone()
    if not row or row[2] is not None:
        conn.close(); return None
    stake, odds, _ = row
    profit = stake * ((odds or 1.0) - 1.0) if result == "win" else -stake if result == "loss" else 0.0
    conn.execute("UPDATE bets SET result=?, profit=?, settled_at=? WHERE id=? AND user_id=?",
                 (result, profit, now_iso(), bet_id, user_id))
    conn.commit(); conn.close(); return profit


def db_summary(user_id):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT stake, result, profit FROM bets WHERE user_id=?", (user_id,)).fetchall()
    conn.close()
    total = sum(r[0] or 0 for r in rows); profit = sum(r[2] or 0 for r in rows)
    settled = [r for r in rows if r[1] in {"win", "loss", "void"}]
    wins = sum(r[1] == "win" for r in settled)
    return total, profit, len(rows), wins, profit / total * 100.0 if total else 0.0


def db_add_prediction(user_id, fixture_id, home_team, away_team, market, selection, probability):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("""INSERT INTO predictions
        (user_id, fixture_id, home_team, away_team, market, selection, probability, predicted_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, str(fixture_id), home_team, away_team, market, selection, probability, now_iso()))
    pid = cur.lastrowid; conn.commit(); conn.close(); return pid


def prediction_outcome(market, selection, home_goals, away_goals):
    if home_goals is None or away_goals is None: return None
    total = home_goals + away_goals
    if market == "1X2": actual = "1" if home_goals > away_goals else "X" if home_goals == away_goals else "2"
    elif market == "BTTS": actual = "Oui" if home_goals > 0 and away_goals > 0 else "Non"
    elif market == "O2.5": actual = "Oui" if total >= 3 else "Non"
    elif market == "O1.5": actual = "Oui" if total >= 2 else "Non"
    elif market == "1X": actual = "Oui" if home_goals >= away_goals else "Non"
    elif market == "X2": actual = "Oui" if away_goals >= home_goals else "Non"
    elif market == "12": actual = "Oui" if home_goals != away_goals else "Non"
    else: return None
    return "win" if str(selection).lower() == actual.lower() else "loss"


def db_performance(user_id):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT market, probability, outcome FROM predictions WHERE user_id=? AND outcome IS NOT NULL", (user_id,)).fetchall()
    conn.close()
    by_market = {}
    for market, probability, outcome in rows:
        d = by_market.setdefault(market, {"total": 0, "wins": 0, "prob_sum": 0.0, "prob_n": 0})
        d["total"] += 1; d["wins"] += outcome == "win"
        if probability is not None: d["prob_sum"] += float(probability); d["prob_n"] += 1
    total = len(rows); wins = sum(o == "win" for _, _, o in rows)
    return {"total": total, "wins": wins, "accuracy": wins / total * 100.0 if total else None, "markets": by_market}

# -------------------------
# HTTP/cache
# -------------------------

async def cached_get(client, url, headers=None, params=None, cache_key=None, ttl=60):
    if cache_key:
        async with CACHE_LOCK:
            cached = CACHE.get(cache_key)
            if cached and cached["expires"] > time.time(): return cached["data"], None
    try:
        r = await client.get(url, headers=headers or {}, params=params or {})
    except Exception as exc:
        return None, f"Erreur réseau : {exc}"
    if r.status_code != 200:
        try: detail = r.json()
        except Exception: detail = r.text[:300]
        return None, f"HTTP {r.status_code} : {detail}"
    try: data = r.json()
    except Exception: return None, "Réponse JSON invalide."
    if cache_key:
        async with CACHE_LOCK: CACHE[cache_key] = {"data": data, "expires": time.time() + ttl}
    return data, None


async def fd_get(client, path, params=None, cache_key=None, ttl=60):
    if not FOOTBALL_DATA_KEY: return None, "Clé FOOTBALL_DATA_KEY absente."
    return await cached_get(client, f"{FD_BASE}{path}", {"X-Auth-Token": FOOTBALL_DATA_KEY}, params, cache_key, ttl)


async def tsdb_get(client, path, params=None, cache_key=None, ttl=60):
    return await cached_get(client, f"{TSDB_BASE}/{path}", params=params, cache_key=cache_key, ttl=ttl)

# -------------------------
# Sources
# -------------------------

async def fd_today_matches(client):
    date = today_paris(); data, error = await fd_get(client, "/matches", {"date": date}, f"fd:today:{date}", 120)
    if error: return [], error
    return [m for m in data.get("matches", []) if m.get("competition", {}).get("code") in FREE_COMPETITIONS], None


async def tsdb_today_events(client):
    date = today_paris(); data, error = await tsdb_get(client, "eventsday.php", {"d": date, "s": "Soccer"}, f"tsdb:today:{date}", 120)
    if error: return [], error
    return data.get("events", []) or [], None


def tsdb_to_match(event):
    raw_date = event.get("strTimestamp") or event.get("dateEvent") or ""; raw_time = event.get("strTime") or "12:00:00"
    try:
        if "T" not in raw_date: raw_date = f"{raw_date}T{raw_time}"
        dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    except Exception: dt = None
    return {"id": f"TSDB-{event.get('idEvent','0')}", "utcDate": dt.astimezone(timezone.utc).isoformat() if dt else "",
            "status": "FINISHED" if event.get("strStatus") in {"Match Finished", "FT"} else "SCHEDULED",
            "competition": {"name": event.get("strLeague", "Soccer"), "code": "TSDB"},
            "homeTeam": {"id": event.get("idHomeTeam"), "name": event.get("strHomeTeam", "Inconnu")},
            "awayTeam": {"id": event.get("idAwayTeam"), "name": event.get("strAwayTeam", "Inconnu")},
            "score": {"fullTime": {"home": num(event.get("intHomeScore")), "away": num(event.get("intAwayScore"))}},
            "venue": event.get("strVenue"), "_tsdb": event}


async def find_fd_match(client, fixture_id):
    if str(fixture_id).startswith("TSDB-"): return None, "Cet ID appartient à TheSportsDB et ne peut pas être analysé par Football-Data.org."
    try: fid = int(fixture_id)
    except ValueError: return None, "ID de match invalide."
    data, error = await fd_get(client, f"/matches/{fid}", {"head2head": 10}, f"fd:match:{fid}", 120)
    if error: return None, error
    return data, None if data and data.get("id") else "Match introuvable."


async def team_recent(client, team_id):
    if not team_id: return [], "ID équipe absent."
    data, error = await fd_get(client, f"/teams/{team_id}/matches", {"status": "FINISHED", "limit": 5}, f"fd:team:{team_id}:recent", 300)
    if error: return [], error
    return data.get("matches", []), None


async def competition_standings(client, code):
    if not code or code == "TSDB": return [], None
    data, error = await fd_get(client, f"/competitions/{code}/standings", {}, f"fd:standings:{code}", 600)
    if error: return [], error
    return data.get("standings", []), None


def parse_form(matches, team_id):
    rows = []
    for item in matches or []:
        hid = item.get("homeTeam", {}).get("id"); aid = item.get("awayTeam", {}).get("id"); hg, ag = score_pair(item)
        if hg is None or ag is None or item.get("status") not in {"FINISHED", "AWARDED"}: continue
        if str(team_id) == str(hid): gf, ga = hg, ag; opponent = item.get("awayTeam", {}).get("name", "?")
        elif str(team_id) == str(aid): gf, ga = ag, hg; opponent = item.get("homeTeam", {}).get("name", "?")
        else: continue
        rows.append({"gf": gf, "ga": ga, "result": "V" if gf > ga else "N" if gf == ga else "D", "opponent": opponent})
    return rows[:5]


def form_metrics(form):
    if not form: return {}
    n = len(form); gf = sum(x["gf"] for x in form); ga = sum(x["ga"] for x in form)
    wins = sum(x["result"] == "V" for x in form); draws = sum(x["result"] == "N" for x in form); losses = sum(x["result"] == "D" for x in form)
    return {"matches": n, "gf_avg": gf/n, "ga_avg": ga/n, "wins": wins, "draws": draws, "losses": losses, "points_avg": (wins*3+draws)/n}


def standings_metrics(standings, team_id):
    for table in standings or []:
        for row in table.get("table", []):
            if str(row.get("team", {}).get("id")) == str(team_id):
                return {"position": row.get("position"), "played": row.get("playedGames"), "won": row.get("won"), "draw": row.get("draw"), "lost": row.get("lost"), "gf": row.get("goalsFor"), "ga": row.get("goalsAgainst"), "points": row.get("points")}
    return {}


def build_model(home_form, away_form):
    hf = form_metrics(home_form); af = form_metrics(away_form)
    if not hf or not af: return None
    home_xg = clamp((hf["gf_avg"] + af["ga_avg"]) / 2.0 * 1.08, 0.15, 4.0)
    away_xg = clamp((af["gf_avg"] + hf["ga_avg"]) / 2.0, 0.10, 4.0)
    matrix = poisson_matrix(home_xg, away_xg); markets = poisson_markets(matrix)
    return {"home_xg": home_xg, "away_xg": away_xg, "matrix": matrix, "markets": markets,
            "final": normalize3(markets["home"], markets["draw"], markets["away"]), "form_home": hf, "form_away": af}


async def full_analysis(fixture_id):
    if str(fixture_id).startswith("TSDB-"): return None, "Les analyses avancées nécessitent actuellement un ID Football-Data.org."
    async with httpx.AsyncClient(timeout=10, limits=httpx.Limits(max_connections=8)) as client:
        match, error = await find_fd_match(client, fixture_id)
        if error: return None, error
        home = match.get("homeTeam", {}); away = match.get("awayTeam", {}); code = match.get("competition", {}).get("code")
        (home_recent, e1), (away_recent, e2), (standings, e3) = await asyncio.gather(
            team_recent(client, home.get("id")), team_recent(client, away.get("id")), competition_standings(client, code))
    if e1 or e2: return None, e1 or e2
    home_form = parse_form(home_recent, home.get("id")); away_form = parse_form(away_recent, away.get("id")); model = build_model(home_form, away_form)
    h2h_obj = match.get("head2head", {}); h2h = h2h_obj.get("matches", []) if isinstance(h2h_obj, dict) else []
    quality = sum([bool(home_form), bool(away_form), bool(standings), bool(h2h)]) / 4.0
    return {"match": match, "home_form": home_form, "away_form": away_form, "standings": standings, "h2h": h2h,
            "model": model, "quality": quality, "standings_home": standings_metrics(standings, home.get("id")),
            "standings_away": standings_metrics(standings, away.get("id")), "standings_error": e3}, None

# -------------------------
# Prediction validation
# -------------------------

async def validate_predictions(limit=15):
    conn = sqlite3.connect(DB_PATH)
    pending = conn.execute("SELECT id, fixture_id, market, selection FROM predictions WHERE outcome IS NULL AND fixture_id NOT LIKE 'TSDB-%' ORDER BY id ASC LIMIT ?", (limit,)).fetchall(); conn.close()
    if not pending or not FOOTBALL_DATA_KEY: return 0
    checked = 0
    async with httpx.AsyncClient(timeout=8) as client:
        for pid, fixture_id, market, selection in pending:
            try:
                match, error = await find_fd_match(client, fixture_id)
                if error or not match or match.get("status") not in {"FINISHED", "AWARDED"}: continue
                hg, ag = score_pair(match); outcome = prediction_outcome(market, selection, hg, ag)
                if outcome is None: continue
                conn = sqlite3.connect(DB_PATH); conn.execute("UPDATE predictions SET outcome=?, settled_at=? WHERE id=? AND outcome IS NULL", (outcome, now_iso(), pid)); conn.commit(); conn.close(); checked += 1
            except Exception as exc: print(f"Validation prediction {pid}: {exc}", flush=True)
    return checked


def record_model_predictions(user_id, data):
    model = data.get("model"); match = data.get("match", {})
    if not model or not match: return 0
    fixture_id = match.get("id"); home = match.get("homeTeam", {}).get("name", "Domicile"); away = match.get("awayTeam", {}).get("name", "Extérieur")
    h, d, a = model["final"]; m = model["markets"]
    candidates = [("1X2", "1", h), ("1X2", "X", d), ("1X2", "2", a),
                  ("BTTS", "Oui" if m["btts"] >= 50 else "Non", max(m["btts"],100-m["btts"])),
                  ("O2.5", "Oui" if m["over25"] >= 50 else "Non", max(m["over25"],100-m["over25"])),
                  ("O1.5", "Oui" if m["over15"] >= 50 else "Non", max(m["over15"],100-m["over15"])),
                  ("1X", "Oui" if h+d >= 50 else "Non", max(h+d,100-h-d)),
                  ("X2", "Oui" if d+a >= 50 else "Non", max(d+a,100-d-a)),
                  ("12", "Oui" if h+a >= 50 else "Non", max(h+a,100-h-a))]
    conn = sqlite3.connect(DB_PATH); existing = set(conn.execute("SELECT market, selection FROM predictions WHERE user_id=? AND fixture_id=?", (user_id, str(fixture_id))).fetchall()); conn.close()
    count = 0
    for market, selection, probability in candidates:
        if (market, selection) not in existing:
            db_add_prediction(user_id, fixture_id, home, away, market, selection, float(probability)); count += 1
    return count

# -------------------------
# Keyboards
# -------------------------

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚽  MATCHS DU JOUR", callback_data="menu:match"), InlineKeyboardButton("🎯  ANALYSE", callback_data="menu:tools")],
        [InlineKeyboardButton("📊  PERFORMANCE", callback_data="menu:performance"), InlineKeyboardButton("💰  BANKROLL", callback_data="menu:bankroll")],
        [InlineKeyboardButton("🔬  SIMULATEUR", callback_data="menu:sim"), InlineKeyboardButton("🔄  VALIDATION", callback_data="menu:validation")],
        [InlineKeyboardButton("🔌  SOURCES", callback_data="menu:status"), InlineKeyboardButton("❓  AIDE", callback_data="menu:help")]])


def match_keyboard(fid):
    fid = str(fid)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 ANALYSE", callback_data=f"analyse:{fid}"), InlineKeyboardButton("🎯 PROBABILITÉS", callback_data=f"prob:{fid}")],
        [InlineKeyboardButton("⚽ BUTS", callback_data=f"buts:{fid}"), InlineKeyboardButton("💰 COTES", callback_data=f"cotes:{fid}")],
        [InlineKeyboardButton("⚽ BUTEURS", callback_data=f"buteur:{fid}"), InlineKeyboardButton("🔬 SIMULER", callback_data=f"sim:{fid}")],
        [InlineKeyboardButton("⬅️ MATCHS", callback_data="menu:match"), InlineKeyboardButton("🏠 ACCUEIL", callback_data="menu:home")]])


def match_list_keyboard(matches):
    rows = []
    for item in matches[:20]:
        fid = item.get("id"); home, away = match_names(item)
        rows.append([InlineKeyboardButton(f"🔎 {home[:18]} - {away[:18]}", callback_data=f"match:{fid}")])
    rows.append([InlineKeyboardButton("🔄 ACTUALISER", callback_data="menu:match"), InlineKeyboardButton("🏠 ACCUEIL", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


def simulator_keyboard(fid):
    fid = str(fid)
    return InlineKeyboardMarkup([[InlineKeyboardButton("🎯 1X2", callback_data=f"market:{fid}:1X2"), InlineKeyboardButton("🔁 Double chance", callback_data=f"market:{fid}:DC")],
                                 [InlineKeyboardButton("⚽ BTTS", callback_data=f"market:{fid}:BTTS"), InlineKeyboardButton("📊 Over/Under", callback_data=f"market:{fid}:OU")],
                                 [InlineKeyboardButton("🔎 Analyse", callback_data=f"analyse:{fid}"), InlineKeyboardButton("⬅️ Match", callback_data=f"match:{fid}")]])


def market_keyboard(fid, market):
    choices = {"1X2": [("1","1"),("X","X"),("2","2")], "DC": [("1X","1X"),("X2","X2"),("12","12")], "BTTS": [("Oui","BTTSY"),("Non","BTTSN")], "OU": [("Over 1.5","O15"),("Over 2.5","O25"),("Over 3.5","O35"),("Under 2.5","U25"),("Under 3.5","U35")]}[market]
    rows=[]; row=[]
    for label, code in choices:
        row.append(InlineKeyboardButton(label, callback_data=f"pick:{fid}:{market}:{code}"))
        if len(row)==2: rows.append(row); row=[]
    if row: rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Simulateur", callback_data=f"sim:{fid}"), InlineKeyboardButton("🏠 Menu", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


def stake_keyboard(fid, market, selection):
    return InlineKeyboardMarkup([[InlineKeyboardButton("5 €", callback_data=f"stake:{fid}:{market}:{selection}:5"), InlineKeyboardButton("10 €", callback_data=f"stake:{fid}:{market}:{selection}:10")],
                                 [InlineKeyboardButton("20 €", callback_data=f"stake:{fid}:{market}:{selection}:20"), InlineKeyboardButton("50 €", callback_data=f"stake:{fid}:{market}:{selection}:50")],
                                 [InlineKeyboardButton("⬅️ Marchés", callback_data=f"sim:{fid}"), InlineKeyboardButton("🏠 Menu", callback_data="menu:home")]])


def tools_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("📈 Performance", callback_data="menu:performance"), InlineKeyboardButton("🔄 Validation", callback_data="menu:validation")],
                                 [InlineKeyboardButton("💶 Bankroll", callback_data="menu:bankroll"), InlineKeyboardButton("🔌 Sources", callback_data="menu:status")],
                                 [InlineKeyboardButton("🏠 Menu principal", callback_data="menu:home")]])

# -------------------------
# UI handlers
# -------------------------

async def send_match_results(message):
    async with httpx.AsyncClient(timeout=8) as client:
        matches=[]; source="Football-Data.org"; error=None
        if FOOTBALL_DATA_KEY: matches,error=await fd_today_matches(client)
        if not matches:
            events, ts_error=await tsdb_today_events(client)
            if events: matches=[tsdb_to_match(e) for e in events]; source="TheSportsDB"
            elif error:
                await message.reply_text("❌ Aucune source disponible.\n\nVérifie FOOTBALL_DATA_KEY dans Render.", reply_markup=main_menu()); return
    matches.sort(key=lambda x: fd_dt(x) or datetime.max.replace(tzinfo=PARIS)); matches=matches[:20]
    if not matches:
        await message.reply_text("⚽ Aucun match disponible aujourd'hui.", reply_markup=main_menu()); return
    await message.reply_text(f"⚽ <b>MATCHS DU JOUR</b>\n📅 {today_paris()}\n🔌 Source : {source}\n📊 {len(matches)} matchs\n\n👇 Clique sur un match pour voir ses options.", parse_mode="HTML", reply_markup=match_list_keyboard(matches))


async def send_match_actions(message, fid):
    async with httpx.AsyncClient(timeout=8) as client:
        if str(fid).startswith("TSDB-"):
            events,error=await tsdb_today_events(client); item=next((tsdb_to_match(e) for e in (events or []) if f"TSDB-{e.get('idEvent')}"==str(fid)),None)
        else: item,error=await find_fd_match(client,fid)
    if error or not item:
        await message.reply_text(f"❌ Match introuvable : {fid}", reply_markup=main_menu()); return
    home,away=match_names(item); comp=item.get("competition",{}).get("name","Football"); dt=fd_dt(item); date_text=dt.strftime("%d/%m/%Y %H:%M") if dt else "N/D"
    await message.reply_text(f"⚽ <b>MATCH SÉLECTIONNÉ</b>\n━━━━━━━━━━━━━━━━━━━━\n<b>{home}</b>\n<i>vs</i>\n<b>{away}</b>\n\n🏆 {comp}\n📅 {date_text}\n🆔 <code>{fid}</code>\n\n👇 <b>CHOISIS TON MODULE</b>", parse_mode="HTML", reply_markup=match_keyboard(fid))


async def run_prob_for_message(message, fid):
    data,error=await full_analysis(fid)
    if error: await message.reply_text(f"❌ {error}",reply_markup=main_menu()); return
    model=data.get("model")
    if not model: await message.reply_text("❌ Données insuffisantes.",reply_markup=main_menu()); return
    h,d,a=model["final"]; home=data["match"]["homeTeam"]["name"]; away=data["match"]["awayTeam"]["name"]
    await message.reply_text(f"🎯 <b>PROBABILITÉS</b>\n\n1 {home} : {h:.1f}%\nX Nul : {d:.1f}%\n2 {away} : {a:.1f}%\n\n1X : {h+d:.1f}%\nX2 : {d+a:.1f}%\n12 : {h+a:.1f}%",parse_mode="HTML",reply_markup=match_keyboard(fid))


async def run_buts_for_message(message,fid):
    data,error=await full_analysis(fid)
    if error: await message.reply_text(f"❌ {error}",reply_markup=main_menu()); return
    model=data.get("model")
    if not model: await message.reply_text("❌ Données insuffisantes.",reply_markup=main_menu()); return
    m=model["markets"]
    await message.reply_text(f"⚽ <b>MARCHÉS DE BUTS</b>\n\nBTTS Oui : {m['btts']:.1f}%\nBTTS Non : {100-m['btts']:.1f}%\nOver 1.5 : {m['over15']:.1f}%\nOver 2.5 : {m['over25']:.1f}%\nOver 3.5 : {m['over35']:.1f}%\nUnder 2.5 : {m['under25']:.1f}%\nUnder 3.5 : {m['under35']:.1f}%\nxG : {model['home_xg']:.2f} - {model['away_xg']:.2f}",parse_mode="HTML",reply_markup=match_keyboard(fid))


async def run_cotes_for_message(message,fid):
    await message.reply_text("💰 <b>COTES</b>\n\nLa source gratuite actuelle ne fournit pas toujours les cotes bookmaker.\nAucune cote n'est inventée.\n\nPour enregistrer une cote manuellement :\n/mise ID marché sélection montant cote",parse_mode="HTML",reply_markup=match_keyboard(fid))


async def run_buteur_for_message(message,fid):
    if str(fid).startswith("TSDB-"):
        eid=str(fid).split("-",1)[1]
        async with httpx.AsyncClient(timeout=8) as client: data,error=await tsdb_get(client,"lookuptimeline.php",{"id":eid},f"tsdb:timeline:{eid}",120)
        if error: await message.reply_text(f"❌ {error}",reply_markup=main_menu()); return
        goals=[x for x in (data.get("timeline",[]) or []) if "goal" in str(x.get("strTimeline","")).lower()]
        if not goals: await message.reply_text("⚽ Aucun événement de but disponible.",reply_markup=match_keyboard(fid)); return
        msg="⚽ <b>BUTS / ÉVÉNEMENTS</b>\n\n"+"\n".join(f"• {g.get('strTimeline','But')} | {g.get('strPlayer','Joueur N/D')}" for g in goals[:15])
        await message.reply_text(msg,parse_mode="HTML",reply_markup=match_keyboard(fid)); return
    async with httpx.AsyncClient(timeout=8) as client: data,error=await find_fd_match(client,fid)
    if error: await message.reply_text(f"❌ {error}",reply_markup=main_menu()); return
    goals=data.get("goals",[]) or []
    if not goals: await message.reply_text("⚽ Aucun détail de buteur disponible pour ce match.",reply_markup=match_keyboard(fid)); return
    msg="⚽ <b>BUTEURS</b>\n\n"
    for g in goals[:20]:
        scorer=g.get("scorer",{}) or {}; assist=g.get("assist",{}) or {}; msg+=f"• {g.get('minute','?')}' {scorer.get('name','N/D')}"+(f" | passe : {assist['name']}" if assist.get('name') else "")+"\n"
    await message.reply_text(msg,parse_mode="HTML",reply_markup=match_keyboard(fid))

# -------------------------
# Dashboard renderer
# -------------------------

FONT_REG="/usr/share/fonts/truetype/lato/Lato-Regular.ttf"; FONT_MED="/usr/share/fonts/truetype/lato/Lato-Medium.ttf"; FONT_BOLD="/usr/share/fonts/truetype/lato/Lato-Bold.ttf"; FONT_ITALIC="/usr/share/fonts/truetype/lato/Lato-Italic.ttf"


def font(path,size):
    try: return ImageFont.truetype(path,size)
    except Exception: return ImageFont.load_default()


def rounded(d,box,fill,outline=None,radius=22,width=2): d.rounded_rectangle(box,radius=radius,fill=fill,outline=outline,width=width)


def background(w,h):
    img=Image.new("RGB",(w,h),(5,14,30)); px=img.load()
    for y in range(h):
        t=y/max(1,h-1); r=int(5+5*t); g=int(12+9*t); b=int(28+18*t)
        for x in range(w):
            glow=int(8*max(0,1-abs(x-w*.52)/(w*.62))); px[x,y]=(r,g+glow//3,min(55,b+glow))
    return img


def pattern(d,w,h):
    for y in range(35,h,115):
        for x in range(35,w,130):
            d.ellipse((x-16,y-16,x+16,y+16),outline=(18,54,83),width=2); d.line((x-9,y,x+9,y),fill=(14,48,76),width=2); d.line((x,y-9,x,y+9),fill=(14,48,76),width=2)


def bar(d,x,y,w,h,value,color):
    rounded(d,(x,y,x+w,y+h),(27,48,76),radius=h//2); fw=int(w*clamp(value)/100) if value>0 else 0
    if fw: rounded(d,(x,y,x+fw,y+h),color,radius=h//2)


def bar_color(v): return (27,235,82) if v>=60 else (255,203,22) if v>=30 else (255,67,67)


def render_dashboard(data):
    W,H=1024,1536; img=background(W,H); d=ImageDraw.Draw(img); pattern(d,W,H); white=(244,247,255); cyan=(0,218,255); panel=(7,25,47)
    match=data["match"]; home=match.get("homeTeam",{}).get("name","Domicile"); away=match.get("awayTeam",{}).get("name","Extérieur"); comp=match.get("competition",{}).get("name","Football"); dt=fd_dt(match); status=fd_status(match.get("status")); model=data.get("model")
    if not model: model={"final":(33.3,33.4,33.3),"markets":{"btts":50,"over15":50,"over25":50,"over35":50,"under25":50,"under35":50},"home_xg":1,"away_xg":1,"matrix":poisson_matrix(1,1)}
    sh=data.get("standings_home") or {}; sa=data.get("standings_away") or {}; hf=data.get("home_form") or []; af=data.get("away_form") or []
    rounded(d,(42,32,982,132),(5,23,45),outline=(35,190,235),radius=48,width=2); d.ellipse((63,47,123,107),outline=cyan,width=4,fill=(8,40,65)); d.text((77,59),"⚽",font=font(FONT_BOLD,31),fill=white); d.text((142,49),"Sport Analyzer",font=font(FONT_BOLD,27),fill=white); d.text((143,82),"bot",font=font(FONT_REG,18),fill=(180,201,224)); d.text((690,55),"▂▅▇▇",font=font(FONT_BOLD,25),fill=cyan); d.multiline_text((755,53),"Des données\ndes analyses\nd'opportunités",font=font(FONT_REG,14),fill=(218,229,245),spacing=1)
    rounded(d,(42,153,982,405),panel,outline=cyan,radius=26,width=2); d.text((63,174),"🏆",font=font(FONT_BOLD,25),fill=(255,203,22)); d.text((108,176),comp.upper()[:30],font=font(FONT_BOLD,20),fill=white); d.text((108,208),f"Journée  •  {dt.strftime('%d/%m/%Y  •  %H:%M') if dt else 'Horaire N/D'}",font=font(FONT_REG,17),fill=(195,211,231)); d.text((641,180),f"ID: {match.get('id')}",font=font(FONT_BOLD,17),fill=white); rounded(d,(797,173,957,213),(8,50,52),outline=(0,239,151),radius=20,width=2); d.text((817,183),f"● {status}",font=font(FONT_BOLD,16),fill=(0,239,151))
    d.text((163,250),home,font=font(FONT_BOLD,23),fill=white); d.text((721,250),away,font=font(FONT_BOLD,21),fill=white); d.text((163,285),f"{sh.get('position','N/D')}e • {sh.get('points','N/D')} pts • {sh.get('gf','N/D')}-{sh.get('ga','N/D')}",font=font(FONT_REG,16),fill=(199,215,234)); d.text((721,285),f"{sa.get('position','N/D')}e • {sa.get('points','N/D')} pts • {sa.get('gf','N/D')}-{sa.get('ga','N/D')}",font=font(FONT_REG,16),fill=(199,215,234)); d.text((481,251),"VS",font=font(FONT_BOLD,28),fill=white); d.text((163,329)," ".join(x.get("result","?") for x in hf[-5:]) or "N/D",font=font(FONT_BOLD,16),fill=white); d.text((721,329)," ".join(x.get("result","?") for x in af[-5:]) or "N/D",font=font(FONT_BOLD,16),fill=white)
    for cx,cy,label in [(103,282,home[:1]),(673,280,away[:1])]: d.ellipse((cx-36,cy-36,cx+36,cy+36),fill=(20,75,116),outline=cyan,width=2); d.text((cx-10,cy-21),label.upper() or "?",font=font(FONT_BOLD,30),fill=white)
    venue=(match.get("venue") or {}).get("name") or "Stade"; d.text((470,350),"🏟",font=font(FONT_BOLD,22),fill=(155,184,215)); d.text((505,350),venue[:28],font=font(FONT_REG,14),fill=(206,220,239))
    tabs=[(42,"Vue d'ensemble"),(286,"📊  Statistiques"),(523,"⚽  Face à face"),(761,"⚽  Compos")]
    for i,(x,lab) in enumerate(tabs): rounded(d,(x,418,x+224,475),(35,69,126) if i==0 else (11,34,60),outline=(70,181,255) if i==0 else (49,86,124),radius=18,width=2); d.text((x+24,434),lab,font=font(FONT_BOLD if i==0 else FONT_MED,16),fill=white)
    x1,y1,x2,y2=(42,488,510,885); rounded(d,(x1,y1,x2,y2),(9,26,49),outline=(47,112,168),radius=24,width=2); d.text((x1+24,y1+20),"🎯  PROBABILITÉS MODÈLE",font=font(FONT_BOLD,25),fill=white); h,dr,a=model["final"]; yy=y1+72
    for i,(lab,v) in enumerate([("1",h),("X",dr),("2",a),("1X",h+dr),("X2",dr+a),("12",h+a)]):
        if i==3: d.line((x1+24,yy-15,x2-24,yy-15),fill=(71,107,139),width=2); yy+=14
        rounded(d,(x1+24,yy,x1+62,yy+35),(34,105,169),radius=7); d.text((x1+34,yy+3),lab,font=font(FONT_BOLD,21),fill=white); c=(0,218,255) if lab=="12" else bar_color(v); bar(d,x1+82,yy+5,260,26,v,c); d.text((x1+365,yy-1),f"{v:.1f}%",font=font(FONT_BOLD,22),fill=c); yy+=48
    x1,y1,x2,y2=(532,488,982,885); rounded(d,(x1,y1,x2,y2),(9,26,49),outline=(47,112,168),radius=24,width=2); d.text((x1+24,y1+20),"⚽  MARCHÉS DE BUTS",font=font(FONT_BOLD,25),fill=white); m=model["markets"]; yy=y1+72
    for lab,v in [("BTTS Oui",m["btts"]),("BTTS Non",100-m["btts"]),("Over 1.5",m["over15"]),("Over 2.5",m["over25"]),("Over 3.5",m["over35"]),("Under 2.5",m["under25"]),("Under 3.5",m["under35"])]: c=bar_color(v); d.text((x1+24,yy-2),lab,font=font(FONT_MED,19),fill=white); bar(d,x1+140,yy+1,215,25,v,c); d.text((x1+370,yy-2),f"{v:.1f}%",font=font(FONT_BOLD,20),fill=c); yy+=43
    d.text((x1+24,y2-49),f"xG estimé : {model['home_xg']:.2f}  •  {model['away_xg']:.2f}",font=font(FONT_BOLD,19),fill=(238,245,255))
    # scores/status cards
    rounded(d,(42,902,510,1164),(9,26,49),outline=(47,112,168),radius=24,width=2); d.text((66,922),"🔢  SCORES LES PLUS PROBABLES",font=font(FONT_BOLD,22),fill=white); yy=980
    for h,a,p in likely_scores(model["matrix"],3): d.text((82,yy),f"⚽ {h}-{a}",font=font(FONT_BOLD,21),fill=white); bar(d,162,yy+3,190,25,p,(0,193,239)); d.text((370,yy-1),f"{p:.1f}%",font=font(FONT_BOLD,20),fill=white); yy+=57
    rounded(d,(532,902,982,1164),(9,26,49),outline=(47,112,168),radius=24,width=2); d.text((556,922),"🧠  INTELLIGENCE MATCH",font=font(FONT_BOLD,22),fill=white); spread=max(model["final"])-min(model["final"]); conf="Élevée" if max(model["final"])>=60 else "Moyenne" if max(model["final"])>=40 else "Faible"; trend="Équilibré" if spread<20 else "Tendance marquée"; rel="Bonne" if data.get("quality",0)>=.65 else "Moyenne" if data.get("quality",0)>=.4 else "Faible"; yy=980
    for lab,val,c in [("Confiance",conf,(255,205,25)),("Tendance",trend,(115,154,210)),("Fiabilité",rel,(28,221,92))]: d.text((556,yy),lab,font=font(FONT_MED,19),fill=white); rounded(d,(805,yy-3,952,yy+32),c,radius=16); d.text((820,yy+2),val,font=font(FONT_BOLD,16),fill=(10,18,30)); yy+=55
    d.text((556,1140),"Estimations statistiques à partir",font=font(FONT_REG,14),fill=(205,220,239)); d.text((556,1159),"des données disponibles.",font=font(FONT_REG,14),fill=(205,220,239))
    buttons=[(42,1182,347,1244,"🔎  Analyse"),(359,1182,664,1244,"🎯  Probabilités"),(677,1182,982,1244,"⚽  Buts"),(42,1255,347,1317,"💰  Cotes"),(359,1255,664,1317,"⚽  Buteurs"),(677,1255,982,1317,"🔬  Simuler"),(42,1330,982,1388,"⬅️  Menu principal")]
    for x1,y1,x2,y2,lab in buttons: rounded(d,(x1,y1,x2,y2),(23,66,126),outline=cyan,radius=16,width=2); f=font(FONT_BOLD,18); d.text(((x1+x2-f.getlength(lab))/2,y1+18),lab,font=f,fill=white)
    d.text((52,1430),"Analyse aujourd'hui, de meilleures décisions demain.",font=font(FONT_ITALIC,14),fill=(181,199,222)); d.text((824,1430),"Sport Analyzer V13.1",font=font(FONT_ITALIC,13),fill=(181,199,222))
    return img


async def run_analysis_dashboard_for_message(message, fid, user_id):
    data,error=await full_analysis(fid)
    if error: await message.reply_text(f"❌ {error}",reply_markup=main_menu()); return
    record_model_predictions(user_id,data)
    match=data.get("match",{}); img=render_dashboard(data)
    # Logo download is optional and intentionally omitted from the critical path for speed.
    output=io.BytesIO(); img.save(output,format="PNG",optimize=True); output.seek(0)
    await message.reply_photo(photo=output.getvalue(),caption="⚡ <b>SPORT ANALYZER • V13.1</b>\nDashboard mobile • données dynamiques",parse_mode="HTML",reply_markup=match_keyboard(fid))


async def send_simulator_menu(message,fid): await message.reply_text("🔬 <b>SIMULATEUR</b>\n\nChoisis le marché à étudier.\nLes rendements sont théoriques.",parse_mode="HTML",reply_markup=simulator_keyboard(fid))


async def send_market_menu(message,fid,market):
    data,error=await full_analysis(fid)
    if error: await message.reply_text(f"❌ {error}",reply_markup=main_menu()); return
    model=data.get("model")
    if not model: await message.reply_text("❌ Données insuffisantes.",reply_markup=main_menu()); return
    h,d,a=model["final"]; m=model["markets"]
    body={"1X2":f"🎯 <b>1X2</b>\n\n1 : {h:.1f}%\nX : {d:.1f}%\n2 : {a:.1f}%", "DC":f"🔁 <b>DOUBLE CHANCE</b>\n\n1X : {h+d:.1f}%\nX2 : {d+a:.1f}%\n12 : {h+a:.1f}%", "BTTS":f"⚽ <b>BTTS</b>\n\nOui : {m['btts']:.1f}%\nNon : {100-m['btts']:.1f}%", "OU":f"📊 <b>OVER / UNDER</b>\n\nOver 1.5 : {m['over15']:.1f}%\nOver 2.5 : {m['over25']:.1f}%\nOver 3.5 : {m['over35']:.1f}%\nUnder 2.5 : {m['under25']:.1f}%\nUnder 3.5 : {m['under35']:.1f}%"}[market]
    await message.reply_text(body+"\n\nChoisis une sélection :",parse_mode="HTML",reply_markup=market_keyboard(fid,market))


async def send_pick_stake_menu(message,fid,market,selection):
    data,error=await full_analysis(fid)
    if error: await message.reply_text(f"❌ {error}",reply_markup=main_menu()); return
    model=data.get("model")
    if not model: await message.reply_text("❌ Données insuffisantes.",reply_markup=main_menu()); return
    h,d,a=model["final"]; m=model["markets"]
    vals={"1":("1",h),"X":("X",d),"2":("2",a),"1X":("1X",h+d),"X2":("X2",d+a),"12":("12",h+a),"BTTSY":("BTTS Oui",m["btts"]),"BTTSN":("BTTS Non",100-m["btts"]),"O15":("Over 1.5",m["over15"]),"O25":("Over 2.5",m["over25"]),"O35":("Over 3.5",m["over35"]),"U25":("Under 2.5",m["under25"]),"U35":("Under 3.5",m["under35"])}
    label,p=vals.get(selection,("Sélection",0));
    if p<=0: await message.reply_text("❌ Probabilité indisponible.",reply_markup=main_menu()); return
    await message.reply_text(f"💶 <b>SIMULATION</b>\n\n🎯 Sélection : {label}\n📊 Probabilité modèle : {p:.1f}%\n📐 Cote juste théorique : {100/p:.2f}\n\nChoisis une mise à simuler :",parse_mode="HTML",reply_markup=stake_keyboard(fid,market,selection))


async def send_stake_result(message,fid,market,selection,stake):
    data,error=await full_analysis(fid)
    if error: await message.reply_text(f"❌ {error}",reply_markup=main_menu()); return
    model=data.get("model")
    if not model: await message.reply_text("❌ Données insuffisantes.",reply_markup=main_menu()); return
    h,d,a=model["final"]; m=model["markets"]; probs={"1":h,"X":d,"2":a,"1X":h+d,"X2":d+a,"12":h+a,"BTTSY":m["btts"],"BTTSN":100-m["btts"],"O15":m["over15"],"O25":m["over25"],"O35":m["over35"],"U25":m["under25"],"U35":m["under35"]}; p=float(probs.get(selection,0))
    if p<=0: await message.reply_text("❌ Probabilité indisponible.",reply_markup=main_menu()); return
    fair=100/p; ret=float(stake)*fair; profit=ret-float(stake)
    await message.reply_text(f"🧮 <b>SIMULATION</b>\n\n🎯 {selection}\n📊 Probabilité : {p:.1f}%\n💶 Mise : {float(stake):.2f} €\n📐 Cote juste théorique : {fair:.2f}\n💰 Retour théorique : {ret:.2f} €\n📈 Profit théorique : {profit:+.2f} €",parse_mode="HTML",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔬 Autre marché",callback_data=f"sim:{fid}"),InlineKeyboardButton("🔎 Analyse",callback_data=f"analyse:{fid}")],[InlineKeyboardButton("🏠 Menu principal",callback_data="menu:home")]]))


async def send_performance_result(message,user_id):
    await validate_predictions(); perf=db_performance(user_id)
    if not perf["total"]: await message.reply_text("📊 <b>PERFORMANCE DU MODÈLE</b>\n\nAucune prédiction terminée n'est encore disponible.",parse_mode="HTML",reply_markup=main_menu()); return
    msg=f"📊 <b>PERFORMANCE DU MODÈLE</b>\n\nPrédictions validées : {perf['total']}\nCorrectes : {perf['wins']}\nTaux de réussite : {perf['accuracy']:.1f}%\n\nPAR MARCHÉ\n"+"".join(f"• {k} : {v['wins']/v['total']*100:.1f}% ({v['wins']}/{v['total']})\n" for k,v in sorted(perf['markets'].items()))
    await message.reply_text(msg,parse_mode="HTML",reply_markup=main_menu())


async def send_validation_result(message,user_id):
    checked=await validate_predictions(); perf=db_performance(user_id); text=f"🔄 <b>VALIDATION</b>\n\nPrédictions nouvellement validées : {checked}\n"
    text += f"Prédictions terminées : {perf['total']}\nTaux de réussite : {perf['accuracy']:.1f}%" if perf["accuracy"] is not None else "Aucune prédiction terminée dans ton historique."
    await message.reply_text(text,parse_mode="HTML",reply_markup=main_menu())


async def send_bankroll_result(message,user_id):
    total,profit,count,wins,roi=db_summary(user_id); await message.reply_text(f"💶 <b>BANKROLL</b>\n\nMises enregistrées : {total:.2f} €\nProfit/perte : {profit:+.2f} €\nParis : {count}\nParis gagnants : {wins}\nROI : {roi:+.2f}%",parse_mode="HTML",reply_markup=main_menu())


async def send_status_result(message):
    async with httpx.AsyncClient(timeout=7) as client:
        fd_ok=False; detail="Clé absente"
        if FOOTBALL_DATA_KEY:
            data,error=await fd_get(client,"/matches",{"date":today_paris()},"fd:status",30); fd_ok=data is not None and error is None; detail="OK" if fd_ok else str(error)
        ts_data,ts_error=await tsdb_get(client,"eventsday.php",{"d":today_paris(),"s":"Soccer"},"tsdb:status",30); ts_ok=ts_data is not None and ts_error is None
    await message.reply_text(f"🔌 <b>ÉTAT DES SOURCES</b>\n\nFootball-Data.org : {'🟢 OK' if fd_ok else '🔴 Indisponible'}\nDétail : {detail[:250]}\n\nTheSportsDB : {'🟢 OK' if ts_ok else '🔴 Indisponible'}\nAPI-Football : ⚪ désactivée dans V13.1",parse_mode="HTML",reply_markup=main_menu())


# -------------------------
# Callback router
# -------------------------

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query=update.callback_query
    if not query: return
    data=query.data or ""; print(f"🔘 CALLBACK : {data}",flush=True)
    try: await query.answer()
    except Exception: pass
    message=query.message
    if not message: return
    uid=update.effective_user.id if update.effective_user else 0
    try:
        if data=="menu:home": await message.reply_text("🤖 <b>SPORT ANALYZER V13.1</b>\n\nChoisis une fonction :",parse_mode="HTML",reply_markup=main_menu()); return
        if data=="menu:match": await send_match_results(message); return
        if data=="menu:status": await send_status_result(message); return
        if data=="menu:performance": await send_performance_result(message,uid); return
        if data=="menu:validation": await send_validation_result(message,uid); return
        if data=="menu:bankroll": await send_bankroll_result(message,uid); return
        if data=="menu:sim": await message.reply_text("🔬 <b>SIMULATEUR</b>\n\nOuvre un match puis clique sur 🔬 Simuler.",parse_mode="HTML",reply_markup=main_menu()); return
        if data=="menu:tools": await message.reply_text("⚙️ <b>OUTILS</b>\n\nChoisis une fonction :",parse_mode="HTML",reply_markup=tools_keyboard()); return
        if data=="menu:help": await message.reply_text("📚 <b>AIDE RAPIDE</b>\n\n⚽ Matchs\n🔎 Analyse\n🎯 Probabilités\n⚽ Buts\n💰 Cotes\n⚽ Buteurs\n🔬 Simulateur\n📊 Performance\n🔄 Validation\n💶 Bankroll",parse_mode="HTML",reply_markup=main_menu()); return
        if ":" not in data: return
        action,value=data.split(":",1)
        if action=="match": await send_match_actions(message,value); return
        if action=="analyse": await run_analysis_dashboard_for_message(message,value,uid); return
        if action=="prob": await run_prob_for_message(message,value); return
        if action=="buts": await run_buts_for_message(message,value); return
        if action=="cotes": await run_cotes_for_message(message,value); return
        if action=="buteur": await run_buteur_for_message(message,value); return
        if action=="sim": await send_simulator_menu(message,value); return
        if action=="market":
            fid,market=value.split(":",1); await send_market_menu(message,fid,market); return
        if action=="pick":
            fid,market,selection=value.split(":",2); await send_pick_stake_menu(message,fid,market,selection); return
        if action=="stake":
            fid,market,selection,stake=value.split(":",3); await send_stake_result(message,fid,market,selection,float(stake)); return
        await message.reply_text(f"⚠️ Action inconnue : {action}",reply_markup=main_menu())
    except Exception as exc:
        print(f"❌ CALLBACK ERROR [{data}] : {exc}",flush=True)
        try: await message.reply_text("❌ <b>Une erreur est survenue.</b>\n\nUtilise 🏠 Accueil pour continuer.",parse_mode="HTML",reply_markup=main_menu())
        except Exception as reply_error: print(f"❌ Reply error: {reply_error}",flush=True)

# -------------------------
# Commands
# -------------------------

async def start(update,context): await update.message.reply_text("⚡ <b>SPORT ANALYZER • V13.1</b>\n╭────────────────────────╮\n│ ⚽ <b>FOOTBALL INTELLIGENCE</b>\n│ 🎯 Probabilités  •  ⚽ Buts\n│ 💰 Cotes  •  🔬 Simulation\n╰────────────────────────╯\n\n👇 <b>CHOISIS TON MODULE</b>",parse_mode="HTML",reply_markup=main_menu())
async def help_command(update,context): await update.message.reply_text("📊 <b>SPORT ANALYZER V13.1</b>\n\n/match\n/analyse ID\n/buts ID\n/probabilite ID\n/buteur ID\n/cotes ID\n/mise ID marché sélection montant [cote]\n/resultat ID_BET win|loss|void\n/bankroll\n/statusapi\n/performance\n/validation",parse_mode="HTML")
async def match_command(update,context): await send_match_results(update.message)
async def analyse_command(update,context):
    if not context.args: await update.message.reply_text("Utilisation : /analyse ID"); return
    if str(context.args[0]).startswith("TSDB-"): await update.message.reply_text("ℹ️ Utilise un ID Football-Data.org pour le dashboard complet."); return
    await update.message.reply_text("🔎 Génération du dashboard…"); await run_analysis_dashboard_for_message(update.message,context.args[0],update.effective_user.id)
async def buts_command(update,context):
    if not context.args: await update.message.reply_text("Utilisation : /buts ID"); return
    await run_buts_for_message(update.message,context.args[0])
async def probabilite_command(update,context):
    if not context.args: await update.message.reply_text("Utilisation : /probabilite ID"); return
    await run_prob_for_message(update.message,context.args[0])
async def cotes_command(update,context): await run_cotes_for_message(update.message,context.args[0]) if context.args else await update.message.reply_text("💰 COTES\n\nAucune cote n'est inventée.")
async def buteur_command(update,context):
    if not context.args: await update.message.reply_text("Utilisation : /buteur ID"); return
    await run_buteur_for_message(update.message,context.args[0])
async def bankroll_command(update,context): await send_bankroll_result(update.message,update.effective_user.id)
async def statusapi_command(update,context): await send_status_result(update.message)
async def performance_command(update,context): await send_performance_result(update.message,update.effective_user.id)
async def validation_command(update,context): await send_validation_result(update.message,update.effective_user.id)

async def mise_command(update,context):
    if len(context.args)<4: await update.message.reply_text("Utilisation : /mise ID marché sélection montant [cote]"); return
    try:
        fid,market,selection=context.args[:3]; stake=float(context.args[3].replace(",",".")); odds=float(context.args[4].replace(",",".")) if len(context.args)>=5 else None
        if stake<=0 or (odds is not None and odds<=1): raise ValueError
    except ValueError: await update.message.reply_text("❌ Montant ou cote invalide."); return
    bid=db_add_bet(update.effective_user.id,fid,market,selection,stake,odds); await update.message.reply_text(f"💶 <b>PARI ENREGISTRÉ</b>\n\nID pari : {bid}\nMatch : {fid}\nMarché : {market}\nSélection : {selection}\nMise : {stake:.2f} €"+(f"\nCote : {odds:.2f}" if odds else ""),parse_mode="HTML")


async def resultat_command(update,context):
    if len(context.args)!=2: await update.message.reply_text("Utilisation : /resultat ID_BET win|loss|void"); return
    try: bid=int(context.args[0])
    except ValueError: await update.message.reply_text("❌ ID de pari invalide."); return
    result=context.args[1].lower()
    if result not in {"win","loss","void"}: await update.message.reply_text("❌ Résultat : win, loss ou void."); return
    profit=db_settle_bet(update.effective_user.id,bid,result); await update.message.reply_text("❌ Pari introuvable ou déjà réglé." if profit is None else f"📊 Pari {bid} réglé\nRésultat : {result}\nProfit/perte : {profit:+.2f} €")

# -------------------------
# Render webhook
# -------------------------

async def webhook(request: Request):
    try:
        payload=await request.json(); update=Update.de_json(payload,telegram_app.bot); await telegram_app.process_update(update); return JSONResponse({"ok":True})
    except Exception as exc:
        print(f"❌ Webhook error: {exc}",flush=True); return JSONResponse({"ok":False},status_code=500)

async def health(request: Request): return JSONResponse({"status":"ok","bot":"sport-analyzer-bot-v13.1"})

@asynccontextmanager
async def lifespan(app):
    global telegram_app
    if not TOKEN: raise RuntimeError("TELEGRAM_TOKEN est manquant.")
    if not RENDER_URL: raise RuntimeError("RENDER_EXTERNAL_URL est manquant.")
    init_db()
    telegram_app=Application.builder().token(TOKEN).concurrent_updates(True).build()
    for command,func in [("start",start),("help",help_command),("match",match_command),("analyse",analyse_command),("cotes",cotes_command),("buteur",buteur_command),("buts",buts_command),("probabilite",probabilite_command),("mise",mise_command),("resultat",resultat_command),("bankroll",bankroll_command),("statusapi",statusapi_command),("performance",performance_command),("validation",validation_command)]: telegram_app.add_handler(CommandHandler(command,func))
    telegram_app.add_handler(CallbackQueryHandler(callback_handler))
    await telegram_app.initialize(); await telegram_app.start(); await telegram_app.bot.set_webhook(f"{RENDER_URL}/telegram"); print("✅ SPORT ANALYZER V13.1 démarré",flush=True)
    try: yield
    finally: await telegram_app.stop(); await telegram_app.shutdown()

routes=[Route("/health",health,methods=["GET"]),Route("/telegram",webhook,methods=["POST"])]
app=Starlette(routes=routes,lifespan=lifespan)

if __name__=="__main__": uvicorn.run(app,host="0.0.0.0",port=PORT)
