import React, { useState, lazy, Suspense } from "react";
import {
  IconSliders,
  IconCpu,
  IconShield,
  IconZap,
  IconPuzzle,
  IconPlug,
} from "../Icons";
import { SettingsLoader } from "./shared";

// Lazy-load each tab — only fetched when the user navigates to it
const GeneralTab = lazy(() => import("./GeneralTab"));
const SkillsTab = lazy(() => import("./SkillsTab"));
const ModelsTab = lazy(() => import("./ModelsTab"));
const SecurityTab = lazy(() => import("./SecurityTab"));
const ProactivityTab = lazy(() => import("./ProactivityTab"));
const MCPTab = lazy(() => import("./McpTab"));

interface SettingsProps {
  onBack: () => void;
}

type Tab = "general" | "skills" | "models" | "security" | "proactivity" | "mcp";

const TABS: { id: Tab; label: string; icon: React.ReactNode }[] = [
  { id: "general", label: "General", icon: <IconSliders size={16} /> },
  { id: "skills", label: "Skills", icon: <IconPuzzle size={16} /> },
  { id: "models", label: "Models", icon: <IconCpu size={16} /> },
  { id: "security", label: "Security", icon: <IconShield size={16} /> },
  { id: "proactivity", label: "Proactivity", icon: <IconZap size={16} /> },
  { id: "mcp", label: "MCP Servers", icon: <IconPlug size={16} /> },
];

export const Settings: React.FC<SettingsProps> = ({ onBack }) => {
  const [activeTab, setActiveTab] = useState<Tab>("general");

  return (
    <div className="settings">
      <div className="settings-nav">
        <div className="settings-nav-header">Settings</div>
        {TABS.map((tab) => (
          <button
            key={tab.id}
            className={`settings-nav-item ${activeTab === tab.id ? "active" : ""}`}
            onClick={() => setActiveTab(tab.id)}
          >
            {tab.icon}
            <span>{tab.label}</span>
          </button>
        ))}
        <div className="settings-nav-footer">
          <button className="settings-nav-back" onClick={onBack}>
            Back to Chat
          </button>
        </div>
      </div>
      <div className="settings-content">
        <Suspense fallback={<SettingsLoader />}>
          {activeTab === "general" && <GeneralTab />}
          {activeTab === "skills" && <SkillsTab />}
          {activeTab === "models" && <ModelsTab />}
          {activeTab === "security" && <SecurityTab />}
          {activeTab === "proactivity" && <ProactivityTab />}
          {activeTab === "mcp" && <MCPTab />}
        </Suspense>
      </div>
    </div>
  );
};
