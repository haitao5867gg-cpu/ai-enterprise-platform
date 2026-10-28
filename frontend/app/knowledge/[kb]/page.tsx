import { KnowledgeDetail } from "../../../components/knowledge-detail";

export const metadata = { title: "知识库详情" };
export default async function KnowledgePage({
  params,
}: {
  params: Promise<{ kb: string }>;
}) {
  const { kb } = await params;
  return <KnowledgeDetail key={kb} id={kb} />;
}
