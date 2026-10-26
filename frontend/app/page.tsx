// Server-rendered on every request so the page always shows live backend status.
export const dynamic = "force-dynamic";

type Readiness = {
  status: string;
  checks?: Record<string, string>;
  errors?: Record<string, string>;
};

async function getReadiness(): Promise<Readiness> {
  const apiUrl = process.env.AEP_API_INTERNAL_URL ?? "http://localhost:8000";
  try {
    const resp = await fetch(`${apiUrl}/readyz`, { cache: "no-store" });
    return (await resp.json()) as Readiness;
  } catch (err) {
    return { status: "unreachable", errors: { api: String(err) } };
  }
}

export default async function Home() {
  const readiness = await getReadiness();
  return (
    <main>
      <h1>AI Enterprise Platform</h1>
      <p>
        后端状态：<strong>{readiness.status}</strong>
      </p>
      {readiness.checks && (
        <ul>
          {Object.entries(readiness.checks).map(([name, state]) => (
            <li key={name}>
              {name}：{state}
            </li>
          ))}
        </ul>
      )}
      {readiness.errors && Object.keys(readiness.errors).length > 0 && (
        <pre>{JSON.stringify(readiness.errors, null, 2)}</pre>
      )}
    </main>
  );
}
