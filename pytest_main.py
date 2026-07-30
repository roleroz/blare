"""Shared py_test entry point: pytest via the pytest-bazel Bazel-protocol shim.

Every py_test target in this repo uses this file as its `main`, per the global
rule to use an existing runner (`pytest-bazel`) rather than hand-rolling one.
`pytest_bazel.main()` maps Bazel's test-protocol env vars (XML_OUTPUT_FILE,
TEST_TMPDIR, sharding, --test_filter) onto pytest's own arguments, and — the
detail the global rules call out explicitly — propagates pytest's exit code 5
("no tests collected") as this process's exit code, so a target that silently
collects nothing fails the build rather than reporting green.
"""

from __future__ import annotations

import sys

import pytest_bazel

if __name__ == "__main__":
    sys.exit(pytest_bazel.main())
