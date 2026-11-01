import { ProjectDetail } from "../../../components/project-detail";

export const metadata = { title: "项目" };
export default async function ProjectPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <ProjectDetail key={id} id={id} />;
}
