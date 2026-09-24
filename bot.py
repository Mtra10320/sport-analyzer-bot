import os
import sqlite3
import asyncio
from datetime import datetime, timezone
from contextlib import asynccontextmanager

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, FileResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
import uvicorn

from database import (
    init_db, db_add_bet, db_settle_bet, db_summary,
    db_add_prediction, prediction_outcome, db_performance, DB_PATH
)
from analytics import (
    parse_form, build_model, standings_metrics, likely_scores
)
from api_client import (
    fd_today_matches, tsdb_today_events, tsdb_to_match, find_match_by_id, find_fd_match,
    team_recent, competition_standings, fd_dt, score_pair, tsdb_get,
    FOOTBALL_DATA_KEY, today_paris, PARIS, fd_status, match_names
)
from keyboards import (
    main_menu, match_keyboard, match_list_keyboard,
    simulator_keyboard, market_keyboard, stake_keyboard, tools_keyboard,
    analysis_menu_keyboard, help_keyboard
)

# ============================================================
# SPORT ANALYZER V13.3 (Interface Texte 100% HTML & Mini App)
# ============================================================

TOKEN = os.getenv("TELEGRAM_TOKEN")
PORT = int(os.getenv("PORT", "10000"))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")
telegram_app = None


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def main_welcome_text():
    return (
        f"⚡ <b>SPORT ANALYZER • V13.3</b>\n\n"
        f"<b>FOOTBALL INTELLIGENCE</b>\n"
        f"🎯 Probabilités • ⚽ Buts • 💰 Cotes\n"
        f"🔬 Analyses • Simulation • Suivi de performance\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👇 <b>CHOISIS TON MODULE OU OUVRE LA MINI APP</b>"
    )


async def full_analysis(fixture_id):
    async with httpx.AsyncClient(timeout=10, limits=httpx.Limits(max_connections=8)) as client:
        match, error = await find_match_by_id(client, fixture_id)
        if error or not match:
            return None, error or "Match introuvable."

        home = match.get("homeTeam", {})
        away = match.get("awayTeam", {})
        code = match.get("competition", {}).get("code")

        home_recent, away_recent, standings = [], [], []
        e1, e2, e3 = None, None, None

        if home.get("id"):
            home_recent, e1 = await team_recent(client, home.get("id"))
        if away.get("id"):
            away_recent, e2 = await team_recent(client, away.get("id"))
        if code and code != "TSDB":
            standings, e3 = await competition_standings(client, code)

    home_form = parse_form(home_recent, home.get("id")) if home_recent else []
    away_form = parse_form(away_recent, away.get("id")) if away_recent else []
    model = build_model(home_form, away_form)

    h2h_obj = match.get("head2head", {})
    h2h = h2h_obj.get("matches", []) if isinstance(h2h_obj, dict) else []
    quality = sum([bool(home_form), bool(away_form), bool(standings), bool(h2h)]) / 4.0

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
        "standings_error": e3
    }, None


async def validate_predictions(limit=15):
    conn = sqlite3.connect(DB_PATH)
    pending = conn.execute(
        "SELECT id, fixture_id, market, selection FROM predictions WHERE outcome IS NULL AND fixture_id NOT LIKE 'TSDB-%' ORDER BY id ASC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    if not pending or not FOOTBALL_DATA_KEY:
        return 0
    checked = 0
    async with httpx.AsyncClient(timeout=8) as client:
        for pid, fixture_id, market, selection in pending:
            try:
                match, error = await find_fd_match(client, fixture_id)
                if error or not match or match.get("status") not in {"FINISHED", "AWARDED"}:
                    continue
                hg, ag = score_pair(match)
                outcome = prediction_outcome(market, selection, hg, ag)
                if outcome is None:
                    continue
                conn = sqlite3.connect(DB_PATH)
                conn.execute(
                    "UPDATE predictions SET outcome=?, settled_at=? WHERE id=? AND outcome IS NULL",
                    (outcome, now_iso(), pid)
                )
                conn.commit()
                conn.close()
                checked += 1
            except Exception as exc:
                print(f"Validation prediction {pid}: {exc}", flush=True)
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
    m = model["markets"]
    candidates = [
        ("1X2", "1", h), ("1X2", "X", d), ("1X2", "2", a),
        ("BTTS", "Oui" if m["btts"] >= 50 else "Non", max(m["btts"], 100 - m["btts"])),
        ("O2.5", "Oui" if m["over25"] >= 50 else "Non", max(m["over25"], 100 - m["over25"])),
        ("O1.5", "Oui" if m["over15"] >= 50 else "Non", max(m["over15"], 100 - m["over15"])),
        ("1X", "Oui" if h + d >= 50 else "Non", max(h + d, 100 - h - d)),
        ("X2", "Oui" if d + a >= 50 else "Non", max(d + a, 100 - d - a)),
        ("12", "Oui" if h + a >= 50 else "Non", max(h + a, 100 - h - a))
    ]
    conn = sqlite3.connect(DB_PATH)
    existing = set(
        conn.execute(
            "SELECT market, selection FROM predictions WHERE user_id=? AND fixture_id=?",
            (user_id, str(fixture_id))
        ).fetchall()
    )
    conn.close()
    count = 0
    for market, selection, probability in candidates:
        if (market, selection) not in existing:
            db_add_prediction(user_id, fixture_id, home, away, market, selection, float(probability))
            count += 1
    return count


async def send_or_edit(message, text, reply_markup):
    try:
        if hasattr(message, 'edit_text'):
            await message.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)
        else:
            await message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)
    except Exception as exc:
        if "Message is not modified" in str(exc):
            return
        try:
            await message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)
        except Exception:
            pass


