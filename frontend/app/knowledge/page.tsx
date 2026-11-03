import { KnowledgeList } from "../../components/knowledge-list";
import { pageTitle } from "../../lib/server/locale";

export const generateMetadata = pageTitle("知识库");
export default function KnowledgePage() {
  return <KnowledgeList />;
}
