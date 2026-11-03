"use client";

import { useRouter } from "next/navigation";
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  DEFAULT_LOCALE,
  htmlLang,
  LOCALE_COOKIE,
  translate,
  type Locale,
  type Vars,
} from "./locale";

export type Translate = (text: string, vars?: Vars) => string;

type LocaleState = {
  locale: Locale;
  setLocale: (next: Locale) => void;
  t: Translate;
};

const LocaleContext = createContext<LocaleState>({
  locale: DEFAULT_LOCALE,
  setLocale: () => {},
  t: (text, vars) => translate(DEFAULT_LOCALE, text, vars),
});

// The language comes from the aep_locale cookie, read by the root layout, so the
// first render on the server is already in the right language.
export function LocaleProvider({
  initial,
  children,
}: {
  initial: Locale;
  children: ReactNode;
}) {
  const router = useRouter();
  const [locale, setState] = useState<Locale>(initial);
  const setLocale = useCallback(
    (next: Locale) => {
      document.cookie = `${LOCALE_COOKIE}=${next}; path=/; max-age=31536000; samesite=lax`;
      document.documentElement.lang = htmlLang(next);
      setState(next);
      // Server-rendered parts (page titles) follow the cookie.
      router.refresh();
    },
    [router],
  );
  const value = useMemo<LocaleState>(
    () => ({
      locale,
      setLocale,
      t: (text, vars) => translate(locale, text, vars),
    }),
    [locale, setLocale],
  );
  return (
    <LocaleContext.Provider value={value}>{children}</LocaleContext.Provider>
  );
}

export function useT(): Translate {
  return useContext(LocaleContext).t;
}

export function useLocale() {
  const { locale, setLocale } = useContext(LocaleContext);
  return { locale, setLocale };
}
