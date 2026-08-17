"""Build and verify the Python artifacts customers install from PyPI."""

from __future__ import annotations

import argparse
import email
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = REPOSITORY_ROOT / "python"
SOURCE_PACKAGE = PACKAGE_ROOT / "src" / "metergraph"


def run(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    subprocess.run(args, cwd=cwd, env=env, check=True)


def assert_artifact_contents(wheel: Path, sdist: Path) -> None:
    required_modules = {
        path.relative_to(SOURCE_PACKAGE).as_posix()
        for path in SOURCE_PACKAGE.glob("*.py")
    }

    with zipfile.ZipFile(wheel) as archive:
        wheel_names = set(archive.namelist())
        packaged_modules = {
            name.removeprefix("metergraph/")
            for name in wheel_names
            if name.startswith("metergraph/") and name.endswith(".py")
        }
        assert required_modules == packaged_modules
        assert not any("/tests/" in f"/{name}" for name in wheel_names)
        metadata_name = next(
            name for name in wheel_names if name.endswith(".dist-info/METADATA")
        )
        metadata = email.message_from_bytes(archive.read(metadata_name))

    assert metadata["Name"] == "metergraph"
    assert metadata["Requires-Python"] == ">=3.10"
    runtime_dependencies = [
        value
        for value in metadata.get_all("Requires-Dist", [])
        if "extra ==" not in value
    ]
    assert runtime_dependencies == []

    with tarfile.open(sdist, "r:gz") as archive:
        sdist_names = {member.name for member in archive.getmembers()}
    assert any(name.endswith("/pyproject.toml") for name in sdist_names)
    for module in required_modules:
        assert any(name.endswith(f"/src/metergraph/{module}") for name in sdist_names)
    assert not any("/tests/" in f"/{name}" for name in sdist_names)


def installed_smoke_test(venv_root: Path, consumer: Path) -> None:
    python = (
        venv_root / "Scripts" / "python.exe"
        if os.name == "nt"
        else venv_root / "bin" / "python"
    )
    smoke = r'''
import importlib.metadata
from pathlib import Path
import sys
import metergraph

required = {
    "init", "wrap", "route", "trace", "set_session", "set_tags",
    "flush", "shutdown", "model_for", "record_outcome", "batch_first",
}
assert required <= set(metergraph.__all__)
assert required <= set(dir(metergraph))
assert Path(metergraph.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
assert importlib.metadata.version("metergraph") == metergraph.__version__
metergraph.init(disabled=True)
assert metergraph.flush()
metergraph.shutdown()
'''
    env = {**os.environ, "PYTHONPATH": ""}
    run(str(python), "-I", "-c", smoke, cwd=consumer, env=env)


def verify_dist(dist: Path, workspace: Path) -> None:
    wheels = list(dist.glob("*.whl"))
    sdists = list(dist.glob("*.tar.gz"))
    assert len(wheels) == 1, f"expected one wheel, found {wheels}"
    assert len(sdists) == 1, f"expected one sdist, found {sdists}"
    assert_artifact_contents(wheels[0], sdists[0])

    venv_root = workspace / "venv"
    venv.EnvBuilder(with_pip=True, clear=True).create(venv_root)
    python = (
        venv_root / "Scripts" / "python.exe"
        if os.name == "nt"
        else venv_root / "bin" / "python"
    )
    run(
        str(python),
        "-m",
        "pip",
        "install",
        "--no-deps",
        "--no-index",
        str(wheels[0].resolve()),
    )
    consumer = workspace / "consumer"
    consumer.mkdir()
    installed_smoke_test(venv_root, consumer)

    print(
        json.dumps(
            {
                "wheel": wheels[0].name,
                "sdist": sdists[0].name,
                "python": sys.version.split()[0],
                "status": "ok",
            }
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dist",
        type=Path,
        help="verify an existing artifact directory instead of building",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="metergraph-python-package-") as raw:
        workspace = Path(raw)
        if args.dist is not None:
            verify_dist(args.dist.resolve(), workspace)
            return

        dist = workspace / "dist"
        run(
            sys.executable,
            "-m",
            "build",
            "--outdir",
            str(dist),
            str(PACKAGE_ROOT),
            cwd=workspace,
        )
        verify_dist(dist, workspace)


if __name__ == "__main__":
    main()
