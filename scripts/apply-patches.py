#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from clang_mg_common import git, git_in_progress_paths, git_output, root_from_script


def fail(message: str) -> int:
    print(message)
    return 1


def is_windows() -> bool:
    return os.name == "nt"


def patch_files(patch_root: Path) -> list[Path]:
    return sorted(
        [
            p for p in patch_root.glob("*.patch")
            if not (
                p.name.endswith(".backup.patch") or
                p.name.endswith(".bak.patch") or
                p.name.endswith(".old.patch") or
                p.name.endswith("~")
            )
        ],
        key=lambda p: p.name,
    )


def show_conflict_help() -> None:
    print(r"""
A patch conflict happened.

Resolve it like this:

  1. Open the conflicted files and fix the conflict markers.
  2. Check the result:
       git status
       git diff
  3. Stage the resolved files:
       git add <files>
  4. Come back here and choose:
       c) continue

Useful commands:

  git am --show-current-patch=diff
  git status
  git diff
  git diff --name-only --diff-filter=U

Note:
  If git says it could not build a fake ancestor, there may be no
  conflict markers yet. Use p to print the current patch and sh to
  open a shell for manual recovery.
""")


def menu_git(args: list[str]) -> int:
    command_text = "git " + " ".join(args)
    cp = subprocess.run(["git", *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if cp.stdout:
        print(cp.stdout, end="")
    else:
        print(f"(no output from {command_text})")
    if cp.returncode != 0:
        print()
        print(f"ERROR: {command_text} failed with exit code {cp.returncode}.")
    return cp.returncode


def open_resolution_shell(llvm_dir: Path) -> None:
    print()
    print("Opening a shell in:")
    print(f"  {llvm_dir}")
    print()
    print("When done resolving conflicts, exit the shell to return here.")
    print()
    if is_windows():
        shell = shutil.which("pwsh") or shutil.which("powershell") or os.environ.get("COMSPEC")
    else:
        shell = os.environ.get("SHELL") or "/bin/bash"
    if not shell:
        print("ERROR: Could not find a shell to open.")
        return
    subprocess.run([shell], cwd=llvm_dir)


def continue_am() -> bool:
    print()
    print("Continuing git am...")
    if menu_git(["am", "--continue"]) == 0:
        print("git am completed successfully.")
        return True
    print()
    print("git am still needs attention.")
    return False


def skip_am() -> bool:
    print()
    print("Skipping current patch...")
    if menu_git(["am", "--skip"]) == 0:
        print("Patch skipped and git am completed successfully.")
        return True
    print()
    print("git am still needs attention.")
    return False


def interactive_am_resolution(llvm_dir: Path) -> bool:
    show_conflict_help()
    if not sys.stdin.isatty():
        print("ERROR: No interactive terminal is available.")
        print()
        print("Resolve manually inside LLVM with:")
        print("  git status")
        print("  git diff --name-only --diff-filter=U")
        print("  git add <files>")
        print("  git am --continue")
        print()
        print("Or abort with:")
        print("  git am --abort")
        return False

    while True:
        paths = git_in_progress_paths(llvm_dir)
        if not paths.get("rebase_apply", Path()).is_dir():
            return True
        print(r"""
Conflict menu:
  s) show status
  u) show unresolved files
  p) show current patch
  d) show diff
  a) git add -A
  c) continue git am
  k) skip current patch
  x) abort git am
  sh) open shell
""")
        try:
            choice = input("Choose an action: ").strip()
        except EOFError:
            print()
            print("ERROR: Could not read from terminal.")
            print("Leaving git am in progress so you can resolve it manually.")
            return False
        if choice == "s":
            menu_git(["status"])
        elif choice == "u":
            menu_git(["diff", "--name-only", "--diff-filter=U"])
        elif choice == "p":
            menu_git(["--no-pager", "am", "--show-current-patch=diff"])
        elif choice == "d":
            menu_git(["diff"])
        elif choice == "a":
            if menu_git(["add", "-A"]) == 0:
                print("Staged all changes.")
            else:
                print("git add failed.")
        elif choice == "c":
            if continue_am():
                return True
        elif choice == "k":
            if skip_am():
                return True
        elif choice == "x":
            print()
            print("Aborting git am...")
            menu_git(["am", "--abort"])
            return False
        elif choice == "sh":
            open_resolution_shell(llvm_dir)
        elif choice == "":
            print("No option entered.")
        else:
            print(f"Unknown option: {choice}")


def ensure_no_git_operation_in_progress(llvm_dir: Path) -> bool:
    paths = git_in_progress_paths(llvm_dir)
    if paths.get("rebase_merge", Path()).is_dir():
        print("ERROR: A rebase is currently in progress.")
        print("Finish or abort it before applying patches.")
        return False
    if paths.get("rebase_apply", Path()).is_dir():
        print("ERROR: A git-am or rebase-apply operation is currently in progress.")
        print("Finish or abort it before applying patches.")
        return False
    if paths.get("merge_head", Path()).is_file():
        print("ERROR: A merge is currently in progress.")
        print("Finish or abort it before applying patches.")
        return False
    if paths.get("cherry_pick_head", Path()).is_file():
        print("ERROR: A cherry-pick is currently in progress.")
        print("Finish or abort it before applying patches.")
        return False
    return True


def apply_all_patches(root_dir: Path, llvm_dir: Path) -> int:
    patch_root = root_dir / "patches"

    print("=== apply clang-mg patch stack ===")
    print(f"Root dir:   {root_dir}")
    print(f"LLVM dir:   {llvm_dir}")
    print(f"Patch root: {patch_root}")
    print()

    if not (llvm_dir / ".git").is_dir():
        return fail(f"ERROR: LLVM repo is not cloned:\n{llvm_dir}")
    if not patch_root.is_dir():
        return fail(f"ERROR: Patch directory does not exist:\n{patch_root}")
    if not ensure_no_git_operation_in_progress(llvm_dir):
        return 1

    old_cwd = Path.cwd()
    os.chdir(llvm_dir)
    try:
        status = git_output(["status", "--porcelain"], check=False)
        if status:
            print("ERROR: LLVM has uncommitted changes.")
            print()
            print("Save, commit, stash, or reset your changes before applying patches.")
            print()
            print("Useful commands:")
            print("  git status")
            print("  git diff")
            print("  python3 build.py save")
            print("  git reset --hard")
            print()
            print("Apply cancelled.")
            return 1

        patches = patch_files(patch_root)
        if not patches:
            print("No top-level .patch files found in:")
            print(f"  {patch_root}")
            return 0

        print("Patch apply order:")
        for p in patches:
            print(f"  {p.name}")
        print()

        cp = git(["am", "--3way", *[str(p) for p in patches]], check=False)
        if cp.returncode == 0:
            print("All clang-mg patches applied.")
            return 0

        if interactive_am_resolution(llvm_dir):
            print()
            print("All clang-mg patches applied.")
            return 0
        return 1
    finally:
        os.chdir(old_cwd)


def main(argv: list[str]) -> int:
    script_root = root_from_script(__file__)
    root_dir = Path(argv[0]).resolve() if len(argv) >= 1 and argv[0].strip() else script_root
    llvm_dir = Path(argv[1]).resolve() if len(argv) >= 2 and argv[1].strip() else root_dir / "work" / "llvm-project"
    return apply_all_patches(root_dir, llvm_dir)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
