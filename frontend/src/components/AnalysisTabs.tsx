import React from 'react';

interface AnalysisTabsProps {
  activeTab: string;
  onTabChange: (tab: string) => void;
}

export const AnalysisTabs: React.FC<AnalysisTabsProps> = ({
  activeTab,
  onTabChange,
}) => {
  const tabs = [
    { id: 'overview', label: "Vue d'ensemble" },
    { id: 'stats', label: 'Statistiques' },
    { id: 'h2h', label: 'Face à face' },
    { id: 'lineups', label: 'Compos' },
  ];

  return (
    <div className="tabs-nav">
      {tabs.map((tab) => (
        <button
          key={tab.id}
          className={`tab-btn ${activeTab === tab.id ? 'active' : ''}`}
          onClick={() => onTabChange(tab.id)}
        >
          {tab.label}
        </button>
      ))}
    </div>
  );
};
