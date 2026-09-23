"""Read-only discovery of repository identity configuration."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass


log = logging.getLogger("metergraph")
CONFIG_DIRNAME = ".metergraph"
CONFIG_FILENAME = "config.json"
SUPPORTED_CONFIG_VERSION = 2
_MAX_WALK_UP = 64


@dataclass(frozen=True)
class RepoConfig:
    repository: str
    repo_root: str


def discover_repo_config(app_root: str) -> RepoConfig | None:
    """Walk upward from app_root looking for .metergraph/config.json."""
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
    if (
        not isinstance(doc, dict)
        or doc.get("version", SUPPORTED_CONFIG_VERSION)
        != SUPPORTED_CONFIG_VERSION
    ):
        log.warning(
            "metergraph: %s has an unsupported schema version; ignoring "
            "(expected version %d)",
            path,
            SUPPORTED_CONFIG_VERSION,
        )
        return None
    repository = doc.get("repository")
    if not isinstance(repository, str) or "/" not in repository:
        log.warning(
            "metergraph: %s is missing a valid 'repository' field; ignoring", path
        )
        return None
    return RepoConfig(repository=repository, repo_root=repo_root)
