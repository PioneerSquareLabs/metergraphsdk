/**
 * Repository-aware ingest protocol v2: discover, detect, and write
 * .metergraph/config.json.
 *
 * Discovery is purely file-based (never shells out to git), so a committed
 * config is honored in production without needing a .git directory at all.
 * Detection + write only ever runs when discovery finds nothing: it shells
 * out to git to find the repo's top level and GitHub origin, then writes the
 * config there exactly once. An existing file is always authoritative and is
 * never overwritten. Every failure mode here is fail-open -- callers get
 * undefined and fall back to legacy protocol v1, never a thrown error.
 */
import { closeSync, mkdirSync, openSync, readFileSync, realpathSync, statSync, writeSync } from "node:fs";
import { dirname, isAbsolute, join, resolve } from "node:path";

export const CONFIG_DIRNAME = ".metergraph";
export const CONFIG_FILENAME = "config.json";
export const SUPPORTED_CONFIG_VERSION = 2;
const MAX_WALK_UP = 64;

const REMOTE_PATTERNS: RegExp[] = [
  /^git@github\.com:([^/]+\/[^/]+?)(\.git)?\/?$/,
  /^https:\/\/github\.com\/([^/]+\/[^/]+?)(\.git)?\/?$/,
  /^ssh:\/\/git@github\.com\/([^/]+\/[^/]+?)(\.git)?\/?$/,
];

export interface RepoConfig {
  repository: string;
  repoRoot: string;
}

/** Return 'owner/repo' from a GitHub SSH or HTTPS remote URL, or undefined
 * if the URL isn't a recognized GitHub origin. */
export function normalizeGithubRemote(url: string): string | undefined {
  const trimmed = url.trim();
  for (const pattern of REMOTE_PATTERNS) {
    const match = pattern.exec(trimmed);
    if (match) return match[1];
  }
  return undefined;
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
  if (typeof doc !== "object" || doc === null || (doc as Record<string, unknown>).version !== SUPPORTED_CONFIG_VERSION) {
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

/** Walk upward from appRoot looking for .metergraph/config.json. Returns
 * undefined -- silently, the normal v1 state -- when nothing is found. */
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
      // no config at this level; keep walking up
    }
    const parent = dirname(current);
    if (parent === current) break;
    current = parent;
  }
  return undefined;
}

function gitDirectory(repoRoot: string): string | undefined {
  const marker = join(repoRoot, ".git");
  try {
    if (statSync(marker).isDirectory()) return marker;
    const match = /^gitdir:\s*(.+)$/m.exec(readFileSync(marker, "utf8"));
    const path = match?.[1];
    if (!path) return undefined;
    return isAbsolute(path) ? path : resolve(repoRoot, path);
  } catch {
    return undefined;
  }
}

function ownedByCurrentUser(path: string): boolean {
  if (typeof process.getuid !== "function") return true;
  try {
    return statSync(path).uid === process.getuid();
  } catch {
    return false;
  }
}

function gitTopLevel(appRoot: string): string | undefined {
  try {
    let current = realpathSync(appRoot);
    for (let i = 0; i < MAX_WALK_UP; i += 1) {
      const gitDir = gitDirectory(current);
      if (gitDir) {
        // Match Git's safe-directory default: never trust repository metadata
        // owned by another Unix user. Unsupported platforms retain v1's
        // existing fail-open behavior.
        return ownedByCurrentUser(current) && ownedByCurrentUser(gitDir)
          ? current
          : undefined;
      }
      const parent = dirname(current);
      if (parent === current) break;
      current = parent;
    }
  } catch {
    // Fall through to the normal v1 path.
  }
  return undefined;
}

function gitOriginUrl(repoRoot: string): string | undefined {
  try {
    const gitDir = gitDirectory(repoRoot);
    if (!gitDir) return undefined;
    let commonDir = gitDir;
    try {
      const relative = readFileSync(join(gitDir, "commondir"), "utf8").trim();
      if (relative) commonDir = resolve(gitDir, relative);
    } catch {
      // Normal repositories keep config directly in .git.
    }
    const config = readFileSync(join(commonDir, "config"), "utf8");
    let inOrigin = false;
    for (const line of config.split(/\r?\n/)) {
      const section = /^\s*\[remote\s+"([^"]+)"\]\s*$/.exec(line);
      if (section) {
        inOrigin = section[1] === "origin";
        continue;
      }
      if (/^\s*\[/.test(line)) inOrigin = false;
      if (inOrigin) {
        const url = /^\s*url\s*=\s*(.+?)\s*$/.exec(line);
        if (url) return url[1];
      }
    }
  } catch {
    // Missing or unreadable Git metadata is the normal production fallback.
  }
  return undefined;
}

/** Create .metergraph/config.json if -- and only if -- it doesn't already
 * exist. Uses O_CREAT|O_EXCL ("wx") for an atomic create-only-if-absent; a
 * concurrent writer (or a file that appeared between discovery and this
 * call) always wins over us, and we simply read back whatever is there. */
export function writeConfigAtomically(repoRoot: string, repository: string): RepoConfig | undefined {
  const configDir = join(repoRoot, CONFIG_DIRNAME);
  const configPath = join(configDir, CONFIG_FILENAME);
  const payload = `${JSON.stringify({ version: SUPPORTED_CONFIG_VERSION, repository })}\n`;
  try {
    mkdirSync(configDir, { recursive: true });
    const fd = openSync(configPath, "wx", 0o644);
    try {
      writeSync(fd, payload);
    } finally {
      closeSync(fd);
    }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EEXIST") {
      console.warn(
        `metergraph: could not write ${configPath}: ${error instanceof Error ? error.message : String(error)}`,
      );
      return undefined;
    }
  }
  return load(configPath, repoRoot);
}

/** Discover an existing repo config, or detect+write one once at the git top
 * level. Fail-open: any detection or write failure returns undefined
 * (legacy protocol v1), never throws. */
export function ensureRepoConfig(appRoot: string): RepoConfig | undefined {
  const existing = discoverRepoConfig(appRoot);
  if (existing) return existing;
  try {
    const repoRoot = gitTopLevel(appRoot);
    if (!repoRoot) return undefined;
    const origin = gitOriginUrl(repoRoot);
    if (!origin) return undefined;
    const repository = normalizeGithubRemote(origin);
    if (!repository) return undefined;
    return writeConfigAtomically(repoRoot, repository);
  } catch {
    return undefined;
  }
}
