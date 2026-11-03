import { AuditList } from "../../components/audit-list";
import { pageTitle } from "../../lib/server/locale";

export const generateMetadata = pageTitle("操作记录");
export default function AuditPage() {
  return <AuditList />;
}
