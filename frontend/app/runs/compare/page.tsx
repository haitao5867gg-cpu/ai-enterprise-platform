import { RunCompare } from "../../../components/run-compare";
import { pageTitle } from "../../../lib/server/locale";

export const generateMetadata = pageTitle("对比运行");

export default async function ComparePage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { a, b } = await searchParams;
  const left = typeof a === "string" ? a : "";
  const right = typeof b === "string" ? b : "";
  return <RunCompare key={`${left}|${right}`} a={left} b={right} />;
}
