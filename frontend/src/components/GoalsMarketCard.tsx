import React from 'react';

interface GoalsMarketCardProps {
  markets: Record<string, number>;
  xgHome?: number;
  xgAway?: number;
}

export const GoalsMarketCard: React.FC<GoalsMarketCardProps> = ({
  markets,
  xgHome = 1.62,
  xgAway = 1.10,
}) => {
  const getBarColorClass = (val: number) => {
    if (val >= 60) return 'bg-green text-green';
    if (val >= 30) return 'bg-yellow text-yellow';
    return 'bg-red text-red';
  };

  const rows = [
    { label: 'BTTS Oui', val: markets['btts_yes'] ?? 48.2 },
    { label: 'BTTS Non', val: markets['btts_no'] ?? 51.8 },
    { label: 'Over 1.5', val: markets['over15'] ?? 75.4 },
    { label: 'Over 2.5', val: markets['over25'] ?? 49.8 },
    { label: 'Over 3.5', val: markets['over35'] ?? 26.2 },
    { label: 'Under 2.5', val: markets['under25'] ?? 50.2 },
    { label: 'Under 3.5', val: markets['under35'] ?? 73.8 },
  ];

  return (
    <div className="section-card">
      <div className="section-title">⚽ MARCHÉS DE BUTS</div>

      {rows.map((item) => {
        const colorClass = getBarColorClass(item.val);
        const [bgClass, textClass] = colorClass.split(' ');

        return (
          <div key={item.label} className="prob-row">
            <div className="prob-label-wide">{item.label}</div>
            <div className="bar-bg">
              <div
                className={`bar-fill ${bgClass}`}
                style={{ width: `${Math.min(100, Math.max(0, item.val))}%` }}
              />
            </div>
            <div className={`prob-val ${textClass}`}>{item.val.toFixed(1)}%</div>
          </div>
        );
      })}

      <div className="xg-footer-row">
        xG estimé : <span>{xgHome.toFixed(2)}</span> • <span>{xgAway.toFixed(2)}</span>
      </div>
    </div>
  );
};
