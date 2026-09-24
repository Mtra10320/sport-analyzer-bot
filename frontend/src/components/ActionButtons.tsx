import React from 'react';

interface ActionButtonsProps {
  onSelectView: (view: string) => void;
}

export const ActionButtons: React.FC<ActionButtonsProps> = ({ onSelectView }) => {
  return (
    <div className="buttons-grid">
      <button className="action-btn" onClick={() => onSelectView('home')}>
        🔎 Analyse
      </button>
      <button className="action-btn" onClick={() => onSelectView('proba')}>
        🎯 Probabilités
      </button>
      <button className="action-btn" onClick={() => onSelectView('goals')}>
        ⚽ Buts
      </button>
      <button className="action-btn" onClick={() => onSelectView('odds')}>
        💰 Cotes
      </button>
      <button className="action-btn" onClick={() => onSelectView('scorers')}>
        ⚽ Buteurs
      </button>
      <button className="action-btn" onClick={() => onSelectView('sim')}>
        🔬 Simuler
      </button>
      <button
        className="action-btn action-btn-full"
        onClick={() => onSelectView('home')}
      >
        ⬅️ Menu principal
      </button>
    </div>
  );
};
