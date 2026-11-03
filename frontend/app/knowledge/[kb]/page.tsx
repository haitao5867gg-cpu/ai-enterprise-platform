import { KnowledgeDetail } from "../../../components/knowledge-detail";
import { pageTitle } from "../../../lib/server/locale";

export const generateMetadata = pageTitle("知识库详情");
export default async function KnowledgePage({
  params,
}: {
  params: Promise<{ kb: string }>;
}) {
  const { kb } = await params;
  return <KnowledgeDetail key={kb} id={kb} />;
}