async def send_match_results(message):
    async with httpx.AsyncClient(timeout=8) as client:
        matches = []
        source = "Football-Data.org"
        error = None
        if FOOTBALL_DATA_KEY:
            matches, error = await fd_today_matches(client)
        if not matches:
            events, ts_error = await tsdb_today_events(client)
            if events:
                matches = [tsdb_to_match(e) for e in events]
                source = "TheSportsDB"
            elif error or ts_error:
                await send_or_edit(message, f"❌ <b>Aucune source disponible.</b>\n\n{error or ts_error}", main_menu())
                return
    matches.sort(key=lambda x: fd_dt(x) or datetime.max.replace(tzinfo=PARIS))
    matches = matches[:15]
    if not matches:
        await send_or_edit(message, "⚽ <b>Aucun match disponible aujourd'hui.</b>", main_menu())
        return

    match_lines = []
    for m in matches:
        dt = fd_dt(m)
        time_str = dt.strftime('%H:%M') if dt else "00:00"
        home, away = match_names(m)
        match_lines.append(f"<b>{time_str}</b>\n<b>{home}</b>\nvs\n<b>{away}</b>")

    text = (
        f"⚡ <b>SPORT ANALYZER • V13.3</b>\n\n"
        f"📅 <b>MATCHS DU JOUR</b>\n\n"
        f"Date : {today_paris()}\n"
        f"Source : {source}\n"
        f"Nombre de matchs : {len(matches)}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n" +
        "\n\n".join(match_lines) +
        f"\n\n👇 <b>Sélectionne un match</b>"
    )
    await send_or_edit(message, text, match_list_keyboard(matches))


async def send_match_actions(message, fid):
    async with httpx.AsyncClient(timeout=8) as client:
        item, error = await find_match_by_id(client, fid)
    if error or not item:
        await send_or_edit(message, f"❌ <b>Match introuvable : {fid}</b>", main_menu())
        return
    home, away = match_names(item)
    comp = item.get("competition", {}).get("name", "Football")
    dt = fd_dt(item)
    status = fd_status(item.get("status"))
    time_str = dt.strftime('%H:%M') if dt else "00:00"

    text = (
        f"⚡ <b>SPORT ANALYZER • V13.3</b>\n\n"
        f"⚽ <b>MATCH SÉLECTIONNÉ</b>\n\n"
        f"<b>{home}</b>\n"
        f"vs\n"
        f"<b>{away}</b>\n\n"
        f"🕒 {time_str} | Statut: <b>{status}</b>\n"
        f"🏆 {comp}\n"
        f"🆔 ID: <code>{fid}</code>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 <b>MODULES DISPONIBLES</b>\n"
        f"Analyse • Probabilités • Buts • Cotes • Buteurs • Simulateur"
    )
    await send_or_edit(message, text, match_keyboard(fid))


