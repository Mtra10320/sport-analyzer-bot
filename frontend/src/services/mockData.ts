import { AnalysisData, Match } from '../types';

export const MOCK_MATCHES: Match[] = [
  {
    id: "1001",
    competition: "PRIMERA DIVISION",
    competition_code: "PD",
    matchday: "Journée 17",
    utc_date: "17/09/2026 • 19:00",
    status: "SCHEDULED",
    home_team: "Real Betis Balompié",
    away_team: "Getafe CF",
    home_logo: "https://crests.football-data.org/90.png",
    away_logo: "https://crests.football-data.org/82.png",
    venue: "Estadio Benito Villamarín"
  },
  {
    id: "1002",
    competition: "LIGUE 1",
    competition_code: "FL1",
    matchday: "Journée 5",
    utc_date: "17/09/2026 • 21:00",
    status: "SCHEDULED",
    home_team: "Paris Saint-Germain",
    away_team: "Olympique de Marseille",
    home_logo: "https://crests.football-data.org/524.png",
    away_logo: "https://crests.football-data.org/516.png",
    venue: "Parc des Princes"
  }
];

export const MOCK_ANALYSIS: AnalysisData = {
  match: MOCK_MATCHES[0],
  home_form: [
    { gf: 2, ga: 0, result: "V", opponent: "Espanyol" },
    { gf: 1, ga: 1, result: "N", opponent: "Sevilla" },
    { gf: 3, ga: 1, result: "V", opponent: "Valencia" },
    { gf: 0, ga: 1, result: "D", opponent: "Real Madrid" },
    { gf: 2, ga: 1, result: "V", opponent: "Villarreal" }
  ],
  away_form: [
    { gf: 1, ga: 0, result: "V", opponent: "Osasuna" },
    { gf: 0, ga: 0, result: "N", opponent: "Mallorca" },
    { gf: 0, ga: 2, result: "D", opponent: "Barcelona" },
    { gf: 1, ga: 1, result: "N", opponent: "Athletic Bilbao" },
    { gf: 0, ga: 1, result: "D", opponent: "Girona" }
  ],
  standings_home: { position: 6, points: 28, gf: 24, ga: 18, played: 16, won: 8, draw: 4, lost: 4 },
  standings_away: { position: 12, points: 19, gf: 14, ga: 17, played: 16, won: 4, draw: 7, lost: 5 },
  model_probabilities: {
    "1": 49.4,
    "X": 24.7,
    "2": 25.8,
    "1X": 74.1,
    "X2": 50.5,
    "12": 75.2
  },
  goal_markets: {
    btts_yes: 48.2,
    btts_no: 51.8,
    over15: 75.4,
    over25: 49.8,
    over35: 26.2,
    under25: 50.2,
    under35: 73.8
  },
  xg_home: 1.62,
  xg_away: 1.10,
  probable_scores: [
    { score: "1-1", probability: 11.7 },
    { score: "1-0", probability: 10.7 },
    { score: "2-1", probability: 9.5 }
  ],
  confidence: "Moyenne",
  trend: "Tendance marquée",
  reliability: "Bonne"
};
