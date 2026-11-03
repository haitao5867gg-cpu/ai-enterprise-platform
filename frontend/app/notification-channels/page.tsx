import { NotificationChannels } from "../../components/notification-channels";
import { pageTitle } from "../../lib/server/locale";

export const generateMetadata = pageTitle("通知渠道");
export default function NotificationChannelsPage() {
  return <NotificationChannels />;
}
