/**
 * Locale system — auto-detects from browser, stores in localStorage,
 * persists to backend so the LLM language directive matches.
 */

import { createContext, useContext } from "react";
import en, { type StringKey } from "./en";
import zh from "./zh";

const TABLES: Record<string, Record<string, string>> = { en, zh };

/** Supported locales shown in the language picker. */
export const LANGUAGES = [
  { code: "en", label: "English" },
  { code: "zh", label: "中文 (简体)" },
] as const;

/** Map locale code → language name sent to the LLM directive. */
const LOCALE_TO_LANGUAGE: Record<string, string> = {
  en: "",                   // empty = no directive, default English
  zh: "Chinese (Simplified)",
};

export function llmLanguage(locale: string): string {
  return LOCALE_TO_LANGUAGE[locale] ?? "";
}

/** Detect locale: stored preference > browser language > English. */
export function detectLocale(): string {
  try {
    const stored = localStorage.getItem("muse_locale");
    if (stored && TABLES[stored]) return stored;
  } catch {}
  if (typeof navigator !== "undefined") {
    const lang = navigator.language;
    if (lang.startsWith("zh")) return "zh";
  }
  return "en";
}

export type TranslateFn = (key: StringKey, vars?: Record<string, string | number>) => string;

export interface LocaleContextValue {
  locale: string;
  setLocale: (locale: string) => void;
  t: TranslateFn;
}

function makeT(locale: string): TranslateFn {
  const table = TABLES[locale] || en;
  return (key, vars) => {
    let str = table[key] ?? en[key] ?? key;
    if (vars) {
      for (const [k, v] of Object.entries(vars)) {
        str = str.replace(`{${k}}`, String(v));
      }
    }
    return str;
  };
}

export const LocaleContext = createContext<LocaleContextValue>({
  locale: "en",
  setLocale: () => {},
  t: makeT("en"),
});

export const useLocale = () => useContext(LocaleContext);

export { makeT };
