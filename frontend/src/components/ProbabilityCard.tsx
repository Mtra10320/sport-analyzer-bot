import React from 'react';

interface ProbabilityCardProps {
  probabilities: Record<string, number>;
}

export const ProbabilityCard: React.FC<ProbabilityCardProps> = ({
  probabilities,
}) => {
  const getBarColorClass = (val: number) => {
    if (val >= 60) return 'bg-green text-green';
    if (val >= 30) return 'bg-yellow text-yellow';
    return 'bg-red text-red';
  };

  const rows = [
    { label: '1', val: probabilities['1'] ?? 49.4 },
    { label: 'X', val: probabilities['X'] ?? 24.7 },
    { label: '2', val: probabilities['2'] ?? 25.8 },
    { label: '1X', val: probabilities['1X'] ?? 74.1 },
    { label: 'X2', val: probabilities['X2'] ?? 50.5 },
    { label: '12', val: probabilities['12'] ?? 75.2 },
  ];

  return (
    <div className="section-card">
      <div className="section-title">🎯 PROBABILITÉS DU MODÈLE</div>

      {rows.map((item, idx) => {
        const colorClass = getBarColorClass(item.val);
        const [bgClass, textClass] = colorClass.split(' ');
        const showDivider = idx === 3;

        return (
          <React.Fragment key={item.label}>
            {showDivider && <div className="prob-divider" />}
            <div className="prob-row">
              <div className="prob-label">{item.label}</div>
              <div className="bar-bg">
                <div
                  className={`bar-fill ${bgClass}`}
                  style={{ width: `${Math.min(100, Math.max(0, item.val))}%` }}
                />
              </div>
              <div className={`prob-val ${item.label === '12' ? 'text-cyan' : textClass}`}>
                {item.val.toFixed(1)}%
              </div>
            </div>
          </React.Fragment>
        );
      })}
    </div>
  );
};
