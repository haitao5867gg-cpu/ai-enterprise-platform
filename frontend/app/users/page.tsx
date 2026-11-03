import { UserList } from "../../components/user-list";
import { pageTitle } from "../../lib/server/locale";

export const generateMetadata = pageTitle("账号");
export default function UsersPage() {
  return <UserList />;
}
