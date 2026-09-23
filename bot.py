import os
import io
import sqlite3
import asyncio
from datetime import datetime, timezone
from contextlib import asynccontextmanager

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn

from database import (
    init_db, db_add_bet, db_settle_bet, db_summary,
    db_add_prediction, prediction_outcome, db_performance, DB_PATH
)
from analytics import (
    parse_form, build_model, standings_metrics
)
from api_client import (
    fd_today_matches, tsdb_today_events, tsdb_to_match, find_fd_match,
    team_recent, competition_standings, fd_dt, score_pair, tsdb_get,
    FOOTBALL_DATA_KEY, today_paris, PARIS
)
from keyboards import (
    main_menu, match_keyboard, match_list_keyboard,
    simulator_keyboard, market_keyboard, stake_keyboard, tools_keyboard
)
from renderer import (
    render_dashboard, render_screen, render_match_list, render_match_selected
)

# ============================================================
# SPORT ANALYZER V13.3
# Optimized mobile dashboard
# Sources: Football-Data.org + TheSportsDB fallback
# ============================================================

TOKEN = os.getenv("TELEGRAM_TOKEN")
PORT = int(os.getenv("PORT", "10000"))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")
telegram_app = None


def now_iso():
    return datetime.now(timezone.utc).isoformat()


async def full_analysis(fixture_id):
    if str(fixture_id).startswith("TSDB-"):
        return None, "Les analyses avancées nécessitent actuellement un ID Football-Data.org."
    async with httpx.AsyncClient(timeout=10, limits=httpx.Limits(max_connections=8)) as client:
        match, error = await find_fd_match(client, fixture_id)
        if error:
            return None, error
        home = match.get("homeTeam", {})
        away = match.get("awayTeam", {})
        code = match.get("competition", {}).get("code")
        (home_recent, e1), (away_recent, e2), (standings, e3) = await asyncio.gather(
            team_recent(client, home.get("id")),
            team_recent(client, away.get("id")),
            competition_standings(client, code)
        )
    if e1 or e2:
        return None, e1 or e2
    home_form = parse_form(home_recent, home.get("id"))
    away_form = parse_form(away_recent, away.get("id"))
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
            elif error:
                await message.reply_text("❌ Aucune source disponible.\n\nVérifie FOOTBALL_DATA_KEY dans Render.", reply_markup=main_menu())
                return
    matches.sort(key=lambda x: fd_dt(x) or datetime.max.replace(tzinfo=PARIS))
    matches = matches[:20]
    if not matches:
        await message.reply_text("⚽ Aucun match disponible aujourd'hui.", reply_markup=main_menu())
        return
    img = render_match_list(matches, source)
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    out.seek(0)
    await message.reply_photo(photo=out.getvalue(), caption="⚡ <b>SPORT ANALYZER • V13.3</b>\nMatchs du jour", parse_mode="HTML", reply_markup=match_list_keyboard(matches))


async def send_match_actions(message, fid):
    async with httpx.AsyncClient(timeout=8) as client:
        if str(fid).startswith("TSDB-"):
            events, error = await tsdb_today_events(client)
            item = next((tsdb_to_match(e) for e in (events or []) if f"TSDB-{e.get('idEvent')}" == str(fid)), None)
        else:
            item, error = await find_fd_match(client, fid)
    if error or not item:
        await message.reply_text(f"❌ Match introuvable : {fid}", reply_markup=main_menu())
        return
    img = render_match_selected(item, fid)
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    out.seek(0)
    await message.reply_photo(photo=out.getvalue(), caption="⚡ <b>SPORT ANALYZER • V13.3</b>\nMatch sélectionné", parse_mode="HTML", reply_markup=match_keyboard(fid))


