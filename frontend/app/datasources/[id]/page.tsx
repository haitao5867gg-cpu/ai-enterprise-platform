import { DatasourceDetail } from "../../../components/datasource-detail";
import { pageTitle } from "../../../lib/server/locale";

export const generateMetadata = pageTitle("数据源与工具");
export default async function DatasourcePage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <DatasourceDetail key={id} id={id} />;
}
