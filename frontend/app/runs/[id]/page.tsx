import { connection } from "next/server";
import { RunDetail } from "../../../components/run-detail";
import { getLangfuseTraceBaseUrl } from "../../../lib/server/config";

export const metadata = { title: "运行详情" };

export default async function RunPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  // Read deployment configuration at request time, including in standalone builds.
  await connection();
  const { id } = await params;
  return (
    <RunDetail
      key={id}
      id={id}
      langfuseTraceBaseUrl={getLangfuseTraceBaseUrl()}
    />
  );
}
