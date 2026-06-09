#!/usr/bin/env python3
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from clang_mg_common import env_or_default, git, git_in_progress_paths, git_output, root_from_script, timestamp


def usage(script_name: str, llvm_dir: Path, work_dir: Path) -> None:
    print("Usage:")
    print(f"  scripts/{script_name} [start-ref]")
    print()
    print("Examples:")
    print(f"  scripts/{script_name} origin/main")
    print(f"  scripts/{script_name} HEAD~12")
    print()
    print("This regenerates the flat patches/ stack from:")
    print("  <start-ref>..HEAD")
    print()
    print("Normally, use the top-level command instead:")
    print("  python build.py refresh")
    print()
    print("Environment variables:")
    print(f"  LLVM_DIR={llvm_dir}")
    print(f"  WORK_DIR={work_dir}")


def fail(message: str) -> int:
    print(message)
    return 1


def ensure_no_git_operation_in_progress(llvm_dir: Path) -> bool:
    paths = git_in_progress_paths(llvm_dir)
    if paths.get("rebase_merge", Path()).is_dir():
        print("ERROR: A rebase is currently in progress.")
        print("Finish or abort it before refreshing patches.")
        return False
    if paths.get("rebase_apply", Path()).is_dir():
        print("ERROR: A git-am or rebase-apply operation is currently in progress.")
        print("Finish or abort it before refreshing patches.")
        return False
    if paths.get("merge_head", Path()).is_file():
        print("ERROR: A merge is currently in progress.")
        print("Finish or abort it before refreshing patches.")
        return False
    if paths.get("cherry_pick_head", Path()).is_file():
        print("ERROR: A cherry-pick is currently in progress.")
        print("Finish or abort it before refreshing patches.")
        return False
    return True


def resolve_default_start_ref(llvm_dir: Path) -> str:
    llvm_ref = env_or_default("LLVM_REF", "main")
    cp = git(["rev-parse", "--verify", f"origin/{llvm_ref}"], cwd=llvm_dir, check=False, quiet=True)
    if cp.returncode == 0:
        return f"origin/{llvm_ref}"
    return llvm_ref


def backup_patch_root(root_dir: Path, patch_root: Path) -> Path | None:
    if not patch_root.exists():
        return None
    backup_root = root_dir / ".backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_dir = backup_root / f"patches.backup.{timestamp()}"
    shutil.copytree(patch_root, backup_dir)
    return backup_dir


def refresh_patches(root_dir: Path, llvm_dir: Path, start_ref: str) -> int:
    patch_root = root_dir / "patches"

    print("=== refresh clang-mg patch stack ===")
    print(f"Root dir:   {root_dir}")
    print(f"LLVM dir:   {llvm_dir}")
    print(f"Patch root: {patch_root}")
    print(f"Start ref:  {start_ref}")
    print()

    if not (llvm_dir / ".git").is_dir():
        return fail(f"ERROR: LLVM repo is not cloned:\n{llvm_dir}")
    if not ensure_no_git_operation_in_progress(llvm_dir):
        return 1

    cp = git(["rev-parse", "--verify", start_ref], cwd=llvm_dir, check=False, quiet=True)
    if cp.returncode != 0:
        print(f"ERROR: Start ref does not exist: {start_ref}")
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

    patch_root.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix=".patch-refresh.", dir=str(root_dir)))
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

        backup_dir = backup_patch_root(root_dir, patch_root)

        for p in sorted(patch_root.glob("*.patch")):
            p.unlink()
        for p in regenerated:
            shutil.copy2(p, patch_root / p.name)

        print("Updated flat patch stack:")
        print(f"  {patch_root}")
        if backup_dir is not None:
            print()
            print("Backup of old patches:")
            print(f"  {backup_dir}")
        print()
        print("Regenerated patches:")
        for p in sorted(patch_root.glob("*.patch")):
            print(f"  {p.name}")
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main(argv: list[str]) -> int:
    script_name = Path(__file__).name
    root_dir = root_from_script(__file__)
    work_dir = Path(env_or_default("WORK_DIR", root_dir / "work"))
    llvm_dir = Path(env_or_default("LLVM_DIR", work_dir / "llvm-project"))

    if argv and argv[0] in {"-h", "--help"}:
        usage(script_name, llvm_dir, work_dir)
        return 0

    start_ref = argv[0] if argv and argv[0].strip() else resolve_default_start_ref(llvm_dir)
    return refresh_patches(root_dir, llvm_dir, start_ref)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
