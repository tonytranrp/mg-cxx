#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

from clang_mg_common import run, root_from_script


def main(argv: list[str]) -> int:
    root_dir = root_from_script(__file__)
    print("NOTICE: scripts/save-feature.py is deprecated.")
    print("Saving now appends one patch to the flat patches/ stack.")
    print()
    return run([sys.executable, str(root_dir / "scripts" / "save-patches.py")], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
