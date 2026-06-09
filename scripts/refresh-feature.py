#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

from clang_mg_common import run, root_from_script


def main(argv: list[str]) -> int:
    root_dir = root_from_script(__file__)
    print("NOTICE: scripts/refresh-feature.py is deprecated.")
    print("Refreshing now regenerates the whole flat patches/ stack.")
    print()
    args = [sys.executable, str(root_dir / "scripts" / "refresh-patches.py")]
    if argv:
        # Old usage was: refresh-feature <feature> <start-ref>.
        # Preserve the explicit start ref if one was provided.
        if len(argv) >= 2:
            args.append(argv[1])
        elif len(argv) == 1:
            print("Ignoring old feature name argument:", argv[0])
    return run(args, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
