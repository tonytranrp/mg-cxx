#!/usr/bin/env python3
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from clang_mg_common import (env_or_default, git, git_in_progress_paths, git_output,
                             root_from_script, timestamp)


def usage(script_name: str, llvm_dir: Path, work_dir: Path) -> None:
    print("Usage:")
    print(f"  scripts/{script_name} <feature-name> <start-ref>")
    print()
    print("Examples:")
    print(f"  scripts/{script_name} traits HEAD~7")
    print(f"  scripts/{script_name} conditional-members 1234abcd")
    print()
    print("This regenerates patches/<feature-name> from:")
    print("  <start-ref>..HEAD")
    print()
    print("Normally, use the safer top-level command instead:")
    print("  python build.py refresh-feature <feature-name>")
    print()
    print("Environment variables:")
    print(f"  LLVM_DIR={llvm_dir}")
    print(f"  WORK_DIR={work_dir}")


def show_features(root_dir: Path) -> None:
    print("Available features:")
    patches_root = root_dir / "patches"
    if not patches_root.is_dir():
        print("  No patches directory found.")
        return
    for p in sorted([p for p in patches_root.iterdir() if p.is_dir()], key=lambda p: p.name):
        print(f"  {p.name}")


def fail(message: str) -> int:
    print(message)
    return 1


def ensure_no_git_operation_in_progress(llvm_dir: Path) -> bool:
    paths = git_in_progress_paths(llvm_dir)
    if paths.get("rebase_merge", Path()).is_dir():
        print("ERROR: A rebase is currently in progress.")
        print("Finish or abort it before refreshing feature patches.")
        return False
    if paths.get("rebase_apply", Path()).is_dir():
        print("ERROR: A git-am or rebase-apply operation is currently in progress.")
        print("Finish or abort it before refreshing feature patches.")
        return False
    if paths.get("merge_head", Path()).is_file():
        print("ERROR: A merge is currently in progress.")
        print("Finish or abort it before refreshing feature patches.")
        return False
    if paths.get("cherry_pick_head", Path()).is_file():
        print("ERROR: A cherry-pick is currently in progress.")
        print("Finish or abort it before refreshing feature patches.")
        return False
    return True


def refresh_feature_patches(root_dir: Path, llvm_dir: Path, feature_name: str, start_ref: str) -> int:
    patch_dir = root_dir / "patches" / feature_name

    print("=== refresh feature ===")
    print(f"Feature:   {feature_name}")
    print(f"Start ref: {start_ref}")
    print(f"Patch dir: {patch_dir}")
    print(f"LLVM dir:  {llvm_dir}")
    print()

    if not (llvm_dir / ".git").is_dir():
        return fail(f"ERROR: LLVM repo is not cloned:\n{llvm_dir}")
    if not patch_dir.is_dir():
        print("ERROR: Feature patch directory does not exist:")
        print(str(patch_dir))
        print()
        show_features(root_dir)
        return 1
    if not ensure_no_git_operation_in_progress(llvm_dir):
        return 1

    cp = git(["rev-parse", "--verify", start_ref], cwd=llvm_dir, check=False, quiet=True)
    if cp.returncode != 0:
        print(f"ERROR: Start ref does not exist: {start_ref}")
        print()
        print("The patch directory was not modified.")
        return 1

    status = git_output(["status", "--porcelain"], cwd=llvm_dir, check=False)
    if status:
        print("ERROR: LLVM has uncommitted changes.")
        print()
        print("Commit, reset, or stash those changes before refreshing patches.")
        print("The patch directory was not modified.")
        return 1

    count_text = git_output(["rev-list", "--count", f"{start_ref}..HEAD"], cwd=llvm_dir, check=False)
    try:
        commit_count = int(count_text.strip() or "0")
    except ValueError:
        commit_count = 0
    if commit_count == 0:
        print(f"ERROR: No commits found in range {start_ref}..HEAD.")
        print("The patch directory was not modified.")
        return 1

    tmp_dir = Path(tempfile.mkdtemp(prefix=f".patch-refresh-{feature_name}.", dir=str(root_dir)))
    try:
        cp = git([
            "format-patch",
            "--zero-commit",
            "--no-stat",
            "--output-directory",
            str(tmp_dir),
            f"{start_ref}..HEAD",
        ], cwd=llvm_dir, check=False, quiet=True)
        if cp.returncode != 0:
            print("ERROR: Failed to regenerate patches.")
            print("The patch directory was not modified.")
            return 1

        regenerated = sorted(tmp_dir.glob("*.patch"))
        if not regenerated:
            print("ERROR: No regenerated patches were produced.")
            print("The patch directory was not modified.")
            return 1

        existing_patches = sorted(patch_dir.glob("*.patch"))
        backup_dir = Path(str(patch_dir) + f".backup.{timestamp()}")
        if existing_patches:
            backup_dir.mkdir(parents=True, exist_ok=True)
            for p in existing_patches:
                shutil.copy2(p, backup_dir / p.name)

        for p in existing_patches:
            p.unlink()
        for p in regenerated:
            shutil.copy2(p, patch_dir / p.name)

        print("Updated patch collection:")
        print(f"  {patch_dir}")
        if existing_patches:
            print()
            print("Backup of old patches:")
            print(f"  {backup_dir}")
        print()
        print("Regenerated patches:")
        for p in sorted(patch_dir.glob("*.patch")):
            print(f"  {p.name}")
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main(argv: list[str]) -> int:
    script_name = Path(__file__).name
    root_dir = root_from_script(__file__)
    work_dir = Path(env_or_default("WORK_DIR", root_dir / "work"))
    llvm_dir = Path(env_or_default("LLVM_DIR", work_dir / "llvm-project"))

    if not argv or argv[0] in {"-h", "--help"}:
        usage(script_name, llvm_dir, work_dir)
        print()
        show_features(root_dir)
        return 0
    if argv[0] in {"list", "--list"}:
        show_features(root_dir)
        return 0

    feature_name = argv[0]
    start_ref = argv[1] if len(argv) >= 2 else ""
    if not start_ref.strip():
        print("ERROR: Missing start ref.")
        print()
        usage(script_name, llvm_dir, work_dir)
        return 1

    return refresh_feature_patches(root_dir, llvm_dir, feature_name, start_ref)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
