import os
from PIL import Image, ImageDraw, ImageFont
from analytics import clamp, poisson_matrix, likely_scores
from api_client import fd_dt, fd_status, match_names, today_paris

FONT_CANDIDATES_REG = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/truetype/lato/Lato-Regular.ttf"
]
FONT_CANDIDATES_MED = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/truetype/lato/Lato-Medium.ttf"
]
FONT_CANDIDATES_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/lato/Lato-Bold.ttf"
]
FONT_CANDIDATES_ITALIC = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansOblique.ttf",
    "/usr/share/fonts/truetype/lato/Lato-Italic.ttf"
]


def find_font_path(candidates):
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


FONT_REG_PATH = find_font_path(FONT_CANDIDATES_REG)
FONT_MED_PATH = find_font_path(FONT_CANDIDATES_MED)
FONT_BOLD_PATH = find_font_path(FONT_CANDIDATES_BOLD)
FONT_ITALIC_PATH = find_font_path(FONT_CANDIDATES_ITALIC)


def font(kind, size):
    path_map = {
        "reg": FONT_REG_PATH,
        "med": FONT_MED_PATH,
        "bold": FONT_BOLD_PATH,
        "italic": FONT_ITALIC_PATH
    }
    font_path = path_map.get(kind)
    if font_path:
        try:
            return ImageFont.truetype(font_path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def rounded(d, box, fill, outline=None, radius=22, width=2):
    d.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def background(w, h):
    img = Image.new("RGB", (w, h), (4, 13, 29))
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        r = int(4 + 4 * t)
        g = int(11 + 8 * t)
        b = int(27 + 20 * t)
        for x in range(w):
            glow = int(10 * max(0, 1 - abs(x - w * .52) / (w * .68)))
            px[x, y] = (r, min(48, g + glow // 3), min(62, b + glow))
    return img


def pattern(d, w, h):
    for y in range(35, h, 115):
        for x in range(35, w, 130):
            d.ellipse((x - 15, y - 15, x + 15, y + 15), outline=(17, 52, 82), width=2)
            d.line((x - 8, y, x + 8, y), fill=(13, 45, 74), width=2)
            d.line((x, y - 8, x, y + 8), fill=(13, 45, 74), width=2)


def bar(d, x, y, w, h, value, color):
    rounded(d, (x, y, x + w, y + h), (25, 47, 75), radius=h // 2)
    fw = int(w * clamp(value) / 100)
    if fw > 0:
        rounded(d, (x, y, x + fw, y + h), color, radius=h // 2)


def bar_color(v):
    return (27, 235, 82) if v >= 60 else (255, 203, 22) if v >= 30 else (255, 67, 67)


def text_fit(d, text, fnt, max_width):
    text = str(text)
    if d.textlength(text, font=fnt) <= max_width:
        return text
    while len(text) > 4 and d.textlength(text + "…", font=fnt) > max_width:
        text = text[:-1]
    return text + "…"


def draw_header(d):
    white = (244, 247, 255)
    muted = (181, 199, 222)
    cyan = (0, 218, 255)
    rounded(d, (38, 28, 986, 142), (5, 24, 46), outline=(35, 190, 235), radius=50, width=3)
    d.ellipse((62, 49, 128, 115), outline=cyan, width=4, fill=(8, 43, 70))
    d.text((83, 62), "S", font=font("bold", 30), fill=white)
    d.text((150, 50), "Sport Analyzer", font=font("bold", 31), fill=white)
    d.text((151, 88), "bot", font=font("reg", 20), fill=muted)
    d.text((680, 57), "DATA", font=font("bold", 22), fill=cyan)
    d.multiline_text((758, 50), "Des données\ndes analyses\nd'opportunités", font=font("med", 16), fill=white, spacing=2)


def draw_title(d, title, subtitle=""):
    white = (244, 247, 255)
    muted = (181, 199, 222)
    cyan = (0, 218, 255)
    panel = (7, 25, 47)
    rounded(d, (38, 164, 986, 276), panel, outline=cyan, radius=25, width=2)
    d.text((66, 184), text_fit(d, title, font("bold", 34), 880), font=font("bold", 34), fill=white)
    if subtitle:
        d.text((66, 228), text_fit(d, subtitle, font("med", 20), 880), font=font("med", 20), fill=muted)


def render_dashboard(data):
    W, H = 1024, 1536
    img = background(W, H)
    d = ImageDraw.Draw(img)
    pattern(d, W, H)
    white = (244, 247, 255)
    cyan = (0, 218, 255)
    muted = (194, 211, 232)
    panel = (7, 25, 47)
    match = data.get("match", {})
    home = match.get("homeTeam", {}).get("name", "Domicile")
    away = match.get("awayTeam", {}).get("name", "Extérieur")
    comp = match.get("competition", {}).get("name", "Football")
    dt = fd_dt(match)
    status = fd_status(match.get("status"))
    model = data.get("model") or {
        "final": (33.3, 33.4, 33.3),
        "markets": {"btts": 50, "over15": 50, "over25": 50, "over35": 50, "under25": 50, "under35": 50},
        "home_xg": 1,
        "away_xg": 1,
        "matrix": poisson_matrix(1, 1)
    }
    sh = data.get("standings_home") or {}
    sa = data.get("standings_away") or {}
    hf = data.get("home_form") or []
    af = data.get("away_form") or []
    draw_header(d)

    rounded(d, (38, 292, 986, 505), panel, outline=cyan, radius=28, width=3)
    d.text((66, 316), comp.upper()[:34], font=font("bold", 25), fill=white)
    d.text((66, 354), f"Journée  •  {dt.strftime('%d/%m/%Y  •  %H:%M') if dt else 'Horaire N/D'}", font=font("med", 20), fill=muted)
    d.text((735, 318), f"ID {match.get('id','N/D')}", font=font("bold", 20), fill=white)
    rounded(d, (766, 355, 958, 403), (8, 55, 53), outline=(0, 239, 151), radius=22, width=2)
    d.text((791, 367), f"{status}", font=font("bold", 18), fill=(0, 239, 151))

    d.ellipse((70, 398, 146, 474), fill=(20, 75, 116), outline=cyan, width=3)
    d.text((93, 413), home[:1].upper() or "?", font=font("bold", 32), fill=white)
    d.text((164, 399), text_fit(d, home, font("bold", 25), 300), font=font("bold", 25), fill=white)
    d.text((164, 436), f"{sh.get('position','N/D')}e  •  {sh.get('points','N/D')} pts  •  {sh.get('gf','N/D')}-{sh.get('ga','N/D')}", font=font("med", 18), fill=muted)
    d.text((466, 405), "VS", font=font("bold", 28), fill=white)
    d.ellipse((878, 398, 954, 474), fill=(20, 75, 116), outline=cyan, width=3)
    d.text((901, 413), away[:1].upper() or "?", font=font("bold", 32), fill=white)
    d.text((590, 399), text_fit(d, away, font("bold", 24), 275), font=font("bold", 24), fill=white)
    d.text((590, 436), f"{sa.get('position','N/D')}e  •  {sa.get('points','N/D')} pts  •  {sa.get('gf','N/D')}-{sa.get('ga','N/D')}", font=font("med", 18), fill=muted)
    d.text((164, 470), "Forme : " + ("  ".join(x.get("result", "?") for x in hf[-5:]) or "N/D"), font=font("bold", 18), fill=white)
    d.text((590, 470), "Forme : " + ("  ".join(x.get("result", "?") for x in af[-5:]) or "N/D"), font=font("bold", 18), fill=white)

    tabs = [(38, "Vue d'ensemble"), (278, "Statistiques"), (518, "Face à face"), (758, "Compositions")]
    for i, (x, lab) in enumerate(tabs):
        rounded(d, (x, 525, x + 228, 588), (35, 69, 126) if i == 0 else (10, 34, 60), outline=(70, 181, 255) if i == 0 else (49, 86, 124), radius=18, width=2)
        f = font("bold", 18 if i == 0 else 17)
        d.text((x + 18, 546), text_fit(d, lab, f, 192), font=f, fill=white)

    # Probabilities
    x1, y1, x2, y2 = (38, 610, 505, 1012)
    rounded(d, (x1, y1, x2, y2), (8, 26, 49), outline=(47, 130, 184), radius=26, width=3)
    d.text((64, 637), "PROBABILITÉS DU MODÈLE", font=font("bold", 25), fill=white)
    h, dr, a = model["final"]
    yy = 705
    for i, (lab, v) in enumerate([("1", h), ("X", dr), ("2", a), ("1X", h + dr), ("X2", dr + a), ("12", h + a)]):
        if i == 3:
            d.line((64, yy - 16, 479, yy - 16), fill=(62, 102, 137), width=2)
            yy += 12
        rounded(d, (64, yy, 108, yy + 40), (34, 105, 169), radius=8)
        d.text((76, yy + 5), lab, font=font("bold", 22), fill=white)
        c = (0, 218, 255) if lab == "12" else bar_color(v)
        bar(d, 128, yy + 6, 270, 28, v, c)
        d.text((410, yy + 2), f"{v:.1f}%", font=font("bold", 21), fill=c)
        yy += 54

    # Goal markets
    x1, y1, x2, y2 = (519, 610, 986, 1012)
    rounded(d, (x1, y1, x2, y2), (8, 26, 49), outline=(47, 130, 184), radius=26, width=3)
    d.text((545, 637), "MARCHÉS DE BUTS", font=font("bold", 25), fill=white)
    m = model["markets"]
    yy = 704
    for lab, v in [("BTTS Oui", m["btts"]), ("BTTS Non", 100 - m["btts"]), ("Over 1.5", m["over15"]), ("Over 2.5", m["over25"]), ("Over 3.5", m["over35"]), ("Under 2.5", m["under25"]), ("Under 3.5", m["under35"])]:
        c = bar_color(v)
        d.text((545, yy), lab, font=font("med", 18), fill=white)
        bar(d, 670, yy + 2, 210, 26, v, c)
        d.text((895, yy), f"{v:.1f}%", font=font("bold", 18), fill=c)
        yy += 39
    d.text((545, 955), f"xG estimé : {model['home_xg']:.2f}  •  {model['away_xg']:.2f}", font=font("bold", 19), fill=white)

    # Scores + intelligence
    rounded(d, (38, 1032, 505, 1254), panel, outline=(47, 130, 184), radius=25, width=3)
    d.text((64, 1058), "SCORES LES PLUS PROBABLES", font=font("bold", 22), fill=white)
    yy = 1108
    for hh, aa, p in likely_scores(model["matrix"], 3):
        d.text((70, yy), f"{hh}-{aa}", font=font("bold", 21), fill=white)
        bar(d, 155, yy + 2, 220, 25, p, (0, 193, 239))
        d.text((392, yy), f"{p:.1f}%", font=font("bold", 19), fill=white)
        yy += 47

    rounded(d, (519, 1032, 986, 1254), panel, outline=(47, 130, 184), radius=25, width=3)
    d.text((545, 1058), "INTELLIGENCE DU MATCH", font=font("bold", 22), fill=white)
    spread = max(model["final"]) - min(model["final"])
    conf = "Élevée" if max(model["final"]) >= 60 else "Moyenne" if max(model["final"]) >= 40 else "Faible"
    trend = "Équilibré" if spread < 20 else "Tendance marquée"
    rel = "Bonne" if data.get("quality", 0) >= .65 else "Moyenne" if data.get("quality", 0) >= .4 else "Faible"
    yy = 1106
    for lab, val, c in [("Confiance", conf, (255, 205, 25)), ("Tendance", trend, (115, 154, 210)), ("Fiabilité", rel, (28, 221, 92))]:
        d.text((545, yy), lab, font=font("med", 18), fill=white)
        rounded(d, (758, yy - 5, 958, yy + 34), c, radius=18)
        f = font("bold", 16)
        d.text((778, yy + 5), text_fit(d, val, f, 165), font=f, fill=(8, 18, 30))
        yy += 51
    d.text((545, 1218), "Données disponibles • modèle statistique", font=font("reg", 15), fill=muted)

    buttons = [(38, 1278, 344, 1343, "Analyse"), (352, 1278, 658, 1343, "Probabilités"), (666, 1278, 986, 1343, "Buts"),
               (38, 1352, 344, 1417, "Cotes"), (352, 1352, 658, 1417, "Buteurs"), (666, 1352, 986, 1417, "Simuler"),
               (38, 1427, 986, 1486, "Menu principal")]
    for bx1, by1, bx2, by2, lab in buttons:
        rounded(d, (bx1, by1, bx2, by2), (23, 66, 126), outline=cyan, radius=17, width=2)
        f = font("bold", 20)
        tw = d.textlength(lab, font=f)
        d.text(((bx1 + bx2 - tw) / 2, by1 + 20), lab, font=f, fill=white)
    d.text((48, 1510), "Analyse aujourd'hui, de meilleures décisions demain.", font=font("italic", 14), fill=muted)
    d.text((820, 1510), "Sport Analyzer V13.3", font=font("italic", 14), fill=muted)
    return img


def render_screen(title, subtitle="", sections=None, accent=(0, 218, 255), footer="Sport Analyzer V13.3"):
    W, H = 1024, 1536
    img = background(W, H)
    d = ImageDraw.Draw(img)
    pattern(d, W, H)
    white = (244, 247, 255)
    muted = (181, 199, 222)
    panel = (7, 25, 47)
    draw_header(d)
    draw_title(d, title, subtitle)
    y = 300
    sections = sections or []
    for sec in sections:
        kind = sec.get("kind", "card")
        height = int(sec.get("height", 180))
        if y + height > 1420:
            height = max(100, 1420 - y)
        rounded(d, (38, y, 986, y + height), panel, outline=(47, 130, 184), radius=26, width=3)
        heading = sec.get("heading", "")
        if heading:
            d.text((66, y + 24), heading, font=font("bold", 26), fill=white)
        yy = y + 78
        if kind == "bars":
            for label, val in sec.get("rows", []):
                c = bar_color(val)
                d.text((66, yy), text_fit(d, label, font("med", 20), 250), font=font("med", 20), fill=white)
                bar(d, 320, yy + 2, 455, 28, val, c)
                d.text((798, yy), f"{float(val):.1f}%", font=font("bold", 20), fill=c)
                yy += 48
        else:
            rows = sec.get("rows", [])
            for row in rows:
                label = str(row[0])
                value = str(row[1]) if len(row) > 1 else ""
                color = row[2] if len(row) > 2 else white
                d.text((66, yy), text_fit(d, label, font("med", 20), 430), font=font("med", 20), fill=white)
                if len(row) > 3 and isinstance(row[3], (int, float)):
                    val = float(row[3])
                    c = color if isinstance(color, tuple) else bar_color(val)
                    bar(d, 520, yy + 2, 280, 28, val, c)
                    d.text((820, yy), f"{val:.1f}%", font=font("bold", 20), fill=c)
                else:
                    d.text((520, yy), text_fit(d, value, font("bold", 20), 390), font=font("bold", 20), fill=color)
                yy += 48
        y += int(sec.get("height", 180)) + 22
        if y >= 1410:
            break
    d.text((48, 1490), "Analyse aujourd'hui, de meilleures décisions demain.", font=font("italic", 14), fill=muted)
    d.text((815, 1490), footer, font=font("italic", 14), fill=muted)
    return img


def render_match_list(matches, source):
    rows = []
    for item in matches[:8]:
        home, away = match_names(item)
        dt = fd_dt(item)
        rows.append((f"{dt.strftime('%H:%M') if dt else '--:--'}  {home}", away))
    return render_screen("MATCHS DU JOUR", f"{today_paris()}  •  {source}  •  {len(matches)} affiché(s)", [
        {"kind": "card", "heading": "RENCONTRES DISPONIBLES", "height": 620, "rows": rows or [("Aucun match", "Aucune rencontre disponible")]}
    ])


def render_match_selected(item, fid):
    home, away = match_names(item)
    comp = item.get("competition", {}).get("name", "Football")
    dt = fd_dt(item)
    rows = [("Compétition", comp), ("Date", dt.strftime('%d/%m/%Y %H:%M') if dt else "N/D"), ("Identifiant", fid), ("Domicile", home), ("Extérieur", away)]
    return render_screen("MATCH SÉLECTIONNÉ", "Détail du match et modules disponibles", [
        {"kind": "card", "heading": "FICHE MATCH", "height": 340, "rows": rows},
        {"kind": "card", "heading": "MODULES DISPONIBLES", "height": 270, "rows": [("Analyse", "Dashboard complet"), ("Probabilités", "1X2 et double chance"), ("Buts", "BTTS et Over / Under"), ("Cotes", "Données disponibles selon la source"), ("Buteurs", "Informations disponibles selon la source")]}
    ])
