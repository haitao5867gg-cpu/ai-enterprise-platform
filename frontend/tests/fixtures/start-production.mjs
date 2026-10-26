import { cpSync } from "node:fs";

// Match Docker's standalone deployment, including its static asset copy.
cpSync(".next/static", ".next/standalone/.next/static", { recursive: true });
await import("../../.next/standalone/server.js");