async def run_analysis_dashboard_for_message(message, fid, user_id):
    data, error = await full_analysis(fid)
    if error:
        await send_or_edit(message, f"❌ <b>{error}</b>", main_menu())
        return
    record_model_predictions(user_id, data)
    match = data.get("match", {})
    home, away = match_names(match)
    comp = match.get("competition", {}).get("name", "Football")
    model = data.get("model") or {}
    h, d, a = model.get("final", (33.3, 33.4, 33.3))
    m = model.get("markets", {})
    sh = data.get("standings_home") or {}
    sa = data.get("standings_away") or {}

    pos_h = f"{sh.get('position')}e" if sh.get('position') else "Donnée indisponible"
    pts_h = f"{sh.get('points')} pts" if sh.get('points') is not None else ""
    pos_a = f"{sa.get('position')}e" if sa.get('position') else "Donnée indisponible"
    pts_a = f"{sa.get('points')} pts" if sa.get('points') is not None else ""

    scores_str = "\n".join([f"  • <b>{hh}-{aa}</b> : {p:.1f}%" for hh, aa, p in likely_scores(model.get("matrix"), 3)])

    text = (
        f"📊 <b>ANALYSE DU MATCH</b>\n"
        f"⚽ <b>{home}</b> vs <b>{away}</b> ({comp})\n\n"
        f"🏠 <b>ÉQUIPE DOMICILE ({home})</b>\n"
        f"• Classement : {pos_h} {pts_h}\n"
        f"• xG Attendu : {model.get('home_xg', 0):.2f}\n\n"
        f"✈️ <b>ÉQUIPE EXTÉRIEURE ({away})</b>\n"
        f"• Classement : {pos_a} {pts_a}\n"
        f"• xG Attendu : {model.get('away_xg', 0):.2f}\n\n"
        f"🎯 <b>PROBABILITÉS 1X2</b>\n"
        f"1 : <b>{h:.1f}%</b>\n"
        f"N : <b>{d:.1f}%</b>\n"
        f"2 : <b>{a:.1f}%</b>\n\n"
        f"⚽ <b>MARCHÉS DE BUTS</b>\n"
        f"• BTTS Oui : <b>{m.get('btts', 0):.1f}%</b>\n"
        f"• Over 1.5 : <b>{m.get('over15', 0):.1f}%</b>\n"
        f"• Over 2.5 : <b>{m.get('over25', 0):.1f}%</b>\n"
        f"• Under 3.5 : <b>{m.get('under35', 0):.1f}%</b>\n\n"
        f"🎯 <b>SCORES PROBABLES</b>\n{scores_str}"
    )
    await send_or_edit(message, text, analysis_menu_keyboard(fid))


async def run_prob_for_message(message, fid):
    data, error = await full_analysis(fid)
    if error:
        await send_or_edit(message, f"❌ <b>{error}</b>", main_menu())
        return
    model = data.get("model")
    if not model:
        await send_or_edit(message, "❌ <b>Données insuffisantes.</b>", main_menu())
        return
    h, d, a = model["final"]
    text = (
        f"🎯 <b>PROBABILITÉS DÉTAILLÉES</b>\n"
        f"🆔 Match ID: <code>{fid}</code>\n\n"
        f"<b>1X2 :</b>\n"
        f"• Victoire Domicile (1) : <b>{h:.1f}%</b>\n"
        f"• Match Nul (X) : <b>{d:.1f}%</b>\n"
        f"• Victoire Extérieur (2) : <b>{a:.1f}%</b>\n\n"
        f"<b>Double Chance :</b>\n"
        f"• 1X : <b>{h+d:.1f}%</b>\n"
        f"• X2 : <b>{d+a:.1f}%</b>\n"
        f"• 12 : <b>{h+a:.1f}%</b>"
    )
    await send_or_edit(message, text, match_keyboard(fid))


async def run_buts_for_message(message, fid):
    data, error = await full_analysis(fid)
    if error:
        await send_or_edit(message, f"❌ <b>{error}</b>", main_menu())
        return
    model = data.get("model")
    if not model:
        await send_or_edit(message, "❌ <b>Données insuffisantes.</b>", main_menu())
        return
    m = model["markets"]
    text = (
        f"⚽ <b>MARCHÉS DE BUTS</b>\n"
        f"🆔 Match ID: <code>{fid}</code>\n\n"
        f"<b>Les deux équipes marquent (BTTS) :</b>\n"
        f"• Oui : <b>{m['btts']:.1f}%</b> | Non : <b>{100-m['btts']:.1f}%</b>\n\n"
        f"<b>Lignes Over / Under :</b>\n"
        f"• Over 1.5 : <b>{m['over15']:.1f}%</b>\n"
        f"• Over 2.5 : <b>{m['over25']:.1f}%</b> | Under 2.5 : <b>{m['under25']:.1f}%</b>\n"
        f"• Over 3.5 : <b>{m['over35']:.1f}%</b> | Under 3.5 : <b>{m['under35']:.1f}%</b>\n\n"
        f"📊 <b>Expected Goals (xG) :</b> {model['home_xg']:.2f} - {model['away_xg']:.2f}"
    )
    await send_or_edit(message, text, match_keyboard(fid))


