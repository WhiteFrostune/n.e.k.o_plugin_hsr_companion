"""Write the checkout's short commit into runtime/build_info.py before packaging."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "runtime" / "build_info.py"


def main() -> int:
    commit = subprocess.check_output(
        ["git", "rev-parse", "--short=12", "HEAD"], cwd=ROOT, text=True).strip()
    source = TARGET.read_text(encoding="utf-8")
    stamped = re.sub(r'^BUILD_COMMIT = "[^"]*"$', f'BUILD_COMMIT = "{commit}"',
                     source, count=1, flags=re.M)
    if stamped == source:
        raise SystemExit("BUILD_COMMIT placeholder not found")
    TARGET.write_text(stamped, encoding="utf-8")
    print(commit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
