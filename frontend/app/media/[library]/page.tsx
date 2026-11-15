import { MediaDetail } from "../../../components/media-detail";
import { pageTitle } from "../../../lib/server/locale";

export const generateMetadata = pageTitle("媒体库详情");
export default async function MediaLibraryPage({
  params,
}: {
  params: Promise<{ library: string }>;
}) {
  const { library } = await params;
  return <MediaDetail key={library} id={library} />;
}