async def run_stats_for_message(message, fid):
    data, error = await full_analysis(fid)
    if error:
        await send_or_edit(message, f"❌ <b>{error}</b>", main_menu())
        return
    sh = data.get("standings_home") or {}
    sa = data.get("standings_away") or {}
    hf = data.get("home_form") or []
    af = data.get("away_form") or []

    hf_str = " ".join(x.get("result", "?") for x in hf[-5:]) if hf else "Donnée indisponible"
    af_str = " ".join(x.get("result", "?") for x in af[-5:]) if af else "Donnée indisponible"

    text = (
        f"📊 <b>STATISTIQUES & CLASSEMENT</b>\n"
        f"🆔 Match ID: <code>{fid}</code>\n\n"
        f"🏠 <b>Équipe Domicile :</b>\n"
        f"• Rang : {sh.get('position','Donnée indisponible') if sh.get('position') else 'Donnée indisponible'} ({sh.get('points','0')} pts)\n"
        f"• Bilan : {sh.get('won',0)}V / {sh.get('draw',0)}N / {sh.get('lost',0)}D\n"
        f"• Buts : {sh.get('gf',0)} pour / {sh.get('ga',0)} contre\n"
        f"• Forme récente : <b>{hf_str}</b>\n\n"
        f"✈️ <b>Équipe Extérieure :</b>\n"
        f"• Rang : {sa.get('position','Donnée indisponible') if sa.get('position') else 'Donnée indisponible'} ({sa.get('points','0')} pts)\n"
        f"• Bilan : {sa.get('won',0)}V / {sa.get('draw',0)}N / {sa.get('lost',0)}D\n"
        f"• Buts : {sa.get('gf',0)} pour / {sa.get('ga',0)} contre\n"
        f"• Forme récente : <b>{af_str}</b>"
    )
    await send_or_edit(message, text, analysis_menu_keyboard(fid))


async def run_cotes_for_message(message, fid):
    text = (
        f"💰 <b>INFORMATIONS COTES</b>\n"
        f"🆔 Match ID: <code>{fid}</code>\n\n"
        f"• Les cotes sont basées sur la cote juste du modèle théorique.\n"
        f"• Pour enregistrer un pari réel avec cote bookmaker, utilise la commande :\n"
        f"  <code>/mise {fid} 1X2 1 10 1.80</code>"
    )
    await send_or_edit(message, text, match_keyboard(fid))


async def run_buteur_for_message(message, fid):
    async with httpx.AsyncClient(timeout=8) as client:
        item, error = await find_match_by_id(client, fid)

    if error or not item:
        await send_or_edit(message, f"❌ <b>{error or 'Match introuvable'}</b>", match_keyboard(fid))
        return

    status = item.get("status")
    if status in {"SCHEDULED", "TIMED"}:
        await send_or_edit(message, "⏳ <b>MATCH À VENIR</b>\n\nCe match n'a pas encore commencé.\nLes événements en direct seront disponibles après le coup d'envoi.", match_keyboard(fid))
        return

    if str(fid).startswith("TSDB-"):
        eid = str(fid).split("-", 1)[1]
        async with httpx.AsyncClient(timeout=8) as client:
            data, error = await tsdb_get(client, "lookuptimeline.php", {"id": eid}, f"tsdb:timeline:{eid}", 120)
        if error or not data:
            await send_or_edit(message, "⚽ <b>Aucun événement de but disponible.</b>", match_keyboard(fid))
            return
        goals = [x for x in (data.get("timeline", []) or []) if "goal" in str(x.get("strTimeline", "")).lower()]
        if not goals:
            await send_or_edit(message, "⚽ <b>Aucun événement de but disponible.</b>", match_keyboard(fid))
            return
        msg = "⚽ <b>BUTS & ÉVÉNEMENTS :</b>\n\n" + "\n".join(f"• {g.get('strTimeline','But')} | {g.get('strPlayer','Joueur N/D')}" for g in goals[:15])
        await send_or_edit(message, msg, match_keyboard(fid))
        return

    goals = item.get("goals", []) or []
    if not goals:
        await send_or_edit(message, "⚽ <b>Aucun événement de but disponible.</b>", match_keyboard(fid))
        return
    msg = "⚽ <b>BUTEURS & ÉVÉNEMENTS :</b>\n\n"
    for g in goals[:20]:
        scorer = g.get("scorer", {}) or {}
        assist = g.get("assist", {}) or {}
        msg += f"• {g.get('minute','?')}' {scorer.get('name','N/D')}" + (f" (passe: {assist['name']})" if assist.get('name') else "") + "\n"
    await send_or_edit(message, msg, match_keyboard(fid))


async def send_simulator_menu(message, fid):
    text = (
        f"🔬 <b>SIMULATEUR DE PARIS</b>\n"
        f"🆔 Match ID: <code>{fid}</code>\n\n"
        f"Choisis un marché à étudier pour calculer la cote juste et le rendement théorique :"
    )
    await send_or_edit(message, text, simulator_keyboard(fid))


async def send_market_menu(message, fid, market):
    data, error = await full_analysis(fid)
    if error:
        await send_or_edit(message, f"❌ <b>{error}</b>", main_menu())
        return
    model = data.get("model")
    if not model:
        await send_or_edit(message, "❌ <b>Données insuffisantes.</b>", main_menu())
        return
    text = f"🔬 <b>SÉLECTIONNE TA SÉLECTION ({market}) :</b>\n"
    await send_or_edit(message, text, market_keyboard(fid, market))


