import React from 'react';

interface FormResultsProps {
  homeForm?: string[];
  awayForm?: string[];
  venue?: string;
}

export const FormResults: React.FC<FormResultsProps> = ({
  homeForm = ['V', 'V', 'N', 'D', 'V'],
  awayForm = ['V', 'N', 'D', 'N', 'D'],
  venue = 'Estadio Benito Villamarín',
}) => {
  return (
    <div className="form-results-container">
      <div className="form-row">
        <div className="form-letters">
          Forme :{' '}
          {homeForm.map((res, i) => (
            <span key={i} className={`form-tag form-${res.toLowerCase()}`}>
              {res}
            </span>
          ))}
        </div>
        <div className="form-letters">
          Forme :{' '}
          {awayForm.map((res, i) => (
            <span key={i} className={`form-tag form-${res.toLowerCase()}`}>
              {res}
            </span>
          ))}
        </div>
      </div>
      {venue && (
        <div className="venue-info">
          📍 Stade : <span>{venue}</span>
        </div>
      )}
    </div>
  );
};
