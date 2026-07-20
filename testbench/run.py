#!/usr/bin/env python3
"""Run the live Python/TypeScript Metergraph cost bench and verify stored rows."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEFAULT_ENV_FILE = REPO / ".env"
DEFAULT_URLS = {
    "aws": "https://d2xus7mp8zdv6t.cloudfront.net",
    "oss": "http://localhost:8787",
}
TOKEN_ENV = {
    "aws": "METERGRAPH_BENCH_AWS_TOKEN",
    "oss": "METERGRAPH_BENCH_OSS_TOKEN",
}
PROVIDER_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_GENAI_API_KEY")
MODELS = {
    "openai": "gpt-5.6-luna",
    "anthropic": "claude-haiku-4-5-20251001",
    "google": "gemini-2.5-flash",
}
RATES = {
    "openai": {
        "input": Decimal("1.00"),
        "output": Decimal("6.00"),
        "cache_read": Decimal("0.10"),
        "cache_write": Decimal("1.25"),
        "input_includes_cache_read": True,
    },
    "anthropic": {
        "input": Decimal("1.00"),
        "output": Decimal("5.00"),
        "cache_read": Decimal("0.10"),
        "cache_write": Decimal("1.25"),
        "input_includes_cache_read": False,
    },
    "google": {
        "input": Decimal("0.30"),
        "output": Decimal("2.50"),
        "cache_read": Decimal("0.075"),
        "cache_write": None,
        "input_includes_cache_read": False,
    },
}
COST_QUANTUM = Decimal("0.00000001")


def load_env(path: Path) -> dict[str, str]:
    """Read simple dotenv assignments without evaluating shell code."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] == value[-1:] and value[:1] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def redact(text: str, env: dict[str, str]) -> str:
    for key in (*PROVIDER_KEYS, *TOKEN_ENV.values(), "METERGRAPH_APP_TOKEN"):
        value = env.get(key)
        if value:
            text = text.replace(value, "<redacted>")
    return text


def prepare() -> None:
    commands = (
        ["uv", "sync", "--project", str(HERE)],
        ["npm", "install", "--prefix", str(REPO / "typescript")],
        ["npm", "run", "build", "--prefix", str(REPO / "typescript")],
        ["npm", "install", "--prefix", str(HERE)],
        ["npm", "run", "build", "--prefix", str(HERE)],
    )
    for command in commands:
        subprocess.run(command, cwd=REPO, check=True)


