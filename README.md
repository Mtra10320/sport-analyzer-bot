# Sport Analyzer V13.3 - Full-Stack Telegram Mini App

## Architecture & Présentation

Sport Analyzer est une application complète de prédictions et d'analyses de matchs de football conçue sur mesure pour fonctionner dans **Telegram sur iPhone, Android et Desktop** via Telegram Mini App SDK.

Elle reprend fidèlement la charte graphique et la disposition visuelle de référence :
- Fond bleu nuit avec motifs discrets
- Header compact avec logo
- Carte du match avec compétition, date, heure, équipes, drapeaux/logos, classements, points, forme récente et stade
- Onglets de navigation (Vue d'ensemble, Statistiques, Face à face, Compos)
- Cartes de statistiques : Probabilités du modèle, Marchés de buts (BTTS, Over/Under), Scores les plus probables, Intelligence du match (Confiance, Tendance, Fiabilité)
- Grille de boutons d'action (Analyse, Probabilités, Buts, Cotes, Buteurs, Simuler, Menu principal)

---

## Structure du Projet

```
sport-analyzer/
├── backend/                  # API REST FastAPI & Moteur Statistique
│   ├── app/
│   │   ├── main.py          # Point d'entrée FastAPI & Service des fichiers statiques
│   │   ├── config.py        # Configuration des variables d'environnement
│   │   ├── database.py      # ORM SQLAlchemy
│   │   ├── models/          # Modèles DB (User, Match, Prediction, Bet, Bankroll, etc.)
│   │   ├── schemas/         # Schémas DTO Pydantic
│   │   ├── api/             # Routes API REST (/api/matches, /api/matches/{id}/analysis, etc.)
│   │   ├── services/        # Abstraction API Football (API-Football & Fallback TheSportsDB)
│   │   └── analysis/        # Moteur de distribution de Poisson & xG
│   └── requirements.txt
│
├── bot/                      # Bot Telegram Python
│   ├── bot.py               # Application python-telegram-bot avec WebAppInfo
│   └── requirements.txt
│
├── frontend/                 # Telegram Mini App (React + Vite + TypeScript)
│   ├── src/
│   │   ├── components/      # Composants modularisés (MatchHeader, TeamCard, ProbabilityCard, etc.)
│   │   ├── services/        # Service API & Mock Data
│   │   ├── types/           # Types TypeScript DTO
│   │   ├── App.tsx          # Application principale
│   │   └── App.css          # Design CSS responsive bleu nuit/cyan/violet
│   ├── index.html
│   ├── package.json
│   └── vite.config.ts
│
├── tests/                    # Suite de tests unitaires (tests/test_backend.py, test_bot.py)
├── Dockerfile                # Build multi-stage Node/Python
├── docker-compose.yml        # Orchestration locale
├── render.yaml               # Déploiement Render
└── README.md
```

---

## Installation & Lancement Local

### 1. Variables d'environnement (`.env`)

Créez un fichier `.env` à la racine :
```env
TELEGRAM_BOT_TOKEN=votre_token_bot_father
API_FOOTBALL_KEY=votre_cle_api_football
FOOTBALL_DATA_KEY=votre_cle_football_data
DATABASE_URL=sqlite:///./sport_analyzer.db
APP_URL=http://localhost:10000
```

### 2. Lancement via Docker Compose

```bash
docker-compose up --build
```

L'application sera accessible sur `http://localhost:10000`.

### 3. Lancement Manuel

#### Backend FastAPI :
```bash
pip install -r backend/requirements.txt -r bot/requirements.txt
python -m uvicorn backend.app.main:app --host 0.0.0.0 --port 10000
```

#### Frontend React / Vite :
```bash
cd frontend
npm install
npm run build
```

#### Bot Telegram :
```bash
python bot/bot.py
```

---

## Configuration BotFather (Telegram)

1. Ouvrez BotFather sur Telegram.
2. Exécutez `/newapp` ou `/setwebapp`.
3. Associez votre Bot au lien HTTPS de votre application (`https://votre-app.onrender.com`).
4. Dans le Bot, la commande `/start` affiche le bouton **📊 OUVRIR SPORT ANALYZER** qui ouvre la Mini App directement sur mobile.

---

## Tests

Pour exécuter toute la suite de tests :
```bash
python3 -m unittest discover -s . -p "test_*.py"
python3 -m unittest discover -s tests -p "test_*.py"
```
