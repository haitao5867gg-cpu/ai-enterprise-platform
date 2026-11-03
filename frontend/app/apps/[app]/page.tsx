import { AppDetailView } from "../../../components/app-detail";
import { pageTitle } from "../../../lib/server/locale";

export const generateMetadata = pageTitle("应用设计与配置");
export default async function AppPage({
  params,
}: {
  params: Promise<{ app: string }>;
}) {
  const { app } = await params;
  return <AppDetailView key={app} app={app} />;
}
