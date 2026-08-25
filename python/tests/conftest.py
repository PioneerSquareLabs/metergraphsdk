"""Make the ``tests`` directory importable so contract tests can reuse the
shared protocol doubles and parity harness in ``tests/fixtures`` (per the
taxonomy in tests/README.md, test-only helpers live under ``fixtures/``,
outside the production package)."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
