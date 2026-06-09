#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from clang_mg_common import (env_or_default, git, git_in_progress_paths, git_output, is_windows,
                             root_from_script, run)


def usage(llvm_dir: Path, work_dir: Path, refresh_patches: str) -> None:
    print("Usage:")
    print("  scripts/apply-feature.py <feature-name>")
    print()
    print("Examples:")
    print("  scripts/apply-feature.py core")
    print("  scripts/apply-feature.py curlinclude")
    print("  scripts/apply-feature.py if-constexpr-members")
    print()
    print("Environment variables:")
    print(f"  LLVM_DIR={llvm_dir}")
    print(f"  WORK_DIR={work_dir}")
    print(f"  REFRESH_PATCHES={refresh_patches}  # set to 1 to refresh this feature after a successful apply")
    print()
    print("Conflict workflow:")
    print("  If git am hits a conflict, this script will pause.")
    print("  Resolve the conflict, run git add on the fixed files, then choose continue.")
    print("  By default, applying patches does not rewrite the patch directory.")
    print("  Set REFRESH_PATCHES=1 only when applying this feature on top of its intended dependencies.")


def show_features(root_dir: Path) -> None:
    print("Available features:")
    patches_root = root_dir / "patches"
    if not patches_root.is_dir():
        print("  No patches directory found.")
        return
    for p in sorted([p for p in patches_root.iterdir() if p.is_dir()], key=lambda p: p.name):
        print(f"  {p.name}")


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


def read_apply_feature_state(state_file: Path) -> dict[str, str]:
    state: dict[str, str] = {}
    if not state_file.is_file():
        return state
    for line in state_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            state[k] = v
    return state


def maybe_refresh_feature_patches(root_dir: Path, llvm_dir: Path, feature_name: str, start_commit: str, refresh_patches: str) -> None:
    if refresh_patches != "1":
        print()
        print(f"Skipping patch refresh because REFRESH_PATCHES={refresh_patches}")
        print("Run this explicitly when you want to rewrite a feature patch stack:")
        print(f"  ./build.py refresh-feature {feature_name}")
        return

    print()
    print("Refreshing feature patches from applied commits...")
    refresh_script = root_dir / "scripts" / "refresh-feature.py"
    if not refresh_script.is_file():
        print("ERROR: refresh-feature Python script not found:")
        print(f"  {refresh_script}")
        raise SystemExit(1)
    env = os.environ.copy()
    env["LLVM_DIR"] = str(llvm_dir)
    run([sys.executable, str(refresh_script), feature_name, start_commit], cwd=llvm_dir, env=env)

def main(argv: list[str]) -> int:
    root_dir = root_from_script(__file__)
    work_dir = Path(env_or_default("WORK_DIR", root_dir / "work"))
    llvm_dir = Path(env_or_default("LLVM_DIR", work_dir / "llvm-project"))
    feature_name = argv[0] if argv else ""
    refresh_patches = env_or_default("REFRESH_PATCHES", "0")

    if feature_name in {"-h", "--help", ""}:
        usage(llvm_dir, work_dir, refresh_patches)
        print()
        show_features(root_dir)
        return 0
    if feature_name in {"list", "--list"}:
        show_features(root_dir)
        return 0

    patch_dir = root_dir / "patches" / feature_name
    print("=== apply feature ===")
    print(f"Feature:   {feature_name}")
    print(f"Patch dir: {patch_dir}")
    print(f"LLVM dir:  {llvm_dir}")
    print()

    if not (llvm_dir / ".git").is_dir():
        print("ERROR: LLVM repo is not cloned:")
        print(str(llvm_dir))
        print()
        print("Run:")
        print("  ./build.py clone")
        print()
        print("or:")
        print("  ./build.py bootstrap")
        return 1
    if not patch_dir.is_dir():
        print("ERROR: Feature patch directory does not exist:")
        print(str(patch_dir))
        print()
        show_features(root_dir)
        return 1
    patches = sorted(patch_dir.glob("*.patch"))
    if not patches:
        print("ERROR: No .patch files found in:")
        print(str(patch_dir))
        return 1

    old_cwd = Path.cwd()
    os.chdir(llvm_dir)
    try:
        paths = git_in_progress_paths(Path.cwd())
        state_file = paths["apply_feature_state"]
        if paths["rebase_merge"].is_dir():
            print("ERROR: A rebase is currently in progress.")
            print("Finish or abort it before applying feature patches.")
            return 1

        if paths["rebase_apply"].is_dir():
            if not (paths["rebase_apply"] / "patch").is_file():
                print("ERROR: A rebase-apply state exists, but it does not look like a git am patch.")
                print()
                print("Run one of these inside LLVM first:")
                print("  git rebase --continue")
                print("  git rebase --abort")
                print("  git am --continue")
                print("  git am --abort")
                return 1
            print("A git am patch application is already in progress.")
            print("Entering the conflict menu for the existing session.")
            if not interactive_am_resolution(Path.cwd()):
                if not paths["rebase_apply"].is_dir():
                    state_file.unlink(missing_ok=True)
                print()
                print("Apply cancelled.")
                return 1
            print()
            print("Existing git am session completed.")
            resume_state = read_apply_feature_state(state_file)
            start = resume_state.get("START", "").strip()
            if start:
                maybe_refresh_feature_patches(root_dir, Path.cwd(), feature_name, start, refresh_patches)
            else:
                print()
                print("Skipping automatic patch refresh because this git am session was started before the script recorded its start commit.")
                print("After confirming the result, you can refresh this feature with:")
                print(f"  ./build.py refresh-feature {feature_name}")
            state_file.unlink(missing_ok=True)
            print()
            print("Recent commits:")
            git(["--no-pager", "log", "--oneline", "-5"])
            return 0

        if paths["merge_head"].is_file():
            print("ERROR: A merge is currently in progress.")
            print("Finish or abort it before applying feature patches.")
            return 1
        if paths["cherry_pick_head"].is_file():
            print("ERROR: A cherry-pick is currently in progress.")
            print("Finish or abort it before applying feature patches.")
            return 1
        status = git_output(["status", "--porcelain"])
        if status:
            print("ERROR: LLVM has uncommitted changes.")
            print()
            print("Save or commit your current work before applying feature patches.")
            print()
            print("Useful commands:")
            print("  git status")
            print("  git diff")
            print("  git add .")
            print('  git commit -m "clang-mg: describe current work"')
            print()
            print("Apply cancelled.")
            return 1

        start_commit = git_output(["rev-parse", "HEAD"])
        print("Applying feature patches...")
        state_file.write_text(f"FEATURE={feature_name}\nSTART={start_commit}\n", encoding="utf-8")
        cp = git(["am", "--3way", *[str(p) for p in patches]], check=False)
        if cp.returncode != 0:
            paths = git_in_progress_paths(Path.cwd())
            if paths["rebase_apply"].is_dir():
                if not interactive_am_resolution(Path.cwd()):
                    if not paths["rebase_apply"].is_dir():
                        state_file.unlink(missing_ok=True)
                    print()
                    print("Apply cancelled.")
                    return 1
            else:
                state_file.unlink(missing_ok=True)
                print()
                print("ERROR: git am failed, but no patch application state was found.")
                return 1

        print()
        print(f"Applied feature successfully: {feature_name}")
        maybe_refresh_feature_patches(root_dir, Path.cwd(), feature_name, start_commit, refresh_patches)
        state_file.unlink(missing_ok=True)
        print()
        print("Recent commits:")
        git(["--no-pager", "log", "--oneline", "-5"])
        return 0
    finally:
        os.chdir(old_cwd)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
