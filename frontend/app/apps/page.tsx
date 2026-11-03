import { AppList } from "../../components/app-list";
import { pageTitle } from "../../lib/server/locale";

export const generateMetadata = pageTitle("应用");
export default function AppsPage() {
  return <AppList />;
}