async def run_prob_for_message(message, fid):
    data, error = await full_analysis(fid)
    if error:
        await message.reply_text(f"❌ {error}", reply_markup=main_menu())
        return
    model = data.get("model")
    if not model:
        await message.reply_text("❌ Données insuffisantes.", reply_markup=main_menu())
        return
    h, d, a = model["final"]
    img = render_screen("PROBABILITÉS", "Probabilités du modèle • match sélectionné", [
        {"kind": "bars", "heading": "1X2", "height": 230, "rows": [("1", h), ("X", d), ("2", a)]},
        {"kind": "bars", "heading": "DOUBLE CHANCE", "height": 230, "rows": [("1X", h + d), ("X2", d + a), ("12", h + a)]}
    ])
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    out.seek(0)
    await message.reply_photo(photo=out.getvalue(), caption="⚡ <b>SPORT ANALYZER • V13.3</b>\nProbabilités", parse_mode="HTML", reply_markup=match_keyboard(fid))


async def run_buts_for_message(message, fid):
    data, error = await full_analysis(fid)
    if error:
        await message.reply_text(f"❌ {error}", reply_markup=main_menu())
        return
    model = data.get("model")
    if not model:
        await message.reply_text("❌ Données insuffisantes.", reply_markup=main_menu())
        return
    m = model["markets"]
    img = render_screen("MARCHÉS DE BUTS", "BTTS et lignes Over / Under", [
        {"kind": "bars", "heading": "BTTS", "height": 160, "rows": [("Oui", m["btts"]), ("Non", 100 - m["btts"])]},
        {"kind": "bars", "heading": "OVER / UNDER", "height": 330, "rows": [("Over 1.5", m["over15"]), ("Over 2.5", m["over25"]), ("Over 3.5", m["over35"]), ("Under 2.5", m["under25"]), ("Under 3.5", m["under35"])]},
        {"kind": "card", "heading": "xG ESTIMÉ", "height": 120, "rows": [("Domicile", f"{model['home_xg']:.2f}"), ("Extérieur", f"{model['away_xg']:.2f}")]}
    ])
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    out.seek(0)
    await message.reply_photo(photo=out.getvalue(), caption="⚡ <b>SPORT ANALYZER • V13.3</b>\nMarchés de buts", parse_mode="HTML", reply_markup=match_keyboard(fid))


async def run_cotes_for_message(message, fid):
    img = render_screen("COTES", "Données bookmaker disponibles selon la source", [
        {"kind": "card", "heading": "ÉTAT", "height": 190, "rows": [("Cotes automatiques", "Non garanties dans la source gratuite"), ("Principe", "Aucune cote n'est inventée")]},
        {"kind": "card", "heading": "SAISIE MANUELLE", "height": 160, "rows": [("Commande", "/mise ID marché sélection montant cote"), ("Exemple", "/mise 123 1X2 1 10 1.80")]}
    ])
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    out.seek(0)
    await message.reply_photo(photo=out.getvalue(), caption="⚡ <b>SPORT ANALYZER • V13.3</b>\nCotes", parse_mode="HTML", reply_markup=match_keyboard(fid))


async def run_buteur_for_message(message, fid):
    if str(fid).startswith("TSDB-"):
        eid = str(fid).split("-", 1)[1]
        async with httpx.AsyncClient(timeout=8) as client:
            data, error = await tsdb_get(client, "lookuptimeline.php", {"id": eid}, f"tsdb:timeline:{eid}", 120)
        if error:
            await message.reply_text(f"❌ {error}", reply_markup=main_menu())
            return
        goals = [x for x in (data.get("timeline", []) or []) if "goal" in str(x.get("strTimeline", "")).lower()]
        if not goals:
            await message.reply_text("⚽ Aucun événement de but disponible.", reply_markup=match_keyboard(fid))
            return
        msg = "⚽ <b>BUTS / ÉVÉNEMENTS</b>\n\n" + "\n".join(f"• {g.get('strTimeline','But')} | {g.get('strPlayer','Joueur N/D')}" for g in goals[:15])
        await message.reply_text(msg, parse_mode="HTML", reply_markup=match_keyboard(fid))
        return
    async with httpx.AsyncClient(timeout=8) as client:
        data, error = await find_fd_match(client, fid)
    if error:
        await message.reply_text(f"❌ {error}", reply_markup=main_menu())
        return
    goals = data.get("goals", []) or []
    if not goals:
        await message.reply_text("⚽ Aucun détail de buteur disponible pour ce match.", reply_markup=match_keyboard(fid))
        return
    msg = "⚽ <b>BUTEURS</b>\n\n"
    for g in goals[:20]:
        scorer = g.get("scorer", {}) or {}
        assist = g.get("assist", {}) or {}
        msg += f"• {g.get('minute','?')}' {scorer.get('name','N/D')}" + (f" | passe : {assist['name']}" if assist.get('name') else "") + "\n"
    await message.reply_text(msg, parse_mode="HTML", reply_markup=match_keyboard(fid))


