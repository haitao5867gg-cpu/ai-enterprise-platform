import { CatalogList } from "../../components/catalog-list";
import { pageTitle } from "../../lib/server/locale";

export const generateMetadata = pageTitle("资产目录");
export default function CatalogPage() {
  return <CatalogList />;
}
