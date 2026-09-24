import React from 'react';

interface MatchHeaderProps {
  competition: string;
  matchday?: string;
  utcDate?: string;
  status: string;
  fixtureId: string;
}

export const MatchHeader: React.FC<MatchHeaderProps> = ({
  competition,
  matchday,
  utcDate,
  status,
  fixtureId,
}) => {
  return (
    <div className="match-header">
      <div className="match-header-info">
        <div className="comp-title">🏆 🇪🇸 {competition.toUpperCase()}</div>
        <div className="comp-subtitle">
          {matchday || 'Journée'} • {utcDate || '17/09/2026 • 19:00'}
        </div>
      </div>
      <div className="match-header-meta">
        <span className="match-id">ID {fixtureId}</span>
        <span className="status-badge">
          {status === 'FINISHED' ? 'Terminé' : 'À venir'}
        </span>
      </div>
    </div>
  );
};
