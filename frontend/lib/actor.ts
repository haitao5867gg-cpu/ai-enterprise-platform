// Shared with the workbench's approval form. Storage can be unavailable in private windows.
const ACTOR_KEY = "aep.actor";

export function savedActor(): string {
  try {
    return localStorage.getItem(ACTOR_KEY) ?? "";
  } catch {
    return "";
  }
}

export function saveActor(actor: string) {
  try {
    localStorage.setItem(ACTOR_KEY, actor);
  } catch {
    // The action still works when the browser refuses storage.
  }
}

export function actorPayload(value = savedActor()): { actor?: string } {
  const actor = value.trim();
  return actor ? { actor } : {};
}
