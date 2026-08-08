# Repository-Aware Ingest Protocol v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add repository-aware ingest protocol v2 to both the Python and TypeScript SDKs — non-secret repo registration, app-token→session-token exchange with refresh, and repo-relative frame paths — landing as SDK package release **0.4.0**, while leaving legacy protocol v1 fully intact for repos with no registered config.

**Architecture:** A new setup CLI (`metergraph-setup` console script in Python, `npx metergraph-setup` in TypeScript) detects the working repo's GitHub origin and commits a non-secret `.metergraph/config.json`; the SDK's own import/runtime path only ever *reads* this file, walking upward from `app_root` to find it (never writes it — no side effect from merely importing/wrapping). When a config is discovered, `init()` builds a `SessionManager` that exchanges the existing app token for a short-lived, repo-scoped session token over `POST /v1/ingest/sessions`, caches it in memory, and refreshes it opportunistically — using the session token itself while it's still valid, or by repeating the app-token exchange once it has expired or been rejected. The transport then sends only that session token on every `/v1/ingest` call and never falls back to the app token there. Repos with no discovered config keep behaving exactly as legacy protocol v1 does today — unmodified code path. Frame capture additionally records a normalized, repo-relative path per captured frame once a repo is registered, additive to the existing frame fields.

**Tech Stack:** Python stdlib only (`urllib`, `threading`, `argparse`, `subprocess`, `json`) and TypeScript/Node 18+ stdlib + platform APIs only (`fetch`, `AbortSignal.timeout`, `node:child_process`, `node:fs`, `node:path`) — no new runtime dependencies in either package, matching both SDKs' existing zero-runtime-dependency design.

## Global Constraints

- This release ships as SDK package version **0.4.0** in both `python/pyproject.toml` and `typescript/package.json` — an ordinary minor bump. `protocol_version` is a separate number and stays **2** for this and all future 0.x releases that keep speaking it.
- `.metergraph/config.json` schema (schema/protocol version, not package version): exactly `{"version": 2, "repository": "owner/repository"}`. No `project_id`, no `root_path`. Only setup tooling writes it; SDK import/runtime code must never write it.
- `POST /v1/ingest/sessions` request: `Authorization: Bearer <app_token>`, JSON body `{"protocol_version": 2, "repository": "owner/repository", "sdk_version": "<actual SDK package semver, e.g. 0.4.0>"}`. Response: `{"session_token": "...", "expires_at": "<ISO-8601>", "repository_id": "..."}`.
- The app token is sent only on that exchange endpoint. Every `/v1/ingest` call made in protocol-v2 mode sends the session token and never the app token.
- Both the initial exchange and every refresh are short-timeout (default 3 seconds) and fail-open: no session ⇒ capture buffers/drops silently and the constructor never blocks the caller — no added latency on the customer's LLM call path.
- Legacy protocol v1 (no discovered `.metergraph/config.json`) is byte-for-byte unmodified: same app-token-per-call transport behavior as SDK 0.3.x.
- No token (app or session) may ever appear in a log line, an exception message, or a URL.
- No new runtime dependency in either package (dev-only tooling dependencies for contract verification -- `jsonschema` in Python, `ajv`/`ajv-formats` in TypeScript -- are fine; they ship in neither package's runtime distribution).
- **Single versioned contract artifact.** The `/v1/ingest/sessions` request/response shape has exactly one canonical, authoritative body: the JSON Schema metergraph-internal publishes at `https://metergraph.dev/contracts/ingest-session-v2.schema.json`. metergraphsdk never stores an independent copy of that schema body -- only a pinned `(url, sha256)` pointer in `contracts/ingest-session-v2.lock.json` -- and validates against the fetched canonical document, not a locally-maintained duplicate.

---

## File map

**New:**
- `contracts/ingest-session-v2.lock.json` — the one pinned pointer `(schema_url, sha256)` metergraphsdk holds to the canonical, metergraph-internal-owned schema; never a copy of the schema body itself.
- `python/scripts/verify_ingest_contract.py` — fetches the canonical schema (or a local/URL override), verifies its SHA-256 against the lock file, and validates Python SDK request/response shapes against it.
- `python/tests/test_ingest_contract_verification.py` — offline tests for the Python verifier (local fake server / file override only, no real network).
- `python/src/metergraph/_repo_config.py` — read-only `.metergraph/config.json` discovery, walking up from `app_root`.
- `python/src/metergraph/_session.py` — `SessionManager`: app-token↔session-token exchange, background refresh, fail-open.
- `python/src/metergraph/_setup.py` — `metergraph-setup` CLI: GitHub-origin detection + config write.
- `python/tests/test_repository_aware_ingest.py` — all new Python SDK-behavior tests (grows across tasks 2–9).
- `typescript/scripts/verify-ingest-contract.mjs` — TS equivalent of `verify_ingest_contract.py`.
- `typescript/test/ingest-contract-verification.test.mjs` — offline tests for the TS verifier.
- `typescript/src/repo-config.ts` — TS equivalent of `_repo_config.py`.
- `typescript/src/session.ts` — TS equivalent of `_session.py`.
- `typescript/src/setup.ts` — TS equivalent of `_setup.py`, exposed as the `metergraph-setup` npm bin.
- `typescript/test/repository-aware-ingest.test.mjs` — all new TS SDK-behavior tests (grows across tasks 11–18).

**Modified:**
- `python/pyproject.toml` — version → `0.4.0`, new `[project.scripts]` entry, `jsonschema` added to dev dependencies.
- `python/src/metergraph/_capture.py` — `Options.repo_root`, `_capture_frames()` gains a `p` (repo-relative path) key per frame, `Runtime.call_state` passes it through.
- `python/src/metergraph/_transport.py` — `Writer.__init__` gains `session=`, `_deliver` sources the bearer token from the session when present, 401/403 invalidates the session instead of going fatal.
- `python/src/metergraph/__init__.py` — `init()` discovers repo config and wires `SessionManager` + session-aware `Writer` + `Options.repo_root`; `shutdown()` stops the session manager.
- `python/src/metergraph/_version.py` — fallback version string → `0.4.0`.
- `typescript/package.json` — version → `0.4.0`, new `bin` entry, `ajv`/`ajv-formats` added to dev dependencies, new `verify:contract` script.
- `typescript/src/capture.ts` — `RuntimeOptions.repoRoot`, `frames()` gains a `p` key per frame, `CaptureRuntime.start` passes it through.
- `typescript/src/transport.ts` — `TransportOptions.session`, `deliver()` sources the bearer token from the session when present, 401/403 invalidates the session instead of going fatal.
- `typescript/src/index.ts` — `init()` discovers repo config and wires `SessionManager` + session-aware `Transport` + `RuntimeOptions.repoRoot`; `shutdown()` stops the session manager.
- `typescript/src/version.ts` — `SDK_VERSION` → `"0.4.0"`.
- `README.md`, `python/README.md`, `typescript/README.md`, `examples/README.md` — setup-command documentation, agent-prompt updates, 0.4 references.
- `.github/workflows/ci.yml` — adds `workflow_dispatch`/`schedule` triggers and a new, separately named `contract-verification` job that runs the canonical (non-override) contract check.

`pytest python/tests -q` and `npm test`'s `node --test test/*.test.mjs` auto-discover any new `test_*.py` / `*.test.mjs` file, so ordinary offline unit tests need no further CI wiring. The one deliberate CI change in this plan is the new `contract-verification` job (Task 1, Step 10), which is the only part of this plan that talks to the real network and is intentionally kept separate from those offline jobs.

---

### Task 1: Canonical ingest-session contract — lock manifest and verification tooling

metergraph-internal is the single source of truth for the `/v1/ingest/sessions` contract: it publishes the immutable JSON Schema at `https://metergraph.dev/contracts/ingest-session-v2.schema.json`, plus a manifest/index of published contracts at `https://metergraph.dev/contracts/index.json` (entries: `id`, `url`, `sha256`, `published_at`). metergraphsdk never stores an independent copy of the schema body. It stores exactly one small **lock manifest** — `contracts/ingest-session-v2.lock.json` — holding only the schema's URL and its pinned SHA-256, plus two verification scripts (one per language) that fetch the canonical schema, verify it against that pinned hash, and validate real SDK request/response shapes against it. A local-file or alternate-URL override lets metergraph-internal and metergraphsdk coordinate on a draft schema before it's deployed, without ever touching the pinned hash. This is the only piece of the plan that talks to the network in "canonical" mode; it runs as a separately named CI job/command, never as part of the offline `pytest`/`node --test` unit-test runs.

The two verifier scripts assume metergraph-internal publishes exactly this JSON Schema (Draft 2020-12) at the canonical URL — this is the artifact whose SHA-256 the lock file below pins:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://metergraph.dev/contracts/ingest-session-v2.schema.json",
  "title": "Metergraph ingest session v2",
  "$defs": {
    "request": {
      "type": "object",
      "additionalProperties": false,
      "required": ["protocol_version", "repository", "sdk_version"],
      "properties": {
        "protocol_version": { "const": 2 },
        "repository": { "type": "string", "pattern": "^[^/]+/[^/]+$" },
        "sdk_version": { "type": "string" }
      }
    },
    "response": {
      "type": "object",
      "additionalProperties": false,
      "required": ["session_token", "expires_at", "repository_id"],
      "properties": {
        "session_token": { "type": "string", "minLength": 1 },
        "expires_at": { "type": "string", "format": "date-time" },
        "repository_id": { "type": "string", "minLength": 1 }
      }
    }
  }
}
```

Serialized with `json.dumps(schema, indent=2)` plus a trailing newline, that document's SHA-256 is `fc20625bec077abfb21bf2ef4da1ba1a44f33cb73447f5e42c2a096052f3d2f9` — the value the lock file below pins. If metergraph-internal's actual published bytes differ (even by whitespace) once the endpoint goes live, the canonical verification command in Step 10 will fail loudly with both hashes printed; that is the pin working as intended, not a bug. The fix at that point is a single deliberate, reviewed edit to this lock file's `sha256` (a normal pinned-dependency bump, the same shape as updating any other lockfile hash after confirming out-of-band that the new content is the agreed contract) — never a silent auto-accept, and never a second independent copy of the schema body living in this repo.

**Files:**
- Create: `contracts/ingest-session-v2.lock.json`
- Create: `python/scripts/verify_ingest_contract.py`
- Create: `python/tests/test_ingest_contract_verification.py`
- Modify: `python/pyproject.toml`
- Create: `typescript/scripts/verify-ingest-contract.mjs`
- Create: `typescript/test/ingest-contract-verification.test.mjs`
- Modify: `typescript/package.json`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Produces (Python, `python/scripts/verify_ingest_contract.py`): `SchemaSource` (dataclass: `content: bytes`, `sha256: str`, `origin: str`, `pinned: bool`), `ContractVerificationError`, `resolve_schema_source() -> SchemaSource`, `load_schema_document(source: SchemaSource) -> dict`, `validate_instance(document: dict, definition: str, instance: Any) -> None`, `main() -> int`. Task 4 extends this module's `main()` with `_validate_sdk_examples()` once `metergraph._session` exists.
- Produces (TypeScript, `typescript/scripts/verify-ingest-contract.mjs`): `resolveSchemaSource()`, `loadSchemaDocument(source)`, `validateInstance(document, definition, instance)`, `main()`. Task 13 extends this module's `main()` with `validateSdkExamples()` once `typescript/dist/session.js` exists.
- Consumes: nothing from earlier tasks (this is the first task).

- [ ] **Step 1: Write the failing Python verifier tests**

```python
# python/tests/test_ingest_contract_verification.py
from __future__ import annotations

import hashlib
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "python" / "scripts"))
import verify_ingest_contract as vic  # noqa: E402


EXAMPLE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://metergraph.dev/contracts/ingest-session-v2.schema.json",
    "$defs": {
        "request": {
            "type": "object",
            "additionalProperties": False,
            "required": ["protocol_version", "repository", "sdk_version"],
            "properties": {
                "protocol_version": {"const": 2},
                "repository": {"type": "string", "pattern": "^[^/]+/[^/]+$"},
                "sdk_version": {"type": "string"},
            },
        },
        "response": {
            "type": "object",
            "additionalProperties": False,
            "required": ["session_token", "expires_at", "repository_id"],
            "properties": {
                "session_token": {"type": "string", "minLength": 1},
                "expires_at": {"type": "string", "format": "date-time"},
                "repository_id": {"type": "string", "minLength": 1},
            },
        },
    },
}


def _serve_bytes(content: bytes):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_resolve_schema_source_accepts_a_matching_pinned_hash(monkeypatch, tmp_path):
    content = json.dumps(EXAMPLE_SCHEMA).encode()
    digest = hashlib.sha256(content).hexdigest()
    server = _serve_bytes(content)
    monkeypatch.setattr(
        vic,
        "_read_lock",
        lambda: {"schema_url": f"http://127.0.0.1:{server.server_port}/schema.json", "sha256": digest},
    )
    monkeypatch.setenv("METERGRAPH_CONTRACT_CACHE_DIR", str(tmp_path))

    source = vic.resolve_schema_source()
    server.shutdown()

    assert source.pinned is True
    assert source.sha256 == digest


def test_resolve_schema_source_rejects_a_hash_mismatch(monkeypatch, tmp_path):
    content = json.dumps(EXAMPLE_SCHEMA).encode()
    server = _serve_bytes(content)
    monkeypatch.setattr(
        vic,
        "_read_lock",
        lambda: {"schema_url": f"http://127.0.0.1:{server.server_port}/schema.json", "sha256": "0" * 64},
    )
    monkeypatch.setenv("METERGRAPH_CONTRACT_CACHE_DIR", str(tmp_path))

    with pytest.raises(vic.ContractVerificationError, match="pins"):
        vic.resolve_schema_source()
    server.shutdown()


def test_resolve_schema_source_local_path_override_skips_the_pin_check(monkeypatch, tmp_path, capsys):
    schema_path = tmp_path / "draft.schema.json"
    schema_path.write_text(json.dumps(EXAMPLE_SCHEMA))
    monkeypatch.setattr(
        vic, "_read_lock", lambda: {"schema_url": "https://unused.example/x", "sha256": "0" * 64}
    )
    monkeypatch.setenv("METERGRAPH_CONTRACT_SCHEMA_PATH", str(schema_path))

    source = vic.resolve_schema_source()

    assert source.pinned is False
    assert "skipping sha256 pin check" in capsys.readouterr().err


def test_resolve_schema_source_url_override_skips_the_pin_check(monkeypatch, tmp_path, capsys):
    content = json.dumps(EXAMPLE_SCHEMA).encode()
    server = _serve_bytes(content)
    monkeypatch.setattr(
        vic, "_read_lock", lambda: {"schema_url": "https://unused.example/x", "sha256": "0" * 64}
    )
    monkeypatch.setenv("METERGRAPH_CONTRACT_SCHEMA_URL", f"http://127.0.0.1:{server.server_port}/draft.json")
    monkeypatch.setenv("METERGRAPH_CONTRACT_CACHE_DIR", str(tmp_path))

    source = vic.resolve_schema_source()
    server.shutdown()

    assert source.pinned is False
    assert "skipping sha256 pin check" in capsys.readouterr().err


def test_validate_instance_passes_for_a_conforming_request():
    vic.validate_instance(
        EXAMPLE_SCHEMA,
        "request",
        {"protocol_version": 2, "repository": "owner/repo", "sdk_version": "0.4.0"},
    )  # does not raise


