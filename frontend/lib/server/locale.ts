import "server-only";
import { cookies } from "next/headers";
import {
  LOCALE_COOKIE,
  parseLocale,
  translate,
  type Locale,
} from "../i18n/locale";

export async function requestLocale(): Promise<Locale> {
  return parseLocale((await cookies()).get(LOCALE_COOKIE)?.value);
}

// A page title in the reader's language: `export const generateMetadata = pageTitle("应用")`.
export function pageTitle(text: string) {
  return async () => ({ title: translate(await requestLocale(), text) });
}
