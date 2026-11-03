import { SkillList } from "../../components/skill-list";
import { pageTitle } from "../../lib/server/locale";

export const generateMetadata = pageTitle("Skill");
export default function SkillsPage() {
  return <SkillList />;
}
