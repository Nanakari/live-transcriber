"""Run the same offline regression checks locally, in CI and before packaging."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-local", action="store_true", help="Also check installed local integrations")
    args = parser.parse_args()
    node = shutil.which("node")
    if not node:
        parser.error("Node.js is required for the frontend regression test.")
    tests = [sys.executable, "-m", "pytest", "-q"]
    if args.run_local:
        tests.append("--run-local")
    commands = [
        tests,
        [node, "tests/test_workbench_results.js"],
        [sys.executable, "-m", "compileall", "-q", "app", "scripts", "main.py"],
    ]
    for command in commands:
        print("Checking: " + " ".join(command), flush=True)
        result = subprocess.run(command, cwd=ROOT)
        if result.returncode:
            return result.returncode
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
