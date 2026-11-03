import { ProjectDetail } from "../../../components/project-detail";
import { pageTitle } from "../../../lib/server/locale";

export const generateMetadata = pageTitle("项目");
export default async function ProjectPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <ProjectDetail key={id} id={id} />;
}