def test_validate_instance_rejects_a_malformed_request():
    import jsonschema

    with pytest.raises(jsonschema.ValidationError):
        vic.validate_instance(
            EXAMPLE_SCHEMA,
            "request",
            {"protocol_version": 1, "repository": "owner/repo", "sdk_version": "0.4.0"},
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd python && python -m pytest tests/test_ingest_contract_verification.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'verify_ingest_contract'`

- [ ] **Step 3: Create the lock manifest and implement the Python verifier**

```json
// contracts/ingest-session-v2.lock.json
{
  "contract_id": "ingest-session-v2",
  "manifest_url": "https://metergraph.dev/contracts/index.json",
  "schema_url": "https://metergraph.dev/contracts/ingest-session-v2.schema.json",
  "sha256": "fc20625bec077abfb21bf2ef4da1ba1a44f33cb73447f5e42c2a096052f3d2f9"
}
```

```python
# python/scripts/verify_ingest_contract.py
"""Fetch, pin-check, and validate the canonical ingest-session-v2 contract.

Canonical source of truth: metergraph-internal publishes the immutable
schema at the URL in contracts/ingest-session-v2.lock.json, plus a
manifest/index at https://metergraph.dev/contracts/index.json. This repo
never stores an independent copy of the schema body -- only a pinned
(URL, sha256) pointer to it in that lock file.

Usage:
    python python/scripts/verify_ingest_contract.py

Override for coordinated development before metergraph-internal deploys a
schema revision (skips the sha256 pin check, still validates shape):
    METERGRAPH_CONTRACT_SCHEMA_PATH=/path/to/draft.schema.json python ...
    METERGRAPH_CONTRACT_SCHEMA_URL=https://staging.metergraph.dev/... python ...
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema


REPO_ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = REPO_ROOT / "contracts" / "ingest-session-v2.lock.json"
FETCH_TIMEOUT_SECONDS = 10.0
CACHE_DIR_ENV = "METERGRAPH_CONTRACT_CACHE_DIR"


@dataclass(frozen=True)
class SchemaSource:
    content: bytes
    sha256: str
    origin: str
    pinned: bool  # True if verified against the lock file's expected hash


class ContractVerificationError(Exception):
    pass


def _cache_dir() -> Path:
    override = os.getenv(CACHE_DIR_ENV)
    path = Path(override) if override else Path(tempfile.gettempdir()) / "metergraph-contracts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read_lock() -> dict[str, Any]:
    return json.loads(LOCK_PATH.read_text())


def resolve_schema_source() -> SchemaSource:
    lock = _read_lock()
    path_override = os.getenv("METERGRAPH_CONTRACT_SCHEMA_PATH")
    url_override = os.getenv("METERGRAPH_CONTRACT_SCHEMA_URL")

    if path_override:
        content = Path(path_override).read_bytes()
        print(
            f"WARNING: using local override schema {path_override}; skipping sha256 pin check",
            file=sys.stderr,
        )
        return SchemaSource(content=content, sha256=_sha256(content), origin=path_override, pinned=False)

    url = url_override or lock["schema_url"]
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
        content = response.read()
    digest = _sha256(content)
    cache_path = _cache_dir() / f"{digest}.json"
    if not cache_path.exists():
        cache_path.write_bytes(content)

    if url_override:
        print(f"WARNING: using override schema URL {url}; skipping sha256 pin check", file=sys.stderr)
        return SchemaSource(content=content, sha256=digest, origin=url, pinned=False)

    expected = lock["sha256"]
    if digest != expected:
        raise ContractVerificationError(
            f"canonical schema at {url} has sha256 {digest}, but {LOCK_PATH} pins "
            f"{expected}. If this is an intentional contract revision, update the "
            "lock file's sha256 after reviewing the change; otherwise this is drift or tampering."
        )
    return SchemaSource(content=content, sha256=digest, origin=url, pinned=True)


def load_schema_document(source: SchemaSource) -> dict[str, Any]:
    return json.loads(source.content)


def validate_instance(document: dict[str, Any], definition: str, instance: Any) -> None:
    jsonschema.validate(instance=instance, schema=document["$defs"][definition])


def main() -> int:
    try:
        source = resolve_schema_source()
        load_schema_document(source)
    except ContractVerificationError as exc:
        print(f"verify-ingest-contract: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"verify-ingest-contract: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    pin_state = "pinned" if source.pinned else "override (unpinned)"
    print(f"verify-ingest-contract: OK -- {source.origin} ({pin_state}, sha256={source.sha256})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

`validate_instance` validates directly against `document["$defs"][definition]` without a `$ref` resolver -- correct as long as `request`/`response` stay self-contained (no cross-references between them), which is true of the schema above.

Add `jsonschema` to `python/pyproject.toml`'s dev dependencies:

```toml
[project.optional-dependencies]
dev = ["pytest>=8", "openai>=2.50.0", "anthropic>=0.40", "google-genai>=1", "jsonschema>=4"]
```

Then install it: `cd python && pip install -e ".[dev]"`. This is a dev-only dependency for contract-verification tooling, not a runtime dependency of the `metergraph` package -- it does not violate the zero-runtime-dependency constraint.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd python && python -m pytest tests/test_ingest_contract_verification.py -v`
Expected: PASS

- [ ] **Step 5: Commit the Python side**

```bash
git add contracts/ingest-session-v2.lock.json python/scripts/verify_ingest_contract.py python/tests/test_ingest_contract_verification.py python/pyproject.toml
git commit -m "Add canonical ingest-session contract lock manifest and Python verifier"
```

- [ ] **Step 6: Write the failing TypeScript verifier tests**

```js
// typescript/test/ingest-contract-verification.test.mjs
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import http from "node:http";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  resolveSchemaSource,
  validateInstance,
} from "../scripts/verify-ingest-contract.mjs";

const EXAMPLE_SCHEMA = {
  $schema: "https://json-schema.org/draft/2020-12/schema",
  $id: "https://metergraph.dev/contracts/ingest-session-v2.schema.json",
  $defs: {
    request: {
      type: "object",
      additionalProperties: false,
      required: ["protocol_version", "repository", "sdk_version"],
      properties: {
        protocol_version: { const: 2 },
        repository: { type: "string", pattern: "^[^/]+/[^/]+$" },
        sdk_version: { type: "string" },
      },
    },
    response: {
      type: "object",
      additionalProperties: false,
      required: ["session_token", "expires_at", "repository_id"],
      properties: {
        session_token: { type: "string", minLength: 1 },
        expires_at: { type: "string", format: "date-time" },
        repository_id: { type: "string", minLength: 1 },
      },
    },
  },
};

function sha256(content) {
  return createHash("sha256").update(content).digest("hex");
}

async function serveBytes(content) {
  const server = http.createServer((request, response) => {
    response.writeHead(200, { "content-type": "application/json" });
    response.end(content);
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return server;
}

test("resolveSchemaSource accepts a matching pinned hash", async (t) => {
  const content = Buffer.from(JSON.stringify(EXAMPLE_SCHEMA));
  const digest = sha256(content);
  const server = await serveBytes(content);
  t.after(async () => new Promise((resolve) => server.close(resolve)));
  const cacheDir = mkdtempSync(join(tmpdir(), "metergraph-contract-cache-"));
  t.after(() => rmSync(cacheDir, { recursive: true, force: true }));

  const source = await resolveSchemaSource({
    lock: { schema_url: `http://127.0.0.1:${server.address().port}/schema.json`, sha256: digest },
    cacheDir,
  });

  assert.equal(source.pinned, true);
  assert.equal(source.sha256, digest);
});

test("resolveSchemaSource rejects a hash mismatch", async (t) => {
  const content = Buffer.from(JSON.stringify(EXAMPLE_SCHEMA));
  const server = await serveBytes(content);
  t.after(async () => new Promise((resolve) => server.close(resolve)));
  const cacheDir = mkdtempSync(join(tmpdir(), "metergraph-contract-cache-"));
  t.after(() => rmSync(cacheDir, { recursive: true, force: true }));

  await assert.rejects(
    () =>
      resolveSchemaSource({
        lock: { schema_url: `http://127.0.0.1:${server.address().port}/schema.json`, sha256: "0".repeat(64) },
        cacheDir,
      }),
    /pins/,
  );
});

test("resolveSchemaSource local path override skips the pin check", async (t) => {
  const dir = mkdtempSync(join(tmpdir(), "metergraph-contract-override-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const schemaPath = join(dir, "draft.schema.json");
  writeFileSync(schemaPath, JSON.stringify(EXAMPLE_SCHEMA));
  const warnings = [];
  const originalWarn = console.warn;
  console.warn = (...args) => warnings.push(args.join(" "));

  const source = await resolveSchemaSource({
    lock: { schema_url: "https://unused.example/x", sha256: "0".repeat(64) },
    schemaPathOverride: schemaPath,
  });
  console.warn = originalWarn;

  assert.equal(source.pinned, false);
  assert.ok(warnings.some((message) => message.includes("skipping sha256 pin check")));
});

test("validateInstance passes for a conforming request", () => {
  validateInstance(EXAMPLE_SCHEMA, "request", {
    protocol_version: 2,
    repository: "owner/repo",
    sdk_version: "0.4.0",
  }); // does not throw
});

test("validateInstance rejects a malformed request", () => {
  assert.throws(() =>
    validateInstance(EXAMPLE_SCHEMA, "request", {
      protocol_version: 1,
      repository: "owner/repo",
      sdk_version: "0.4.0",
    }),
  );
});
```

- [ ] **Step 7: Run tests to verify they fail**

Run: `cd typescript && node --test test/ingest-contract-verification.test.mjs`
Expected: FAIL -- `Cannot find module '../scripts/verify-ingest-contract.mjs'`

- [ ] **Step 8: Implement the TypeScript verifier**

```js
#!/usr/bin/env node
// typescript/scripts/verify-ingest-contract.mjs
// Fetch, pin-check, and validate the canonical ingest-session-v2 contract.
//
// Canonical source of truth: metergraph-internal publishes the immutable
// schema at the URL in contracts/ingest-session-v2.lock.json, plus a
// manifest/index at https://metergraph.dev/contracts/index.json. This repo
// never stores an independent copy of the schema body -- only a pinned
// (URL, sha256) pointer to it in that lock file.
//
// Usage:
//   node typescript/scripts/verify-ingest-contract.mjs
//
// Override for coordinated development before metergraph-internal deploys a
// schema revision (skips the sha256 pin check, still validates shape):
//   METERGRAPH_CONTRACT_SCHEMA_PATH=/path/to/draft.schema.json node ...
//   METERGRAPH_CONTRACT_SCHEMA_URL=https://staging.metergraph.dev/... node ...

import Ajv from "ajv";
import addFormats from "ajv-formats";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(__dirname, "..", "..");
const LOCK_PATH = join(REPO_ROOT, "contracts", "ingest-session-v2.lock.json");
const FETCH_TIMEOUT_MS = 10_000;

function sha256(content) {
  return createHash("sha256").update(content).digest("hex");
}

function defaultCacheDir() {
  const dir = join(tmpdir(), "metergraph-contracts");
  mkdirSync(dir, { recursive: true });
  return dir;
}

function readLock() {
  return JSON.parse(readFileSync(LOCK_PATH, "utf8"));
}

export async function resolveSchemaSource(options = {}) {
  const lock = options.lock ?? readLock();
  const pathOverride = options.schemaPathOverride ?? process.env.METERGRAPH_CONTRACT_SCHEMA_PATH;
  const urlOverride = options.schemaUrlOverride ?? process.env.METERGRAPH_CONTRACT_SCHEMA_URL;

  if (pathOverride) {
    const content = readFileSync(pathOverride);
    console.warn(`WARNING: using local override schema ${pathOverride}; skipping sha256 pin check`);
    return { content, sha256: sha256(content), origin: pathOverride, pinned: false };
  }

  const url = urlOverride ?? lock.schema_url;
  const response = await fetch(url, { signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) });
  if (!response.ok) throw new Error(`fetching ${url} failed with HTTP ${response.status}`);
  const content = Buffer.from(await response.arrayBuffer());
  const digest = sha256(content);
  const cacheDir = options.cacheDir ?? defaultCacheDir();
  const cachePath = join(cacheDir, `${digest}.json`);
  if (!existsSync(cachePath)) writeFileSync(cachePath, content);

  if (urlOverride) {
    console.warn(`WARNING: using override schema URL ${url}; skipping sha256 pin check`);
    return { content, sha256: digest, origin: url, pinned: false };
  }

  if (digest !== lock.sha256) {
    throw new Error(
      `canonical schema at ${url} has sha256 ${digest}, but ${LOCK_PATH} pins ` +
      `${lock.sha256}. If this is an intentional contract revision, update the ` +
      "lock file's sha256 after reviewing the change; otherwise this is drift or tampering.",
    );
  }
  return { content, sha256: digest, origin: url, pinned: true };
}

export function loadSchemaDocument(source) {
  return JSON.parse(source.content.toString("utf8"));
}

export function validateInstance(document, definition, instance) {
  const ajv = new Ajv({ allErrors: true });
  addFormats(ajv);
  const validate = ajv.compile(document.$defs[definition]);
  if (!validate(instance)) {
    throw new Error(`schema validation failed: ${ajv.errorsText(validate.errors)}`);
  }
}

export async function main() {
  try {
    const source = await resolveSchemaSource();
    loadSchemaDocument(source);
    const pinState = source.pinned ? "pinned" : "override (unpinned)";
    console.log(`verify-ingest-contract: OK -- ${source.origin} (${pinState}, sha256=${source.sha256})`);
    return 0;
  } catch (error) {
    console.error(`verify-ingest-contract: ${error instanceof Error ? error.message : String(error)}`);
    return 1;
  }
}

if (process.argv[1] && import.meta.url === `file://${process.argv[1]}`) {
  process.exit(await main());
}
```

Add `ajv`/`ajv-formats` as dev dependencies and a `verify:contract` script in `typescript/package.json`:

```json
{
  "scripts": {
    "build": "tsc -p tsconfig.json",
    "prepack": "npm run build",
    "test": "npm run build && npm run test:types && node --test test/*.test.mjs",
    "test:types": "tsc --noEmit --strict --skipLibCheck --target ES2022 --module NodeNext --moduleResolution NodeNext test/ai-sdk-current.types.ts",
    "verify:contract": "node scripts/verify-ingest-contract.mjs"
  },
  "devDependencies": {
    "ai": "^7.0.55",
    "ajv": "^8.17.0",
    "ajv-formats": "^3.0.0",
    "@types/node": "^24.0.0",
    "typescript": "^5.8.0",
    "openai": "^7",
    "@anthropic-ai/sdk": "^0.30",
    "@google/genai": "^1"
  }
}
```

Then install: `cd typescript && npm install`.

- [ ] **Step 9: Run tests to verify they pass**

Run: `cd typescript && npm install && node --test test/ingest-contract-verification.test.mjs`
Expected: PASS

- [ ] **Step 10: Wire canonical contract verification as a named CI job**

Edit `.github/workflows/ci.yml`'s `on:` block to add manual and scheduled triggers alongside the existing ones:

```yaml
on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:
  schedule:
    - cron: "17 6 * * *"
```

Add a new job:

```yaml
  contract-verification:
    name: contract-verification
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e "python[dev]"
      - run: python python/scripts/verify_ingest_contract.py
      - uses: actions/setup-node@v4
        with:
          node-version: 22
      - run: npm install
        working-directory: typescript
      - run: npm run build
        working-directory: typescript
      - run: npm run verify:contract
        working-directory: typescript
```

This job runs the *canonical* check (no override env vars set) against the real `https://metergraph.dev/contracts/ingest-session-v2.schema.json`. Until metergraph-internal deploys that endpoint, this job is expected to fail -- that is a known, external, out-of-this-repo's-control bootstrap gap, not a defect in this tooling. Do not add it as a required branch-protection status check until it has gone green at least once; it remains a real, always-invokable named job (`contract-verification`) and command (`python python/scripts/verify_ingest_contract.py`, `npm run verify:contract` in `typescript/`) from the moment this task lands, satisfying "named CI job/command" without gating unrelated SDK PRs on an endpoint this repo doesn't own or control the deploy timing of.

- [ ] **Step 11: Commit the TypeScript side and CI wiring**

```bash
git add typescript/scripts/verify-ingest-contract.mjs typescript/test/ingest-contract-verification.test.mjs typescript/package.json typescript/package-lock.json .github/workflows/ci.yml
git commit -m "Add TypeScript ingest-contract verifier and named canonical CI job"
```

### Task 2: Python — GitHub remote normalization and config writer

**Files:**
- Create: `python/src/metergraph/_setup.py`
- Create: `python/tests/test_repository_aware_ingest.py`

**Interfaces:**
- Produces: `normalize_github_remote(url: str) -> str` (raises `RepoDetectionError`), `write_config(repo_root: str, repository: str) -> Path`, `main(argv: list[str] | None = None) -> int`, exception `RepoDetectionError`.

- [ ] **Step 1: Write the failing tests**

This creates the shared test file every later Python task (3 onward) appends to; the header below (`json`, `Path`) is relied on by those later appends without being re-imported.

```python
# python/tests/test_repository_aware_ingest.py
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from metergraph._setup import RepoDetectionError, main, normalize_github_remote


@pytest.mark.parametrize(
    "url,expected",
    [
        ("git@github.com:owner/repo.git", "owner/repo"),
        ("git@github.com:owner/repo", "owner/repo"),
        ("https://github.com/owner/repo.git", "owner/repo"),
        ("https://github.com/owner/repo", "owner/repo"),
        ("https://github.com/owner/repo/", "owner/repo"),
        ("ssh://git@github.com/owner/repo.git", "owner/repo"),
    ],
)
def test_normalize_github_remote_handles_ssh_and_https_forms(url, expected):
    assert normalize_github_remote(url) == expected


def test_normalize_github_remote_rejects_non_github_hosts():
    with pytest.raises(RepoDetectionError):
        normalize_github_remote("git@gitlab.com:owner/repo.git")


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_setup_writes_config_for_https_origin(tmp_path):
    _git("init", cwd=tmp_path)
    _git("remote", "add", "origin", "https://github.com/acme/widgets.git", cwd=tmp_path)

    exit_code = main([str(tmp_path)])

    assert exit_code == 0
    config_path = tmp_path / ".metergraph" / "config.json"
    assert json.loads(config_path.read_text()) == {
        "version": 2,
        "repository": "acme/widgets",
    }


def test_setup_writes_config_for_ssh_origin(tmp_path):
    _git("init", cwd=tmp_path)
    _git("remote", "add", "origin", "git@github.com:acme/widgets.git", cwd=tmp_path)

    assert main([str(tmp_path)]) == 0
    config_path = tmp_path / ".metergraph" / "config.json"
    assert json.loads(config_path.read_text())["repository"] == "acme/widgets"


def test_setup_errors_when_no_git_repo(tmp_path):
    assert main([str(tmp_path)]) == 1
    assert not (tmp_path / ".metergraph").exists()


def test_setup_errors_when_origin_is_not_github(tmp_path):
    _git("init", cwd=tmp_path)
    _git("remote", "add", "origin", "git@gitlab.com:acme/widgets.git", cwd=tmp_path)

    assert main([str(tmp_path)]) == 1
    assert not (tmp_path / ".metergraph").exists()


def test_setup_is_idempotent_when_config_already_matches(tmp_path, capsys):
    _git("init", cwd=tmp_path)
    _git("remote", "add", "origin", "https://github.com/acme/widgets.git", cwd=tmp_path)
    main([str(tmp_path)])
    config_path = tmp_path / ".metergraph" / "config.json"
    written_at = config_path.stat().st_mtime_ns

    assert main([str(tmp_path)]) == 0
    assert config_path.stat().st_mtime_ns == written_at
    assert "already configured" in capsys.readouterr().out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd python && python -m pytest tests/test_repository_aware_ingest.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'metergraph._setup'`

- [ ] **Step 3: Implement `_setup.py`**

```python
# python/src/metergraph/_setup.py
"""CLI: detect the GitHub origin of the current repo and write .metergraph/config.json.

Only this explicit setup entry point ever writes the config file. Importing
or running the SDK at runtime must never trigger a write here.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


CONFIG_RELATIVE_PATH = Path(".metergraph") / "config.json"
CONFIG_SCHEMA_VERSION = 2

_REMOTE_PATTERNS = (
    re.compile(r"^git@github\.com:(?P<path>[^/]+/[^/]+?)(\.git)?/?$"),
    re.compile(r"^https://github\.com/(?P<path>[^/]+/[^/]+?)(\.git)?/?$"),
    re.compile(r"^ssh://git@github\.com/(?P<path>[^/]+/[^/]+?)(\.git)?/?$"),
)


class RepoDetectionError(Exception):
    pass


def normalize_github_remote(url: str) -> str:
    """Return 'owner/repo' from a GitHub SSH or HTTPS remote URL."""
    trimmed = url.strip()
    for pattern in _REMOTE_PATTERNS:
        match = pattern.match(trimmed)
        if match:
            return match.group("path")
    raise RepoDetectionError(f"remote '{url}' is not a recognized GitHub origin")


def _read_origin_url(repo_path: str) -> str:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RepoDetectionError(f"could not run git in {repo_path!r}: {exc}") from exc
    if result.returncode != 0:
        raise RepoDetectionError(
            f"no git remote named 'origin' found in {repo_path!r}; "
            "run this inside a git repo with a GitHub origin"
        )
    return result.stdout.strip()


def _read_existing_repository(config_path: Path) -> str | None:
    try:
        doc = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return doc.get("repository") if isinstance(doc, dict) else None


def write_config(repo_root: str, repository: str) -> Path:
    config_path = Path(repo_root) / CONFIG_RELATIVE_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({"version": CONFIG_SCHEMA_VERSION, "repository": repository}, indent=2)
        + "\n"
    )
    return config_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="metergraph-setup",
        description="Detect this repo's GitHub origin and write .metergraph/config.json",
    )
    parser.add_argument(
        "path", nargs="?", default=".", help="Repository root (default: current directory)"
    )
    args = parser.parse_args(argv)

    try:
        remote = _read_origin_url(args.path)
        repository = normalize_github_remote(remote)
    except RepoDetectionError as exc:
        print(f"metergraph-setup: {exc}", file=sys.stderr)
        return 1

    config_path = Path(args.path) / CONFIG_RELATIVE_PATH
    if _read_existing_repository(config_path) == repository:
        print(f"metergraph-setup: {config_path} already configured for {repository}")
        return 0

    written_path = write_config(args.path, repository)
    print(f"metergraph-setup: wrote {written_path} for repository {repository}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd python && python -m pytest tests/test_repository_aware_ingest.py -v`
Expected: PASS (all tests from Task 1 and Task 2)

- [ ] **Step 5: Commit**

```bash
git add python/src/metergraph/_setup.py python/tests/test_repository_aware_ingest.py
git commit -m "Add Python metergraph-setup: GitHub origin detection and config write"
```

### Task 3: Python — repo config discovery

**Files:**
- Create: `python/src/metergraph/_repo_config.py`
- Modify: `python/tests/test_repository_aware_ingest.py`

**Interfaces:**
- Consumes: nothing new (pure filesystem read).
- Produces: `RepoConfig` (frozen dataclass: `repository: str`, `repo_root: str`), `discover_repo_config(app_root: str) -> RepoConfig | None`. Consumed by Task 8 (`init()` wiring).

- [ ] **Step 1: Write the failing tests**

```python
# append to python/tests/test_repository_aware_ingest.py
import logging

from metergraph._repo_config import discover_repo_config


def test_discover_repo_config_finds_file_at_app_root(tmp_path):
    (tmp_path / ".metergraph").mkdir()
    (tmp_path / ".metergraph" / "config.json").write_text(
        json.dumps({"version": 2, "repository": "acme/widgets"})
    )

    config = discover_repo_config(str(tmp_path))

    assert config is not None
    assert config.repository == "acme/widgets"
    assert config.repo_root == str(tmp_path)


def test_discover_repo_config_walks_up_from_a_nested_app_root(tmp_path):
    (tmp_path / ".metergraph").mkdir()
    (tmp_path / ".metergraph" / "config.json").write_text(
        json.dumps({"version": 2, "repository": "acme/monorepo"})
    )
    nested = tmp_path / "services" / "backend"
    nested.mkdir(parents=True)

    config = discover_repo_config(str(nested))

    assert config is not None
    assert config.repository == "acme/monorepo"
    assert config.repo_root == str(tmp_path)


def test_discover_repo_config_returns_none_and_logs_nothing_when_absent(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="metergraph"):
        config = discover_repo_config(str(tmp_path))

    assert config is None
    assert caplog.records == []


def test_discover_repo_config_ignores_malformed_json_and_warns(tmp_path, caplog):
    (tmp_path / ".metergraph").mkdir()
    (tmp_path / ".metergraph" / "config.json").write_text("{not json")

    with caplog.at_level(logging.WARNING, logger="metergraph"):
        config = discover_repo_config(str(tmp_path))

    assert config is None
    assert any("could not read it" in r.getMessage() for r in caplog.records)


def test_discover_repo_config_ignores_unsupported_version_and_warns(tmp_path, caplog):
    (tmp_path / ".metergraph").mkdir()
    (tmp_path / ".metergraph" / "config.json").write_text(
        json.dumps({"version": 99, "repository": "acme/widgets"})
    )

    with caplog.at_level(logging.WARNING, logger="metergraph"):
        config = discover_repo_config(str(tmp_path))

    assert config is None
    assert any("unsupported schema version" in r.getMessage() for r in caplog.records)


def test_discover_repo_config_works_without_a_git_directory(tmp_path):
    """Simulates a container/build artifact that ships .metergraph/ but not .git/."""
    (tmp_path / ".metergraph").mkdir()
    (tmp_path / ".metergraph" / "config.json").write_text(
        json.dumps({"version": 2, "repository": "acme/widgets"})
    )
    assert not (tmp_path / ".git").exists()

    config = discover_repo_config(str(tmp_path))

    assert config is not None
    assert config.repository == "acme/widgets"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd python && python -m pytest tests/test_repository_aware_ingest.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'metergraph._repo_config'`

- [ ] **Step 3: Implement `_repo_config.py`**

```python
# python/src/metergraph/_repo_config.py
"""Read-only discovery of the repository-aware ingest config (.metergraph/config.json).

Never writes anything -- only metergraph._setup does that. Discovery is
purely file-based (it never shells out to git), so it works the same way
inside a container or build artifact that ships .metergraph/ but not .git/.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any


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
            doc: Any = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("metergraph: found %s but could not read it: %s", path, exc)
        return None
    if not isinstance(doc, dict) or doc.get("version") != SUPPORTED_CONFIG_VERSION:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd python && python -m pytest tests/test_repository_aware_ingest.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/src/metergraph/_repo_config.py python/tests/test_repository_aware_ingest.py
git commit -m "Add Python .metergraph/config.json discovery"
```

### Task 4: Python — SessionManager initial exchange

**Files:**
- Create: `python/src/metergraph/_session.py`
- Modify: `python/tests/test_repository_aware_ingest.py`
- Modify: `python/scripts/verify_ingest_contract.py`
- Modify: `python/tests/test_ingest_contract_verification.py`

**Interfaces:**
- Consumes: `FailureLogger` from `metergraph._failure_log`; `resolve_schema_source`, `load_schema_document`, `validate_instance` from `verify_ingest_contract` (Task 1).
- Produces: `build_exchange_request_body(repository: str, sdk_version: str) -> dict`, `parse_session_response(doc: Mapping) -> tuple[str, float, str | None]` (raises `ValueError` if `session_token` is missing), `SessionManager(app_token, base_url, *, repository, sdk_version, poll_seconds=15.0, timeout_seconds=3.0)` with `get_token() -> str | None`, `invalidate() -> None`, `stop() -> None`. Consumed by Task 6 (`Writer`) and Task 8 (`init()`).

- [ ] **Step 1: Write the failing tests**

```python
# append to python/tests/test_repository_aware_ingest.py
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from metergraph._session import SessionManager


def _serve(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_session_manager_caches_token_from_a_successful_exchange():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "session_token": "session-abc",
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                "repository_id": "repo_123",
            }).encode())

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    manager = SessionManager(
        "app-token-secret",
        f"http://127.0.0.1:{server.server_port}",
        repository="owner/repo",
        sdk_version="0.4.0",
        poll_seconds=10,
    )
    time.sleep(0.3)
    manager.stop()
    server.shutdown()

    assert manager.get_token() == "session-abc"


def test_session_manager_exchange_request_has_the_agreed_shape():
    captured = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            captured["headers"] = dict(self.headers)
            captured["body"] = json.loads(
                self.rfile.read(int(self.headers["Content-Length"]))
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "session_token": "session-abc",
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                "repository_id": "repo_123",
            }).encode())

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    manager = SessionManager(
        "app-token-secret",
        f"http://127.0.0.1:{server.server_port}",
        repository="owner/repository",
        sdk_version="0.4.0",
        poll_seconds=10,
    )
    time.sleep(0.3)
    manager.stop()
    server.shutdown()

    assert sorted(captured["body"].keys()) == ["protocol_version", "repository", "sdk_version"]
    assert captured["body"]["protocol_version"] == 2
    assert captured["headers"]["Authorization"] == "Bearer app-token-secret"
    assert manager.get_token() == "session-abc"


def test_session_manager_stays_unset_on_server_error(caplog):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(500)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    with caplog.at_level(logging.WARNING, logger="metergraph"):
        manager = SessionManager(
            "app-token-secret",
            f"http://127.0.0.1:{server.server_port}",
            repository="owner/repo",
            sdk_version="0.4.0",
            poll_seconds=10,
        )
        time.sleep(0.3)
    manager.stop()
    server.shutdown()

    assert manager.get_token() is None
    assert any("session exchange" in r.getMessage() for r in caplog.records)


def test_session_manager_stays_unset_on_malformed_response():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"unexpected": "shape"}).encode())

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    manager = SessionManager(
        "app-token-secret",
        f"http://127.0.0.1:{server.server_port}",
        repository="owner/repo",
        sdk_version="0.4.0",
        poll_seconds=10,
    )
    time.sleep(0.3)
    manager.stop()
    server.shutdown()

    assert manager.get_token() is None


def test_session_manager_is_fail_open_on_timeout():
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen(1)
    port = server_socket.getsockname()[1]

    def accept_and_hang():
        try:
            conn, _ = server_socket.accept()
            time.sleep(2)
            conn.close()
        except OSError:
            pass

    threading.Thread(target=accept_and_hang, daemon=True).start()

    start = time.monotonic()
    manager = SessionManager(
        "app-token-secret",
        f"http://127.0.0.1:{port}",
        repository="owner/repo",
        sdk_version="0.4.0",
        timeout_seconds=0.2,
        poll_seconds=10,
    )
    time.sleep(0.5)
    elapsed = time.monotonic() - start

    assert manager.get_token() is None
    assert elapsed < 2.0
    manager.stop()
    server_socket.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd python && python -m pytest tests/test_repository_aware_ingest.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'metergraph._session'`

- [ ] **Step 3: Implement `_session.py`**

```python
# python/src/metergraph/_session.py
"""Session-token exchange and refresh for repository-aware (protocol v2) ingest."""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from ._failure_log import FailureLogger


log = logging.getLogger("metergraph")

EXCHANGE_TIMEOUT_SECONDS = 3.0
REFRESH_MARGIN_SECONDS = 30.0
MIN_TTL_SECONDS = 5.0
DEFAULT_TTL_SECONDS = 60.0


def _ttl_seconds(expires_at: Any) -> float:
    if not isinstance(expires_at, str):
        return DEFAULT_TTL_SECONDS
    try:
        parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return DEFAULT_TTL_SECONDS
    return max(MIN_TTL_SECONDS, (parsed - datetime.now(timezone.utc)).total_seconds())


def build_exchange_request_body(repository: str, sdk_version: str) -> dict[str, Any]:
    """The exact body shape POSTed to /v1/ingest/sessions -- pulled out as a
    pure function so python/scripts/verify_ingest_contract.py (Task 1) can
    validate it against the canonical schema without needing a live HTTP
    round-trip."""
    return {"protocol_version": 2, "repository": repository, "sdk_version": sdk_version}


def parse_session_response(doc: Any) -> tuple[str, float, str | None]:
    """Extract (session_token, ttl_seconds, repository_id) from a response
    body. Raises ValueError if the document has no usable session_token --
    pulled out as a pure function for the same reason as
    build_exchange_request_body above."""
    token = doc.get("session_token") if isinstance(doc, dict) else None
    if not isinstance(token, str) or not token:
        raise ValueError("response missing session_token")
    return token, _ttl_seconds(doc.get("expires_at")), doc.get("repository_id")


class SessionManager:
    """Exchanges the long-lived app token for a short-lived, repo-scoped
    session token and keeps it refreshed in the background.

    Fails open: get_token() returns None rather than blocking whenever no
    valid session is cached, and construction never blocks the caller --
    the first exchange happens on a background thread.
    """

    def __init__(
        self,
        app_token: str,
        base_url: str,
        *,
        repository: str,
        sdk_version: str,
        poll_seconds: float = 15.0,
        timeout_seconds: float = EXCHANGE_TIMEOUT_SECONDS,
    ) -> None:
        self._app_token = app_token
        self._url = f"{base_url.rstrip('/')}/v1/ingest/sessions"
        self._repository = repository
        self._sdk_version = sdk_version
        self._poll_seconds = max(0.05, poll_seconds)
        self._timeout_seconds = max(0.05, timeout_seconds)
        self._lock = threading.Lock()
        self._session_token: str | None = None
        self._expires_at = 0.0
        self._repository_id: str | None = None
        self._failure_log = FailureLogger()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="metergraph-session", daemon=True
        )
        self._thread.start()

    def get_token(self) -> str | None:
        with self._lock:
            if self._session_token and time.monotonic() < self._expires_at:
                return self._session_token
            return None

    def invalidate(self) -> None:
        with self._lock:
            self._session_token = None
            self._expires_at = 0.0

    def _run(self) -> None:
        self._exchange(use_session_token=False)
        while not self._stop.is_set():
            with self._lock:
                has_token = self._session_token is not None
                remaining = self._expires_at - time.monotonic()
            wait = (
                min(self._poll_seconds, remaining - REFRESH_MARGIN_SECONDS)
                if has_token and remaining > REFRESH_MARGIN_SECONDS
                else self._poll_seconds
            )
            self._stop.wait(max(0.05, wait))
            if self._stop.is_set():
                return
            with self._lock:
                use_session_token = self._session_token is not None
            self._exchange(use_session_token=use_session_token)

    def _exchange(self, *, use_session_token: bool) -> bool:
        with self._lock:
            bearer = (
                self._session_token
                if use_session_token and self._session_token
                else self._app_token
            )
        body = json.dumps(
            build_exchange_request_body(self._repository, self._sdk_version)
        ).encode()
        request = urllib.request.Request(
            self._url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {bearer}",
                "Content-Type": "application/json",
                "Cache-Control": "no-store",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self._timeout_seconds
            ) as response:
                doc = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if use_session_token and exc.code in (401, 403):
                self.invalidate()
                return self._exchange(use_session_token=False)
            self._failure_log.report(
                "session_exchange_error",
                f"session exchange against {self._url} failed with HTTP {exc.code}",
            )
            return False
        except Exception as exc:
            self._failure_log.report(
                "session_exchange_error",
                f"session exchange against {self._url} failed: "
                f"{type(exc).__name__}: {exc}",
            )
            return False
        try:
            token, ttl_seconds, repository_id = parse_session_response(doc)
        except ValueError:
            self._failure_log.report(
                "session_exchange_error",
                f"session exchange against {self._url} returned no session_token",
            )
            return False
        with self._lock:
            self._session_token = token
            self._expires_at = time.monotonic() + ttl_seconds
            self._repository_id = repository_id
        return True

    def stop(self) -> None:
        self._stop.set()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=1)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd python && python -m pytest tests/test_repository_aware_ingest.py -v`
Expected: PASS

- [ ] **Step 5: Write the failing test tying the verifier to the real SDK**

Now that `metergraph._session` exists, extend the contract-verification tests from Task 1 to prove the real request/response handling -- not just a literal example -- conforms to the schema. This still runs fully offline via the local-path override.

```python
# append to python/tests/test_ingest_contract_verification.py
def test_verify_ingest_contract_validates_the_real_python_sdk_request_and_response(
    monkeypatch, tmp_path
):
    schema_path = tmp_path / "draft.schema.json"
    schema_path.write_text(json.dumps(EXAMPLE_SCHEMA))
    monkeypatch.setenv("METERGRAPH_CONTRACT_SCHEMA_PATH", str(schema_path))

    exit_code = vic.main()

    assert exit_code == 0
```

- [ ] **Step 6: Run test to verify it fails**

Run: `cd python && python -m pytest tests/test_ingest_contract_verification.py -v -k real_python_sdk`
Expected: FAIL -- `main()` currently only fetches and parses the schema; it never imports or validates against `metergraph._session`, so this test either errors or passes vacuously without exercising the real SDK. Confirm it fails (or add a temporary `assert False` marker check) before proceeding -- the point of this step is to prove Step 7 is necessary, not to skip straight to it.

- [ ] **Step 7: Wire the verifier to the real SDK**

Edit `python/scripts/verify_ingest_contract.py`. Add a function that imports and exercises the real SDK, and call it from `main()`:

```python
def _validate_sdk_examples(document: dict[str, Any]) -> None:
    """Validate the real Python SDK's request/response handling, not just a
    literal example -- imported lazily so this module is still usable
    (fetch/hash/cache/override logic) before metergraph._session exists."""
    from metergraph._session import build_exchange_request_body, parse_session_response
    from metergraph._version import SDK_VERSION

    request_body = build_exchange_request_body("owner/repository", SDK_VERSION)
    validate_instance(document, "request", request_body)

    example_response = {
        "session_token": "mgs_example_session_token",
        "expires_at": "2026-08-08T12:34:56+00:00",
        "repository_id": "repo_example",
    }
    validate_instance(document, "response", example_response)
    token, _ttl, _repo_id = parse_session_response(example_response)
    assert token == example_response["session_token"]
```

Update `main()` to call it:

```python
def main() -> int:
    try:
        source = resolve_schema_source()
        document = load_schema_document(source)
        _validate_sdk_examples(document)
    except ContractVerificationError as exc:
        print(f"verify-ingest-contract: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"verify-ingest-contract: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    pin_state = "pinned" if source.pinned else "override (unpinned)"
    print(f"verify-ingest-contract: OK -- {source.origin} ({pin_state}, sha256={source.sha256})")
    return 0
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `cd python && python -m pytest tests/test_ingest_contract_verification.py tests/test_repository_aware_ingest.py -v`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add python/src/metergraph/_session.py python/tests/test_repository_aware_ingest.py python/scripts/verify_ingest_contract.py python/tests/test_ingest_contract_verification.py
git commit -m "Add Python SessionManager and wire it into contract verification"
```

### Task 5: Python — SessionManager refresh and expiry/rejection fallback

**Files:**
- Modify: `python/tests/test_repository_aware_ingest.py`

**Interfaces:**
- Consumes: `SessionManager` from Task 4 (no production code changes -- `_exchange`'s `use_session_token` branch and the 401/403 fallback in Task 4's implementation already cover this; this task adds the tests that prove it).

- [ ] **Step 1: Write the failing tests**

```python
# append to python/tests/test_repository_aware_ingest.py
def test_session_manager_refreshes_using_the_session_token_before_expiry():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            calls.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "session_token": f"session-{len(calls)}",
                "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
                "repository_id": "repo_123",
            }).encode())

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    manager = SessionManager(
        "app-token-secret",
        f"http://127.0.0.1:{server.server_port}",
        repository="owner/repo",
        sdk_version="0.4.0",
        poll_seconds=0.1,
    )
    time.sleep(0.5)
    manager.stop()
    server.shutdown()

    assert len(calls) >= 2
    assert calls[0] == "Bearer app-token-secret"
    assert calls[1] == "Bearer session-1"


def test_session_manager_falls_back_to_app_token_when_session_token_is_rejected():
    calls = []
    issued = {"count": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            auth = self.headers.get("Authorization")
            calls.append(auth)
            if auth == "Bearer app-token-secret":
                issued["count"] += 1
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "session_token": f"session-{issued['count']}",
                    "expires_at": (
                        datetime.now(timezone.utc) + timedelta(seconds=1)
                    ).isoformat(),
                    "repository_id": "repo_123",
                }).encode())
            else:
                self.send_response(401)
                self.end_headers()

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    manager = SessionManager(
        "app-token-secret",
        f"http://127.0.0.1:{server.server_port}",
        repository="owner/repo",
        sdk_version="0.4.0",
        poll_seconds=0.1,
    )
    time.sleep(0.5)
    manager.stop()
    server.shutdown()

    # every session-token refresh gets rejected, so the manager keeps
    # re-exchanging the app token instead of getting stuck unauthenticated
    assert issued["count"] >= 2
    assert manager.get_token() is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd python && python -m pytest tests/test_repository_aware_ingest.py -v -k refresh`
Expected: These specific tests should already PASS against Task 4's implementation (no new production code in this task) -- run once to confirm before proceeding. If either fails, the bug is in `_session.py`'s `_run`/`_exchange` interaction from Task 4; fix there.

- [ ] **Step 3: N/A**

No production code change in this task -- it is pure test coverage over existing behavior.

- [ ] **Step 4: Run the full new test file to verify everything passes**

Run: `cd python && python -m pytest tests/test_repository_aware_ingest.py -v`
Expected: PASS (all tests so far)

- [ ] **Step 5: Commit**

```bash
git add python/tests/test_repository_aware_ingest.py
git commit -m "Add Python SessionManager refresh and rejection-fallback tests"
```

### Task 6: Python — Writer session-mode wiring

**Files:**
- Modify: `python/src/metergraph/_transport.py`
- Modify: `python/tests/test_repository_aware_ingest.py`

**Interfaces:**
- Consumes: any object exposing `get_token() -> str | None` and `invalidate() -> None` (duck-typed; `SessionManager` from Task 4, or a test fake).
- Produces: `Writer(token, base_url, *, session=None, ...)`. Consumed by Task 8 (`init()`).

- [ ] **Step 1: Write the failing tests**

```python
# append to python/tests/test_repository_aware_ingest.py
from metergraph._transport import Writer


class FakeSession:
    def __init__(self, token=None):
        self._token = token
        self.invalidated = False

    def get_token(self):
        return self._token

    def invalidate(self):
        self.invalidated = True
        self._token = None


def test_writer_buffers_without_sending_until_a_session_token_exists():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.headers.get("Authorization"), body))
            self.send_response(202)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    session = FakeSession(token=None)
    writer = Writer(
        "app-token-unused",
        f"http://127.0.0.1:{server.server_port}",
        session=session,
        flush_seconds=0.2,
    )
    writer.enqueue({"row": 1})
    writer.flush(0.5)
    assert received == []  # no session token yet -- nothing sent

    session._token = "session-abc"
    writer.enqueue({"row": 2})
    assert writer.flush(2)
    writer.shutdown()
    server.shutdown()

    assert received
    auth, body = received[-1]
    assert auth == "Bearer session-abc"
    assert [row["row"] for row in body["rows"]] == [2]


def test_writer_invalidates_session_on_401_instead_of_going_fatal():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(401)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    session = FakeSession(token="stale-session-token")
    writer = Writer(
        "app-token-unused",
        f"http://127.0.0.1:{server.server_port}",
        session=session,
        flush_seconds=0.2,
    )
    writer.enqueue({"row": 1})
    writer.flush(1)
    writer.shutdown()
    server.shutdown()

    assert session.invalidated is True
    assert writer._fatal is False


def test_writer_without_a_session_keeps_legacy_app_token_behavior():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.headers.get("Authorization"), body))
            self.send_response(202)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    writer = Writer(
        "app-token-secret", f"http://127.0.0.1:{server.server_port}", flush_seconds=0.2
    )
    writer.enqueue({"row": 1})
    assert writer.flush(2)
    writer.shutdown()
    server.shutdown()

    assert received
    assert received[0][0] == "Bearer app-token-secret"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd python && python -m pytest tests/test_repository_aware_ingest.py -v`
Expected: FAIL with `TypeError: Writer.__init__() got an unexpected keyword argument 'session'`

- [ ] **Step 3: Wire `Writer` for session-mode delivery**

Edit `python/src/metergraph/_transport.py`:

```python
# add near the top, alongside the existing imports
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._session import SessionManager
```

```python
# Writer.__init__ signature: add a session parameter
    def __init__(
        self,
        token: str,
        base_url: str,
        *,
        queue_size: int = 2000,
        batch_size: int = 100,
        flush_seconds: float = 5.0,
        session: "SessionManager | None" = None,
    ) -> None:
        self._token = token
        self._session = session
        self._url = f"{base_url.rstrip('/')}/v1/ingest"
        ...  # rest unchanged
```

Replace the start of `_deliver` (the auth header construction) with a session-aware token lookup:

```python
    def _deliver(self, rows: list[dict[str, Any]]) -> bool:
        if self._fatal or time.monotonic() < self._retry_at:
            self._dropped += len(rows)
            return False
        token = self._session.get_token() if self._session else self._token
        if token is None:
            self._dropped += len(rows)
            self._retry_at = time.monotonic() + 1.0
            self._failure_log.report(
                "session_pending",
                f"no active ingest session yet for {self._url}; "
                "buffering until one is established",
            )
            return False
        meta = {"dropped": self._dropped, "transport_errors": self._errors}
        body = json.dumps(
            {"schema_version": 1, "rows": rows, "meta": meta},
            separators=(",", ":"),
        ).encode()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": f"metergraph-python/{SDK_VERSION}",
        }
```

Update the 401/403 branch inside the existing `except urllib.error.HTTPError as exc:` block:

```python
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                if self._session is not None:
                    self._session.invalidate()
                    self._dropped += len(rows)
                    self._failure_log.report(
                        "session_rejected",
                        f"ingest rejected the session token with HTTP {exc.code}; "
                        "will re-exchange",
                    )
                    return False
                self._fatal = True
                log.warning(
                    "Metergraph authentication failed; capture disabled for this process"
                )
            elif exc.code == 413 and len(rows) > 1:
                ...  # unchanged
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd python && python -m pytest tests/test_repository_aware_ingest.py -v`
Expected: PASS. Also re-run the full pre-existing suite to confirm no regression: `cd python && python -m pytest tests -v` -- `test_writer_auth_failure_is_fatal_and_logged_once` (legacy, no session) must still PASS unchanged.

- [ ] **Step 5: Commit**

```bash
git add python/src/metergraph/_transport.py python/tests/test_repository_aware_ingest.py
git commit -m "Wire Python Writer to send the session token when present"
```

### Task 7: Python — frame capture repo-relative path

**Files:**
- Modify: `python/src/metergraph/_capture.py`
- Modify: `python/tests/test_repository_aware_ingest.py`

**Interfaces:**
- Produces: `Options.repo_root: str | None = None`; each frame dict in `frames_json` gains an additive `"p"` key (repo-relative POSIX path) when `repo_root` is set and the frame's file is under it.

- [ ] **Step 1: Write the failing test**

```python
# append to python/tests/test_repository_aware_ingest.py
from types import SimpleNamespace

from metergraph import _capture
from metergraph._capture import Options, Runtime


class _Rows:
    def __init__(self):
        self.rows = []

    def enqueue(self, row):
        self.rows.append(row)
        return True


def test_frame_capture_adds_repo_relative_path_alongside_existing_fields():
    rows = _Rows()
    package_root = Path(__file__).parents[1]  # python/
    repo_root = Path(__file__).parents[2]  # metergraphsdk/
    _capture.set_runtime(
        Runtime(rows, Options(app_root=str(package_root), repo_root=str(repo_root)))
    )

    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(id="req_1", model="gpt-test", choices=[])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions()), responses=None
    )
    wrapped = _capture.wrap(client, provider="openai")
    wrapped.chat.completions.create(model="gpt-test", messages=[])

    frame = rows.rows[0]["frames_json"][0]
    assert frame["m"] == "tests.test_repository_aware_ingest"
    assert frame["f"].endswith(
        "test_frame_capture_adds_repo_relative_path_alongside_existing_fields"
    )
    assert frame["p"] == "python/tests/test_repository_aware_ingest.py"
    _capture.set_runtime(None)


def test_frame_capture_omits_repo_relative_path_without_repo_root():
    rows = _Rows()
    package_root = Path(__file__).parents[1]
    _capture.set_runtime(Runtime(rows, Options(app_root=str(package_root))))

    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(id="req_1", model="gpt-test", choices=[])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions()), responses=None
    )
    wrapped = _capture.wrap(client, provider="openai")
    wrapped.chat.completions.create(model="gpt-test", messages=[])

    frame = rows.rows[0]["frames_json"][0]
    assert "p" not in frame
    _capture.set_runtime(None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd python && python -m pytest tests/test_repository_aware_ingest.py -v`
Expected: FAIL with `TypeError: Options.__init__() got an unexpected keyword argument 'repo_root'`

- [ ] **Step 3: Implement the repo-relative frame path**

Edit `python/src/metergraph/_capture.py`. Update `_capture_frames`:

```python
def _capture_frames(
    app_root: str, skip_frames: tuple[str, ...], repo_root: str | None = None
) -> tuple[str | None, str | None, list[dict]]:
    frames: list[dict] = []
    root = os.path.realpath(app_root)
    repo = os.path.realpath(repo_root) if repo_root else None
    frame = sys._getframe(2)
    while frame is not None and len(frames) < 5:
        filename = os.path.realpath(frame.f_code.co_filename)
        if filename.startswith(root) and not any(
            part in filename for part in skip_frames
        ):
            relative = os.path.relpath(filename, root)
            module = str(Path(relative).with_suffix("")).replace(os.sep, ".")
            qualname = getattr(frame.f_code, "co_qualname", frame.f_code.co_name)
            entry: dict[str, Any] = {"m": module, "f": qualname, "l": frame.f_lineno}
            if repo and filename.startswith(repo):
                entry["p"] = Path(os.path.relpath(filename, repo)).as_posix()
            frames.append(entry)
        frame = frame.f_back
    if not frames:
        return None, None, []
    return f"{frames[0]['m']}:{frames[0]['f']}", frames[0]["m"], frames
```

Update `Options` to add the field:

```python
@dataclass
class Options:
    capture_text: bool = True
    redact: Callable[[str, str], str] | None = None
    app_root: str = os.getcwd()
    skip_frames: tuple[str, ...] = ()
    environment: str | None = None
    text_max_bytes: int = 100 * 1024
    repo_root: str | None = None
```

Update `Runtime.call_state` to pass it through:

```python
        func, module, frames = _capture_frames(
            self.options.app_root,
            (
                "site-packages",
                "metergraph/_capture.py",
                "concurrent/futures",
                "threading.py",
                *self.options.skip_frames,
            ),
            self.options.repo_root,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd python && python -m pytest tests/test_repository_aware_ingest.py -v`
Expected: PASS. Also re-run: `cd python && python -m pytest tests -v` to confirm no regression in existing frame-dependent assertions (e.g. `row["func"].endswith(...)`).

- [ ] **Step 5: Commit**

```bash
git add python/src/metergraph/_capture.py python/tests/test_repository_aware_ingest.py
git commit -m "Add repo-relative frame path to Python captured frames"
```

### Task 8: Python — wire `init()`/`shutdown()` end to end

**Files:**
- Modify: `python/src/metergraph/__init__.py`
- Modify: `python/tests/test_repository_aware_ingest.py`

**Interfaces:**
- Consumes: `discover_repo_config` (Task 3), `SessionManager` (Task 4), `Writer(..., session=...)` (Task 6), `Options.repo_root` (Task 7).
- Produces: module-level `_session: SessionManager | None`, stopped by `shutdown()`.

- [ ] **Step 1: Write the failing test**

```python
# append to python/tests/test_repository_aware_ingest.py
import importlib


def test_wrap_activates_protocol_v2_session_when_repo_config_is_present(tmp_path, monkeypatch):
    (tmp_path / ".metergraph").mkdir()
    (tmp_path / ".metergraph" / "config.json").write_text(
        json.dumps({"version": 2, "repository": "acme/widgets"})
    )
    exchanged = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path == "/v1/ingest/sessions":
                self.rfile.read(int(self.headers["Content-Length"]))
                exchanged["auth"] = self.headers.get("Authorization")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "session_token": "session-xyz",
                    "expires_at": (
                        datetime.now(timezone.utc) + timedelta(minutes=5)
                    ).isoformat(),
                    "repository_id": "repo_123",
                }).encode())
            else:
                self.send_response(202)
                self.end_headers()

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("METERGRAPH_APP_TOKEN", raising=False)
    monkeypatch.delenv("METERGRAPH_INGEST_URL", raising=False)

    metergraph._initialized = False
    metergraph._writer = None
    metergraph._config = None
    metergraph._session = None
    try:
        metergraph.init(
            token="app-token-secret",
            ingest_url=f"http://127.0.0.1:{server.server_port}",
        )
        assert metergraph._session is not None
        deadline = time.monotonic() + 2.0
        while metergraph._session.get_token() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert metergraph._session.get_token() == "session-xyz"
        assert exchanged["auth"] == "Bearer app-token-secret"
    finally:
        metergraph.shutdown()
        metergraph._initialized = False
        server.shutdown()


def test_wrap_stays_on_legacy_v1_writer_without_repo_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("METERGRAPH_APP_TOKEN", raising=False)
    monkeypatch.delenv("METERGRAPH_INGEST_URL", raising=False)

    metergraph._initialized = False
    metergraph._writer = None
    metergraph._config = None
    metergraph._session = None
    try:
        metergraph.init(token="app-token-secret", ingest_url="http://127.0.0.1:1")
        assert metergraph._session is None
        assert metergraph._writer is not None
        assert metergraph._writer._session is None
    finally:
        metergraph.shutdown()
        metergraph._initialized = False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd python && python -m pytest tests/test_repository_aware_ingest.py -v`
Expected: FAIL -- `metergraph._session` does not exist yet (`AttributeError`).

- [ ] **Step 3: Wire `init()`/`shutdown()`**

Edit `python/src/metergraph/__init__.py`. Add imports:

```python
from ._repo_config import discover_repo_config
from ._session import SessionManager
```

Add a module global next to the existing ones:

```python
_session: SessionManager | None = None
```

Inside `init()`, after `_initialized = True` and inside the existing `try:` block, resolve the repo config before constructing `Options`/`Writer`:

```python
    _initialized = True
    try:
        app_root_resolved = os.path.realpath(app_root or os.getcwd())
        repo_config = discover_repo_config(app_root_resolved)
        session = (
            SessionManager(
                token,
                ingest_url,
                repository=repo_config.repository,
                sdk_version=SDK_VERSION,
            )
            if repo_config is not None
            else None
        )
        _writer = Writer(
            token,
            ingest_url,
            queue_size=int(os.getenv("METERGRAPH_QUEUE_SIZE", "2000")),
            batch_size=int(os.getenv("METERGRAPH_BATCH_SIZE", "100")),
            flush_seconds=float(os.getenv("METERGRAPH_FLUSH_SECONDS", "5")),
            session=session,
        )
        _session = session
        options = Options(
            capture_text=(
                _env_bool("METERGRAPH_CAPTURE_TEXT", True)
                if capture_text is None
                else capture_text
            ),
            redact=redact,
            app_root=app_root_resolved,
            skip_frames=tuple(skip_frames or ()),
            environment=environment or os.getenv("METERGRAPH_ENV"),
            text_max_bytes=min(
                100 * 1024,
                max(
                    1,
                    int(
                        os.getenv(
                            "METERGRAPH_TEXT_MAX_BYTES", str(100 * 1024)
                        )
                    ),
                ),
            ),
            repo_root=repo_config.repo_root if repo_config is not None else None,
        )
        set_runtime(Runtime(_writer, options))
        _config = ConfigPoller(
            token,
            ingest_url,
            poll_seconds=float(os.getenv("METERGRAPH_CONFIG_POLL_SECONDS", "30")),
            hard_ttl_seconds=float(
                os.getenv("METERGRAPH_CONFIG_HARD_TTL_SECONDS", "120")
            ),
        )
        atexit.register(shutdown)
    except Exception:
        set_runtime(None)
        if _writer:
            _writer.shutdown()
        if _session:
            _session.stop()
        _writer = None
        _config = None
        _session = None
        log.warning(
            "Metergraph initialization failed; application is running uninstrumented"
        )
```

Update `shutdown()`:

```python
def shutdown() -> None:
    global _writer, _config, _session
    if _config:
        _config.stop()
        _config = None
    if _session:
        _session.stop()
        _session = None
    if _writer:
        _writer.shutdown()
        _writer = None
    set_runtime(None)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd python && python -m pytest tests/test_repository_aware_ingest.py -v`
Expected: PASS. Also re-run: `cd python && python -m pytest tests -v` to confirm no regressions across the whole Python suite.

- [ ] **Step 5: Commit**

```bash
git add python/src/metergraph/__init__.py python/tests/test_repository_aware_ingest.py
git commit -m "Wire Python init()/shutdown() to protocol-v2 session discovery"
```

### Task 9: Python — credential non-logging sweep

**Files:**
- Modify: `python/tests/test_repository_aware_ingest.py`

**Interfaces:**
- Consumes: `SessionManager` (Task 4/6), `Writer` (Task 6) -- no production code expected to change unless a leak is found (see Step 3).

- [ ] **Step 1: Write the failing tests**

```python
# append to python/tests/test_repository_aware_ingest.py
def test_session_manager_never_logs_the_app_token_on_failure(caplog):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(500)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    secret = "app-token-super-secret-value"
    with caplog.at_level(logging.WARNING, logger="metergraph"):
        manager = SessionManager(
            secret,
            f"http://127.0.0.1:{server.server_port}",
            repository="owner/repo",
            sdk_version="0.4.0",
            poll_seconds=10,
        )
        time.sleep(0.3)
    manager.stop()
    server.shutdown()

    for record in caplog.records:
        assert secret not in record.getMessage()


def test_writer_never_logs_the_session_token_on_rejection(caplog):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(401)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    secret = "session-token-super-secret-value"
    session = FakeSession(token=secret)
    with caplog.at_level(logging.WARNING, logger="metergraph"):
        writer = Writer(
            "app-token-unused",
            f"http://127.0.0.1:{server.server_port}",
            session=session,
            flush_seconds=0.2,
        )
        writer.enqueue({"row": 1})
        writer.flush(1)
    writer.shutdown()
    server.shutdown()

    for record in caplog.records:
        assert secret not in record.getMessage()
```

- [ ] **Step 2: Run tests to verify they pass or fail**

Run: `cd python && python -m pytest tests/test_repository_aware_ingest.py -v -k never_logs`
Expected: These should PASS against the Task 4/6 implementations, since `_session.py` and `_transport.py` never interpolate the raw token into `_failure_log.report(...)` or `log.warning(...)` calls -- they only ever include the URL, HTTP status, and exception type/message. Run to confirm; if either fails, it means a leak was introduced earlier in this plan and must be fixed in `_session.py`/`_transport.py` before proceeding (never log a token value under any circumstance).

- [ ] **Step 3: Fix any leak found (expected: none)**

No changes expected. If Step 2 fails, locate the offending `log.warning`/`_failure_log.report` call and remove the token from the interpolated message, keeping only non-secret context (URL, status code, exception type).

- [ ] **Step 4: Run the full new test file to verify everything passes**

Run: `cd python && python -m pytest tests/test_repository_aware_ingest.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/tests/test_repository_aware_ingest.py
git commit -m "Add Python credential non-logging regression tests"
```

### Task 10: Python — packaging: console script and version 0.4.0

**Files:**
- Modify: `python/pyproject.toml`
- Modify: `python/src/metergraph/_version.py`
- Modify: `python/tests/test_repository_aware_ingest.py`

**Interfaces:**
- Produces: an installed `metergraph-setup` console script resolving to `metergraph._setup:main`.

- [ ] **Step 1: Write the failing test**

```python
# append to python/tests/test_repository_aware_ingest.py
import shutil


def test_metergraph_setup_console_script_is_installed():
    assert shutil.which("metergraph-setup") is not None


def test_sdk_version_is_0_4_0():
    assert metergraph.SDK_VERSION == "0.4.0"
```

Also add this import near the top of the file if not already present: `import metergraph`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd python && python -m pytest tests/test_repository_aware_ingest.py -v -k "console_script or sdk_version_is_0_4_0"`
Expected: FAIL -- `shutil.which("metergraph-setup")` is `None` (no entry point registered yet), and `metergraph.SDK_VERSION == "0.3.2"`.

- [ ] **Step 3: Add the console script entry point and bump the version**

Edit `python/pyproject.toml`:

```toml
[project]
name = "metergraph"
version = "0.4.0"
```

Add a new table after `[project.optional-dependencies]`:

```toml
[project.scripts]
metergraph-setup = "metergraph._setup:main"
```

Edit `python/src/metergraph/_version.py`'s fallback:

```python
try:
    SDK_VERSION = importlib.metadata.version("metergraph")
except Exception:
    SDK_VERSION = "0.4.0"
```

Re-install in editable mode so the new console script and version metadata are picked up:

```bash
cd python && pip install -e ".[dev]"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd python && python -m pytest tests/test_repository_aware_ingest.py -v`
Expected: PASS. Also re-run: `cd python && python -m pytest tests -v` for a full regression pass (`test_hosted_default_is_https` and friends are version-independent and should be unaffected).

- [ ] **Step 5: Commit**

```bash
git add python/pyproject.toml python/src/metergraph/_version.py python/tests/test_repository_aware_ingest.py
git commit -m "Release Python SDK 0.4.0 with the metergraph-setup console script"
```

### Task 11: TypeScript — GitHub remote normalization and config writer

**Files:**
- Create: `typescript/src/setup.ts`
- Create: `typescript/test/repository-aware-ingest.test.mjs`

**Interfaces:**
- Produces: `normalizeGithubRemote(url: string): string` (throws `RepoDetectionError`), `writeConfig(repoRoot: string, repository: string): string`, `main(argv?: string[]): number`, class `RepoDetectionError`.

- [ ] **Step 1: Write the failing tests**

```js
// typescript/test/repository-aware-ingest.test.mjs
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  main,
  normalizeGithubRemote,
  RepoDetectionError,
} from "../dist/setup.js";

