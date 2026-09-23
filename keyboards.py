from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu():
    """
    INTERFACE PRINCIPALE - GRILLE 2 COLONNES
    ⚽ MATCHS DU JOUR | 🎯 ANALYSE
    📊 PERFORMANCE   | 💰 BANKROLL
    🔬 SIMULATEUR    | 🔄 VALIDATION
    🔌 SOURCES       | ❓ AIDE
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚽ MATCHS DU JOUR", callback_data="menu:match"), InlineKeyboardButton("🎯 ANALYSE", callback_data="menu:tools")],
        [InlineKeyboardButton("📊 PERFORMANCE", callback_data="menu:performance"), InlineKeyboardButton("💰 BANKROLL", callback_data="menu:bankroll")],
        [InlineKeyboardButton("🔬 SIMULATEUR", callback_data="menu:sim"), InlineKeyboardButton("🔄 VALIDATION", callback_data="menu:validation")],
        [InlineKeyboardButton("🔌 SOURCES", callback_data="menu:status"), InlineKeyboardButton("❓ AIDE", callback_data="menu:help")]
    ])


def match_list_keyboard(matches):
    """
    LISTE DES MATCHS - UN BOUTON PAR MATCH
    """
    rows = []
    for item in matches[:15]:
        fid = item.get("id")
        home = item.get("homeTeam", {}).get("name", "Inconnu")
        away = item.get("awayTeam", {}).get("name", "Inconnu")
        label = f"🔎 {home[:13]} - {away[:13]}"
        rows.append([InlineKeyboardButton(label, callback_data=f"match:{fid}")])
    rows.append([InlineKeyboardButton("🔄 ACTUALISER", callback_data="menu:match"), InlineKeyboardButton("🏠 ACCUEIL", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


def match_keyboard(fid):
    """
    MATCH SÉLECTIONNÉ - GRILLE 2 COLONNES
    🔎 ANALYSE   | 🎯 PROBABILITÉS
    ⚽ BUTS      | 💰 COTES
    ⚽ BUTEURS   | 🔬 SIMULER
    ⬅️ MATCHS    | 🏠 ACCUEIL
    """
    fid = str(fid)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 ANALYSE", callback_data=f"analyse:{fid}"), InlineKeyboardButton("🎯 PROBABILITÉS", callback_data=f"prob:{fid}")],
        [InlineKeyboardButton("⚽ BUTS", callback_data=f"buts:{fid}"), InlineKeyboardButton("💰 COTES", callback_data=f"cotes:{fid}")],
        [InlineKeyboardButton("⚽ BUTEURS", callback_data=f"buteur:{fid}"), InlineKeyboardButton("🔬 SIMULER", callback_data=f"sim:{fid}")],
        [InlineKeyboardButton("⬅️ MATCHS", callback_data="menu:match"), InlineKeyboardButton("🏠 ACCUEIL", callback_data="menu:home")]
    ])


def analysis_menu_keyboard(fid):
    """
    SOUS-MENU ANALYSE - GRILLE 2 COLONNES
    🎯 1X2       | 🔄 Double chance
    ⚽ Buts      | 📊 Statistiques
    ⚽ Buteurs   | 💰 Cotes
    🔬 Simulateur| ⬅️ RETOUR
    🏠 ACCUEIL
    """
    fid = str(fid)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 1X2", callback_data=f"prob:{fid}"), InlineKeyboardButton("🔄 Double chance", callback_data=f"prob:{fid}")],
        [InlineKeyboardButton("⚽ Marchés de buts", callback_data=f"buts:{fid}"), InlineKeyboardButton("📊 Statistiques", callback_data=f"stats:{fid}")],
        [InlineKeyboardButton("⚽ Buteurs", callback_data=f"buteur:{fid}"), InlineKeyboardButton("💰 Cotes", callback_data=f"cotes:{fid}")],
        [InlineKeyboardButton("🔬 Simulateur", callback_data=f"sim:{fid}"), InlineKeyboardButton("⬅️ RETOUR", callback_data=f"match:{fid}")],
        [InlineKeyboardButton("🏠 ACCUEIL", callback_data="menu:home")]
    ])


def simulator_keyboard(fid):
    """
    SIMULATEUR - GRILLE 2 COLONNES
    🎯 1X2       | 🔄 Double chance
    ⚽ BTTS      | 📈 Over / Under
    ⬅️ RETOUR   | 🏠 ACCUEIL
    """
    fid = str(fid)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 1X2", callback_data=f"market:{fid}:1X2"), InlineKeyboardButton("🔄 Double chance", callback_data=f"market:{fid}:DC")],
        [InlineKeyboardButton("⚽ BTTS", callback_data=f"market:{fid}:BTTS"), InlineKeyboardButton("📈 Over / Under", callback_data=f"market:{fid}:OU")],
        [InlineKeyboardButton("⬅️ RETOUR", callback_data=f"match:{fid}"), InlineKeyboardButton("🏠 ACCUEIL", callback_data="menu:home")]
    ])


def market_keyboard(fid, market):
    choices = {
        "1X2": [("1", "1"), ("X", "X"), ("2", "2")],
        "DC": [("1X", "1X"), ("X2", "X2"), ("12", "12")],
        "BTTS": [("Oui", "BTTSY"), ("Non", "BTTSN")],
        "OU": [("Over 1.5", "O15"), ("Over 2.5", "O25"), ("Over 3.5", "O35"), ("Under 2.5", "U25"), ("Under 3.5", "U35")]
    }[market]
    rows = []
    row = []
    for label, code in choices:
        row.append(InlineKeyboardButton(label, callback_data=f"pick:{fid}:{market}:{code}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ RETOUR", callback_data=f"sim:{fid}"), InlineKeyboardButton("🏠 ACCUEIL", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


def stake_keyboard(fid, market, selection):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("5 €", callback_data=f"stake:{fid}:{market}:{selection}:5"), InlineKeyboardButton("10 €", callback_data=f"stake:{fid}:{market}:{selection}:10")],
        [InlineKeyboardButton("20 €", callback_data=f"stake:{fid}:{market}:{selection}:20"), InlineKeyboardButton("50 €", callback_data=f"stake:{fid}:{market}:{selection}:50")],
        [InlineKeyboardButton("⬅️ RETOUR", callback_data=f"market:{fid}:{market}"), InlineKeyboardButton("🏠 ACCUEIL", callback_data="menu:home")]
    ])


def tools_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 PERFORMANCE", callback_data="menu:performance"), InlineKeyboardButton("🔄 VALIDATION", callback_data="menu:validation")],
        [InlineKeyboardButton("💰 BANKROLL", callback_data="menu:bankroll"), InlineKeyboardButton("🔌 SOURCES", callback_data="menu:status")],
        [InlineKeyboardButton("🏠 ACCUEIL", callback_data="menu:home")]
    ])


def help_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚽ MATCHS DU JOUR", callback_data="menu:match"), InlineKeyboardButton("📈 PERFORMANCE", callback_data="menu:performance")],
        [InlineKeyboardButton("💶 BANKROLL", callback_data="menu:bankroll"), InlineKeyboardButton("🔄 VALIDATION", callback_data="menu:validation")],
        [InlineKeyboardButton("🔌 SOURCES API", callback_data="menu:status"), InlineKeyboardButton("🏠 ACCUEIL", callback_data="menu:home")]
    ])