def parse_last_json(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def run_language(language: str, env: dict[str, str]) -> dict[str, Any]:
    if language == "python":
        command = [
            "uv",
            "run",
            "--project",
            str(HERE),
            "python",
            str(HERE / "providers_python.py"),
        ]
    else:
        command = ["node", str(HERE / "dist" / "providers_typescript.js")]
    result = subprocess.run(command, cwd=REPO, env=env, text=True, capture_output=True)
    parsed = parse_last_json(result.stdout) or {
        "language": language,
        "flushed": False,
        "calls": [],
        "error": "provider runner emitted no JSON result",
    }
    parsed["exit_code"] = result.returncode
    diagnostics = "\n".join(
        part for part in (result.stdout, result.stderr) if part
    ).strip()
    if diagnostics and result.returncode:
        parsed["diagnostics"] = redact(diagnostics, env)[-4000:]
    return parsed


def api_json(url: str, token: str, path: str, params: dict[str, str] | None = None):
    query = "?" + urllib.parse.urlencode(params) if params else ""
    request = urllib.request.Request(
        f"{url.rstrip('/')}{path}{query}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read(2000).decode("utf-8", "replace")
        raise RuntimeError(f"{path} returned HTTP {exc.code}: {body}") from exc


def expected_cost(row: dict[str, Any]) -> Decimal:
    rates = RATES[str(row["provider"])]
    input_tokens = int(row.get("input_tokens") or 0)
    output_tokens = int(row.get("output_tokens") or 0)
    cache_read = int(row.get("cache_read_tokens") or 0)
    cache_write = int(row.get("cache_write_tokens") or 0)
    billable_input = (
        input_tokens - cache_read
        if rates["input_includes_cache_read"]
        else input_tokens
    )
    cost = Decimal(billable_input) * rates["input"]
    cost += Decimal(output_tokens) * rates["output"]
    cost += Decimal(cache_read) * rates["cache_read"]
    if cache_write:
        if rates["cache_write"] is None:
            raise ValueError(
                "cache-write tokens were reported without a configured benchmark rate"
            )
        cost += Decimal(cache_write) * rates["cache_write"]
    return (cost / Decimal(1_000_000)).quantize(COST_QUANTUM, rounding=ROUND_HALF_UP)


def verify_row(
    row: dict[str, Any], expected_route: str, language: str, provider: str
) -> dict[str, Any]:
    errors = []
    if row.get("route") != expected_route:
        errors.append(f"route mismatch: {row.get('route')!r}")
    if row.get("sdk") not in {None, language}:
        errors.append(f"SDK language mismatch: {row.get('sdk')!r}")
    if row.get("provider") not in {None, provider}:
        errors.append(f"provider mismatch: {row.get('provider')!r}")
    if row.get("model") != MODELS.get(provider):
        errors.append(f"model mismatch: {row.get('model')!r}")
    if int(row.get("calls") or 1) != 1:
        errors.append(f"expected one call, got {row.get('calls')!r}")
    if int(row.get("input_tokens") or 0) <= 0:
        errors.append("input tokens were not captured")
    if int(row.get("output_tokens") or 0) <= 0:
        errors.append("output tokens were not captured")

    expected = expected_cost(row)
    actual_raw = row.get("cost_usd")
    actual = (
        Decimal(str(actual_raw)).quantize(COST_QUANTUM)
        if actual_raw is not None
        else None
    )
    if row.get("cost_status") not in {"priced", "reported"}:
        errors.append(f"cost status is {row.get('cost_status')!r}")
    if actual is None:
        errors.append("cost_usd is null")
    elif actual != expected:
        errors.append(f"cost mismatch: got {actual}, expected {expected}")
    return {
        "source": row.get("source", "calls"),
        "language": language,
        "provider": provider,
        "model": row.get("model"),
        "route": row.get("route"),
        "input_tokens": row.get("input_tokens"),
        "output_tokens": row.get("output_tokens"),
        "cache_read_tokens": row.get("cache_read_tokens"),
        "cache_write_tokens": row.get("cache_write_tokens"),
        "reasoning_tokens": row.get("reasoning_tokens"),
        "cost_status": row.get("cost_status"),
        "cost_usd": actual_raw,
        "expected_cost_usd": float(expected),
        "ok": not errors,
        "errors": errors,
    }


def poll_rows(
    target: str,
    url: str,
    token: str,
    environment: str,
    run_id: str,
    timeout: float,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    prefix = f"live-bench:{run_id}:{target}:"
    deadline = time.monotonic() + timeout
    last_error = None
    rows: list[dict[str, Any]] = []
    routes: set[str] = set()
    warnings: list[str] = []
    calls_available = True
    expected_routes = {
        f"{prefix}{language}:{provider}"
        for language in ("python", "typescript")
        for provider in MODELS
    }
    while time.monotonic() < deadline:
        if calls_available:
            try:
                _, payload = api_json(
                    url,
                    token,
                    "/v1/calls",
                    {"limit": "500", "environment": environment},
                )
                rows = [
                    {**row, "source": "calls"}
                    for row in payload.get("items", [])
                    if str(row.get("route") or "").startswith(prefix)
                ]
                routes = {str(row.get("route")) for row in rows}
                if expected_routes <= routes:
                    return rows, [], warnings
            except Exception as exc:
                calls_available = False
                warnings.append(
                    f"/v1/calls unavailable; verified through /v1/usage instead: {exc}"
                )
        try:
            _, payload = api_json(
                url,
                token,
                "/v1/usage",
                {"group_by": "route", "environment": environment},
            )
            rows = []
            for item in payload.get("items", []):
                route = str(item.get("key") or "")
                if not route.startswith(prefix):
                    continue
                provider = route.rsplit(":", 1)[-1]
                unpriced = int(item.get("unpriced_calls") or 0)
                reported = int(item.get("reported_calls") or 0)
                rows.append(
                    {
                        **item,
                        "source": "usage",
                        "route": route,
                        "provider": provider,
                        "model": MODELS.get(provider),
                        "cost_status": (
                            "unpriced"
                            if unpriced
                            else "reported"
                            if reported
                            else "priced"
                        ),
                    }
                )
            routes = {str(row.get("route")) for row in rows}
            if expected_routes <= routes:
                return rows, [], warnings
        except Exception as exc:
            last_error = str(exc)
        time.sleep(2)
    missing = [
        f"{prefix}{language}:{provider}"
        for language in ("python", "typescript")
        for provider in MODELS
        if f"{prefix}{language}:{provider}" not in routes
    ]
    errors = [f"timed out waiting for {len(missing)} row(s)"]
    if last_error:
        errors.append(last_error)
    return rows, errors, warnings


def verify_model_groups(
    url: str, token: str, environment: str
) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        _, payload = api_json(
            url,
            token,
            "/v1/usage",
            {"group_by": "model", "environment": environment},
        )
    except Exception as exc:
        return [], [f"model aggregate query failed: {exc}"]
    items = payload.get("items", [])
    results = []
    errors = []
    for provider, model in MODELS.items():
        matches = [
            item
            for item in items
            if item.get("key") == model and item.get("provider") == provider
        ]
        if len(matches) != 1:
            errors.append(
                f"expected one {provider}/{model} aggregate, found {len(matches)}"
            )
            continue
        item = matches[0]
        item_errors = []
        if int(item.get("calls") or 0) != 2:
            item_errors.append(f"expected two calls, got {item.get('calls')!r}")
        results.append(
            {
                "provider": provider,
                "model": model,
                "calls": item.get("calls"),
                "cost_usd": item.get("cost_usd"),
                "unpriced_calls": item.get("unpriced_calls"),
                "reported_calls": item.get("reported_calls"),
                "ok": not item_errors,
                "errors": item_errors,
            }
        )
        errors.extend(f"{provider}/{model}: {error}" for error in item_errors)
    return results, errors


def target_result(
    target: str,
    url: str,
    token: str,
    base_env: dict[str, str],
    run_id: str,
    environment: str,
    poll_timeout: float,
    issue_calls: bool = True,
) -> dict[str, Any]:
    env = {
        **base_env,
        "METERGRAPH_APP_TOKEN": token,
        "METERGRAPH_INGEST_URL": url,
        "METERGRAPH_ENV": environment,
        "METERGRAPH_BENCH_RUN_ID": run_id,
        "METERGRAPH_BENCH_TARGET": target,
    }
    runners = (
        [run_language(language, env) for language in ("python", "typescript")]
        if issue_calls
        else []
    )
    rows, polling_errors, reporting_warnings = poll_rows(
        target, url, token, environment, run_id, poll_timeout
    )
    model_groups, model_errors = verify_model_groups(url, token, environment)
    by_route = {str(row.get("route")): row for row in rows}
    verified = []
    missing = []
    prefix = f"live-bench:{run_id}:{target}:"
    for language in ("python", "typescript"):
        for provider in MODELS:
            route = f"{prefix}{language}:{provider}"
            row = by_route.get(route)
            if row is None:
                missing.append(route)
            else:
                verified.append(verify_row(row, route, language, provider))
    ok = (
        all(runner.get("exit_code") == 0 for runner in runners)
        and not polling_errors
        and not model_errors
        and not missing
        and len(verified) == 6
        and all(row["ok"] for row in verified)
    )
    return {
        "target": target,
        "url": url,
        "ok": ok,
        "runners": runners,
        "rows": verified,
        "model_groups": model_groups,
        "model_errors": model_errors,
        "missing_routes": missing,
        "polling_errors": polling_errors,
        "reporting_warnings": reporting_warnings,
    }


def args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", default="aws,oss", help="comma-separated: aws,oss")
    parser.add_argument(
        "--aws-url", default=os.getenv("METERGRAPH_BENCH_AWS_URL", DEFAULT_URLS["aws"])
    )
    parser.add_argument(
        "--oss-url", default=os.getenv("METERGRAPH_BENCH_OSS_URL", DEFAULT_URLS["oss"])
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(
            os.getenv("METERGRAPH_BENCH_ENV_FILE", str(DEFAULT_ENV_FILE))
        ).expanduser(),
    )
    parser.add_argument("--poll-timeout", type=float, default=90)
    parser.add_argument("--skip-setup", action="store_true")
    parser.add_argument(
        "--verify-run-id",
        help="skip provider calls and re-verify rows from an earlier run ID",
    )
    parser.add_argument("--output", type=Path, help="optional JSON report path")
    return parser.parse_args()


def main() -> int:
    options = args()
    targets = [item.strip() for item in options.targets.split(",") if item.strip()]
    unknown = set(targets) - DEFAULT_URLS.keys()
    if unknown:
        raise SystemExit(f"unknown targets: {', '.join(sorted(unknown))}")
    file_env = load_env(options.env_file)
    base_env = {
        **os.environ,
        **{key: value for key, value in file_env.items() if key in PROVIDER_KEYS},
    }
    missing_provider_keys = [key for key in PROVIDER_KEYS if not base_env.get(key)]
    if missing_provider_keys:
        raise SystemExit(f"missing provider keys: {', '.join(missing_provider_keys)}")
    target_tokens = {}
    for target in targets:
        token = os.environ.get(TOKEN_ENV[target])
        if not token:
            raise SystemExit(
                f"missing target token environment variable {TOKEN_ENV[target]}"
            )
        target_tokens[target] = token
    if not options.skip_setup and not options.verify_run_id:
        prepare()

    run_id = options.verify_run_id or (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + secrets.token_hex(3)
    )
    environment = f"metergraph-live-bench-{run_id}"
    urls = {"aws": options.aws_url, "oss": options.oss_url}
    report = {
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "environment": environment,
        "mode": "verify-only" if options.verify_run_id else "live",
        "models": MODELS,
        "targets": [
            target_result(
                target,
                urls[target],
                target_tokens[target],
                base_env,
                run_id,
                environment,
                options.poll_timeout,
                issue_calls=not options.verify_run_id,
            )
            for target in targets
        ],
    }
    report["ok"] = all(target["ok"] for target in report["targets"])
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if options.output:
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(rendered + "\n")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
