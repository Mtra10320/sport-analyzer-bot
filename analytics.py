import math

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


def parse_form(matches, team_id):
    rows = []
    for item in matches or []:
        hid = item.get("homeTeam", {}).get("id")
        aid = item.get("awayTeam", {}).get("id")
        score = item.get("score", {})
        full = score.get("fullTime", {}) if isinstance(score, dict) else {}
        hg, ag = full.get("home"), full.get("away")
        if hg is None or ag is None or item.get("status") not in {"FINISHED", "AWARDED"}:
            continue
        if str(team_id) == str(hid):
            gf, ga = hg, ag
            opponent = item.get("awayTeam", {}).get("name", "?")
        elif str(team_id) == str(aid):
            gf, ga = ag, hg
            opponent = item.get("homeTeam", {}).get("name", "?")
        else:
            continue
        rows.append({"gf": gf, "ga": ga, "result": "V" if gf > ga else "N" if gf == ga else "D", "opponent": opponent})
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
    return {"matches": n, "gf_avg": gf/n, "ga_avg": ga/n, "wins": wins, "draws": draws, "losses": losses, "points_avg": (wins*3+draws)/n}


def standings_metrics(standings, team_id):
    for table in standings or []:
        for row in table.get("table", []):
            if str(row.get("team", {}).get("id")) == str(team_id):
                return {
                    "position": row.get("position"),
                    "played": row.get("playedGames"),
                    "won": row.get("won"),
                    "draw": row.get("draw"),
                    "lost": row.get("lost"),
                    "gf": row.get("goalsFor"),
                    "ga": row.get("goalsAgainst"),
                    "points": row.get("points")
                }
    return {}


def build_model(home_form, away_form):
    hf = form_metrics(home_form)
    af = form_metrics(away_form)
    if not hf or not af:
        return None
    home_xg = clamp((hf["gf_avg"] + af["ga_avg"]) / 2.0 * 1.08, 0.15, 4.0)
    away_xg = clamp((af["gf_avg"] + hf["ga_avg"]) / 2.0, 0.10, 4.0)
    matrix = poisson_matrix(home_xg, away_xg)
    markets = poisson_markets(matrix)
    return {
        "home_xg": home_xg,
        "away_xg": away_xg,
        "matrix": matrix,
        "markets": markets,
        "final": normalize3(markets["home"], markets["draw"], markets["away"]),
        "form_home": hf,
        "form_away": af
    }
