import { ProjectList } from "../../components/project-list";
import { pageTitle } from "../../lib/server/locale";

export const generateMetadata = pageTitle("项目");
export default function ProjectsPage() {
  return <ProjectList />;
}
