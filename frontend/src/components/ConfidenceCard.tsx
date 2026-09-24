import React from 'react';

interface ConfidenceCardProps {
  confidence?: string;
  trend?: string;
  reliability?: string;
}

export const ConfidenceCard: React.FC<ConfidenceCardProps> = ({
  confidence = 'Moyenne',
  trend = 'Tendance marquée',
  reliability = 'Bonne',
}) => {
  return (
    <div className="section-card">
      <div className="section-title">🧠 INTELLIGENCE DU MATCH</div>

      <div className="badge-row">
        <span className="badge-label">Confiance</span>
        <span className="pill-badge bg-yellow">{confidence}</span>
      </div>

      <div className="badge-row">
        <span className="badge-label">Tendance</span>
        <span className="pill-badge pill-blue">{trend}</span>
      </div>

      <div className="badge-row">
        <span className="badge-label">Fiabilité</span>
        <span className="pill-badge bg-green">{reliability}</span>
      </div>

      <div className="intelligence-footer">
        Données disponibles • modèle statistique
      </div>
    </div>
  );
};
