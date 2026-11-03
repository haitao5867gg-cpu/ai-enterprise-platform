import { OpsDashboard } from "../../components/ops-dashboard";
import { pageTitle } from "../../lib/server/locale";

export const generateMetadata = pageTitle("运营看板");
export default function OpsPage() {
  return <OpsDashboard />;
}
