/** Read-only discovery of repository identity configuration. */
import { readFileSync, realpathSync } from "node:fs";
import { dirname, join } from "node:path";

export const CONFIG_DIRNAME = ".metergraph";
export const CONFIG_FILENAME = "config.json";
export const SUPPORTED_CONFIG_VERSION = 2;
const MAX_WALK_UP = 64;

export interface RepoConfig {
  repository: string;
  repoRoot: string;
}

function load(path: string, repoRoot: string): RepoConfig | undefined {
  let doc: unknown;
  try {
    doc = JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    console.warn(
      `metergraph: found ${path} but could not read it: ${error instanceof Error ? error.message : String(error)}`,
    );
    return undefined;
  }
  if (
    typeof doc !== "object"
    || doc === null
    || ((doc as Record<string, unknown>).version ?? SUPPORTED_CONFIG_VERSION)
      !== SUPPORTED_CONFIG_VERSION
  ) {
    console.warn(
      `metergraph: ${path} has an unsupported schema version; ignoring (expected version ${SUPPORTED_CONFIG_VERSION})`,
    );
    return undefined;
  }
  const repository = (doc as Record<string, unknown>).repository;
  if (typeof repository !== "string" || !repository.includes("/")) {
    console.warn(`metergraph: ${path} is missing a valid 'repository' field; ignoring`);
    return undefined;
  }
  return { repository, repoRoot };
}

/** Walk upward from appRoot looking for .metergraph/config.json. */
export function discoverRepoConfig(appRoot: string): RepoConfig | undefined {
  let current: string;
  try {
    current = realpathSync(appRoot);
  } catch {
    return undefined;
  }
  for (let i = 0; i < MAX_WALK_UP; i += 1) {
    const candidate = join(current, CONFIG_DIRNAME, CONFIG_FILENAME);
    try {
      readFileSync(candidate);
      return load(candidate, current);
    } catch {
      // No config at this level; keep walking upward.
    }
    const parent = dirname(current);
    if (parent === current) break;
    current = parent;
  }
  return undefined;
}
