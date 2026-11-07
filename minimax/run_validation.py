"""Run the before/after acceptance checks for the SDD sample."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
TEST_FILE = ROOT / "sample_project" / "test_shipping.py"
BEFORE_FILE = ROOT / "sample_project" / "before" / "shipping.py"
CURRENT_FILE = ROOT / "sample_project" / "shipping.py"


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location("shipping_validation", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def run_module_checks(path: Path) -> tuple[bool, list[str]]:
    module = load_module(path)
    failures = []
    checks = [
        (module.qualifies_for_free_shipping(98.99, "Zhejiang"), False),
        (module.qualifies_for_free_shipping(99, "Zhejiang"), True),
        (module.qualifies_for_free_shipping(120, "Tibet"), False),
    ]
    for actual, expected in checks:
        if actual is not expected:
            failures.append(f"expected {expected}, got {actual}")
    return not failures, failures


def main() -> int:
    before_ok, before_failures = run_module_checks(BEFORE_FILE)
    after_ok, after_failures = run_module_checks(CURRENT_FILE)
    report = {
        "before": {"passed": before_ok, "failures": before_failures},
        "after": {"passed": after_ok, "failures": after_failures},
        "same_test_cases": True,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if before_ok or not after_ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
