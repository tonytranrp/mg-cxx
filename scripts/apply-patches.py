#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path
from collections import deque

from clang_mg_common import (env_or_default, git, git_output, is_ignored_patch_dir_name,
                             parse_enabled, read_feature_config, root_from_script, run,
                             write_default_feature_config)


def fail(message: str) -> None:
    print(message)
    raise SystemExit(1)


def feature_dirs(patch_root: Path) -> list[Path]:
    return sorted([p for p in patch_root.iterdir() if p.is_dir() and not is_ignored_patch_dir_name(p.name)], key=lambda p: str(p))


def ignored_patch_dirs(patch_root: Path) -> list[Path]:
    return sorted([p for p in patch_root.iterdir() if p.is_dir() and is_ignored_patch_dir_name(p.name)], key=lambda p: str(p))


def load_feature_configs(patch_root: Path, feature_config_name: str):
    features: list[str] = []
    feature_dirs_by_name: dict[str, Path] = {}
    feature_has_patches: dict[str, int] = {}
    feature_enabled: dict[str, int] = {}
    feature_deps: dict[str, list[str]] = {}
    feature_before: dict[str, list[str]] = {}

    ignored = ignored_patch_dirs(patch_root)
    if ignored:
        print("Ignoring backup/temp patch directories:")
        for p in ignored:
            print(f"  {p}")
        print()

    for feature_dir in feature_dirs(patch_root):
        feature_name = feature_dir.name
        config_file = feature_dir / feature_config_name
        if any(ch.isspace() for ch in feature_name):
            fail(f"ERROR: Feature directory names cannot contain whitespace:\n  {feature_name}")
        if not config_file.is_file():
            print("Generating missing config:")
            print(f"  {config_file}")
            write_default_feature_config(config_file, feature_name)
        features.append(feature_name)
        feature_dirs_by_name[feature_name] = feature_dir
        feature_has_patches[feature_name] = 1 if list(feature_dir.glob("*.patch")) else 0
        cfg = read_feature_config(config_file)
        feature_enabled[feature_name] = 1 if parse_enabled(str(cfg["enabled"])) else 0
        feature_deps[feature_name] = list(cfg["depends"])  # type: ignore[arg-type]
        feature_before[feature_name] = list(cfg["before"])  # type: ignore[arg-type]
    return features, feature_dirs_by_name, feature_has_patches, feature_enabled, feature_deps, feature_before


def build_feature_order(features: list[str], feature_dirs_by_name: dict[str, Path], feature_has_patches: dict[str, int],
                        feature_enabled: dict[str, int], feature_deps: dict[str, list[str]],
                        feature_before: dict[str, list[str]]) -> list[str]:
    enabled_features: list[str] = []
    enabled_feature_map: set[str] = set()
    in_degree: dict[str, int] = {}
    edges: dict[str, list[str]] = {}

    for feature in features:
        if feature_has_patches[feature] != 1:
            print(f"Skipping feature with no .patch files: {feature}")
            continue
        if feature_enabled[feature] != 1:
            print(f"Skipping disabled feature: {feature}")
            continue
        enabled_features.append(feature)
        enabled_feature_map.add(feature)
        in_degree[feature] = 0
        edges[feature] = []

    def validate_dependency(feature: str, dep: str) -> None:
        if dep not in feature_dirs_by_name:
            print(f"ERROR: Feature '{feature}' depends on unknown feature '{dep}'.")
            print()
            print("Known features:")
            for known in features:
                print(f"  {known}")
            raise SystemExit(1)
        if feature_has_patches[dep] != 1:
            fail(f"ERROR: Feature '{feature}' depends on '{dep}', but '{dep}' has no .patch files.")
        if feature_enabled[dep] != 1:
            print(f"ERROR: Feature '{feature}' depends on '{dep}', but '{dep}' is disabled.")
            print()
            print(f"Either enable '{dep}' or disable '{feature}'.")
            raise SystemExit(1)

    def add_edge(src: str, dst: str) -> None:
        edges.setdefault(src, []).append(dst)
        in_degree[dst] = in_degree.get(dst, 0) + 1

    for feature in enabled_features:
        for dep in feature_deps.get(feature, []):
            if not dep.strip():
                continue
            validate_dependency(feature, dep)
            add_edge(dep, feature)
        for before in feature_before.get(feature, []):
            if not before.strip():
                continue
            if before not in feature_dirs_by_name:
                fail(f"ERROR: Feature '{feature}' has BEFORE entry for unknown feature '{before}'.")
            if before not in enabled_feature_map:
                print(f"NOTE: '{feature}' says it should run before '{before}', but '{before}' is not enabled. Ignoring.")
                continue
            add_edge(feature, before)

    queue: list[str] = sorted([f for f in enabled_features if in_degree[f] == 0])
    ordered: list[str] = []
    while queue:
        feature = queue.pop(0)
        ordered.append(feature)
        for nxt in edges.get(feature, []):
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                queue.append(nxt)
                queue.sort()

    if len(ordered) != len(enabled_features):
        print("ERROR: Dependency cycle detected between enabled features.")
        print()
        print("Features still blocked:")
        remaining = False
        for feature in enabled_features:
            if in_degree[feature] > 0:
                print(f"  {feature}")
                remaining = True
        if not remaining:
            print("  unknown")
        raise SystemExit(1)
    return ordered