async def run_analysis_dashboard_for_message(message, fid, user_id):
    data, error = await full_analysis(fid)
    if error:
        await message.reply_text(f"❌ {error}", reply_markup=main_menu())
        return
    record_model_predictions(user_id, data)
    img = render_dashboard(data)
    output = io.BytesIO()
    img.save(output, format="PNG", optimize=True)
    output.seek(0)
    await message.reply_photo(photo=output.getvalue(), caption="⚡ <b>SPORT ANALYZER • V13.3</b>\nDashboard mobile • données dynamiques", parse_mode="HTML", reply_markup=match_keyboard(fid))


async def send_simulator_menu(message, fid):
    img = render_screen("SIMULATEUR", "Étudie un marché puis une mise théorique", [
        {"kind": "card", "heading": "MARCHÉS", "height": 250, "rows": [("1X2", "Victoire domicile / nul / extérieur"), ("Double chance", "1X • X2 • 12"), ("BTTS", "Oui / Non"), ("Over / Under", "1.5 • 2.5 • 3.5")]}
    ])
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    out.seek(0)
    await message.reply_photo(photo=out.getvalue(), caption="⚡ <b>SPORT ANALYZER • V13.3</b>\nSimulateur", parse_mode="HTML", reply_markup=simulator_keyboard(fid))


async def send_market_menu(message, fid, market):
    data, error = await full_analysis(fid)
    if error:
        await message.reply_text(f"❌ {error}", reply_markup=main_menu())
        return
    model = data.get("model")
    if not model:
        await message.reply_text("❌ Données insuffisantes.", reply_markup=main_menu())
        return
    h, d, a = model["final"]
    m = model["markets"]
    datasets = {
        "1X2": [("1", h), ("X", d), ("2", a)],
        "DC": [("1X", h + d), ("X2", d + a), ("12", h + a)],
        "BTTS": [("Oui", m["btts"]), ("Non", 100 - m["btts"])],
        "OU": [("Over 1.5", m["over15"]), ("Over 2.5", m["over25"]), ("Over 3.5", m["over35"]), ("Under 2.5", m["under25"]), ("Under 3.5", m["under35"])]
    }
    title = {"1X2": "1X2", "DC": "DOUBLE CHANCE", "BTTS": "BTTS", "OU": "OVER / UNDER"}[market]
    img = render_screen(title, "Choisis une sélection à simuler", [{"kind": "bars", "heading": "PROBABILITÉS", "height": 300, "rows": datasets[market]}])
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    out.seek(0)
    await message.reply_photo(photo=out.getvalue(), caption="⚡ <b>SPORT ANALYZER • V13.3</b>\nChoix du marché", parse_mode="HTML", reply_markup=market_keyboard(fid, market))


