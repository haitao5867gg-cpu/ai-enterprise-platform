import "server-only";

export function getLangfuseTraceBaseUrl(): string | null {
  const uiUrl = process.env.AEP_LANGFUSE_UI_URL ?? "http://localhost:3100";
  if (!uiUrl) return null;
  const projectId = process.env.AEP_LANGFUSE_PROJECT_ID ?? "aep-local";
  return `${uiUrl.replace(/\/+$/, "")}/project/${encodeURIComponent(projectId)}/traces/`;
}

export function isLocalMockEnabled(): boolean {
  return (
    process.env.NODE_ENV === "development" &&
    process.env.AEP_MOCK_RUNS === "true"
  );
}