function tmpRepo() {
  return mkdtempSync(join(tmpdir(), "metergraph-setup-"));
}

function git(args, cwd) {
  execFileSync("git", args, { cwd });
}

test("normalizeGithubRemote handles SSH and HTTPS forms", () => {
  const cases = [
    ["git@github.com:owner/repo.git", "owner/repo"],
    ["git@github.com:owner/repo", "owner/repo"],
    ["https://github.com/owner/repo.git", "owner/repo"],
    ["https://github.com/owner/repo", "owner/repo"],
    ["https://github.com/owner/repo/", "owner/repo"],
    ["ssh://git@github.com/owner/repo.git", "owner/repo"],
  ];
  for (const [input, expected] of cases) {
    assert.equal(normalizeGithubRemote(input), expected);
  }
});

test("normalizeGithubRemote rejects non-GitHub hosts", () => {
  assert.throws(
    () => normalizeGithubRemote("git@gitlab.com:owner/repo.git"),
    RepoDetectionError,
  );
});

test("setup writes config for an HTTPS origin", (t) => {
  const dir = tmpRepo();
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  git(["init"], dir);
  git(["remote", "add", "origin", "https://github.com/acme/widgets.git"], dir);

  const exitCode = main([dir]);

  assert.equal(exitCode, 0);
  const configPath = join(dir, ".metergraph", "config.json");
  assert.deepEqual(JSON.parse(readFileSync(configPath, "utf8")), {
    version: 2,
    repository: "acme/widgets",
  });
});

