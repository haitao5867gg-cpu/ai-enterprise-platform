import { NotificationList } from "../../components/notification-list";
import { pageTitle } from "../../lib/server/locale";

export const generateMetadata = pageTitle("通知");
export default function NotificationsPage() {
  return <NotificationList />;
}
