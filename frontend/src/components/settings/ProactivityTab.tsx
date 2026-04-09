import { useState, useEffect } from "react";
import { apiFetch } from "../../hooks/useApiToken";
import { SettingsSection, SettingsLoader } from "./shared";

/* ─── Types ─── */

interface RecipeInfo {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  builtin: boolean;
  user_toggleable: boolean;
  min_relationship: number;
  cooldown: number;
  trigger_type: string;
  trigger_params: Record<string, unknown>;
}

/* ─── Trigger type labels ─── */

const TRIGGER_LABELS: Record<string, string> = {
  cron: "Scheduled",
  idle: "When idle",
  session: "On connect",
  memory: "On memory change",
  calendar: "Before events",
  pattern: "From patterns",
  emotion: "Emotional",
  post_task: "After task",
};

function formatCron(params: Record<string, unknown>): string {
  const schedule = (params.schedule as string) || "";
  const parts = schedule.split(" ");
  if (parts.length !== 5) return schedule;

  const [minute, hour, , , weekday] = parts;
  const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  let timeStr = "";
  if (hour !== "*" && minute !== "*") {
    const h = parseInt(hour);
    const m = parseInt(minute);
    const ampm = h >= 12 ? "PM" : "AM";
    const h12 = h === 0 ? 12 : h > 12 ? h - 12 : h;
    timeStr = `${h12}:${m.toString().padStart(2, "0")} ${ampm}`;
  }

  let dayStr = "daily";
  if (weekday !== "*") {
    const dayIdx = parseInt(weekday);
    dayStr = days[dayIdx] || `day ${weekday}`;
  }

  return timeStr ? `${dayStr} at ${timeStr}` : dayStr;
}

function formatTrigger(recipe: RecipeInfo): string {
  if (recipe.trigger_type === "cron") {
    return formatCron(recipe.trigger_params);
  }
  if (recipe.trigger_type === "calendar") {
    const mins = recipe.trigger_params.minutes_before as number;
    return mins ? `${mins} min before events` : "Before events";
  }
  if (recipe.trigger_type === "emotion") {
    return "When needed";
  }
  if (recipe.trigger_type === "memory") {
    return "On relevant memory";
  }
  return TRIGGER_LABELS[recipe.trigger_type] || recipe.trigger_type;
}

/* ─── Proactivity Tab ─── */

function ProactivityTab() {
  const [settings, setSettings] = useState<Record<string, string>>({});
  const [skills, setSkills] = useState<{ skill_id: string; name: string }[]>([]);
  const [recipes, setRecipes] = useState<RecipeInfo[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      apiFetch("/api/settings").then((r) => r.json()),
      apiFetch("/api/skills").then((r) => r.json()),
      apiFetch("/api/recipes").then((r) => r.json()),
    ])
      .then(([settingsRes, skillsRes, recipesRes]) => {
        setSettings(settingsRes.settings || {});
        setSkills((skillsRes.skills || []).map((s: any) => ({
          skill_id: s.skill_id,
          name: s.name,
        })));
        setRecipes(recipesRes.recipes || []);
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

  const toggleRecipe = async (recipeId: string, enabled: boolean) => {
    setRecipes((prev) =>
      prev.map((r) => (r.id === recipeId ? { ...r, enabled } : r))
    );
    await apiFetch(`/api/recipes/${recipeId}/toggle`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
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

  const REL_LABELS: Record<number, string> = {
    1: "",
    2: "Level 2+",
    3: "Level 3+",
    4: "Level 4",
  };

  return (
    <div className="settings-tab">
      <div className="settings-tab-header">
        <h2>Proactivity</h2>
        <p>Control how proactively the agent suggests actions and acts on your behalf.</p>
      </div>

      {/* ── Proactive Behaviors (Recipes) ── */}
      <SettingsSection
        title="Proactive Behaviors"
        description="Toggle specific behaviors the agent can perform automatically."
      >
        {recipes.map((recipe) => (
          <label key={recipe.id} className="settings-toggle-row">
            <span>
              <strong>{recipe.name}</strong>
              {recipe.min_relationship > 1 && (
                <span className="settings-recipe-badge">
                  {REL_LABELS[recipe.min_relationship]}
                </span>
              )}
              <br />
              <span className="settings-toggle-hint">
                {recipe.description}
              </span>
              <span className="settings-recipe-schedule">
                {formatTrigger(recipe)}
              </span>
            </span>
            <input
              type="checkbox"
              className="settings-toggle"
              checked={recipe.enabled}
              disabled={!recipe.user_toggleable}
              onChange={(e) => toggleRecipe(recipe.id, e.target.checked)}
            />
          </label>
        ))}
      </SettingsSection>

      {/* ── Legacy levels ── */}
      <SettingsSection
        title="General Settings"
        description="Core proactivity controls."
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
            <strong>LLM-powered greeting</strong><br />
            <span className="settings-toggle-hint">
              Use an LLM call to compose a context-aware greeting each session.
              When off, uses a static greeting from your identity.
            </span>
          </span>
          <input
            type="checkbox"
            className="settings-toggle"
            checked={getSetting("proactivity.llm_greeting", "true") === "true"}
            onChange={(e) => setSetting("proactivity.llm_greeting", e.target.checked ? "true" : "false")}
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
          <span>Suggestions per day</span>
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
          <span>Autonomous actions per day</span>
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
