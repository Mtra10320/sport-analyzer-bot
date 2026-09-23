import os
import time
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
import httpx
from analytics import num

PARIS = ZoneInfo("Europe/Paris")
FD_BASE = "https://api.football-data.org/v4"
TSDB_BASE = "https://www.thesportsdb.com/api/v1/json/123"

FOOTBALL_DATA_KEY = os.getenv("FOOTBALL_DATA_KEY")
CACHE = {}
CACHE_LOCK = asyncio.Lock()

FREE_COMPETITIONS = {
    "CL": "Ligue des champions", "PPL": "Primeira Liga", "PL": "Premier League",
    "DED": "Eredivisie", "BL1": "Bundesliga", "FL1": "Ligue 1",
    "SA": "Serie A", "PD": "La Liga", "ELC": "Championship",
    "BSA": "Serie A Brésil", "WC": "Coupe du monde", "EC": "Euro",
}


def today_paris():
    return datetime.now(PARIS).strftime("%Y-%m-%d")


def fd_dt(item):
    try:
        return datetime.fromisoformat(item.get("utcDate", "").replace("Z", "+00:00")).astimezone(PARIS)
    except Exception:
        return None


def match_names(item):
    return (item.get("homeTeam", {}).get("name", "Inconnu"),
            item.get("awayTeam", {}).get("name", "Inconnu"))


def fd_status(status):
    return {"SCHEDULED": "À venir", "TIMED": "Programmé", "IN_PLAY": "En direct",
            "PAUSED": "Mi-temps", "FINISHED": "Terminé", "POSTPONED": "Reporté",
            "SUSPENDED": "Suspendu", "CANCELLED": "Annulé", "AWARDED": "Attribué"}.get(
        status, status or "N/D")


def score_pair(item):
    score = item.get("score", {})
    full = score.get("fullTime", {}) if isinstance(score, dict) else {}
    return full.get("home"), full.get("away")


async def cached_get(client, url, headers=None, params=None, cache_key=None, ttl=60):
    if cache_key:
        async with CACHE_LOCK:
            cached = CACHE.get(cache_key)
            if cached and cached["expires"] > time.time():
                return cached["data"], None
    try:
        r = await client.get(url, headers=headers or {}, params=params or {})
    except Exception as exc:
        return None, f"Erreur réseau : {exc}"
    if r.status_code != 200:
        try:
            detail = r.json()
        except Exception:
            detail = r.text[:300]
        return None, f"HTTP {r.status_code} : {detail}"
    try:
        data = r.json()
    except Exception:
        return None, "Réponse JSON invalide."
    if cache_key:
        async with CACHE_LOCK:
            CACHE[cache_key] = {"data": data, "expires": time.time() + ttl}
    return data, None


async def fd_get(client, path, params=None, cache_key=None, ttl=60, api_key=None):
    key = api_key or os.getenv("FOOTBALL_DATA_KEY")
    if not key:
        return None, "Clé FOOTBALL_DATA_KEY absente."
    return await cached_get(client, f"{FD_BASE}{path}", {"X-Auth-Token": key}, params, cache_key, ttl)


async def tsdb_get(client, path, params=None, cache_key=None, ttl=60):
    return await cached_get(client, f"{TSDB_BASE}/{path}", params=params, cache_key=cache_key, ttl=ttl)


async def fd_today_matches(client):
    date = today_paris()
    data, error = await fd_get(client, "/matches", {"date": date}, f"fd:today:{date}", 120)
    if error:
        return [], error
    return [m for m in data.get("matches", []) if m.get("competition", {}).get("code") in FREE_COMPETITIONS], None


async def tsdb_today_events(client):
    date = today_paris()
    data, error = await tsdb_get(client, "eventsday.php", {"d": date, "s": "Soccer"}, f"tsdb:today:{date}", 120)
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
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    except Exception:
        dt = None
    return {
        "id": f"TSDB-{event.get('idEvent','0')}",
        "utcDate": dt.astimezone(ZoneInfo("UTC")).isoformat() if dt else "",
        "status": "FINISHED" if event.get("strStatus") in {"Match Finished", "FT"} else "SCHEDULED",
        "competition": {"name": event.get("strLeague", "Soccer"), "code": "TSDB"},
        "homeTeam": {"id": event.get("idHomeTeam"), "name": event.get("strHomeTeam", "Inconnu")},
        "awayTeam": {"id": event.get("idAwayTeam"), "name": event.get("strAwayTeam", "Inconnu")},
        "score": {"fullTime": {"home": num(event.get("intHomeScore")), "away": num(event.get("intAwayScore"))}},
        "venue": event.get("strVenue"),
        "_tsdb": event
    }


async def find_fd_match(client, fixture_id):
    if str(fixture_id).startswith("TSDB-"):
        return None, "Cet ID appartient à TheSportsDB et ne peut pas être analysé par Football-Data.org."
    try:
        fid = int(fixture_id)
    except ValueError:
        return None, "ID de match invalide."
    data, error = await fd_get(client, f"/matches/{fid}", {"head2head": 10}, f"fd:match:{fid}", 120)
    if error:
        return None, error
    return data, None if data and data.get("id") else "Match introuvable."


async def team_recent(client, team_id):
    if not team_id:
        return [], "ID équipe absent."
    data, error = await fd_get(client, f"/teams/{team_id}/matches", {"status": "FINISHED", "limit": 5}, f"fd:team:{team_id}:recent", 300)
    if error:
        return [], error
    return data.get("matches", []), None


async def competition_standings(client, code):
    if not code or code == "TSDB":
        return [], None
    data, error = await fd_get(client, f"/competitions/{code}/standings", {}, f"fd:standings:{code}", 600)
    if error:
        return [], error
    return data.get("standings", []), None