test("setup writes config for an SSH origin", (t) => {
  const dir = tmpRepo();
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  git(["init"], dir);
  git(["remote", "add", "origin", "git@github.com:acme/widgets.git"], dir);

  assert.equal(main([dir]), 0);
  const configPath = join(dir, ".metergraph", "config.json");
  assert.equal(JSON.parse(readFileSync(configPath, "utf8")).repository, "acme/widgets");
});

test("setup errors when there is no git repo", (t) => {
  const dir = tmpRepo();
  t.after(() => rmSync(dir, { recursive: true, force: true }));

  assert.equal(main([dir]), 1);
  assert.equal(existsSync(join(dir, ".metergraph")), false);
});

test("setup errors when the origin is not GitHub", (t) => {
  const dir = tmpRepo();
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  git(["init"], dir);
  git(["remote", "add", "origin", "git@gitlab.com:acme/widgets.git"], dir);

  assert.equal(main([dir]), 1);
  assert.equal(existsSync(join(dir, ".metergraph")), false);
});

test("setup is idempotent when the config already matches", (t) => {
  const dir = tmpRepo();
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  git(["init"], dir);
  git(["remote", "add", "origin", "https://github.com/acme/widgets.git"], dir);
  main([dir]);
  const configPath = join(dir, ".metergraph", "config.json");
  const writtenAt = readFileSync(configPath, "utf8");

  assert.equal(main([dir]), 0);
  assert.equal(readFileSync(configPath, "utf8"), writtenAt);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd typescript && npm run build && node --test test/repository-aware-ingest.test.mjs`
Expected: FAIL -- `npm run build` fails or the test fails to import `../dist/setup.js` (module does not exist yet).

- [ ] **Step 3: Implement `setup.ts`**

```ts
#!/usr/bin/env node
// typescript/src/setup.ts
// Detect the GitHub origin of the current repo and write .metergraph/config.json.
// Only this explicit setup entry point ever writes the config file. Importing
// or running the SDK at runtime must never trigger a write here.

import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

export const CONFIG_SCHEMA_VERSION = 2;

const REMOTE_PATTERNS = [
  /^git@github\.com:([^/]+\/[^/]+?)(\.git)?\/?$/,
  /^https:\/\/github\.com\/([^/]+\/[^/]+?)(\.git)?\/?$/,
  /^ssh:\/\/git@github\.com\/([^/]+\/[^/]+?)(\.git)?\/?$/,
];

export class RepoDetectionError extends Error {}

export function normalizeGithubRemote(url: string): string {
  const trimmed = url.trim();
  for (const pattern of REMOTE_PATTERNS) {
    const match = trimmed.match(pattern);
    if (match) return match[1];
  }
  throw new RepoDetectionError(`remote '${url}' is not a recognized GitHub origin`);
}

function readOriginUrl(repoPath: string): string {
  try {
    return execFileSync("git", ["remote", "get-url", "origin"], {
      cwd: repoPath,
      encoding: "utf8",
      timeout: 5_000,
    }).trim();
  } catch {
    throw new RepoDetectionError(
      `no git remote named 'origin' found in '${repoPath}'; ` +
      "run this inside a git repo with a GitHub origin",
    );
  }
}

function readExistingRepository(configPath: string): string | undefined {
  try {
    const doc = JSON.parse(readFileSync(configPath, "utf8"));
    return typeof doc?.repository === "string" ? doc.repository : undefined;
  } catch {
    return undefined;
  }
}

export function writeConfig(repoRoot: string, repository: string): string {
  const configDir = join(repoRoot, ".metergraph");
  const configPath = join(configDir, "config.json");
  mkdirSync(configDir, { recursive: true });
  writeFileSync(
    configPath,
    JSON.stringify({ version: CONFIG_SCHEMA_VERSION, repository }, null, 2) + "\n",
  );
  return configPath;
}

export function main(argv: string[] = process.argv.slice(2)): number {
  const repoPath = argv[0] ?? ".";
  let repository: string;
  try {
    repository = normalizeGithubRemote(readOriginUrl(repoPath));
  } catch (error) {
    console.error(
      `metergraph-setup: ${error instanceof Error ? error.message : String(error)}`,
    );
    return 1;
  }
  const configPath = join(repoPath, ".metergraph", "config.json");
  if (readExistingRepository(configPath) === repository) {
    console.log(`metergraph-setup: ${configPath} already configured for ${repository}`);
    return 0;
  }
  const writtenPath = writeConfig(repoPath, repository);
  console.log(`metergraph-setup: wrote ${writtenPath} for repository ${repository}`);
  return 0;
}

if (process.argv[1] && import.meta.url === `file://${process.argv[1]}`) {
  process.exit(main());
}
```

TypeScript preserves a leading `#!` shebang line verbatim in the compiled output (since TS 4.8), so `dist/setup.js` keeps it after `npm run build`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd typescript && npm run build && node --test test/repository-aware-ingest.test.mjs`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add typescript/src/setup.ts typescript/test/repository-aware-ingest.test.mjs
git commit -m "Add TypeScript metergraph-setup: GitHub origin detection and config write"
```

### Task 12: TypeScript — repo config discovery

**Files:**
- Create: `typescript/src/repo-config.ts`
- Modify: `typescript/test/repository-aware-ingest.test.mjs`

**Interfaces:**
- Produces: `interface RepoConfig { repository: string; repoRoot: string }`, `discoverRepoConfig(appRoot: string): RepoConfig | undefined`. Consumed by Task 17 (`index.ts` wiring).

- [ ] **Step 1: Write the failing tests**

```js
// append to typescript/test/repository-aware-ingest.test.mjs
import { mkdirSync, writeFileSync } from "node:fs";

import { discoverRepoConfig } from "../dist/repo-config.js";

function writeRepoConfig(dir, doc) {
  mkdirSync(join(dir, ".metergraph"), { recursive: true });
  writeFileSync(join(dir, ".metergraph", "config.json"), JSON.stringify(doc));
}

test("discoverRepoConfig finds the file at appRoot", (t) => {
  const dir = tmpRepo();
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  writeRepoConfig(dir, { version: 2, repository: "acme/widgets" });

  const config = discoverRepoConfig(dir);

  assert.equal(config?.repository, "acme/widgets");
  assert.equal(config?.repoRoot, dir);
});

test("discoverRepoConfig walks up from a nested appRoot", (t) => {
  const dir = tmpRepo();
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  writeRepoConfig(dir, { version: 2, repository: "acme/monorepo" });
  const nested = join(dir, "services", "backend");
  mkdirSync(nested, { recursive: true });

  const config = discoverRepoConfig(nested);

  assert.equal(config?.repository, "acme/monorepo");
  assert.equal(config?.repoRoot, dir);
});

test("discoverRepoConfig returns undefined when absent", (t) => {
  const dir = tmpRepo();
  t.after(() => rmSync(dir, { recursive: true, force: true }));

  assert.equal(discoverRepoConfig(dir), undefined);
});

test("discoverRepoConfig ignores malformed JSON", (t) => {
  const dir = tmpRepo();
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  mkdirSync(join(dir, ".metergraph"), { recursive: true });
  writeFileSync(join(dir, ".metergraph", "config.json"), "{not json");

  assert.equal(discoverRepoConfig(dir), undefined);
});

test("discoverRepoConfig ignores an unsupported schema version", (t) => {
  const dir = tmpRepo();
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  writeRepoConfig(dir, { version: 99, repository: "acme/widgets" });

  assert.equal(discoverRepoConfig(dir), undefined);
});

test("discoverRepoConfig works without a .git directory", (t) => {
  const dir = tmpRepo();
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  writeRepoConfig(dir, { version: 2, repository: "acme/widgets" });
  assert.equal(existsSync(join(dir, ".git")), false);

  const config = discoverRepoConfig(dir);

  assert.equal(config?.repository, "acme/widgets");
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd typescript && npm run build && node --test test/repository-aware-ingest.test.mjs`
Expected: FAIL -- build fails or import of `../dist/repo-config.js` fails.

- [ ] **Step 3: Implement `repo-config.ts`**

```ts
// typescript/src/repo-config.ts
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";

export interface RepoConfig {
  repository: string;
  repoRoot: string;
}

const CONFIG_DIRNAME = ".metergraph";
const CONFIG_FILENAME = "config.json";
const SUPPORTED_CONFIG_VERSION = 2;
const MAX_WALK_UP = 64;

/**
 * Walk upward from appRoot looking for .metergraph/config.json. Never
 * writes anything -- only setup.ts does that. Never throws: any error
 * (including running where the filesystem is unavailable) is treated the
 * same as "not found."
 */
export function discoverRepoConfig(appRoot: string): RepoConfig | undefined {
  try {
    let current = resolve(appRoot);
    for (let i = 0; i < MAX_WALK_UP; i += 1) {
      const candidate = join(current, CONFIG_DIRNAME, CONFIG_FILENAME);
      if (existsSync(candidate)) return load(candidate, current);
      const parent = dirname(current);
      if (parent === current) break;
      current = parent;
    }
    return undefined;
  } catch {
    return undefined;
  }
}

function load(path: string, repoRoot: string): RepoConfig | undefined {
  let doc: unknown;
  try {
    doc = JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    console.warn(
      `metergraph: found ${path} but could not read it: ` +
      `${error instanceof Error ? error.message : String(error)}`,
    );
    return undefined;
  }
  const record = doc as Record<string, unknown> | null;
  if (!record || typeof record !== "object" || record.version !== SUPPORTED_CONFIG_VERSION) {
    console.warn(
      `metergraph: ${path} has an unsupported schema version; ignoring ` +
      `(expected version ${SUPPORTED_CONFIG_VERSION})`,
    );
    return undefined;
  }
  const repository = record.repository;
  if (typeof repository !== "string" || !repository.includes("/")) {
    console.warn(`metergraph: ${path} is missing a valid 'repository' field; ignoring`);
    return undefined;
  }
  return { repository, repoRoot };
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd typescript && npm run build && node --test test/repository-aware-ingest.test.mjs`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add typescript/src/repo-config.ts typescript/test/repository-aware-ingest.test.mjs
git commit -m "Add TypeScript .metergraph/config.json discovery"
```

### Task 13: TypeScript — SessionManager initial exchange

**Files:**
- Create: `typescript/src/session.ts`
- Modify: `typescript/test/repository-aware-ingest.test.mjs`
- Modify: `typescript/scripts/verify-ingest-contract.mjs`
- Modify: `typescript/test/ingest-contract-verification.test.mjs`

**Interfaces:**
- Consumes: `FailureLogger` from `./failure-log.js`; `resolveSchemaSource`, `loadSchemaDocument`, `validateInstance` from `./scripts/verify-ingest-contract.mjs` (Task 1).
- Produces: `buildExchangeRequestBody(repository, sdkVersion)`, `parseSessionResponse(doc)` (throws if `session_token` is missing; returns `{ sessionToken, ttlMs, repositoryId }`), `SessionManager` with constructor `(appToken, baseUrl, repository, sdkVersion, pollMs?, timeoutMs?)`, `getToken(): string | undefined`, `invalidate(): void`, `stop(): void`. Consumed by Task 15 (`Transport`) and Task 17 (`index.ts`).

- [ ] **Step 1: Write the failing tests**

```js
// append to typescript/test/repository-aware-ingest.test.mjs
import http from "node:http";

import { SessionManager } from "../dist/session.js";

async function serve(handler) {
  const server = http.createServer(handler);
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return server;
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

test("SessionManager caches the token from a successful exchange", async (t) => {
  const server = await serve(async (request, response) => {
    for await (const _chunk of request);
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({
      session_token: "session-abc",
      expires_at: new Date(Date.now() + 5 * 60_000).toISOString(),
      repository_id: "repo_123",
    }));
  });
  t.after(async () => new Promise((resolve) => server.close(resolve)));

  const manager = new SessionManager(
    "app-token-secret",
    `http://127.0.0.1:${server.address().port}`,
    "owner/repo",
    "0.4.0",
    10_000,
  );
  await manager.ready;
  t.after(() => manager.stop());

  assert.equal(manager.getToken(), "session-abc");
});

test("SessionManager exchange request has the agreed shape", async (t) => {
  let capturedAuth;
  let capturedBody;
  const server = await serve(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    capturedAuth = request.headers.authorization;
    capturedBody = JSON.parse(Buffer.concat(chunks).toString());
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({
      session_token: "session-abc",
      expires_at: new Date(Date.now() + 5 * 60_000).toISOString(),
      repository_id: "repo_123",
    }));
  });
  t.after(async () => new Promise((resolve) => server.close(resolve)));

  const manager = new SessionManager(
    "app-token-secret",
    `http://127.0.0.1:${server.address().port}`,
    "owner/repository",
    "0.4.0",
    10_000,
  );
  await manager.ready;
  t.after(() => manager.stop());

  assert.deepEqual(Object.keys(capturedBody).sort(), ["protocol_version", "repository", "sdk_version"]);
  assert.equal(capturedBody.protocol_version, 2);
  assert.equal(capturedAuth, "Bearer app-token-secret");
  assert.equal(manager.getToken(), "session-abc");
});

