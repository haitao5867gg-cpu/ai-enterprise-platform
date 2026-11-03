import { Suspense } from "react";
import { LoginPage } from "../../components/login-page";
import { pageTitle } from "../../lib/server/locale";

export const generateMetadata = pageTitle("登录");
export default function Login() {
  return (
    <Suspense>
      <LoginPage />
    </Suspense>
  );
}
