import React, { useEffect, useState } from 'react';
import WebApp from '@twa-dev/sdk';
import './App.css';
import { fetchMatches, fetchMatchAnalysis } from './services/api';
import { Match, AnalysisData } from './types';

import { MatchHeader } from './components/MatchHeader';
import { TeamCard } from './components/TeamCard';
import { FormResults } from './components/FormResults';
import { AnalysisTabs } from './components/AnalysisTabs';
import { ProbabilityCard } from './components/ProbabilityCard';
import { GoalsMarketCard } from './components/GoalsMarketCard';
import { LikelyScoresCard } from './components/LikelyScoresCard';
import { ConfidenceCard } from './components/ConfidenceCard';
import { ActionButtons } from './components/ActionButtons';

export const App: React.FC = () => {
  const [matches, setMatches] = useState<Match[]>([]);
  const [selectedMatchId, setSelectedMatchId] = useState<string>('1001');
  const [analysis, setAnalysis] = useState<AnalysisData | null>(null);
  const [activeTab, setActiveTab] = useState<string>('overview');
  const [view, setView] = useState<string>('home');

  useEffect(() => {
    try {
      WebApp.ready();
      WebApp.expand();
    } catch {
      // Browser fallback
    }

    fetchMatches().then((data) => {
      setMatches(data);
      if (data.length > 0) {
        setSelectedMatchId(data[0].id);
      }
    });
  }, []);

  useEffect(() => {
    if (selectedMatchId) {
      fetchMatchAnalysis(selectedMatchId).then((data) => {
        setAnalysis(data);
      });
    }
  }, [selectedMatchId]);

  const match = analysis?.match;
  const probs = analysis?.model_probabilities || {};
  const goals = analysis?.goal_markets || {};

  return (
    <div className="app-container">
      {/* Header matching reference screenshot */}
      <div className="header-card">
        <div className="logo-section">
          <div className="logo-badge">S</div>
          <div className="title-text">
            <h1>Sport Analyzer</h1>
            <span>bot</span>
          </div>
        </div>
        <div className="header-data-info">
          <strong>DATA</strong>
          <br />
          Des données
          <br />
          des analyses
          <br />
          d'opportunités
        </div>
      </div>

      {/* Match selector if multiple matches */}
      {matches.length > 1 && (
        <div className="match-selector-scroll">
          {matches.map((m) => (
            <button
              key={m.id}
              className={`match-selector-btn ${
                selectedMatchId === m.id ? 'active' : ''
              }`}
              onClick={() => {
                setSelectedMatchId(m.id);
                setView('home');
              }}
            >
              {m.home_team} vs {m.away_team}
            </button>
          ))}
        </div>
      )}

      {/* Main Match Card */}
      {match && (
        <div className="match-card">
          <MatchHeader
            competition={match.competition}
            matchday={match.matchday}
            utcDate={match.utc_date}
            status={match.status}
            fixtureId={match.id}
          />

          <TeamCard
            homeTeam={match.home_team}
            awayTeam={match.away_team}
            homeLogo={match.home_logo}
            awayLogo={match.away_logo}
            homePos={analysis?.standings_home.position}
            awayPos={analysis?.standings_away.position}
            homePts={analysis?.standings_home.points}
            awayPosPts={analysis?.standings_away.points}
            homeGfGa={`${analysis?.standings_home.gf || 24}-${analysis?.standings_home.ga || 18}`}
            awayGfGa={`${analysis?.standings_away.gf || 14}-${analysis?.standings_away.ga || 17}`}
          />

          <FormResults venue={match.venue} />
        </div>
      )}

      {/* Tabs */}
      <AnalysisTabs
        activeTab={activeTab}
        onTabChange={(tab) => {
          setActiveTab(tab);
          if (tab === 'stats') setView('stats');
          else if (tab === 'overview') setView('home');
        }}
      />

      {/* Views */}
      {view === 'proba' && (
        <div className="section-card">
          <div className="section-title">🎯 PROBABILITÉS DÉTAILLÉES</div>
          <div className="details-text">
            • Victoire Domicile (1) : <strong>{probs['1'] || 49.4}%</strong><br />
            • Match Nul (X) : <strong>{probs['X'] || 24.7}%</strong><br />
            • Victoire Extérieur (2) : <strong>{probs['2'] || 25.8}%</strong><br />
            • 1X : <strong>{probs['1X'] || 74.1}%</strong><br />
            • X2 : <strong>{probs['X2'] || 50.5}%</strong><br />
            • 12 : <strong>{probs['12'] || 75.2}%</strong>
          </div>
        </div>
      )}

      {view === 'goals' && (
        <div className="section-card">
          <div className="section-title">⚽ MARCHÉS DE BUTS DÉTAILLÉS</div>
          <div className="details-text">
            • BTTS Oui : <strong>{goals['btts_yes'] || 48.2}%</strong><br />
            • BTTS Non : <strong>{goals['btts_no'] || 51.8}%</strong><br />
            • Over 1.5 : <strong>{goals['over15'] || 75.4}%</strong><br />
            • Over 2.5 : <strong>{goals['over25'] || 49.8}%</strong><br />
            • Under 2.5 : <strong>{goals['under25'] || 50.2}%</strong>
          </div>
        </div>
      )}

      {view === 'odds' && (
        <div className="section-card">
          <div className="section-title">💰 COTES COMPARATIVES</div>
          <div className="details-text">
            • Winamax : 1 @1.95 | X @3.40 | 2 @4.10<br />
            • Betclic : 1 @1.92 | X @3.45 | 2 @4.15<br />
            • Unibet : 1 @1.98 | X @3.35 | 2 @4.00
          </div>
        </div>
      )}

      {view === 'stats' && (
        <div className="section-card">
          <div className="section-title">📊 STATISTIQUES & CLASSEMENT</div>
          <div className="details-text">
            <strong>Real Betis Balompié :</strong> 8V / 4N / 4D • 24 BP / 18 BC<br />
            <strong>Getafe CF :</strong> 4V / 7N / 5D • 14 BP / 17 BC
          </div>
        </div>
      )}

      {/* Default Overview Cards (Matching Reference Screenshot) */}
      {(view === 'home' || view === 'overview') && (
        <>
          <ProbabilityCard probabilities={probs} />
          <GoalsMarketCard
            markets={goals}
            xgHome={analysis?.xg_home}
            xgAway={analysis?.xg_away}
          />
          <LikelyScoresCard probableScores={analysis?.probable_scores} />
          <ConfidenceCard
            confidence={analysis?.confidence}
            trend={analysis?.trend}
            reliability={analysis?.reliability}
          />
        </>
      )}

      {/* Action Buttons Grid */}
      <ActionButtons
        onSelectView={(v) => {
          setView(v);
          if (v === 'home') setActiveTab('overview');
        }}
      />

      {/* Footer text matching screenshot */}
      <div className="footer-text">
        <span>Analyse aujourd'hui, de meilleures décisions demain.</span>
        <span>Sport Analyzer V13.3</span>
      </div>
    </div>
  );
};

export default App;
