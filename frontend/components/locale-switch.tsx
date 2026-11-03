"use client";

import { useLocale, useT } from "../lib/i18n/client";
import type { Locale } from "../lib/i18n/locale";

const OPTIONS: { locale: Locale; label: string; name: string }[] = [
  { locale: "zh", label: "中", name: "中文" }, // i18n-ignore
  { locale: "en", label: "EN", name: "English" },
];

// Interface language: Chinese or English. Each option is named in its own language.
export function LocaleSwitch() {
  const { locale, setLocale } = useLocale();
  const t = useT();
  return (
    <span className="locale-switch" role="group" aria-label={t("界面语言")}>
      {OPTIONS.map((option) => (
        <button
          key={option.locale}
          type="button"
          lang={option.locale === "en" ? "en" : "zh-CN"}
          aria-label={option.name}
          aria-pressed={locale === option.locale}
          className={locale === option.locale ? "selected" : ""}
          onClick={() => locale !== option.locale && setLocale(option.locale)}
        >
          {option.label}
        </button>
      ))}
    </span>
  );
}
