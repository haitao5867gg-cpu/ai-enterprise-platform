import { RunDetail } from "../../../components/run-detail";

export const metadata = { title: "运行详情" };

export default async function RunPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <RunDetail key={id} id={id} />;
}
