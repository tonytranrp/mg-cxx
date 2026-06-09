#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from clang_mg_common import (env_or_default, git, git_in_progress_paths, git_output, is_windows,
                             root_from_script, timestamp)


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
    print(f"  REFRESH_PATCHES={refresh_patches}")
    print("  CLANG_MG_AUTO_UNION=1        Try safe automatic union resolution before the conflict menu")
    print("  CLANG_MG_AUTO_UNION_VERBOSE=1 Print why conflict blocks were or were not auto-resolved")
    print()
    print("Conflict workflow:")
    print("  If git am hits a conflict, this script will pause.")
    print("  Resolve the conflict, run git add on the fixed files, then choose continue.")
    print("  After all patches apply, the feature patch directory is refreshed automatically.")


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


# -----------------------------------------------------------------------------
# Safe automatic conflict resolution
# -----------------------------------------------------------------------------
# This is intentionally conservative. It is meant for the common clang-mg case
# where two feature patch stacks add independent declarations/macros/nodes at the
# same insertion point. It should not be treated as a general semantic merge.

AUTO_UNION_TRUTHY = {"1", "true", "TRUE", "yes", "YES", "on", "ON", "enabled", "ENABLED"}
AUTO_UNION_FALSY = {"0", "false", "FALSE", "no", "NO", "off", "OFF", "disabled", "DISABLED"}

CONFLICT_START_RE = re.compile(r"^<<<<<<<(?:\s|$)")
CONFLICT_BASE_RE = re.compile(r"^\|\|\|\|\|\|\|(?:\s|$)")
CONFLICT_MID_RE = re.compile(r"^=======(?:\s|$)")
CONFLICT_END_RE = re.compile(r"^>>>>>>>(?:\s|$)")

# Tokens that usually identify clang-mg feature additions rather than unrelated
# upstream edits. Keep this broad enough for feature names like traits, but not
# so broad that every C++ conflict becomes eligible.
CLANG_MG_TOKEN_RE = re.compile(
    r"CXXMG|cxxmg|clang-mg|__cxxmg|Trait|trait|ConditionalMember|conditional-member"
)

# Files where list/registry-style conflicts are especially common.
AUTO_UNION_FILE_ALLOWLIST = (
    "clang/include/clang/Basic/DeclNodes.td",
    "clang/include/clang/Basic/Features.def",
    "clang/include/clang/Parse/Parser.h",
    "clang/include/clang/Sema/Sema.h",
    "clang/lib/Frontend/InitPreprocessor.cpp",
)

AUTO_UNION_SUFFIX_ALLOWLIST = {
    ".def", ".td", ".h", ".hpp", ".inc", ".cpp", ".cxx", ".cc"
}


class ConflictBlock:
    def __init__(self, ours: list[str], base: list[str], theirs: list[str]) -> None:
        self.ours = ours
        self.base = base
        self.theirs = theirs


class ResolveResult:
    def __init__(self, ok: bool, changed: bool, text: str, reasons: list[str]) -> None:
        self.ok = ok
        self.changed = changed
        self.text = text
        self.reasons = reasons


def env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name, "")
    if not value.strip():
        return default
    if value in AUTO_UNION_TRUTHY:
        return True
    if value in AUTO_UNION_FALSY:
        return False
    print(f"WARNING: Invalid {name}={value!r}; using {'enabled' if default else 'disabled'}.")
    return default


def auto_union_enabled() -> bool:
    return env_flag("CLANG_MG_AUTO_UNION", True)


def auto_union_verbose() -> bool:
    return env_flag("CLANG_MG_AUTO_UNION_VERBOSE", False)


def line_has_conflict_marker(line: str) -> bool:
    return (
        CONFLICT_START_RE.match(line) is not None or
        CONFLICT_BASE_RE.match(line) is not None or
        CONFLICT_MID_RE.match(line) is not None or
        CONFLICT_END_RE.match(line) is not None
    )


def has_conflict_markers(text: str) -> bool:
    return any(line_has_conflict_marker(line) for line in text.splitlines())


def unresolved_git_paths(llvm_dir: Path) -> list[Path]:
    text = git_output(["diff", "--name-only", "--diff-filter=U"], cwd=llvm_dir, check=False)
    return [Path(line.strip()) for line in text.splitlines() if line.strip()]


