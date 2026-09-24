import React from 'react';

interface LikelyScoresCardProps {
  probableScores?: Array<{ score: string; probability: number }>;
}

export const LikelyScoresCard: React.FC<LikelyScoresCardProps> = ({
  probableScores = [
    { score: '1-1', probability: 11.7 },
    { score: '1-0', probability: 10.7 },
    { score: '2-1', probability: 9.5 },
  ],
}) => {
  return (
    <div className="section-card">
      <div className="section-title">🔢 SCORES LES PLUS PROBABLES</div>

      {probableScores.map((s) => (
        <div key={s.score} className="prob-row">
          <div className="score-badge">{s.score}</div>
          <div className="bar-bg">
            <div
              className="bar-fill bg-cyan"
              style={{ width: `${Math.min(100, s.probability * 6)}%` }}
            />
          </div>
          <div className="prob-val text-white">{s.probability.toFixed(1)}%</div>
        </div>
      ))}
    </div>
  );
};
