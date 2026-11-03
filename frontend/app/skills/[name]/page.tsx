import { SkillDetailView } from "../../../components/skill-detail";
import { pageTitle } from "../../../lib/server/locale";

export const generateMetadata = pageTitle("Skill 内容与版本");
export default async function SkillPage({
  params,
}: {
  params: Promise<{ name: string }>;
}) {
  const { name } = await params;
  return <SkillDetailView key={name} name={name} />;
}
