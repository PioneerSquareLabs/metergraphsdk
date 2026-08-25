from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
VERIFY_COMMAND = "python python/tests/package/verify_artifacts.py"


def test_ci_and_publish_workflows_verify_python_artifacts():
    workflows = REPOSITORY_ROOT / ".github" / "workflows"
    ci = (workflows / "ci.yml").read_text(encoding="utf-8")
    publish = (workflows / "publish-python.yml").read_text(encoding="utf-8")

    assert VERIFY_COMMAND in ci
    assert VERIFY_COMMAND in publish