test("SessionManager stays unset on server error", async (t) => {
  const server = await serve(async (request, response) => {
    for await (const _chunk of request);
    response.writeHead(500);
    response.end();
  });
  t.after(async () => new Promise((resolve) => server.close(resolve)));

  const manager = new SessionManager(
    "app-token-secret",
    `http://127.0.0.1:${server.address().port}`,
    "owner/repo",
    "0.4.0",
    10_000,
  );
  await manager.ready;
  t.after(() => manager.stop());

  assert.equal(manager.getToken(), undefined);
});

test("SessionManager stays unset on a malformed response", async (t) => {
  const server = await serve(async (request, response) => {
    for await (const _chunk of request);
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({ unexpected: "shape" }));
  });
  t.after(async () => new Promise((resolve) => server.close(resolve)));

  const manager = new SessionManager(
    "app-token-secret",
    `http://127.0.0.1:${server.address().port}`,
    "owner/repo",
    "0.4.0",
    10_000,
  );
  await manager.ready;
  t.after(() => manager.stop());

  assert.equal(manager.getToken(), undefined);
});

test("SessionManager is fail-open on timeout", async (t) => {
  const server = http.createServer(() => {
    // never respond
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(async () => new Promise((resolve) => server.close(resolve)));

  const start = Date.now();
  const manager = new SessionManager(
    "app-token-secret",
    `http://127.0.0.1:${server.address().port}`,
    "owner/repo",
    "0.4.0",
    10_000,
    200,
  );
  await manager.ready;
  t.after(() => manager.stop());
  const elapsed = Date.now() - start;

  assert.equal(manager.getToken(), undefined);
  assert.ok(elapsed < 2_000);
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd typescript && npm run build && node --test test/repository-aware-ingest.test.mjs`
Expected: FAIL -- build fails or import of `../dist/session.js` fails.

- [ ] **Step 3: Implement `session.ts`**

```ts
// typescript/src/session.ts
import { FailureLogger } from "./failure-log.js";

const EXCHANGE_TIMEOUT_MS = 3_000;
const REFRESH_MARGIN_MS = 30_000;
const DEFAULT_TTL_MS = 60_000;

interface SessionResponse {
  session_token?: string;
  expires_at?: string;
  repository_id?: string;
}

export interface ParsedSessionResponse {
  sessionToken: string;
  ttlMs: number;
  repositoryId?: string;
}

/**
 * The exact body shape POSTed to /v1/ingest/sessions -- pulled out as a
 * pure function so scripts/verify-ingest-contract.mjs (Task 1) can
 * validate it against the canonical schema without a live HTTP round-trip.
 */
export function buildExchangeRequestBody(
  repository: string,
  sdkVersion: string,
): Record<string, unknown> {
  return { protocol_version: 2, repository, sdk_version: sdkVersion };
}

/**
 * Extract the session token/TTL/repository id from a response body. Throws
 * if there is no usable session_token -- pulled out as a pure function for
 * the same reason as buildExchangeRequestBody above.
 */
export function parseSessionResponse(doc: SessionResponse): ParsedSessionResponse {
  if (!doc.session_token) throw new Error("response missing session_token");
  return {
    sessionToken: doc.session_token,
    ttlMs: doc.expires_at ? Date.parse(doc.expires_at) - Date.now() : DEFAULT_TTL_MS,
    repositoryId: doc.repository_id,
  };
}

export class SessionManager {
  private sessionToken?: string;
  private expiresAtMs = 0;
  private repositoryId?: string;
  private stopped = false;
  private timer?: ReturnType<typeof setTimeout>;
  private readonly failureLog = new FailureLogger();

  /** Resolves once the constructor's initial exchange attempt has completed. */
  readonly ready: Promise<boolean>;

  constructor(
    private readonly appToken: string,
    private readonly baseUrl: string,
    private readonly repository: string,
    private readonly sdkVersion: string,
    private readonly pollMs = 15_000,
    private readonly timeoutMs = EXCHANGE_TIMEOUT_MS,
  ) {
    this.ready = this.exchange(false).then((ok) => {
      this.scheduleNext();
      return ok;
    });
  }

  getToken(): string | undefined {
    return this.sessionToken && Date.now() < this.expiresAtMs
      ? this.sessionToken
      : undefined;
  }

  invalidate(): void {
    this.sessionToken = undefined;
    this.expiresAtMs = 0;
  }

  private scheduleNext(): void {
    if (this.stopped) return;
    const remaining = this.expiresAtMs - Date.now();
    const delay =
      this.sessionToken && remaining > REFRESH_MARGIN_MS
        ? Math.min(this.pollMs, remaining - REFRESH_MARGIN_MS)
        : this.pollMs;
    this.timer = setTimeout(() => void this.tick(), Math.max(50, delay));
    this.timer.unref?.();
  }

  private async tick(): Promise<void> {
    if (this.stopped) return;
    await this.exchange(Boolean(this.getToken()));
    this.scheduleNext();
  }

  private async exchange(useSessionToken: boolean): Promise<boolean> {
    const bearer = useSessionToken && this.sessionToken ? this.sessionToken : this.appToken;
    try {
      const response = await fetch(`${this.baseUrl.replace(/\/$/, "")}/v1/ingest/sessions`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${bearer}`,
          "Content-Type": "application/json",
          "Cache-Control": "no-store",
        },
        body: JSON.stringify(buildExchangeRequestBody(this.repository, this.sdkVersion)),
        signal: AbortSignal.timeout(this.timeoutMs),
      });
      if ((response.status === 401 || response.status === 403) && useSessionToken) {
        this.invalidate();
        return this.exchange(false);
      }
      if (!response.ok) {
        this.failureLog.report(
          "session_exchange_error",
          `session exchange against ${this.baseUrl} failed with HTTP ${response.status}`,
        );
        return false;
      }
      const body = (await response.json()) as SessionResponse;
      let parsed: ParsedSessionResponse;
      try {
        parsed = parseSessionResponse(body);
      } catch {
        this.failureLog.report(
          "session_exchange_error",
          `session exchange against ${this.baseUrl} returned no session_token`,
        );
        return false;
      }
      this.sessionToken = parsed.sessionToken;
      this.expiresAtMs = Date.now() + parsed.ttlMs;
      this.repositoryId = parsed.repositoryId;
      return true;
    } catch (error) {
      this.failureLog.report(
        "session_exchange_error",
        `session exchange against ${this.baseUrl} failed: ` +
        `${error instanceof Error ? error.message : String(error)}`,
      );
      return false;
    }
  }

  stop(): void {
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
  }
}
```

Check `typescript/src/failure-log.ts`'s `FailureLogger.report` signature before wiring this in -- reuse it exactly as the existing `ConfigPoller`/`Transport` call it (`this.failureLog.report(kind, message)`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd typescript && npm run build && node --test test/repository-aware-ingest.test.mjs`
Expected: PASS

- [ ] **Step 5: Write the failing test tying the verifier to the real SDK**

Now that `typescript/dist/session.js` exists, extend the contract-verification tests from Task 1 to prove the real request/response handling -- not just a literal example -- conforms to the schema. This still runs fully offline via the local-path override.

```js
// append to typescript/test/ingest-contract-verification.test.mjs
import { main } from "../scripts/verify-ingest-contract.mjs";

test("verify-ingest-contract validates the real TypeScript SDK request and response", async (t) => {
  const dir = mkdtempSync(join(tmpdir(), "metergraph-contract-sdk-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const schemaPath = join(dir, "draft.schema.json");
  writeFileSync(schemaPath, JSON.stringify(EXAMPLE_SCHEMA));
  process.env.METERGRAPH_CONTRACT_SCHEMA_PATH = schemaPath;
  t.after(() => {
    delete process.env.METERGRAPH_CONTRACT_SCHEMA_PATH;
  });

  const exitCode = await main();

  assert.equal(exitCode, 0);
});
```

- [ ] **Step 6: Run test to verify it fails**

Run: `cd typescript && npm run build && node --test test/ingest-contract-verification.test.mjs`
Expected: FAIL -- `main()` currently only fetches and parses the schema; it never imports or validates against `typescript/dist/session.js`. Confirm this fails before proceeding -- the point of this step is to prove Step 7 is necessary, not to skip straight to it.

- [ ] **Step 7: Wire the verifier to the real SDK**

Edit `typescript/scripts/verify-ingest-contract.mjs`. Add a function that imports and exercises the real SDK, and call it from `main()`:

```js
async function validateSdkExamples(document) {
  const { buildExchangeRequestBody, parseSessionResponse } = await import(
    join(REPO_ROOT, "typescript", "dist", "session.js")
  );
  const { SDK_VERSION } = await import(join(REPO_ROOT, "typescript", "dist", "version.js"));

  const requestBody = buildExchangeRequestBody("owner/repository", SDK_VERSION);
  validateInstance(document, "request", requestBody);

  const exampleResponse = {
    session_token: "mgs_example_session_token",
    expires_at: "2026-08-08T12:34:56+00:00",
    repository_id: "repo_example",
  };
  validateInstance(document, "response", exampleResponse);
  const parsed = parseSessionResponse(exampleResponse);
  if (parsed.sessionToken !== exampleResponse.session_token) {
    throw new Error("parseSessionResponse did not round-trip session_token");
  }
}
```

Update `main()` to call it:

```js
export async function main() {
  try {
    const source = await resolveSchemaSource();
    const document = loadSchemaDocument(source);
    await validateSdkExamples(document);
    const pinState = source.pinned ? "pinned" : "override (unpinned)";
    console.log(`verify-ingest-contract: OK -- ${source.origin} (${pinState}, sha256=${source.sha256})`);
    return 0;
  } catch (error) {
    console.error(`verify-ingest-contract: ${error instanceof Error ? error.message : String(error)}`);
    return 1;
  }
}
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `cd typescript && npm run build && node --test test/ingest-contract-verification.test.mjs test/repository-aware-ingest.test.mjs`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add typescript/src/session.ts typescript/test/repository-aware-ingest.test.mjs typescript/scripts/verify-ingest-contract.mjs typescript/test/ingest-contract-verification.test.mjs
git commit -m "Add TypeScript SessionManager and wire it into contract verification"
```

### Task 14: TypeScript — SessionManager refresh and expiry/rejection fallback

**Files:**
- Modify: `typescript/test/repository-aware-ingest.test.mjs`

**Interfaces:**
- Consumes: `SessionManager` from Task 13 (no production code changes expected).

- [ ] **Step 1: Write the failing tests**

```js
// append to typescript/test/repository-aware-ingest.test.mjs
test("SessionManager refreshes using the session token before expiry", async (t) => {
  const authHeaders = [];
  const server = await serve(async (request, response) => {
    for await (const _chunk of request);
    authHeaders.push(request.headers.authorization);
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({
      session_token: `session-${authHeaders.length}`,
      expires_at: new Date(Date.now() + 1_000).toISOString(),
      repository_id: "repo_123",
    }));
  });
  t.after(async () => new Promise((resolve) => server.close(resolve)));

  const manager = new SessionManager(
    "app-token-secret",
    `http://127.0.0.1:${server.address().port}`,
    "owner/repo",
    "0.4.0",
    100,
  );
  await manager.ready;
  await wait(500);
  manager.stop();

  assert.ok(authHeaders.length >= 2);
  assert.equal(authHeaders[0], "Bearer app-token-secret");
  assert.equal(authHeaders[1], "Bearer session-1");
});

test("SessionManager falls back to the app token when the session token is rejected", async (t) => {
  const authHeaders = [];
  let issued = 0;
  const server = await serve(async (request, response) => {
    for await (const _chunk of request);
    const auth = request.headers.authorization;
    authHeaders.push(auth);
    if (auth === "Bearer app-token-secret") {
      issued += 1;
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({
        session_token: `session-${issued}`,
        expires_at: new Date(Date.now() + 1_000).toISOString(),
        repository_id: "repo_123",
      }));
    } else {
      response.writeHead(401);
      response.end();
    }
  });
  t.after(async () => new Promise((resolve) => server.close(resolve)));

  const manager = new SessionManager(
    "app-token-secret",
    `http://127.0.0.1:${server.address().port}`,
    "owner/repo",
    "0.4.0",
    100,
  );
  await manager.ready;
  await wait(500);
  manager.stop();

  assert.ok(issued >= 2);
  assert.notEqual(manager.getToken(), undefined);
});
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `cd typescript && npm run build && node --test test/repository-aware-ingest.test.mjs`
Expected: PASS against the Task 13 implementation with no new production code. If either fails, the bug is in `session.ts`'s `tick`/`exchange` interaction from Task 13; fix there before proceeding.

- [ ] **Step 3: N/A**

No production code change in this task -- it is pure test coverage over existing behavior.

- [ ] **Step 4: Run the full new test file to verify everything passes**

Run: `cd typescript && node --test test/repository-aware-ingest.test.mjs`
Expected: PASS (all tests so far)

- [ ] **Step 5: Commit**

```bash
git add typescript/test/repository-aware-ingest.test.mjs
git commit -m "Add TypeScript SessionManager refresh and rejection-fallback tests"
```

### Task 15: TypeScript — Transport session-mode wiring

**Files:**
- Modify: `typescript/src/transport.ts`
- Modify: `typescript/test/repository-aware-ingest.test.mjs`

**Interfaces:**
- Consumes: any object exposing `getToken(): string | undefined` and `invalidate(): void` (duck-typed; `SessionManager` from Task 13, or a test fake).
- Produces: `TransportOptions.session?`. Consumed by Task 17 (`index.ts`).

- [ ] **Step 1: Write the failing tests**

```js
// append to typescript/test/repository-aware-ingest.test.mjs
function fakeSession(token) {
  const state = { token, invalidated: false };
  return {
    state,
    getToken: () => state.token,
    invalidate: () => {
      state.invalidated = true;
      state.token = undefined;
    },
  };
}

test("Transport buffers without sending until a session token exists", async (t) => {
  const received = [];
  const server = await serve(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    received.push([request.headers.authorization, JSON.parse(Buffer.concat(chunks).toString())]);
    response.writeHead(202);
    response.end();
  });
  t.after(async () => new Promise((resolve) => server.close(resolve)));

  const session = fakeSession(undefined);
  const transport = new Transport(
    "app-token-unused",
    `http://127.0.0.1:${server.address().port}`,
    { mode: "background", flushMs: 100_000, session },
  );
  transport.enqueue({ row: 1 });
  await transport.flush(500);
  assert.deepEqual(received, []);

  session.state.token = "session-abc";
  transport.enqueue({ row: 2 });
  assert.equal(await transport.flush(2_000), true);
  await transport.shutdown();

  assert.ok(received.length > 0);
  const [auth, body] = received[received.length - 1];
  assert.equal(auth, "Bearer session-abc");
  assert.deepEqual(body.rows.map((r) => r.row), [2]);
});

test("Transport invalidates the session on 401 instead of going fatal", async (t) => {
  const server = await serve(async (request, response) => {
    for await (const _chunk of request);
    response.writeHead(401);
    response.end();
  });
  t.after(async () => new Promise((resolve) => server.close(resolve)));

  const session = fakeSession("stale-session-token");
  const transport = new Transport(
    "app-token-unused",
    `http://127.0.0.1:${server.address().port}`,
    { mode: "background", flushMs: 100_000, session },
  );
  transport.enqueue({ row: 1 });
  await transport.flush(1_000);
  await transport.shutdown();

  assert.equal(session.state.invalidated, true);
});

test("Transport without a session keeps legacy app-token behavior", async (t) => {
  const received = [];
  const server = await serve(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    received.push(request.headers.authorization);
    response.writeHead(202);
    response.end();
  });
  t.after(async () => new Promise((resolve) => server.close(resolve)));

  const transport = new Transport(
    "app-token-secret",
    `http://127.0.0.1:${server.address().port}`,
    { mode: "background", flushMs: 100_000 },
  );
  transport.enqueue({ row: 1 });
  assert.equal(await transport.flush(2_000), true);
  await transport.shutdown();

  assert.equal(received[0], "Bearer app-token-secret");
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd typescript && npm run build && node --test test/repository-aware-ingest.test.mjs`
Expected: FAIL -- TypeScript compile error, `session` is not a recognized `TransportOptions` key.

- [ ] **Step 3: Wire `Transport` for session-mode delivery**

Edit `typescript/src/transport.ts`. Add the import and extend the options interface:

```ts
import { FailureLogger } from "./failure-log.js";

export interface SessionLike {
  getToken(): string | undefined;
  invalidate(): void;
}

export type TransportMode = "auto" | "background" | "buffered";
export type WaitUntil = (promise: Promise<unknown>) => void;
export const MAX_BATCH_BYTES = 512 * 1024;

export interface TransportOptions {
  queueSize?: number;
  batchSize?: number;
  flushMs?: number;
  mode?: TransportMode;
  session?: SessionLike;
}
```

Store it in the constructor:

```ts
  private readonly session?: SessionLike;

  constructor(
    private readonly token: string,
    private readonly baseUrl: string,
    options: TransportOptions = {},
  ) {
    this.queueSize = Math.max(1, options.queueSize ?? 2_000);
    this.batchSize = Math.max(1, Math.min(1_000, options.batchSize ?? 100));
    this.session = options.session;
    this.mode = options.mode === "auto" || !options.mode ? detectedMode() : options.mode;
    if (this.mode === "background") {
      this.timer = setInterval(() => void this.flush(), Math.max(50, options.flushMs ?? 5_000));
      this.timer.unref?.();
    }
  }
```

Update `deliver()` to source the bearer token from the session, and update the 401/403 branch:

```ts
  private async deliver(rows: Record<string, unknown>[]): Promise<void> {
    if (this.fatal || Date.now() < this.retryAt) {
      this.dropped += rows.length;
      return;
    }
    const token = this.session ? this.session.getToken() : this.token;
    if (!token) {
      this.dropped += rows.length;
      this.retryAt = Date.now() + 1_000;
      this.failureLog.report(
        "session_pending",
        `no active ingest session yet for ${this.baseUrl}; buffering until one is established`,
      );
      return;
    }
    const encoded = new TextEncoder().encode(JSON.stringify({
      schema_version: 1,
      rows,
      meta: { dropped: this.dropped, transport_errors: this.errors },
    }));
    const raw = encoded.buffer.slice(
      encoded.byteOffset, encoded.byteOffset + encoded.byteLength,
    ) as ArrayBuffer;
    const compressed = raw.byteLength > 32 * 1024;
    const body = compressed ? await gzipBody(raw) : raw;
    if (body.byteLength > MAX_BATCH_BYTES) {
      if (rows.length === 1) {
        this.dropped += 1;
        return;
      }
      const midpoint = Math.floor(rows.length / 2);
      await this.deliver(rows.slice(0, midpoint));
      await this.deliver(rows.slice(midpoint));
      return;
    }
    try {
      const response = await fetch(`${this.baseUrl.replace(/\/$/, "")}/v1/ingest`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
          ...(compressed && body !== raw ? { "Content-Encoding": "gzip" } : {}),
        },
        body: new Blob([body], { type: "application/json" }),
        keepalive: this.mode === "buffered" && body.byteLength <= 64 * 1024,
      });
      if (response.status === 401 || response.status === 403) {
        if (this.session) {
          this.session.invalidate();
          this.dropped += rows.length;
          this.failureLog.report(
            "session_rejected",
            `ingest rejected the session token with HTTP ${response.status}; will re-exchange`,
          );
          return;
        }
        this.fatal = true;
        console.warn("Metergraph authentication failed; capture disabled for this process");
        return;
      }
      if (response.status === 413 && rows.length > 1) {
        const midpoint = Math.floor(rows.length / 2);
        await this.deliver(rows.slice(0, midpoint));
        await this.deliver(rows.slice(midpoint));
        return;
      }
      if ([400, 404, 413, 422].includes(response.status)) {
        this.dropped += rows.length;
        this.failureLog.report(
          "client_error",
          `ingest rejected batch with HTTP ${response.status} against ${this.baseUrl}; dropping this batch (payload-specific, not a process-wide failure)`,
        );
        return;
      }
      if (response.status !== 202) {
        this.markTransientFailure(rows.length);
        this.failureLog.report(
          "server_error",
          `ingest request failed with HTTP ${response.status} against ${this.baseUrl}`,
        );
        return;
      }
      this.backoffMs = 1_000;
      this.retryAt = 0;
    } catch (error) {
      this.markTransientFailure(rows.length);
      this.failureLog.report(
        "transport_error",
        `ingest request to ${this.baseUrl} failed: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd typescript && npm run build && node --test test/repository-aware-ingest.test.mjs`
Expected: PASS. Also re-run `cd typescript && npm test` to confirm no regression -- the existing "authentication failed" fatal-401 test (no session) must still pass unchanged.

- [ ] **Step 5: Commit**

```bash
git add typescript/src/transport.ts typescript/test/repository-aware-ingest.test.mjs
git commit -m "Wire TypeScript Transport to send the session token when present"
```

### Task 16: TypeScript — frame capture repo-relative path

**Files:**
- Modify: `typescript/src/capture.ts`
- Modify: `typescript/test/repository-aware-ingest.test.mjs`

**Interfaces:**
- Produces: `RuntimeOptions.repoRoot?: string`; each frame in `CallState.frames` gains an additive `p?: string` (repo-relative path) when `repoRoot` is set and the frame's file is under it.

- [ ] **Step 1: Write the failing tests**

```js
// append to typescript/test/repository-aware-ingest.test.mjs
import { CaptureRuntime } from "../dist/capture.js";

function fakeTransport() {
  const rows = [];
  return { rows, enqueue(row) { rows.push(row); return true; } };
}

test("frame capture adds a repo-relative path alongside existing fields", () => {
  const stack = [
    "Error",
    "    at handler (/repo/backend/app.js:10:5)",
    "    at Object.<anonymous> (/repo/backend/index.js:20:3)",
  ].join("\n");
  const runtime = new CaptureRuntime(fakeTransport(), {
    captureText: true,
    appRoot: "/repo/backend",
    skipFrames: [],
    textMaxBytes: 1_000,
    repoRoot: "/repo",
  });

  const state = runtime.start("openai", "chat.completions", {}, stack);

  assert.equal(state.frames[0].m, "app.js");
  assert.equal(state.frames[0].p, "backend/app.js");
  assert.equal(state.frames[1].m, "index.js");
  assert.equal(state.frames[1].p, "backend/index.js");
});

test("frame capture omits the repo-relative path without repoRoot", () => {
  const stack = [
    "Error",
    "    at handler (/repo/backend/app.js:10:5)",
  ].join("\n");
  const runtime = new CaptureRuntime(fakeTransport(), {
    captureText: true,
    appRoot: "/repo/backend",
    skipFrames: [],
    textMaxBytes: 1_000,
  });

  const state = runtime.start("openai", "chat.completions", {}, stack);

  assert.equal(state.frames[0].m, "app.js");
  assert.equal("p" in state.frames[0], false);
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd typescript && npm run build && node --test test/repository-aware-ingest.test.mjs`
Expected: FAIL -- TypeScript compile error (`repoRoot` is not a recognized `RuntimeOptions` key) or `state.frames[0].p` is `undefined`.

- [ ] **Step 3: Implement the repo-relative frame path**

Edit `typescript/src/capture.ts`. Update `RuntimeOptions`:

```ts
export interface RuntimeOptions {
  captureText: boolean;
  redact?: (text: string, kind: "request" | "response") => string;
  appRoot: string;
  skipFrames: string[];
  environment?: string;
  textMaxBytes: number;
  repoRoot?: string;
}
```

Update the `Frame` interface:

```ts
interface Frame {
  m: string;
  f: string;
  l: number;
  p?: string;
}
```

Update `frames()` to accept and use `repoRoot`:

```ts
function frames(stack: string | undefined, appRoot: string, skip: string[], repoRoot?: string): Frame[] {
  if (!stack) return [];
  const result: Frame[] = [];
  for (const line of stack.split("\n").slice(1)) {
    const match = line.match(/^\s*at\s+(?:(.*?)\s+\()?(.+?):(\d+):\d+\)?$/);
    if (!match) continue;
    const [, fn = "<anonymous>", file, lineNo] = match;
    if (!file || !lineNo || !file.includes(appRoot)) continue;
    if (["node_modules", "node:internal", SDK_DIR, ...skip].some((value) => file.includes(value))) continue;
    const entry: Frame = {
      m: file.slice(file.indexOf(appRoot) + appRoot.length).replace(/^\//, ""),
      f: fn,
      l: Number(lineNo),
    };
    if (repoRoot && file.includes(repoRoot)) {
      entry.p = file.slice(file.indexOf(repoRoot) + repoRoot.length).replace(/^\//, "");
    }
    result.push(entry);
    if (result.length === 5) break;
  }
  return result;
}
```

Update `CaptureRuntime.start` to pass it through:

```ts
  start(
    provider: string,
    endpoint: string,
    request: Record<string, unknown>,
    stack?: string,
    context: CaptureContext = contextSnapshot(),
  ): CallState {
    return {
      provider,
      endpoint,
      request,
      context,
      started: performance.now(),
      ts: new Date().toISOString(),
      frames: frames(stack, this.options.appRoot, this.options.skipFrames, this.options.repoRoot),
      traceId: context.traceId ?? randomBytes(16).toString("hex"),
      spanId: randomBytes(8).toString("hex"),
      parentSpanId: context.parentSpanId,
      traceName: context.traceName ?? context.route ?? context.funcName
        ?? endpoint,
      done: false,
    };
  }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd typescript && npm run build && node --test test/repository-aware-ingest.test.mjs`
Expected: PASS. Also re-run `cd typescript && npm test` to confirm no regressions.

- [ ] **Step 5: Commit**

```bash
git add typescript/src/capture.ts typescript/test/repository-aware-ingest.test.mjs
git commit -m "Add repo-relative frame path to TypeScript captured frames"
```

### Task 17: TypeScript — wire `init()`/`shutdown()` end to end

**Files:**
- Modify: `typescript/src/index.ts`
- Modify: `typescript/test/repository-aware-ingest.test.mjs`

**Interfaces:**
- Consumes: `discoverRepoConfig` (Task 12), `SessionManager` (Task 13), `Transport({ session })` (Task 15), `RuntimeOptions.repoRoot` (Task 16).
- Produces: module-level `session: SessionManager | undefined`, stopped by `shutdown()`; exported `MetergraphOptions.sessionPollMs?` for test control.

- [ ] **Step 1: Write the failing test**

```js
// append to typescript/test/repository-aware-ingest.test.mjs
import { init, shutdown } from "../dist/index.js";

test("wrap activates protocol-v2 session when repo config is present", async (t) => {
  const dir = tmpRepo();
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  writeRepoConfig(dir, { version: 2, repository: "acme/widgets" });

  let exchangeAuth;
  const server = await serve(async (request, response) => {
    for await (const _chunk of request);
    if (request.url === "/v1/ingest/sessions") {
      exchangeAuth = request.headers.authorization;
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({
        session_token: "session-xyz",
        expires_at: new Date(Date.now() + 5 * 60_000).toISOString(),
        repository_id: "repo_123",
      }));
    } else {
      response.writeHead(202);
      response.end();
    }
  });
  t.after(async () => new Promise((resolve) => server.close(resolve)));
  t.after(() => shutdown());

  init({
    token: "app-token-secret",
    ingestUrl: `http://127.0.0.1:${server.address().port}`,
    appRoot: dir,
  });

  const deadline = Date.now() + 2_000;
  let exchanged = false;
  while (Date.now() < deadline) {
    if (exchangeAuth) {
      exchanged = true;
      break;
    }
    await wait(20);
  }

  assert.equal(exchanged, true);
  assert.equal(exchangeAuth, "Bearer app-token-secret");
});
```

`init()`'s module-level `initialized` flag makes `init()` a no-op after the first successful call in a process, and `shutdown()` does not reset it -- this is why the existing suite (`typescript/test/sdk.test.mjs`) calls `init()` exactly once, in its own file. `node --test test/*.test.mjs` (the `npm test` script) runs each matched file as its own child process, so this new file gets fresh module state regardless of what `sdk.test.mjs` does -- no reset mechanism is needed as long as this is the only `init()` call added to `repository-aware-ingest.test.mjs`.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd typescript && npm run build && node --test test/repository-aware-ingest.test.mjs`
Expected: FAIL -- `exchangeAuth` stays `undefined` because `init()` does not yet discover repo config or construct a `SessionManager`.

- [ ] **Step 3: Wire `init()`/`shutdown()`**

Edit `typescript/src/index.ts`. Add imports:

```ts
import { discoverRepoConfig } from "./repo-config.js";
import { SessionManager } from "./session.js";
```

Add the `sessionPollMs` option and a module-level variable:

```ts
export interface MetergraphOptions {
  token?: string;
  ingestUrl?: string;
  captureText?: boolean;
  redact?: (text: string, kind: "request" | "response") => string;
  appRoot?: string;
  skipFrames?: string[];
  environment?: string;
  disabled?: boolean;
  transport?: TransportMode;
  queueSize?: number;
  batchSize?: number;
  flushMs?: number;
  configPollMs?: number;
  configHardTtlMs?: number;
  sessionPollMs?: number;
}
```

```ts
let session: SessionManager | undefined;
```

Update `init()`'s body:

```ts
export function init(options: MetergraphOptions = {}): void {
  if (initialized) return;
  if (env("METERGRAPH_DISABLED") === "1" || options.disabled) {
    initialized = true;
    return;
  }
  const token = options.token ?? env("METERGRAPH_APP_TOKEN");
  const ingestUrl = options.ingestUrl ?? env("METERGRAPH_INGEST_URL") ?? DEFAULT_INGEST_URL;
  if (!token || !ingestUrl) {
    if (!warnedNoToken) {
      warnedNoToken = true;
      console.warn("Metergraph capture disabled: token and ingest URL are required");
    }
    return;
  }
  initialized = true;
  try {
    const appRoot = options.appRoot ?? (typeof process === "undefined" ? "" : process.cwd());
    const repoConfig = discoverRepoConfig(appRoot);
    session = repoConfig
      ? new SessionManager(token, ingestUrl, repoConfig.repository, SDK_VERSION, options.sessionPollMs)
      : undefined;
    transport = new Transport(token, ingestUrl, {
      queueSize: options.queueSize ?? Number(env("METERGRAPH_QUEUE_SIZE") ?? 2_000),
      batchSize: options.batchSize ?? Number(env("METERGRAPH_BATCH_SIZE") ?? 100),
      flushMs: options.flushMs ?? Number(env("METERGRAPH_FLUSH_MS") ?? 5_000),
      mode: options.transport ?? "auto",
      session,
    });
    setCaptureRuntime(new CaptureRuntime(transport, {
      captureText: options.captureText ?? envBool("METERGRAPH_CAPTURE_TEXT", true),
      redact: options.redact,
      appRoot,
      skipFrames: options.skipFrames ?? [],
      environment: options.environment ?? env("METERGRAPH_ENV"),
      textMaxBytes: Math.min(
        100 * 1024,
        Math.max(1, Number(env("METERGRAPH_TEXT_MAX_BYTES") ?? 100 * 1024)),
      ),
      repoRoot: repoConfig?.repoRoot,
    }));
    config = new ConfigPoller(
      token,
      ingestUrl,
      options.configPollMs,
      options.configHardTtlMs,
    );
  } catch {
    transport = undefined;
    config = undefined;
    session?.stop();
    session = undefined;
    setCaptureRuntime();
    console.warn("Metergraph initialization failed; application is running uninstrumented");
  }
}
```

Update `shutdown()`:

```ts
export async function shutdown(): Promise<void> {
  config?.stop();
  config = undefined;
  session?.stop();
  session = undefined;
  await transport?.shutdown();
  transport = undefined;
  setCaptureRuntime();
}
```

Do not reset `initialized`/`warnedNoToken` inside `shutdown()` -- the existing suite relies on `init()` being a one-shot-per-process call (see Step 1), and this plan does not change that contract.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd typescript && npm run build && node --test test/repository-aware-ingest.test.mjs`
Expected: PASS. Also re-run `cd typescript && npm test` for a full regression pass.

- [ ] **Step 5: Commit**

```bash
git add typescript/src/index.ts typescript/test/repository-aware-ingest.test.mjs
git commit -m "Wire TypeScript init()/shutdown() to protocol-v2 session discovery"
```

### Task 18: TypeScript — credential non-logging sweep

**Files:**
- Modify: `typescript/test/repository-aware-ingest.test.mjs`

**Interfaces:**
- Consumes: `SessionManager` (Task 13/14), `Transport` (Task 15) -- no production code expected to change unless a leak is found (see Step 3).

- [ ] **Step 1: Write the failing tests**

```js
// append to typescript/test/repository-aware-ingest.test.mjs
test("SessionManager never logs the app token on failure", async (t) => {
  const server = await serve(async (request, response) => {
    for await (const _chunk of request);
    response.writeHead(500);
    response.end();
  });
  t.after(async () => new Promise((resolve) => server.close(resolve)));

  const secret = "app-token-super-secret-value";
  const warnings = [];
  const originalWarn = console.warn;
  console.warn = (...args) => warnings.push(args.join(" "));
  try {
    const manager = new SessionManager(
      secret,
      `http://127.0.0.1:${server.address().port}`,
      "owner/repo",
      "0.4.0",
      10_000,
    );
    await manager.ready;
    t.after(() => manager.stop());
  } finally {
    console.warn = originalWarn;
  }

  for (const message of warnings) assert.ok(!message.includes(secret));
});

test("Transport never logs the session token on rejection", async (t) => {
  const server = await serve(async (request, response) => {
    for await (const _chunk of request);
    response.writeHead(401);
    response.end();
  });
  t.after(async () => new Promise((resolve) => server.close(resolve)));

  const secret = "session-token-super-secret-value";
  const session = fakeSession(secret);
  const warnings = [];
  const originalWarn = console.warn;
  console.warn = (...args) => warnings.push(args.join(" "));
  try {
    const transport = new Transport(
      "app-token-unused",
      `http://127.0.0.1:${server.address().port}`,
      { mode: "background", flushMs: 100_000, session },
    );
    transport.enqueue({ row: 1 });
    await transport.flush(1_000);
    await transport.shutdown();
  } finally {
    console.warn = originalWarn;
  }

  for (const message of warnings) assert.ok(!message.includes(secret));
});
```

- [ ] **Step 2: Run tests to verify they pass or fail**

Run: `cd typescript && node --test test/repository-aware-ingest.test.mjs`
Expected: These should PASS against the Task 13/15 implementations, since `session.ts` and `transport.ts` never interpolate the raw token into `failureLog.report(...)` or `console.warn(...)` calls -- only the URL, HTTP status, and error message. Run to confirm; if either fails, it means a leak was introduced earlier in this plan and must be fixed in `session.ts`/`transport.ts` before proceeding.

- [ ] **Step 3: Fix any leak found (expected: none)**

No changes expected. If Step 2 fails, locate the offending `console.warn`/`failureLog.report` call and remove the token from the interpolated message, keeping only non-secret context.

- [ ] **Step 4: Run the full new test file to verify everything passes**

Run: `cd typescript && npm run build && node --test test/repository-aware-ingest.test.mjs`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add typescript/test/repository-aware-ingest.test.mjs
git commit -m "Add TypeScript credential non-logging regression tests"
```

### Task 19: TypeScript — packaging: bin entry and version 0.4.0

**Files:**
- Modify: `typescript/package.json`
- Modify: `typescript/src/version.ts`
- Modify: `typescript/test/repository-aware-ingest.test.mjs`

**Interfaces:**
- Produces: an npm `bin` entry `metergraph-setup` resolving to `./dist/setup.js`.

- [ ] **Step 1: Write the failing test**

```js
// append to typescript/test/repository-aware-ingest.test.mjs
import { SDK_VERSION } from "../dist/version.js";

test("package.json declares the metergraph-setup bin entry", () => {
  const pkg = JSON.parse(readFileSync(join(process.cwd(), "package.json"), "utf8"));
  assert.equal(pkg.bin["metergraph-setup"], "./dist/setup.js");
});

test("SDK_VERSION is 0.4.0", () => {
  assert.equal(SDK_VERSION, "0.4.0");
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd typescript && npm run build && node --test test/repository-aware-ingest.test.mjs`
Expected: FAIL -- `pkg.bin` is `undefined` (`TypeError`), and `SDK_VERSION` is `"0.3.2"`.

- [ ] **Step 3: Add the bin entry and bump the version**

Edit `typescript/package.json`:

```json
{
  "name": "metergraph",
  "version": "0.4.0",
  "description": "Fire-and-forget LLM spend capture for Metergraph",
  "type": "module",
  "main": "./dist/index.js",
  "types": "./dist/index.d.ts",
  "bin": {
    "metergraph-setup": "./dist/setup.js"
  },
  "exports": {
```

Edit `typescript/src/version.ts`:

```ts
export const SDK_VERSION = "0.4.0";
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd typescript && npm run build && node --test test/repository-aware-ingest.test.mjs`
Expected: PASS. Also re-run `cd typescript && npm test` for a full regression pass.

- [ ] **Step 5: Commit**

```bash
git add typescript/package.json typescript/src/version.ts typescript/test/repository-aware-ingest.test.mjs
git commit -m "Release TypeScript SDK 0.4.0 with the metergraph-setup bin entry"
```

### Task 20: Docs and examples updated to 0.4

**Files:**
- Modify: `README.md`
- Modify: `python/README.md`
- Modify: `typescript/README.md`
- Modify: `examples/README.md`

**Interfaces:** None -- documentation only.

- [ ] **Step 1: Add repository-aware setup to the root `README.md` agent prompt**

In `README.md`, inside the fenced agent-prompt block (the numbered list under "Set up with an AI coding agent"), append a new final step after the existing step 7:

```text
8. If this repo has a GitHub origin, run `metergraph-setup` (installed with
   the `metergraph` Python package) or `npx metergraph-setup` (TypeScript)
   once from the repo root, and commit the resulting `.metergraph/config.json`
   if it is new or changed. It is non-secret (just `{"version": 2,
   "repository": "owner/repo"}`) and unlocks repository-aware delivery; the
   SDK works the same without it.
```

Directly below the agent-prompt block (before the `## Python` heading), add one short paragraph:

```markdown
SDK 0.4 adds repository-aware ingest: once `metergraph-setup` has committed
`.metergraph/config.json`, the SDK exchanges your app token for a short-lived,
repository-scoped session token on startup and sends only that session token
on every trace call. Repos without that file keep working exactly as before
(protocol v1); nothing else changes in how you call `wrap()`.
```

- [ ] **Step 2: Add the same setup step to `python/README.md`**

In `python/README.md`, append to the numbered agent-prompt block:

```text
On serverless, call metergraph.flush() before the handler returns. If this
repo has a GitHub origin, also run `metergraph-setup` once from the repo root
and commit the resulting non-secret `.metergraph/config.json` -- it unlocks
repository-aware delivery and requires no other code change. When done, list
every client you wrapped and flag LLM calls made outside the official
openai / anthropic / google-genai SDKs, since those are not captured.
```

(This replaces the sentence that currently ends with "...call metergraph.flush() before the handler returns. When done, list...", inserting the new sentence in between.)

Add a short paragraph in the main body, after the "Configuration:" bullet list:

```markdown
Run `metergraph-setup` once from the repo root (installed automatically with
this package) to detect the repo's GitHub origin and commit
`.metergraph/config.json`. Once that file exists, `init()`/`wrap()`
automatically exchange the app token for a short-lived, repository-scoped
session token and send only that on every trace call. Without the file, the
SDK behaves exactly as it does today.
```

- [ ] **Step 3: Add the same setup step to `typescript/README.md`**

In `typescript/README.md`, append to the numbered agent-prompt block, inserting before the final "When done, list every client..." sentence:

```text
If this repo has a GitHub origin, also run `npx metergraph-setup` once from
the repo root and commit the resulting non-secret `.metergraph/config.json`
-- it unlocks repository-aware delivery and requires no other code change.
```

Add the equivalent paragraph in the main body, after the "Vercel AI SDK" section's closing paragraph (or immediately before "## Set up with an AI coding agent"):

```markdown
Run `npx metergraph-setup` once from the repo root to detect the repo's
GitHub origin and commit `.metergraph/config.json`. Once that file exists,
`init()`/`wrap()` automatically exchange the app token for a short-lived,
repository-scoped session token and send only that on every trace call.
Without the file, the SDK behaves exactly as it does today.
```

- [ ] **Step 4: Add a brief note to `examples/README.md`**

In `examples/README.md`, add one sentence after the intro paragraph:

```markdown
None of these examples need `.metergraph/config.json` to run; it only matters
in a real GitHub-hosted repo where you want repository-aware delivery (SDK
0.4), and is entirely optional.
```

- [ ] **Step 5: Self-review and commit**

Run:

```bash
git diff --check
```

Expected: no output (no trailing-whitespace/conflict-marker issues). Read the four modified files once more end to end to confirm every reference to "SDK 0.3" that describes *current* default behavior was left alone (0.3's content-capture defaults are still accurate) and only version-specific/setup-specific additions were made for 0.4 -- do not blanket-replace "0.3" with "0.4" anywhere.

```bash
git add README.md python/README.md typescript/README.md examples/README.md
git commit -m "Document repository-aware ingest protocol v2 and SDK 0.4 setup"
```

---

## Final self-review checklist (run once, after Task 20)

- [ ] **Spec coverage.** Re-read the design doc's §3 (registration + session exchange), §4 (v1/v2 coexistence), and §8's frame-capture paragraph, plus the single-versioned-artifact requirement this amendment closes. Confirm: Task 1 establishes the one canonical, metergraph-internal-owned schema artifact plus a pinned `(url, sha256)` lock file, with a local/URL override for coordinated pre-deployment development, and a separately named `contract-verification` CI job/command that is the only network-touching piece; Task 2/11 cover non-secret config write with SDK import/runtime never writing (Tasks 3/12, 8/17 assert read-only discovery); Task 4/13 cover the exact `POST /v1/ingest/sessions` shape with `sdk_version` as the real package version, and extend Task 1's verifier to validate the real SDK's request/response functions (offline, via override) rather than a literal fixture; Task 6/15 cover "app token never sent on a normal trace call"; Task 5/14 cover the two-tier refresh; Task 9/18 cover redaction; Task 7/16 cover the additive repo-relative frame path; Task 10/19/20 cover the 0.4 release.
- [ ] **No independent schema copy.** Confirm `contracts/ingest-session-v2.lock.json` holds only `(schema_url, sha256, manifest_url, contract_id)` -- never a copy of the schema body -- and that no file in this repo (a test fixture, a doc, a script) claims to be an independent or manually-synchronized source of truth for the contract shape. Test-only literal examples (e.g. `EXAMPLE_SCHEMA` in the verifier test files, or the inline response bodies in Task 4/13's SessionManager tests) are fine; they're synthetic test data feeding a local/override path, not a second persisted "real" copy.
- [ ] **Placeholder scan.** Grep the finished plan for `TBD`, `TODO`, `FIXME`, `...`, and "similar to Task" -- none should appear in a place standing in for real content (only inside literal example strings/URLs, if any).
- [ ] **Type consistency.** `SessionManager.get_token()`/`getToken()` and `.invalidate()` are the only two methods `Writer`/`Transport` call on a session object in every task (4/6/8/9 for Python, 13/15/17/18 for TS) -- confirm no task drifted to a different method name. `RepoConfig.repository`/`.repo_root` (Python) and `.repository`/`.repoRoot` (TS) are used identically in Tasks 3/8 and 12/17. `build_exchange_request_body`/`parse_session_response` (Python) and `buildExchangeRequestBody`/`parseSessionResponse` (TS), introduced in Task 4/13, are the only functions `_session.py`/`session.ts` and `verify_ingest_contract.py`/`verify-ingest-contract.mjs` share -- confirm no task drifted to inlining the request/response shape again instead of reusing them.
- [ ] **`git diff --check`** across the whole branch before considering the plan executed.

Report the plan's final commit hash and total task count (20) back to whoever requested this plan once all tasks and the self-review checklist are complete.
