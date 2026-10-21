/**
 * The shape of the standalone output that the Docker image copies (#1468).
 *
 * Next 15.5 wrote `.next`, `node_modules`, `package.json` and `server.js`. Next 16.4.2 with Turbopack
 * wrote the whole project (`src`, `scripts`, `bun.lock`, the configs) next to them, because one
 * `readFileSync` with a computed path made the file tracer include every file. The Dockerfile copies
 * `.next/standalone` into the image, so an extra file there is an extra file in the image.
 * This module has no side effects at import. `tests/standalone-shape.test.ts` drives it.
 */

import { existsSync, readdirSync } from "node:fs";
import { join } from "node:path";

export const STANDALONE_ENTRIES = Object.freeze([".next", "node_modules", "package.json", "server.js"]);

/**
 * @param {string} directory the `.next/standalone` directory
 * @returns {string[] | null} the entries that must not be there; `null` if there is no standalone build
 */
export function unexpectedStandaloneEntries(directory) {
  if (!existsSync(join(directory, "server.js"))) return null;
  return readdirSync(directory).filter((name) => !STANDALONE_ENTRIES.includes(name)).sort();
}
