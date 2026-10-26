import "server-only";

export function isLocalMockEnabled(): boolean {
  return (
    process.env.NODE_ENV === "development" &&
    process.env.AEP_MOCK_RUNS === "true"
  );
}
