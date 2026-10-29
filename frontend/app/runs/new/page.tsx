import { NewRun } from "../../../components/new-run";

export const metadata = { title: "新建运行" };

export default async function NewRunPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  // ?from=<run id> copies that run: same app version, its input to edit.
  const { from } = await searchParams;
  const source = typeof from === "string" && from ? from : undefined;
  return <NewRun key={source ?? ""} from={source} />;
}
