import { MediaList } from "../../components/media-list";
import { pageTitle } from "../../lib/server/locale";

export const generateMetadata = pageTitle("媒体库");
export default function MediaPage() {
  return <MediaList />;
}
