import { useState, useEffect } from "react";
import { apiFetch } from "../../hooks/useApiToken";
import { SettingsSection, SettingsLoader } from "./shared";

/* ─── Proactivity Tab ─── */

function ProactivityTab() {
  const [settings, setSettings] = useState<Record<string, string>>({});
  const [skills, setSkills] = useState<{ skill_id: string; name: string }[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      apiFetch("/api/settings").then((r) => r.json()),
      apiFetch("/api/skills").then((r) => r.json()),
    ])
      .then(([settingsRes, skillsRes]) => {
        setSettings(settingsRes.settings || {});
        setSkills((skillsRes.skills || []).map((s: any) => ({
          skill_id: s.skill_id,
          name: s.name,
        })));
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const getSetting = (key: string, def: string) => settings[key] ?? def;
  const setSetting = async (key: string, value: string) => {
    setSettings((prev) => ({ ...prev, [key]: value }));
    await apiFetch(`/api/settings/${key}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    });
  };

  const level3Skills: string[] = (() => {
    try { return JSON.parse(getSetting("proactivity.level3_skills", "[]")); }
    catch { return []; }
  })();

  const toggleLevel3Skill = async (skillId: string) => {
    const updated = level3Skills.includes(skillId)
      ? level3Skills.filter((s) => s !== skillId)
      : [...level3Skills, skillId];
    await setSetting("proactivity.level3_skills", JSON.stringify(updated));
  };

  if (loading) return <SettingsLoader />;

  return (
    <div className="settings-tab">
      <div className="settings-tab-header">
        <h2>Proactivity</h2>
        <p>Control how proactively the agent suggests actions and acts on your behalf.</p>
      </div>

      <SettingsSection
        title="Suggestion Levels"
        description="Choose what kinds of proactive behavior the agent can perform."
      >
        <label className="settings-toggle-row">
          <span>
            <strong>Post-task suggestions</strong><br />
            <span className="settings-toggle-hint">
              After completing a task, suggest a natural follow-up action.
            </span>
          </span>
          <input
            type="checkbox"
            className="settings-toggle"
            checked={getSetting("proactivity.level1", "true") === "true"}
            onChange={(e) => setSetting("proactivity.level1", e.target.checked ? "true" : "false")}
          />
        </label>

        <label className="settings-toggle-row">
          <span>
            <strong>Idle nudges</strong><br />
            <span className="settings-toggle-hint">
              When you're quiet for a while, suggest helpful actions based on context.
            </span>
          </span>
          <input
            type="checkbox"
            className="settings-toggle"
            checked={getSetting("proactivity.level2", "true") === "true"}
            onChange={(e) => setSetting("proactivity.level2", e.target.checked ? "true" : "false")}
          />
        </label>

        <label className="settings-toggle-row">
          <span>
            <strong>Autonomous actions</strong><br />
            <span className="settings-toggle-hint">
              Allow the agent to run skills in the background without asking. Only
              enabled skills (below) can be used autonomously.
            </span>
          </span>
          <input
            type="checkbox"
            className="settings-toggle"
            checked={getSetting("proactivity.level3", "false") === "true"}
            onChange={(e) => setSetting("proactivity.level3", e.target.checked ? "true" : "false")}
          />
        </label>

        <label className="settings-toggle-row">
          <span>
            <strong>LLM-powered greeting</strong><br />
            <span className="settings-toggle-hint">
              Use an LLM call to compose a context-aware greeting each session.
              When off, uses a static greeting from your identity — saves tokens.
            </span>
          </span>
          <input
            type="checkbox"
            className="settings-toggle"
            checked={getSetting("proactivity.llm_greeting", "true") === "true"}
            onChange={(e) => setSetting("proactivity.llm_greeting", e.target.checked ? "true" : "false")}
          />
        </label>
      </SettingsSection>

      {getSetting("proactivity.level3", "false") === "true" && (
        <SettingsSection
          title="Autonomous Skills"
          description="Select which skills the agent can run on its own."
        >
          {skills.map((s) => (
            <label key={s.skill_id} className="settings-toggle-row">
              <span>{s.name}</span>
              <input
                type="checkbox"
                className="settings-toggle"
                checked={level3Skills.includes(s.skill_id)}
                onChange={() => toggleLevel3Skill(s.skill_id)}
              />
            </label>
          ))}
        </SettingsSection>
      )}

      <SettingsSection
        title="Daily Budgets"
        description="Limit how many proactive actions the agent takes per day."
      >
        <div className="settings-budget-row">
          <span>Suggestions per day (Levels 1 &amp; 2)</span>
          <input
            type="number"
            className="settings-budget-input"
            min="0"
            max="50"
            value={getSetting("proactivity.suggestion_budget", "10")}
            onChange={(e) => setSetting("proactivity.suggestion_budget", e.target.value)}
          />
        </div>
        <div className="settings-budget-row">
          <span>Autonomous actions per day (Level 3)</span>
          <input
            type="number"
            className="settings-budget-input"
            min="0"
            max="20"
            value={getSetting("proactivity.action_budget", "3")}
            onChange={(e) => setSetting("proactivity.action_budget", e.target.value)}
          />
        </div>
      </SettingsSection>
    </div>
  );
}

export default ProactivityTab;
