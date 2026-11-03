import { AppUse } from "../../../components/app-use";
import { pageTitle } from "../../../lib/server/locale";

export const generateMetadata = pageTitle("使用应用");
export default async function UsePage({
  params,
}: {
  params: Promise<{ app: string }>;
}) {
  const { app } = await params;
  return <AppUse key={app} app={app} />;
}