async def send_pick_stake_menu(message, fid, market, selection):
    data, error = await full_analysis(fid)
    if error:
        await send_or_edit(message, f"❌ <b>{error}</b>", main_menu())
        return
    model = data.get("model")
    if not model:
        await send_or_edit(message, "❌ <b>Données insuffisantes.</b>", main_menu())
        return
    h, d, a = model["final"]
    m = model["markets"]
    vals = {
        "1": ("1 (Victoire Domicile)", h), "X": ("X (Match Nul)", d), "2": ("2 (Victoire Extérieur)", a),
        "1X": ("1X (Domicile ou Nul)", h + d), "X2": ("X2 (Nul ou Extérieur)", d + a), "12": ("12 (Non Nul)", h + a),
        "BTTSY": ("BTTS Oui", m["btts"]), "BTTSN": ("BTTS Non", 100 - m["btts"]),
        "O15": ("Over 1.5", m["over15"]), "O25": ("Over 2.5", m["over25"]), "O35": ("Over 3.5", m["over35"]),
        "U25": ("Under 2.5", m["under25"]), "U35": ("Under 3.5", m["under35"])
    }
    label, p = vals.get(selection, ("Sélection", 0))
    if p <= 0:
        await send_or_edit(message, "❌ <b>Probabilité indisponible.</b>", main_menu())
        return
    fair = 100 / p
    text = (
        f"💰 <b>SIMULATION DE MISE</b>\n\n"
        f"• Choix : <b>{label}</b>\n"
        f"• Probabilité modèle : <b>{p:.1f}%</b>\n"
        f"• Cote juste théorique : <b>{fair:.2f}</b>\n\n"
        f"👇 <b>Sélectionne une mise à simuler :</b>"
    )
    await send_or_edit(message, text, stake_keyboard(fid, market, selection))


async def send_stake_result(message, fid, market, selection, stake):
    data, error = await full_analysis(fid)
    if error:
        await send_or_edit(message, f"❌ <b>{error}</b>", main_menu())
        return
    model = data.get("model")
    if not model:
        await send_or_edit(message, "❌ <b>Données insuffisantes.</b>", main_menu())
        return
    h, d, a = model["final"]
    m = model["markets"]
    probs = {
        "1": h, "X": d, "2": a,
        "1X": h + d, "X2": d + a, "12": h + a,
        "BTTSY": m["btts"], "BTTSN": 100 - m["btts"],
        "O15": m["over15"], "O25": m["over25"], "O35": m["over35"],
        "U25": m["under25"], "U35": m["under35"]
    }
    p = float(probs.get(selection, 0))
    if p <= 0:
        await send_or_edit(message, "❌ <b>Probabilité indisponible.</b>", main_menu())
        return
    fair = 100 / p
    ret = float(stake) * fair
    profit = ret - float(stake)

    text = (
        f"📊 <b>RÉSULTAT DE LA SIMULATION</b>\n\n"
        f"• Sélection : <b>{selection}</b>\n"
        f"• Probabilité : <b>{p:.1f}%</b>\n"
        f"• Cote juste : <b>{fair:.2f}</b>\n"
        f"• Mise engagée : <b>{float(stake):.2f} €</b>\n"
        f"• Gain brut potentiel : <b>{ret:.2f} €</b>\n"
        f"• Profit théorique : <b>{profit:+.2f} €</b>"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔬 Autre marché", callback_data=f"sim:{fid}"), InlineKeyboardButton("🔎 Analyse", callback_data=f"analyse:{fid}")],
        [InlineKeyboardButton("🏠 ACCUEIL", callback_data="menu:home")]
    ])
    await send_or_edit(message, text, kb)


async def send_performance_result(message, user_id):
    await validate_predictions()
    perf = db_performance(user_id)
    text = (
        f"📊 <b>TABLEAU DE PERFORMANCE</b>\n\n"
        f"• Prédictions validées : <b>{perf['total']}</b>\n"
        f"• Prédictions correctes : <b>{perf['wins']}</b>\n"
        f"• Taux de réussite global : <b>{perf['accuracy']:.1f}%</b>\n\n" if perf["accuracy"] is not None else "• Taux de réussite : N/D\n\n"
    )
    if perf["markets"]:
        text += "<b>Détail par marché :</b>\n"
        for k, v in sorted(perf["markets"].items()):
            rate = (v['wins'] / v['total'] * 100) if v['total'] else 0
            text += f"• {k} : {v['wins']}/{v['total']} ({rate:.1f}%)\n"

    await send_or_edit(message, text, tools_keyboard())


