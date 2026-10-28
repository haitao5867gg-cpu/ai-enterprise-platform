import { AppDetailView } from "../../../components/app-detail";

export const metadata = { title: "应用设计与配置" };
export default async function AppPage({
  params,
}: {
  params: Promise<{ app: string }>;
}) {
  const { app } = await params;
  return <AppDetailView key={app} app={app} />;
}
