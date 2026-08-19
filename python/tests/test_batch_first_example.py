import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_DIR = REPO_ROOT / "examples" / "python-batch-first"


def test_batch_first_example_is_valid_and_explicitly_accepts_duplicate_execution():
    source = (EXAMPLE_DIR / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "metergraph"
        and node.func.attr == "batch_first"
    ]

    assert len(calls) == 1
    acknowledgement = next(
        keyword.value
        for keyword in calls[0].keywords
        if keyword.arg == "accept_duplicate_provider_execution"
    )
    assert isinstance(acknowledgement, ast.Constant)
    assert acknowledgement.value is True
    assert not any(
        isinstance(node, ast.Attribute) and node.attr == "output_text"
        for node in ast.walk(tree)
    ), "batch results are JSON dictionaries, while direct results are SDK objects"


def test_batch_first_example_readme_warns_about_cost_and_capture_behavior():
    readme = (EXAMPLE_DIR / "README.md").read_text(encoding="utf-8").lower()

    assert "execute and bill" in readme
    assert "not captured" in readme