def apply_loose_patches(patch_root: Path) -> None:
    loose = sorted([
        p for p in patch_root.glob("*.patch")
        if not (p.name.endswith(".backup.patch") or p.name.endswith(".bak.patch") or p.name.endswith(".old.patch") or p.name.endswith("~"))
    ], key=lambda p: str(p))
    if not loose:
        print("No loose top-level patches found.")
        return
    print()
    print("Applying loose top-level patches from:")
    print(str(patch_root))
    git(["am", "--3way", *[str(p) for p in loose]])


def apply_ordered_features(root_dir: Path, llvm_dir: Path, ordered_features: list[str]) -> None:
    print()
    print("Feature apply order:")
    if not ordered_features:
        print("  No enabled feature patch directories found.")
        return
    for f in ordered_features:
        print(f"  {f}")
    apply_feature_script = root_dir / "scripts" / "apply-feature.py"
    for feature_name in ordered_features:
        print()
        print(f"Applying feature: {feature_name}")
        env = os.environ.copy()
        env["LLVM_DIR"] = str(llvm_dir)
        env["REFRESH_PATCHES"] = "0"
        run([sys.executable, str(apply_feature_script), feature_name], env=env)


def main(argv: list[str]) -> int:
    script_root = root_from_script(__file__)
    root_dir = Path(argv[0]).resolve() if len(argv) >= 1 and argv[0].strip() else script_root
    llvm_dir = Path(argv[1]).resolve() if len(argv) >= 2 and argv[1].strip() else root_dir / "work" / "llvm-project"
    patch_root = root_dir / "patches"
    apply_feature_script = root_dir / "scripts" / "apply-feature.py"
    feature_config_name = env_or_default("FEATURE_CONFIG_NAME", "feature.conf")

    print("=== apply all clang-mg patches ===")
    print(f"Root dir:   {root_dir}")
    print(f"LLVM dir:   {llvm_dir}")
    print(f"Patch root: {patch_root}")
    print()

    if not (llvm_dir / ".git").is_dir():
        fail(f"ERROR: LLVM repo is not cloned:\n{llvm_dir}")
    if not patch_root.is_dir():
        fail(f"ERROR: Patch directory does not exist:\n{patch_root}")
    if not apply_feature_script.is_file():
        print("ERROR: apply-feature Python script not found:")
        print(str(apply_feature_script))
        print()
        print("Expected this file to exist:")
        print("  scripts/apply-feature.py")
        return 1

    old_cwd = Path.cwd()
    os.chdir(llvm_dir)
    try:
        status = git_output(["status", "--porcelain"], check=False)
        if status:
            print("ERROR: LLVM has uncommitted changes.")
            print()
            print("Save, commit, or reset your changes before applying patches.")
            print()
            print("Useful commands:")
            print("  git status")
            print("  git diff")
            print("  git add .")
            print('  git commit -m "clang-mg: describe current work"')
            print()
            print("Apply cancelled.")
            return 1
        features, fdirs, has_patches, enabled, deps, before = load_feature_configs(patch_root, feature_config_name)
        ordered = build_feature_order(features, fdirs, has_patches, enabled, deps, before)
        apply_loose_patches(patch_root)
        apply_ordered_features(root_dir, llvm_dir, ordered)
        print()
        print("All enabled clang-mg patches applied.")
        return 0
    finally:
        os.chdir(old_cwd)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
