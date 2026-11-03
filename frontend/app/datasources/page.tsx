import { DatasourceList } from "../../components/datasource-list";
import { pageTitle } from "../../lib/server/locale";

export const generateMetadata = pageTitle("数据源与工具");
export default function DatasourcesPage() {
  return <DatasourceList />;
}
