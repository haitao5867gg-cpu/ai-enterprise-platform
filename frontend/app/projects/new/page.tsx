import { NewProject } from "../../../components/project-detail";
import { pageTitle } from "../../../lib/server/locale";

export const generateMetadata = pageTitle("新建项目");
export default function NewProjectPage() {
  return <NewProject />;
}