def safe_relpath(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def is_auto_union_candidate_file(rel_path: Path) -> bool:
    rel = rel_path.as_posix()
    if rel in AUTO_UNION_FILE_ALLOWLIST:
        return True
    return rel_path.suffix in AUTO_UNION_SUFFIX_ALLOWLIST


def strip_line(line: str) -> str:
    return line.strip()


def nontrivial_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        s = strip_line(line)
        if not s:
            continue
        if s in {"{", "}", ";", ",", "};", ");", "};"}:
            continue
        if s.startswith("//"):
            continue
        out.append(s)
    return out


def side_mentions_clang_mg(lines: list[str]) -> bool:
    return any(CLANG_MG_TOKEN_RE.search(line) for line in lines)


def normalized_noncomment_text(lines: list[str]) -> str:
    parts: list[str] = []
    for line in lines:
        s = strip_line(line)
        if not s or s.startswith("//"):
            continue
        parts.append(re.sub(r"\s+", " ", s))
    return "\n".join(parts)


def normalized_noncomment_line_set(lines: list[str]) -> set[str]:
    items: set[str] = set()
    for line in lines:
        s = strip_line(line)
        if not s or s.startswith("//"):
            continue
        items.add(re.sub(r"\s+", " ", s))
    return items


def strip_trailing_whitespace_preserving_newlines(text: str) -> str:
    # Keep line endings as-is, but remove trailing spaces/tabs introduced by
    # conflict-marker surgery. This avoids git diff --check noise without
    # reformatting the whole file.
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.endswith("\r\n"):
            body, ending = line[:-2], "\r\n"
        elif line.endswith("\n"):
            body, ending = line[:-1], "\n"
        else:
            body, ending = line, ""
        out.append(body.rstrip(" \t") + ending)
    return "".join(out)


def brace_balance(lines: list[str]) -> int:
    # Good enough for generated conflict chunks. We deliberately do not try to
    # parse C++; we only use this as a weak signal that a chunk is complete.
    balance = 0
    in_block_comment = False
    for raw in lines:
        line = raw
        if in_block_comment:
            end = line.find("*/")
            if end < 0:
                continue
            line = line[end + 2:]
            in_block_comment = False
        while "/*" in line:
            start = line.find("/*")
            end = line.find("*/", start + 2)
            if end < 0:
                line = line[:start]
                in_block_comment = True
                break
            line = line[:start] + line[end + 2:]
        line = re.sub(r"//.*$", "", line)
        # Remove simple string/char literals so braces inside diagnostics/macros
        # do not dominate the heuristic.
        line = re.sub(r'"(?:\\.|[^"\\])*"', '""', line)
        line = re.sub(r"'(?:\\.|[^'\\])*'", "''", line)
        balance += line.count("{") - line.count("}")
    return balance


def extract_additive_keys(lines: list[str]) -> set[str]:
    keys: set[str] = set()
    for line in lines:
        s = strip_line(line)
        m = re.match(r"def\s+([A-Za-z_][A-Za-z0-9_]*)\b", s)
        if m:
            keys.add(f"td:{m.group(1)}")
        m = re.match(r"FEATURE\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)", s)
        if m:
            keys.add(f"feature:{m.group(1)}")
        m = re.search(r"defineMacro\s*\(\s*\"([^\"]+)\"", s)
        if m:
            keys.add(f"macro:{m.group(1)}")
        m = re.search(r"\bSema::([A-Za-z_][A-Za-z0-9_]*)\s*\(", s)
        if m:
            keys.add(f"func:Sema::{m.group(1)}")
        # clang-mg parser/sema prototypes and helpers. This catches multiline
        # declarations where the return type is on a different line.
        for m in re.finditer(
            r"\b((?:Parse|ActOn|Begin|End|Add|Build|Check|Diagnose|is|try|Maybe)"
            r"[A-Za-z0-9_]*(?:CXXMG|Trait|ConditionalMember)[A-Za-z0-9_]*)\s*\(",
            s,
        ):
            keys.add(f"symbol:{m.group(1)}")
        m = re.search(r"\b([A-Za-z_][A-Za-z0-9_]*(?:Decl|Expr|Type|Requirement))\s*::\s*Create\s*\(", s)
        if m and CLANG_MG_TOKEN_RE.search(m.group(1)):
            keys.add(f"factory:{m.group(1)}")
    return keys


def looks_like_simple_additive_line(line: str) -> bool:
    s = strip_line(line)
    if not s:
        return True
    if s.startswith("//") or s.startswith("/*") or s.startswith("*") or s.endswith("*/"):
        return True
    simple_patterns = [
        r"def\s+[A-Za-z_][A-Za-z0-9_]*\b.*;?$",
        r"FEATURE\s*\(.*\)$",
        r"Builder\.defineMacro\s*\(.*\)\s*;?$",
        r"#\s*include\b.*$",
        r"(?:class|struct|enum)\s+[A-Za-z_][A-Za-z0-9_]*\b.*;?$",
        r"(?:public|private|protected)\s*:\s*$",
        r"[A-Za-z_][A-Za-z0-9_:<>~,\s\*&]+\s+[A-Za-z_][A-Za-z0-9_:<>~]*\s*\(.*$",
        r"[A-Za-z_][A-Za-z0-9_:<>~,\s\*&]+\s*\*$",
        r"[A-Za-z_][A-Za-z0-9_:<>~,\s\*&]+\s*&$",
        r"[A-Za-z_][A-Za-z0-9_:<>~,\s\*&]+,$",
        r"[A-Za-z_][A-Za-z0-9_:<>~,\s\*&]+\);?$",
        r"[A-Za-z_][A-Za-z0-9_:<>~,\s\*&]+;$",
        r"[(){};,]+$",
    ]
    return any(re.match(p, s) for p in simple_patterns)


def looks_like_simple_additive_block(block: ConflictBlock) -> bool:
    lines = block.ours + block.theirs
    return all(looks_like_simple_additive_line(line) for line in lines)


def lines_look_like_independent_addition(lines: list[str]) -> bool:
    """Return true for a single side that looks safe to keep by itself.

    This is used for add/add-nearby and add/empty conflicts. The goal is not to
    prove semantic correctness; it is to recognize complete clang-mg additions
    that Git could not place because another feature added neighboring code.
    """
    if not nontrivial_lines(lines):
        return False
    if not side_mentions_clang_mg(lines):
        return False
    if all(looks_like_simple_additive_line(line) for line in lines):
        return True
    keys = extract_additive_keys(lines)
    return bool(keys) and brace_balance(lines) == 0


def block_can_union(rel_path: Path, block: ConflictBlock) -> tuple[bool, str]:
    ours_text = normalized_noncomment_text(block.ours)
    theirs_text = normalized_noncomment_text(block.theirs)
    if ours_text == theirs_text:
        return True, "both sides are equivalent"

    if any(line_has_conflict_marker(line) for line in block.ours + block.theirs + block.base):
        return False, "nested conflict markers were found"

    if not is_auto_union_candidate_file(rel_path):
        return False, "file type is not in the auto-union allowlist"

    ours_lines = nontrivial_lines(block.ours)
    theirs_lines = nontrivial_lines(block.theirs)

    # Another common patch-stack case: a later patch conflicts because one side
    # is already a strict superset of the other. Example: HEAD has an existing
    # clang-mg ParseDeclCXXMG.cpp implementation with #include RAIIObjects, while
    # the incoming patch side only contains the same #include. Keeping the
    # superset is the intended union, while appending both would move an #include
    # into the middle of a .cpp file.
    if ours_lines and theirs_lines and (side_mentions_clang_mg(block.ours) or side_mentions_clang_mg(block.theirs)):
        ours_set = normalized_noncomment_line_set(block.ours)
        theirs_set = normalized_noncomment_line_set(block.theirs)
        if theirs_set and theirs_set.issubset(ours_set):
            return True, "theirs is already contained in ours; kept ours superset"
        if ours_set and ours_set.issubset(theirs_set):
            return True, "ours is already contained in theirs; kept theirs superset"
    ours_empty = not ours_lines
    theirs_empty = not theirs_lines

    # Important clang-mg patch-stack case:
    #
    #   already-applied feature: adds a complete clang-mg block here
    #   current feature patch:   expected this area to be empty / different
    #
    # Git may represent that as an empty-vs-nonempty conflict. For ordinary
    # code this might be a modify/delete conflict, so only keep the non-empty
    # side when it independently looks like a complete clang-mg addition.
    if ours_empty or theirs_empty:
        nonempty = block.theirs if ours_empty else block.ours
        side_name = "theirs" if ours_empty else "ours"
        if lines_look_like_independent_addition(nonempty):
            return True, f"kept {side_name} clang-mg addition against empty side"
        return False, "one side is empty and the other side is not a safe standalone clang-mg addition"

    if not (side_mentions_clang_mg(block.ours) or side_mentions_clang_mg(block.theirs)):
        return False, "neither side looks like a clang-mg feature addition"

    ours_keys = extract_additive_keys(block.ours)
    theirs_keys = extract_additive_keys(block.theirs)
    overlap = ours_keys & theirs_keys
    if overlap:
        return False, "both sides mention the same additive key(s): " + ", ".join(sorted(overlap))

    if looks_like_simple_additive_block(block):
        return True, "simple additive declaration/list conflict"

    # Larger C++ chunks can still be safe when both sides add independent
    # clang-mg functions at the same top-level insertion point. Require keys on
    # both sides and balanced braces so we do not union arbitrary edited logic.
    if ours_keys and theirs_keys and brace_balance(block.ours) == 0 and brace_balance(block.theirs) == 0:
        return True, "independent balanced clang-mg function/declaration chunks"

    return False, "conflict does not look like a pure additive union"


def union_block_lines(block: ConflictBlock) -> list[str]:
    ours_text = normalized_noncomment_text(block.ours)
    theirs_text = normalized_noncomment_text(block.theirs)
    if ours_text == theirs_text:
        return block.ours

    ours_set = normalized_noncomment_line_set(block.ours)
    theirs_set = normalized_noncomment_line_set(block.theirs)
    if block.ours and block.theirs:
        if theirs_set and theirs_set.issubset(ours_set):
            return block.ours
        if ours_set and ours_set.issubset(theirs_set):
            return block.theirs

    out = list(block.ours)
    if out and block.theirs:
        # If both chunks have real code and neither side left spacing between
        # them, add one blank line for C++ function/declaration chunks. Do not do
        # this for TableGen/Features.def one-line lists.
        last = out[-1]
        first = block.theirs[0]
        if strip_line(last) and strip_line(first) and not looks_like_simple_additive_block(block):
            out.append("\n")
    out.extend(block.theirs)
    return out


def resolve_conflict_text(rel_path: Path, text: str) -> ResolveResult:
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    changed = False
    reasons: list[str] = []

    while i < len(lines):
        line = lines[i]
        if not CONFLICT_START_RE.match(line):
            out.append(line)
            i += 1
            continue

        start_line = i + 1
        i += 1
        ours: list[str] = []
        base: list[str] = []
        theirs: list[str] = []
        mode = "ours"
        found_mid = False
        found_end = False

        while i < len(lines):
            cur = lines[i]
            if CONFLICT_BASE_RE.match(cur) and mode == "ours":
                mode = "base"
                i += 1
                continue
            if CONFLICT_MID_RE.match(cur) and mode in {"ours", "base"}:
                mode = "theirs"
                found_mid = True
                i += 1
                continue
            if CONFLICT_END_RE.match(cur) and mode == "theirs":
                found_end = True
                i += 1
                break
            if mode == "ours":
                ours.append(cur)
            elif mode == "base":
                base.append(cur)
            else:
                theirs.append(cur)
            i += 1

        if not found_mid or not found_end:
            reasons.append(f"{rel_path}: malformed conflict block near line {start_line}")
            return ResolveResult(False, False, text, reasons)

        block = ConflictBlock(ours, base, theirs)
        ok, reason = block_can_union(rel_path, block)
        if not ok:
            reasons.append(f"{rel_path}: skipped block near line {start_line}: {reason}")
            return ResolveResult(False, False, text, reasons)

        if auto_union_verbose():
            print(f"  auto-union: {rel_path}:{start_line}: {reason}")
        out.extend(union_block_lines(block))
        changed = True

    new_text = strip_trailing_whitespace_preserving_newlines("".join(out))
    if has_conflict_markers(new_text):
        reasons.append(f"{rel_path}: conflict markers remain after tentative resolution")
        return ResolveResult(False, False, text, reasons)
    return ResolveResult(True, changed, new_text, reasons)


def try_auto_union_resolution(llvm_dir: Path) -> bool:
    """Try to resolve all current unmerged paths with conservative union merges.

    Returns True only when all currently-unmerged paths were resolved and staged.
    If some files are not safe to resolve automatically, any files that were safe
    are still staged to reduce the remaining manual work, but the function
    returns False so the normal conflict menu can take over.
    """
    if not auto_union_enabled():
        return False

    unresolved = unresolved_git_paths(llvm_dir)
    if not unresolved:
        return True

    print()
    print("Trying safe automatic clang-mg union merge...")

    resolved_paths: list[Path] = []
    skipped: list[str] = []

    for rel_path in unresolved:
        abs_path = llvm_dir / rel_path
        if not abs_path.is_file():
            skipped.append(f"{rel_path}: path is not a regular file")
            continue
        original = abs_path.read_text(encoding="utf-8", errors="surrogateescape")
        if not has_conflict_markers(original):
            skipped.append(f"{rel_path}: file is unmerged but has no conflict markers")
            continue
        result = resolve_conflict_text(rel_path, original)
        if not result.ok or not result.changed:
            skipped.extend(result.reasons or [f"{rel_path}: no safe automatic resolution found"])
            continue
        abs_path.write_text(result.text, encoding="utf-8", errors="surrogateescape")
        resolved_paths.append(rel_path)

    if resolved_paths:
        cp = git(["diff", "--check", "--", *[str(p) for p in resolved_paths]], cwd=llvm_dir, check=False)
        if cp.returncode != 0:
            print()
            print("WARNING: git diff --check reported issues after automatic union merge.")
            print("Continuing anyway because no conflict markers remain in the auto-resolved files.")
        git(["add", "--", *[str(p) for p in resolved_paths]], cwd=llvm_dir)
        print("Auto-resolved and staged:")
        for rel_path in resolved_paths:
            print(f"  {rel_path}")

    if skipped:
        print("Auto-union skipped:")
        for reason in skipped:
            print(f"  {reason}")

    remaining = unresolved_git_paths(llvm_dir)
    if remaining:
        print()
        print("Some conflicts still need manual resolution.")
        return False

    print()
    print("All current conflicts were auto-resolved.")
    return True


def automatic_then_interactive_am_resolution(llvm_dir: Path) -> bool:
    """Resolve a git-am session with auto-union first, then the existing menu."""
    max_auto_rounds = 25
    rounds = 0

    while True:
        paths = git_in_progress_paths(llvm_dir)
        if not paths.get("rebase_apply", Path()).is_dir():
            return True

        if rounds >= max_auto_rounds:
            print()
            print("Automatic union merge stopped after too many rounds.")
            break
        rounds += 1

        if not try_auto_union_resolution(llvm_dir):
            break

        if continue_am():
            # git am may have completed the whole patch queue. If it did not,
            # loop again and try auto-union on the next conflict.
            continue

        # git am --continue can fail because a later patch hit a new conflict.
        # Try the automatic resolver again before falling back to the menu.

    return interactive_am_resolution(llvm_dir)


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


def refresh_feature_patches(root_dir: Path, patch_dir: Path, feature_name: str, start_commit: str, refresh_patches: str) -> None:
    if refresh_patches != "1":
        print()
        print(f"Skipping patch refresh because REFRESH_PATCHES={refresh_patches}")
        return

    print()
    print("Refreshing feature patches from applied commits...")
    tmp_dir = Path(tempfile.mkdtemp(prefix=f".patch-refresh-{feature_name}.", dir=str(root_dir)))
    try:
        cp = git(["format-patch", "--zero-commit", "--no-stat", "--output-directory", str(tmp_dir), f"{start_commit}..HEAD"], check=False, quiet=True)
        if cp.returncode != 0:
            print("ERROR: Failed to regenerate patches.")
            raise SystemExit(1)
        regenerated = sorted(tmp_dir.glob("*.patch"))
        if not regenerated:
            print("ERROR: No regenerated patches were produced.")
            raise SystemExit(1)

        backup_dir = Path(str(patch_dir) + f".backup.{timestamp()}")
        backup_dir.mkdir(parents=True, exist_ok=True)
        for p in sorted(patch_dir.glob("*.patch")):
            shutil.copy2(p, backup_dir / p.name)
        for p in sorted(patch_dir.glob("*.patch")):
            p.unlink()
        for p in regenerated:
            shutil.copy2(p, patch_dir / p.name)

        print()
        print("Updated patch collection:")
        print(f"  {patch_dir}")
        print()
        print("Backup of old patches:")
        print(f"  {backup_dir}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main(argv: list[str]) -> int:
    root_dir = root_from_script(__file__)
    work_dir = Path(env_or_default("WORK_DIR", root_dir / "work"))
    llvm_dir = Path(env_or_default("LLVM_DIR", work_dir / "llvm-project"))
    feature_name = argv[0] if argv else ""
    refresh_patches = env_or_default("REFRESH_PATCHES", "1")

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
            print("Trying automatic resolution before opening the conflict menu.")
            if not automatic_then_interactive_am_resolution(Path.cwd()):
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
                refresh_feature_patches(root_dir, patch_dir, feature_name, start, refresh_patches)
            else:
                print()
                print("Skipping automatic patch refresh because this git am session was started before the script recorded its start commit.")
                print("After confirming the result, you can refresh this feature with:")
                print(f"  ./build.py save {feature_name}")
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
                if not automatic_then_interactive_am_resolution(Path.cwd()):
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
        refresh_feature_patches(root_dir, patch_dir, feature_name, start_commit, refresh_patches)
        state_file.unlink(missing_ok=True)
        print()
        print("Recent commits:")
        git(["--no-pager", "log", "--oneline", "-5"])
        return 0
    finally:
        os.chdir(old_cwd)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
