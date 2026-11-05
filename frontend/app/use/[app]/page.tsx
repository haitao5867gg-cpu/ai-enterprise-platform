import { AppUse } from "../../../components/app-use";
import { pageTitle } from "../../../lib/server/locale";

export const generateMetadata = pageTitle("使用应用");
export default async function UsePage({
  params,
  searchParams,
}: {
  params: Promise<{ app: string }>;
  searchParams: Promise<{ conversation?: string | string[] }>;
}) {
  const { app } = await params;
  // `?conversation=` opens that conversation (a run page links to it).
  const { conversation } = await searchParams;
  return (
    <AppUse
      key={app}
      app={app}
      conversation={typeof conversation === "string" ? conversation : null}
    />
  );
}
