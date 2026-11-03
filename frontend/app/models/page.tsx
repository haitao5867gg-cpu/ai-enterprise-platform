import { ModelList } from "../../components/model-list";
import { pageTitle } from "../../lib/server/locale";

export const generateMetadata = pageTitle("模型");
export default function ModelsPage() {
  return <ModelList />;
}