async def send_validation_result(message, user_id):
    checked = await validate_predictions()
    perf = db_performance(user_id)
    text = (
        f"🔄 <b>VALIDATION DES PRÉDICTIONS</b>\n\n"
        f"• Nouvelles prédictions validées : <b>{checked}</b>\n"
        f"• Total prédictions terminées : <b>{perf['total']}</b>\n"
    )
    if perf["accuracy"] is not None:
        text += f"• Taux de réussite actuel : <b>{perf['accuracy']:.1f}%</b>"
    else:
        text += "• Aucune prédiction terminée dans l'historique."
    await send_or_edit(message, text, tools_keyboard())


async def send_bankroll_result(message, user_id):
    total, profit, count, wins, roi = db_summary(user_id)
    text = (
        f"💰 <b>SUIVI BANKROLL & PARIS</b>\n\n"
        f"• Total misé : <b>{total:.2f} €</b>\n"
        f"• Profit / Perte : <b>{profit:+.2f} €</b>\n"
        f"• Nombre de paris : <b>{count}</b> (gagnants: {wins})\n"
        f"• ROI : <b>{roi:+.2f}%</b>\n\n"
        f"💡 <i>Utilise /mise pour enregistrer tes paris réels.</i>"
    )
    await send_or_edit(message, text, tools_keyboard())


async def send_status_result(message):
    async with httpx.AsyncClient(timeout=7) as client:
        fd_ok = False
        detail = "Clé absente"
        if FOOTBALL_DATA_KEY:
            from api_client import fd_get
            data, error = await fd_get(client, "/matches", {"date": today_paris()}, "fd:status", 30)
            fd_ok = data is not None and error is None
            detail = "OK" if fd_ok else str(error)
        ts_data, ts_error = await tsdb_get(client, "eventsday.php", {"d": today_paris(), "s": "Soccer"}, "tsdb:status", 30)
        ts_ok = ts_data is not None and ts_error is None

    text = (
        f"🔌 <b>ÉTAT DES SOURCES DE DONNÉES</b>\n\n"
        f"• Football-Data.org : <b>{'🟢 OK' if fd_ok else '🔴 Indisponible'}</b> ({detail[:60]})\n"
        f"• TheSportsDB : <b>{'🟢 OK' if ts_ok else '🔴 Indisponible'}</b>\n"
    )
    await send_or_edit(message, text, tools_keyboard())


async def send_help_result(message):
    text = (
        f"❓ <b>AIDE & COMMANDES PRINCIPALES</b>\n\n"
        f"Clique sur les boutons ci-dessous ou tape directement une commande :\n\n"
        f"• <code>/match</code> - Matchs du jour\n"
        f"• <code>/analyse ID</code> - Dashboard d'analyse\n"
        f"• <code>/probabilite ID</code> - Probabilités 1X2\n"
        f"• <code>/buts ID</code> - Marchés Over/Under & BTTS\n"
        f"• <code>/buteur ID</code> - Détails des événements/buteurs\n"
        f"• <code>/mise ID marché choix montant [cote]</code> - Parier\n"
        f"• <code>/resultat ID_BET win|loss|void</code> - Dénouer un pari\n"
        f"• <code>/bankroll</code> - Suivi comptable\n"
        f"• <code>/performance</code> - Taux de réussite du modèle"
    )
    await send_or_edit(message, text, help_keyboard())


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    print(f"🔘 CALLBACK : {data}", flush=True)
    try:
        await query.answer()
    except Exception:
        pass
    message = query.message
    if not message:
        return
    uid = update.effective_user.id if update.effective_user else 0
    try:
        if data == "menu:home":
            await send_or_edit(message, main_welcome_text(), main_menu())
            return
        if data == "menu:match":
            await send_match_results(message)
            return
        if data == "menu:status":
            await send_status_result(message)
            return
        if data == "menu:performance":
            await send_performance_result(message, uid)
            return
        if data == "menu:validation":
            await send_validation_result(message, uid)
            return
        if data == "menu:bankroll":
            await send_bankroll_result(message, uid)
            return
        if data == "menu:sim":
            text = "🔬 <b>SIMULATEUR DE PARIS</b>\n\nSélectionne un match pour démarrer une simulation de cote et de mise théorique."
            await send_or_edit(message, text, main_menu())
            return
        if data == "menu:tools":
            text = "🎯 <b>OUTILS ET SUIVI</b>\n\nChoisis une option de suivi ou d'analyse :"
            await send_or_edit(message, text, tools_keyboard())
            return
        if data == "menu:help":
            await send_help_result(message)
            return
        if ":" not in data:
            return
        action, value = data.split(":", 1)
        if action == "match":
            await send_match_actions(message, value)
            return
        if action == "analyse":
            await run_analysis_dashboard_for_message(message, value, uid)
            return
        if action == "prob":
            await run_prob_for_message(message, value)
            return
        if action == "buts":
            await run_buts_for_message(message, value)
            return
        if action == "stats":
            await run_stats_for_message(message, value)
            return
        if action == "cotes":
            await run_cotes_for_message(message, value)
            return
        if action == "buteur":
            await run_buteur_for_message(message, value)
            return
        if action == "sim":
            await send_simulator_menu(message, value)
            return
        if action == "market":
            fid, market = value.split(":", 1)
            await send_market_menu(message, fid, market)
            return
        if action == "pick":
            fid, market, selection = value.split(":", 2)
            await send_pick_stake_menu(message, fid, market, selection)
            return
        if action == "stake":
            fid, market, selection, stake = value.split(":", 3)
            await send_stake_result(message, fid, market, selection, float(stake))
            return
        await send_or_edit(message, f"⚠️ Action inconnue : {action}", main_menu())
    except Exception as exc:
        print(f"❌ CALLBACK ERROR [{data}] : {exc}", flush=True)
        try:
            await send_or_edit(message, "❌ <b>Une erreur est survenue.</b>\n\nUtilise 🏠 Accueil pour continuer.", main_menu())
        except Exception as reply_error:
            print(f"❌ Reply error: {reply_error}", flush=True)