async def send_pick_stake_menu(message, fid, market, selection):
    data, error = await full_analysis(fid)
    if error:
        await message.reply_text(f"❌ {error}", reply_markup=main_menu())
        return
    model = data.get("model")
    if not model:
        await message.reply_text("❌ Données insuffisantes.", reply_markup=main_menu())
        return
    h, d, a = model["final"]
    m = model["markets"]
    vals = {
        "1": ("1", h), "X": ("X", d), "2": ("2", a),
        "1X": ("1X", h + d), "X2": ("X2", d + a), "12": ("12", h + a),
        "BTTSY": ("BTTS Oui", m["btts"]), "BTTSN": ("BTTS Non", 100 - m["btts"]),
        "O15": ("Over 1.5", m["over15"]), "O25": ("Over 2.5", m["over25"]), "O35": ("Over 3.5", m["over35"]),
        "U25": ("Under 2.5", m["under25"]), "U35": ("Under 3.5", m["under35"])
    }
    label, p = vals.get(selection, ("Sélection", 0))
    if p <= 0:
        await message.reply_text("❌ Probabilité indisponible.", reply_markup=main_menu())
        return
    fair = 100 / p
    img = render_screen("SIMULATION", "Sélection et cote juste théorique", [
        {"kind": "card", "heading": "SÉLECTION", "height": 220, "rows": [("Choix", label), ("Probabilité modèle", f"{p:.1f}%"), ("Cote juste", f"{fair:.2f}")]},
        {"kind": "card", "heading": "MISE THÉORIQUE", "height": 160, "rows": [("5 €", "simulation"), ("10 €", "simulation"), ("20 €", "simulation"), ("50 €", "simulation")]}
    ])
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    out.seek(0)
    await message.reply_photo(photo=out.getvalue(), caption="⚡ <b>SPORT ANALYZER • V13.3</b>\nSimulation", parse_mode="HTML", reply_markup=stake_keyboard(fid, market, selection))


