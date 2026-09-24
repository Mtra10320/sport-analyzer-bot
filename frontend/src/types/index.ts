export interface Match {
  id: string;
  competition: string;
  competition_code?: string;
  matchday?: string;
  utc_date?: string;
  status: string;
  home_team: string;
  away_team: string;
  home_logo?: string;
  away_logo?: string;
  home_score?: number;
  away_score?: number;
  venue?: string;
}

export interface FormItem {
  gf: number;
  ga: number;
  result: string;
  opponent: string;
}

export interface StandingsItem {
  position?: number;
  played?: number;
  won?: number;
  draw?: number;
  lost?: number;
  gf?: number;
  ga?: number;
  points?: number;
}

export interface AnalysisData {
  match: Match;
  home_form: FormItem[];
  away_form: FormItem[];
  standings_home: StandingsItem;
  standings_away: StandingsItem;
  model_probabilities: Record<string, number>;
  goal_markets: Record<string, number>;
  xg_home: number;
  xg_away: number;
  probable_scores: Array<{ score: string; probability: number }>;
  confidence: string;
  trend: string;
  reliability: string;
}