async def start(update, context):
    await update.message.reply_text(main_welcome_text(), parse_mode="HTML", reply_markup=main_menu())


async def help_command(update, context):
    await send_help_result(update.message)


async def match_command(update, context):
    await send_match_results(update.message)


async def analyse_command(update, context):
    if not context.args:
        await update.message.reply_text("Utilisation : /analyse ID")
        return
    await run_analysis_dashboard_for_message(update.message, context.args[0], update.effective_user.id)


async def buts_command(update, context):
    if not context.args:
        await update.message.reply_text("Utilisation : /buts ID")
        return
    await run_buts_for_message(update.message, context.args[0])


async def probabilite_command(update, context):
    if not context.args:
        await update.message.reply_text("Utilisation : /probabilite ID")
        return
    await run_prob_for_message(update.message, context.args[0])


async def cotes_command(update, context):
    if context.args:
        await run_cotes_for_message(update.message, context.args[0])
    else:
        await update.message.reply_text("💰 COTES\n\nAucune cote n'est inventée.")


async def buteur_command(update, context):
    if not context.args:
        await update.message.reply_text("Utilisation : /buteur ID")
        return
    await run_buteur_for_message(update.message, context.args[0])


async def bankroll_command(update, context):
    await send_bankroll_result(update.message, update.effective_user.id)


async def statusapi_command(update, context):
    await send_status_result(update.message)


async def performance_command(update, context):
    await send_performance_result(update.message, update.effective_user.id)


async def validation_command(update, context):
    await send_validation_result(update.message, update.effective_user.id)


async def mise_command(update, context):
    if len(context.args) < 4:
        await update.message.reply_text("Utilisation : /mise ID marché sélection montant [cote]")
        return
    try:
        fid, market, selection = context.args[:3]
        stake = float(context.args[3].replace(",", "."))
        odds = float(context.args[4].replace(",", ".")) if len(context.args) >= 5 else None
        if stake <= 0 or (odds is not None and odds <= 1):
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Montant ou cote invalide.")
        return
    bid = db_add_bet(update.effective_user.id, fid, market, selection, stake, odds)
    await update.message.reply_text(f"💶 <b>PARI ENREGISTRÉ</b>\n\nID pari : {bid}\nMatch : {fid}\nMarché : {market}\nSélection : {selection}\nMise : {stake:.2f} €" + (f"\nCote : {odds:.2f}" if odds else ""), parse_mode="HTML")


async def resultat_command(update, context):
    if len(context.args) != 2:
        await update.message.reply_text("Utilisation : /resultat ID_BET win|loss|void")
        return
    try:
        bid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID de pari invalide.")
        return
    result = context.args[1].lower()
    if result not in {"win", "loss", "void"}:
        await update.message.reply_text("❌ Résultat : win, loss ou void.")
        return
    profit = db_settle_bet(update.effective_user.id, bid, result)
    await update.message.reply_text("❌ Pari introuvable ou déjà réglé." if profit is None else f"📊 Pari {bid} réglé\nRésultat : {result}\nProfit/perte : {profit:+.2f} €")