async def send_stake_result(message, fid, market, selection, stake):
    data, error = await full_analysis(fid)
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
        "1X": h + d, "X2": d + a, "12": h + a,
        "BTTSY": m["btts"], "BTTSN": 100 - m["btts"],
        "O15": m["over15"], "O25": m["over25"], "O35": m["over35"],
        "U25": m["under25"], "U35": m["under35"]
    }
    p = float(probs.get(selection, 0))
    if p <= 0:
        await message.reply_text("❌ Probabilité indisponible.", reply_markup=main_menu())
        return
    fair = 100 / p
    ret = float(stake) * fair
    profit = ret - float(stake)
    img = render_screen("SIMULATION", "Résultat théorique • aucune mise réelle engagée", [
        {"kind": "card", "heading": "RÉSULTAT", "height": 320, "rows": [
            ("Sélection", selection), ("Probabilité", f"{p:.1f}%"), ("Mise", f"{float(stake):.2f} €"),
            ("Cote juste", f"{fair:.2f}"), ("Retour théorique", f"{ret:.2f} €"), ("Profit théorique", f"{profit:+.2f} €")
        ]}
    ])
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    out.seek(0)
    await message.reply_photo(photo=out.getvalue(), caption="⚡ <b>SPORT ANALYZER • V13.3</b>\nSimulation terminée", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔬 Autre marché", callback_data=f"sim:{fid}"), InlineKeyboardButton("🔎 Analyse", callback_data=f"analyse:{fid}")], [InlineKeyboardButton("🏠 Menu principal", callback_data="menu:home")]]))


async def send_performance_result(message, user_id):
    await validate_predictions()
    perf = db_performance(user_id)
    rows = [("Prédictions validées", perf["total"]), ("Correctes", perf["wins"]), ("Taux de réussite", f"{perf['accuracy']:.1f}%" if perf["accuracy"] is not None else "N/D")]
    for k, v in sorted(perf["markets"].items()):
        rows.append((k, f"{v['wins']}/{v['total']}  •  {v['wins']/v['total']*100:.1f}%"))
    img = render_screen("PERFORMANCE", "Résultats calculés sur les prédictions validées", [{"kind": "card", "heading": "TABLEAU DE BORD", "height": 560, "rows": rows}])
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    out.seek(0)
    await message.reply_photo(photo=out.getvalue(), caption="⚡ <b>SPORT ANALYZER • V13.3</b>\nPerformance", parse_mode="HTML", reply_markup=main_menu())


async def send_validation_result(message, user_id):
    checked = await validate_predictions()
    perf = db_performance(user_id)
    text = f"🔄 <b>VALIDATION</b>\n\nPrédictions nouvellement validées : {checked}\n"
    text += f"Prédictions terminées : {perf['total']}\nTaux de réussite : {perf['accuracy']:.1f}%" if perf["accuracy"] is not None else "Aucune prédiction terminée dans ton historique."
    await message.reply_text(text, parse_mode="HTML", reply_markup=main_menu())


async def send_bankroll_result(message, user_id):
    total, profit, count, wins, roi = db_summary(user_id)
    img = render_screen("BANKROLL", "Suivi des mises enregistrées", [
        {"kind": "card", "heading": "SUIVI FINANCIER", "height": 300, "rows": [("Mises enregistrées", f"{total:.2f} €"), ("Profit / perte", f"{profit:+.2f} €"), ("Paris", count), ("Paris gagnants", wins), ("ROI", f"{roi:+.2f}%")]},
        {"kind": "card", "heading": "NOTE", "height": 130, "rows": [("Comptabilité", "Les mises doivent être saisies par l'utilisateur")]}
    ])
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    out.seek(0)
    await message.reply_photo(photo=out.getvalue(), caption="⚡ <b>SPORT ANALYZER • V13.3</b>\nBankroll", parse_mode="HTML", reply_markup=main_menu())


async def send_status_result(message):
    async with httpx.AsyncClient(timeout=7) as client:
        fd_ok = False
        detail = "Clé absente"
        if FOOTBALL_DATA_KEY:
            data, error = await find_fd_match(client, 1)  # simple request check or today matches
            from api_client import fd_get
            data, error = await fd_get(client, "/matches", {"date": today_paris()}, "fd:status", 30)
            fd_ok = data is not None and error is None
            detail = "OK" if fd_ok else str(error)
        ts_data, ts_error = await tsdb_get(client, "eventsday.php", {"d": today_paris(), "s": "Soccer"}, "tsdb:status", 30)
        ts_ok = ts_data is not None and ts_error is None
    img = render_screen("ÉTAT DES SOURCES", "Contrôle rapide des fournisseurs de données", [
        {"kind": "card", "heading": "SOURCES", "height": 230, "rows": [("Football-Data.org", "OK" if fd_ok else "Indisponible", (28, 221, 92) if fd_ok else (255, 67, 67)), ("TheSportsDB", "OK" if ts_ok else "Indisponible", (28, 221, 92) if ts_ok else (255, 67, 67)), ("API-Football", "désactivée dans V13.3", (181, 199, 222))]},
        {"kind": "card", "heading": "DÉTAIL", "height": 170, "rows": [("Football-Data.org", detail[:90])]}
    ])
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    out.seek(0)
    await message.reply_photo(photo=out.getvalue(), caption="⚡ <b>SPORT ANALYZER • V13.3</b>\nÉtat des sources", parse_mode="HTML", reply_markup=main_menu())


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
            img = render_screen("MENU PRINCIPAL", "Football intelligence • données dynamiques", [
                {"kind": "card", "heading": "MODULES", "height": 380, "rows": [("Matchs du jour", "Rencontres disponibles"), ("Analyse", "Dashboard complet"), ("Probabilités", "1X2 • double chance"), ("Buts", "BTTS • Over/Under"), ("Cotes", "Sources et saisie manuelle"), ("Buteurs", "Événements disponibles"), ("Simulateur", "Mises théoriques")]},
                {"kind": "card", "heading": "SUIVI", "height": 150, "rows": [("Performance", "Validation des prédictions"), ("Bankroll", "Mises et ROI")]}
            ])
            out = io.BytesIO()
            img.save(out, format="PNG", optimize=True)
            out.seek(0)
            await message.reply_photo(photo=out.getvalue(), caption="⚡ <b>SPORT ANALYZER • V13.3</b>\nMenu principal", parse_mode="HTML", reply_markup=main_menu())
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
            img = render_screen("SIMULATEUR", "Les rendements affichés sont théoriques", [{"kind": "card", "heading": "UTILISATION", "height": 220, "rows": [("1", "Ouvre un match"), ("2", "Choisis un marché"), ("3", "Choisis une mise théorique")]}])
            out = io.BytesIO()
            img.save(out, format="PNG", optimize=True)
            out.seek(0)
            await message.reply_photo(photo=out.getvalue(), caption="⚡ <b>SPORT ANALYZER • V13.3</b>\nSimulateur", parse_mode="HTML", reply_markup=main_menu())
            return
        if data == "menu:tools":
            img = render_screen("OUTILS", "Suivi, validation et sources", [{"kind": "card", "heading": "OUTILS", "height": 260, "rows": [("Performance", "Taux de réussite"), ("Validation", "Résultats terminés"), ("Bankroll", "Mises et ROI"), ("Sources", "État des APIs")]}])
            out = io.BytesIO()
            img.save(out, format="PNG", optimize=True)
            out.seek(0)
            await message.reply_photo(photo=out.getvalue(), caption="⚡ <b>SPORT ANALYZER • V13.3</b>\nOutils", parse_mode="HTML", reply_markup=tools_keyboard())
            return
        if data == "menu:help":
            img = render_screen("AIDE RAPIDE", "Commandes et fonctionnement", [{"kind": "card", "heading": "COMMANDES", "height": 430, "rows": [("/match", "Matchs du jour"), ("/analyse ID", "Dashboard"), ("/probabilite ID", "Probabilités"), ("/buts ID", "Marchés de buts"), ("/buteur ID", "Buteurs / événements"), ("/mise ...", "Enregistrer une mise"), ("/performance", "Performance"), ("/validation", "Validation")]}])
            out = io.BytesIO()
            img.save(out, format="PNG", optimize=True)
            out.seek(0)
            await message.reply_photo(photo=out.getvalue(), caption="⚡ <b>SPORT ANALYZER • V13.3</b>\nAide", parse_mode="HTML", reply_markup=main_menu())
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
        await message.reply_text(f"⚠️ Action inconnue : {action}", reply_markup=main_menu())
    except Exception as exc:
        print(f"❌ CALLBACK ERROR [{data}] : {exc}", flush=True)
        try:
            await message.reply_text("❌ <b>Une erreur est survenue.</b>\n\nUtilise 🏠 Accueil pour continuer.", parse_mode="HTML", reply_markup=main_menu())
        except Exception as reply_error:
            print(f"❌ Reply error: {reply_error}", flush=True)


async def start(update, context):
    await update.message.reply_text("⚡ <b>SPORT ANALYZER • V13.3</b>\n╭────────────────────────╮\n│ ⚽ <b>FOOTBALL INTELLIGENCE</b>\n│ 🎯 Probabilités  •  ⚽ Buts\n│ 💰 Cotes  •  🔬 Simulation\n╰────────────────────────╯\n\n👇 <b>CHOISIS TON MODULE</b>", parse_mode="HTML", reply_markup=main_menu())


async def help_command(update, context):
    await update.message.reply_text("📊 <b>SPORT ANALYZER V13.3</b>\n\n/match\n/analyse ID\n/buts ID\n/probabilite ID\n/buteur ID\n/cotes ID\n/mise ID marché sélection montant [cote]\n/resultat ID_BET win|loss|void\n/bankroll\n/statusapi\n/performance\n/validation", parse_mode="HTML")


async def match_command(update, context):
    await send_match_results(update.message)


async def analyse_command(update, context):
    if not context.args:
        await update.message.reply_text("Utilisation : /analyse ID")
        return
    if str(context.args[0]).startswith("TSDB-"):
        await update.message.reply_text("ℹ️ Utilise un ID Football-Data.org pour le dashboard complet.")
        return
    await update.message.reply_text("🔎 Génération du dashboard…")
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
    print("✅ SPORT ANALYZER V13.3 démarré", flush=True)
    try:
        yield
    finally:
        await telegram_app.stop()
        await telegram_app.shutdown()


routes = [Route("/health", health, methods=["GET"]), Route("/telegram", webhook, methods=["POST"])]
app = Starlette(routes=routes, lifespan=lifespan)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
