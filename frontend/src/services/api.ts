import { Match, AnalysisData } from '../types';
import { MOCK_MATCHES, MOCK_ANALYSIS } from './mockData';

const API_BASE = '/api';

export async function fetchMatches(): Promise<Match[]> {
  try {
    const res = await fetch(`${API_BASE}/matches`);
    if (!res.ok) throw new Error("HTTP error");
    return await res.json();
  } catch {
    return MOCK_MATCHES;
  }
}

export async function fetchMatchAnalysis(fixtureId: string): Promise<AnalysisData> {
  try {
    const res = await fetch(`${API_BASE}/matches/${fixtureId}/analysis`);
    if (!res.ok) throw new Error("HTTP error");
    return await res.json();
  } catch {
    return MOCK_ANALYSIS;
  }
}
