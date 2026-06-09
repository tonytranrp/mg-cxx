#!/usr/bin/env python3
from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    print("ERROR: scripts/apply-feature.py is no longer used.")
    print("clang-mg now uses one flat patch stack in patches/.")
    print()
    print("Use:")
    print("  python build.py apply")
    print()
    print("or directly:")
    print("  python scripts/apply-patches.py")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
