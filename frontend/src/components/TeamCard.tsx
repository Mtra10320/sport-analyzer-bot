import React from 'react';

interface TeamCardProps {
  homeTeam: string;
  awayTeam: string;
  homeLogo?: string;
  awayLogo?: string;
  homePos?: number;
  awayPos?: number;
  homePts?: number;
  awayPosPts?: number;
  homeGfGa?: string;
  awayGfGa?: string;
}

export const TeamCard: React.FC<TeamCardProps> = ({
  homeTeam,
  awayTeam,
  homeLogo,
  awayLogo,
  homePos = 6,
  awayPos = 12,
  homePts = 28,
  awayPosPts = 19,
  homeGfGa = '24-18',
  awayGfGa = '14-17',
}) => {
  return (
    <div className="teams-vs-container">
      <div className="team-box">
        <div className="team-avatar">
          {homeLogo ? (
            <img src={homeLogo} alt={homeTeam} className="team-logo-img" />
          ) : (
            homeTeam[0]
          )}
        </div>
        <div className="team-name">{homeTeam}</div>
        <div className="team-stats">
          {homePos}e • {homePts} pts • {homeGfGa}
        </div>
      </div>

      <div className="vs-badge">VS</div>

      <div className="team-box">
        <div className="team-avatar">
          {awayLogo ? (
            <img src={awayLogo} alt={awayTeam} className="team-logo-img" />
          ) : (
            awayTeam[0]
          )}
        </div>
        <div className="team-name">{awayTeam}</div>
        <div className="team-stats">
          {awayPos}e • {awayPosPts} pts • {awayGfGa}
        </div>
      </div>
    </div>
  );
};
