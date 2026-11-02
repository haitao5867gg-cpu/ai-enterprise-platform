import { Suspense } from "react";
import { LoginPage } from "../../components/login-page";

export const metadata = { title: "登录" };
export default function Login() {
  return (
    <Suspense>
      <LoginPage />
    </Suspense>
  );
}
