import sqlite3
import os
from datetime import datetime, timezone

DB_PATH = os.getenv("DB_PATH", "sport_analyzer.db")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def init_db(db_path=None):
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
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
    conn.commit()
    conn.close()


def db_add_bet(user_id, fixture_id, market, selection, stake, odds=None, probability=None, db_path=None):
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    cur = conn.execute("""INSERT INTO bets
        (user_id, fixture_id, market, selection, stake, odds, probability, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, str(fixture_id), market, selection, stake, odds, probability, now_iso()))
    bet_id = cur.lastrowid
    conn.commit()
    conn.close()
    return bet_id


def db_settle_bet(user_id, bet_id, result, db_path=None):
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    row = conn.execute("SELECT stake, odds, result FROM bets WHERE id=? AND user_id=?", (bet_id, user_id)).fetchone()
    if not row or row[2] is not None:
        conn.close()
        return None
    stake, odds, _ = row
    profit = stake * ((odds or 1.0) - 1.0) if result == "win" else -stake if result == "loss" else 0.0
    conn.execute("UPDATE bets SET result=?, profit=?, settled_at=? WHERE id=? AND user_id=?",
                 (result, profit, now_iso(), bet_id, user_id))
    conn.commit()
    conn.close()
    return profit


def db_summary(user_id, db_path=None):
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    rows = conn.execute("SELECT stake, result, profit FROM bets WHERE user_id=?", (user_id,)).fetchall()
    conn.close()
    total = sum(r[0] or 0 for r in rows)
    profit = sum(r[2] or 0 for r in rows)
    settled = [r for r in rows if r[1] in {"win", "loss", "void"}]
    wins = sum(r[1] == "win" for r in settled)
    return total, profit, len(rows), wins, profit / total * 100.0 if total else 0.0


def db_add_prediction(user_id, fixture_id, home_team, away_team, market, selection, probability, db_path=None):
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    cur = conn.execute("""INSERT INTO predictions
        (user_id, fixture_id, home_team, away_team, market, selection, probability, predicted_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, str(fixture_id), home_team, away_team, market, selection, probability, now_iso()))
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return pid


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
    return "win" if str(selection).lower() == actual.lower() else "loss"


def db_performance(user_id, db_path=None):
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    rows = conn.execute("SELECT market, probability, outcome FROM predictions WHERE user_id=? AND outcome IS NOT NULL", (user_id,)).fetchall()
    conn.close()
    by_market = {}
    for market, probability, outcome in rows:
        d = by_market.setdefault(market, {"total": 0, "wins": 0, "prob_sum": 0.0, "prob_n": 0})
        d["total"] += 1
        d["wins"] += outcome == "win"
        if probability is not None:
            d["prob_sum"] += float(probability)
            d["prob_n"] += 1
    total = len(rows)
    wins = sum(o == "win" for _, _, o in rows)
    return {"total": total, "wins": wins, "accuracy": wins / total * 100.0 if total else None, "markets": by_market}