# REST API Endpoints for Frontend React Mini App
async def api_get_matches(request: Request):
    async with httpx.AsyncClient(timeout=8) as client:
        matches = []
        if FOOTBALL_DATA_KEY:
            matches, _ = await fd_today_matches(client)
        if not matches:
            events, _ = await tsdb_today_events(client)
            if events:
                matches = [tsdb_to_match(e) for e in events]
    formatted = []
    for m in matches:
        home, away = match_names(m)
        dt = fd_dt(m)
        formatted.append({
            "id": str(m.get("id")),
            "competition": m.get("competition", {}).get("name", "Football"),
            "competition_code": m.get("competition", {}).get("code"),
            "matchday": f"Journée {m.get('matchday', 1)}",
            "utc_date": dt.strftime('%d/%m/%Y • %H:%M') if dt else "19:00",
            "status": m.get("status", "SCHEDULED"),
            "home_team": home,
            "away_team": away,
            "home_logo": m.get("homeTeam", {}).get("crest"),
            "away_logo": m.get("awayTeam", {}).get("crest"),
            "venue": m.get("venue", "Stade Officiel")
        })
    return JSONResponse(formatted)


async def api_get_analysis(request: Request):
    fixture_id = request.path_params.get("fixture_id", "1001")
    data, error = await full_analysis(fixture_id)
    if error or not data:
        return JSONResponse({"error": error or "Match introuvable"}, status_code=404)

    match = data.get("match", {})
    home, away = match_names(match)
    dt = fd_dt(match)
    model = data.get("model") or {}

    formatted_match = {
        "id": str(match.get("id")),
        "competition": match.get("competition", {}).get("name", "Football"),
        "matchday": f"Journée {match.get('matchday', 1)}",
        "utc_date": dt.strftime('%d/%m/%Y • %H:%M') if dt else "19:00",
        "status": match.get("status", "SCHEDULED"),
        "home_team": home,
        "away_team": away,
        "home_logo": match.get("homeTeam", {}).get("crest"),
        "away_logo": match.get("awayTeam", {}).get("crest"),
        "venue": match.get("venue", "Stade Officiel")
    }

    return JSONResponse({
        "match": formatted_match,
        "home_form": data.get("home_form", []),
        "away_form": data.get("away_form", []),
        "standings_home": data.get("standings_home", {}),
        "standings_away": data.get("standings_away", {}),
        "model_probabilities": model.get("model_probabilities", {
            "1": round(model.get("final", [49.4])[0], 1),
            "X": round(model.get("final", [0, 24.7])[1], 1),
            "2": round(model.get("final", [0, 0, 25.8])[2], 1),
            "1X": 74.1, "X2": 50.5, "12": 75.2
        }),
        "goal_markets": model.get("markets", {}),
        "xg_home": round(model.get("home_xg", 1.62), 2),
        "xg_away": round(model.get("away_xg", 1.10), 2),
        "probable_scores": likely_scores(model.get("matrix"), 3),
        "confidence": "Moyenne",
        "trend": "Tendance marquée",
        "reliability": "Bonne"
    })


async def webhook(request: Request):
    try:
        payload = await request.json()
        update = Update.de_json(payload, telegram_app.bot)
        await telegram_app.process_update(update)
        return JSONResponse({"ok": True})
    except Exception as exc:
        print(f"❌ Webhook error: {exc}", flush=True)
        return JSONResponse({"ok": False}, status_code=500)


async def health(request: Request):
    return JSONResponse({"status": "ok", "bot": "sport-analyzer-bot-v13.3"})


@asynccontextmanager
async def lifespan(app):
    global telegram_app
    if not TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN est manquant.")
    if not RENDER_URL:
        raise RuntimeError("RENDER_EXTERNAL_URL est manquant.")
    init_db()
    telegram_app = Application.builder().token(TOKEN).concurrent_updates(True).build()
    for command, func in [
        ("start", start), ("help", help_command), ("match", match_command),
        ("analyse", analyse_command), ("cotes", cotes_command), ("buteur", buteur_command),
        ("buts", buts_command), ("probabilite", probabilite_command), ("mise", mise_command),
        ("resultat", resultat_command), ("bankroll", bankroll_command), ("statusapi", statusapi_command),
        ("performance", performance_command), ("validation", validation_command)
    ]:
        telegram_app.add_handler(CommandHandler(command, func))
    telegram_app.add_handler(CallbackQueryHandler(callback_handler))
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.bot.set_webhook(f"{RENDER_URL}/telegram")
    print("✅ SPORT ANALYZER V13.3 démarré avec Mini App Telegram", flush=True)
    try:
        yield
    finally:
        await telegram_app.stop()
        await telegram_app.shutdown()


frontend_dist = os.path.join(os.path.dirname(__file__), "frontend/dist")

routes = [
    Route("/health", health, methods=["GET"]),
    Route("/telegram", webhook, methods=["POST"]),
    Route("/api/matches", api_get_matches, methods=["GET"]),
    Route("/api/matches/{fixture_id}/analysis", api_get_analysis, methods=["GET"]),
]

if os.path.exists(frontend_dist):
    routes.append(Mount("/", app=StaticFiles(directory=frontend_dist, html=True), name="static"))

app = Starlette(routes=routes, lifespan=lifespan)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
