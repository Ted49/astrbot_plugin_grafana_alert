"""Run every test function without requiring pytest."""

import importlib
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent
for path in (str(PLUGIN_DIR), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)


def _load_tests():
    tests = []
    for path in sorted(HERE.glob("test_*.py")):
        module = importlib.import_module(path.stem)
        for name, value in vars(module).items():
            if name.startswith("test_") and callable(value):
                tests.append((f"{path.stem}.{name}", value))
    return tests


def main() -> int:
    failures = 0
    for name, test in _load_tests():
        try:
            test()
        except Exception:
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            print(f"PASS {name}")
    return failures


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
