import en from "./en.json";

// The interface languages (zh is the source text in the code, en is a translation).
// Text written by people and apps (app names, plans, model output) is never translated.
export const LOCALES = ["zh", "en"] as const;
export type Locale = (typeof LOCALES)[number];
export const DEFAULT_LOCALE: Locale = "zh";
export const LOCALE_COOKIE = "aep_locale";

export type Vars = Record<string, string | number>;

const dictionaries: Record<Locale, Record<string, string>> = { zh: {}, en };

export function parseLocale(value: string | null | undefined): Locale {
  return LOCALES.includes(value as Locale) ? (value as Locale) : DEFAULT_LOCALE;
}

// For <html lang> and Intl formatting.
export function htmlLang(locale: Locale): string {
  return locale === "en" ? "en" : "zh-CN";
}

// The Chinese text is the key; a missing translation shows the Chinese text.
// `{name}` in the text is replaced by vars.name.
export function translate(locale: Locale, text: string, vars?: Vars): string {
  const base = dictionaries[locale][text] ?? text;
  if (!vars) return base;
  return base.replace(/\{(\w+)\}/g, (match, key: string) =>
    key in vars ? String(vars[key]) : match,
  );
}
