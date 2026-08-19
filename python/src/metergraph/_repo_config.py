"""SDK 0.4+ repository-aware ingestion: discover, detect, and write
.metergraph/config.json.

Discovery is purely file-based (never shells out to git), so a committed
config is honored in production without needing a .git directory at all.
Detection + write only ever runs when discovery finds nothing: it shells out
to git to find the repo's top level and GitHub origin, then writes the config
there exactly once. An existing file is always authoritative and is never
overwritten. Every failure mode here is fail-open -- callers get None and
fall back to app-token ingestion, never an exception.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass


log = logging.getLogger("metergraph")

CONFIG_DIRNAME = ".metergraph"
CONFIG_FILENAME = "config.json"
SUPPORTED_CONFIG_VERSION = 2
_MAX_WALK_UP = 64
_GIT_TIMEOUT_SECONDS = 5

_REMOTE_PATTERNS = (
    re.compile(r"^git@github\.com:(?P<path>[^/]+/[^/]+?)(\.git)?/?$"),
    re.compile(r"^https://github\.com/(?P<path>[^/]+/[^/]+?)(\.git)?/?$"),
    re.compile(r"^ssh://git@github\.com/(?P<path>[^/]+/[^/]+?)(\.git)?/?$"),
)


@dataclass(frozen=True)
class RepoConfig:
    repository: str
    repo_root: str


def normalize_github_remote(url: str) -> str | None:
    """Return 'owner/repo' from a GitHub SSH or HTTPS remote URL, or None
    if the URL isn't a recognized GitHub origin."""
    trimmed = url.strip()
    for pattern in _REMOTE_PATTERNS:
        match = pattern.match(trimmed)
        if match:
            return match.group("path")
    return None


def discover_repo_config(app_root: str) -> RepoConfig | None:
    """Walk upward from app_root looking for .metergraph/config.json.

    Returns None -- silently, this is the normal v1 state -- when nothing is
    found. Logs a warning (but still returns None) if a config file exists
    but fails to parse or carries an unsupported schema version.
    """
    current = os.path.realpath(app_root)
    for _ in range(_MAX_WALK_UP):
        candidate = os.path.join(current, CONFIG_DIRNAME, CONFIG_FILENAME)
        if os.path.isfile(candidate):
            return _load(candidate, current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def _load(path: str, repo_root: str) -> RepoConfig | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("metergraph: found %s but could not read it: %s", path, exc)
        return None
    if not isinstance(doc, dict) or doc.get("version", SUPPORTED_CONFIG_VERSION) != SUPPORTED_CONFIG_VERSION:
        log.warning(
            "metergraph: %s has an unsupported schema version; ignoring "
            "(expected version %d)",
            path,
            SUPPORTED_CONFIG_VERSION,
        )
        return None
    repository = doc.get("repository")
    if not isinstance(repository, str) or "/" not in repository:
        log.warning("metergraph: %s is missing a valid 'repository' field; ignoring", path)
        return None
    return RepoConfig(repository=repository, repo_root=repo_root)


def _run_git(args: list[str], cwd: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    return output or None


def _git_top_level(app_root: str) -> str | None:
    top = _run_git(["rev-parse", "--show-toplevel"], app_root)
    return os.path.realpath(top) if top else None


def _git_origin_url(repo_root: str) -> str | None:
    return _run_git(["remote", "get-url", "origin"], repo_root)


def _write_config_atomically(repo_root: str, repository: str) -> RepoConfig | None:
    """Create .metergraph/config.json if -- and only if -- it doesn't
    already exist. Uses O_CREAT|O_EXCL for an atomic create-only-if-absent;
    a concurrent writer (or a file that appeared between discovery and this
    call) always wins over us, and we simply read back whatever is there."""
    config_dir = os.path.join(repo_root, CONFIG_DIRNAME)
    config_path = os.path.join(config_dir, CONFIG_FILENAME)
    payload = json.dumps({"version": SUPPORTED_CONFIG_VERSION, "repository": repository}) + "\n"
    try:
        os.makedirs(config_dir, exist_ok=True)
        fd = os.open(config_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
    except FileExistsError:
        pass
    except OSError as exc:
        log.warning("metergraph: could not write %s: %s", config_path, exc)
        return None
    return _load(config_path, repo_root)


def ensure_repo_config(app_root: str) -> RepoConfig | None:
    """Discover an existing repo config, or detect+write one once at the
    git top level. Fail-open: any detection or write failure returns None
    (app-token ingestion), never raises."""
    existing = discover_repo_config(app_root)
    if existing is not None:
        return existing
    try:
        repo_root = _git_top_level(app_root)
        if repo_root is None:
            return None
        origin = _git_origin_url(repo_root)
        if origin is None:
            return None
        repository = normalize_github_remote(origin)
        if repository is None:
            return None
        return _write_config_atomically(repo_root, repository)
    except Exception as exc:
        log.debug("metergraph: repo config detection failed: %s", exc)
        return None
