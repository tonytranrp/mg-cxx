#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from clang_mg_common import env_or_default, git, git_in_progress_paths, git_output, root_from_script, run


DEFAULT_OLLAMA_MODEL = "gemma3:4b"
FALLBACK_OLLAMA_MODEL = "gemma4b"


def fail(message: str) -> int:
    print(message)
    return 1


def prompt_yes_no(prompt: str, default: bool = False) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    while True:
        try:
            answer = input(prompt + suffix).strip().lower()
        except EOFError:
            print()
            return default
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please answer yes or no.")


def prompt_text(prompt: str, default: str = "") -> str:
    if default:
        print(f"{prompt}")
        print(f"  Default: {default}")
        try:
            value = input("  Enter new value, or press Enter to keep default: ").strip()
        except EOFError:
            print()
            return default
        return value or default
    while True:
        try:
            value = input(f"{prompt}: ").strip()
        except EOFError:
            print()
            value = ""
        if value:
            return value
        print("A value is required.")


def prompt_multiline(prompt: str, default: str = "") -> str:
    print(prompt)
    if default.strip():
        print()
        print("Current description:")
        print("---")
        print(default.rstrip())
        print("---")
        print()
        print("Press Enter on the first line to keep it.")
    print("Enter description lines. Finish with a single '.' line.")
    lines: list[str] = []
    first = True
    while True:
        try:
            line = input()
        except EOFError:
            print()
            break
        if first and line == "" and default.strip():
            return default.strip()
        first = False
        if line == ".":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def ensure_no_git_operation_in_progress(llvm_dir: Path) -> bool:
    paths = git_in_progress_paths(llvm_dir)
    if paths.get("rebase_merge", Path()).is_dir():
        print("ERROR: A rebase is currently in progress.")
        print("Finish or abort it before saving patches.")
        return False
    if paths.get("rebase_apply", Path()).is_dir():
        print("ERROR: A git-am or rebase-apply operation is currently in progress.")
        print("Finish or abort it before saving patches.")
        return False
    if paths.get("merge_head", Path()).is_file():
        print("ERROR: A merge is currently in progress.")
        print("Finish or abort it before saving patches.")
        return False
    if paths.get("cherry_pick_head", Path()).is_file():
        print("ERROR: A cherry-pick is currently in progress.")
        print("Finish or abort it before saving patches.")
        return False
    return True


def run_cmd(args: list[str], *, cwd: Path | None = None, input_text: str | None = None,
            timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=str(cwd) if cwd else None, input=input_text, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def ollama_installed() -> bool:
    return shutil.which("ollama") is not None


def ollama_running() -> bool:
    try:
        cp = run_cmd(["ollama", "list"], timeout=6)
    except Exception:
        return False
    return cp.returncode == 0


def start_ollama_server() -> bool:
    print("Starting Ollama with `ollama serve`...")
    try:
        kwargs: dict[str, object] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(["ollama", "serve"], **kwargs)
    except Exception as exc:
        print(f"ERROR: Failed to start Ollama: {exc}")
        return False

    for _ in range(20):
        if ollama_running():
            print("Ollama is running.")
            return True
        time.sleep(0.5)
    print("Ollama did not become ready in time.")
    return False


def installed_ollama_models() -> list[str]:
    try:
        cp = run_cmd(["ollama", "list"], timeout=10)
    except Exception:
        return []
    if cp.returncode != 0:
        return []
    models: list[str] = []
    for i, line in enumerate((cp.stdout or "").splitlines()):
        line = line.strip()
        if not line or i == 0 and line.lower().startswith("name"):
            continue
        parts = line.split()
        if parts:
            models.append(parts[0])
    return models


def preferred_model(models: list[str]) -> str:
    env_model = os.environ.get("OLLAMA_MODEL", "").strip()
    if env_model:
        return env_model
    for candidate in (DEFAULT_OLLAMA_MODEL, FALLBACK_OLLAMA_MODEL, "gemma:4b"):
        if candidate in models:
            return candidate
    for model in models:
        lowered = model.lower()
        if "gemma" in lowered and "4b" in lowered:
            return model
    return models[0] if models else DEFAULT_OLLAMA_MODEL


def select_ollama_model(models: list[str]) -> str:
    default = preferred_model(models)
    if not models:
        return default
    print()
    print("Installed Ollama models:")
    for idx, model in enumerate(models, start=1):
        marker = " (default)" if model == default else ""
        print(f"  {idx}) {model}{marker}")
    print()
    while True:
        try:
            answer = input(f"Choose a model, or press Enter for {default}: ").strip()
        except EOFError:
            print()
            return default
        if not answer:
            return default
        if answer.isdigit():
            idx = int(answer)
            if 1 <= idx <= len(models):
                return models[idx - 1]
        if answer in models:
            return answer
        print("Unknown model selection.")


def truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n[diff truncated]\n"


def collect_change_summary(llvm_dir: Path) -> str:
    status = git_output(["status", "--short"], cwd=llvm_dir, check=False)
    name_status = git_output(["diff", "--name-status"], cwd=llvm_dir, check=False)
    stat = git_output(["diff", "--stat"], cwd=llvm_dir, check=False)
    diff = git_output(["diff", "--"], cwd=llvm_dir, check=False)
    return f"""Git status:
{status}

Changed files:
{name_status}

Diff stat:
{stat}

Diff:
{truncate(diff, 14000)}
"""


AI_REASONING_LABELS = {
    "thinking",
    "think",
    "thought",
    "thoughts",
    "reasoning",
    "plan",
    "analysis",
    "steps",
    "step",
    "changes",
    "diff",
    "output",
    "answer",
    "final",
    "final answer",
    "commit message",
    "commit",
    "message",
}


def strip_markdown_fence(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("```") and stripped.endswith("```") and len(stripped) > 6:
        return stripped.strip("`").strip()
    return stripped


def parse_json_ai_message(cleaned: str) -> tuple[str, str] | None:
    # Accept JSON if the model returns it despite the plain-text request.
    json_candidates = [cleaned]
    json_candidates += re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, flags=re.IGNORECASE | re.DOTALL)
    json_candidates += re.findall(r"(\{.*?\})", cleaned, flags=re.DOTALL)

    for candidate in json_candidates:
        try:
            data = json.loads(candidate)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        subject = str(data.get("subject") or data.get("message") or "").strip()
        description = str(data.get("description") or data.get("body") or "").strip()
        if subject or description:
            return subject, description
    return None


def normalize_ai_subject_candidate(line: str) -> str:
    line = strip_markdown_fence(line)
    line = re.sub(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", "", line).strip()
    line = line.strip().strip('"').strip("'").strip("`").strip()
    line = re.sub(r"\s+", " ", line)
    return line


def is_ai_reasoning_label(line: str) -> bool:
    stripped = normalize_ai_subject_candidate(line).strip(":").strip().lower()
    stripped = re.sub(r'["*_`#>]', "", stripped).strip()
    if stripped in AI_REASONING_LABELS:
        return True
    return bool(re.match(
        r"^(?:thinking|reasoning|plan|analysis|steps?|commit message|description|body|changes|final answer)\b\s*:*$",
        stripped,
    ))


def looks_like_ai_reasoning(line: str) -> bool:
    raw = line.strip()
    raw_lowered = raw.lower()
    if re.match(r"^\d+[.)]\s+", raw_lowered):
        return True

    candidate = normalize_ai_subject_candidate(line)
    lowered = candidate.lower()
    if not candidate:
        return True
    if is_ai_reasoning_label(candidate):
        return True
    if lowered in {"thinking...", "thinking …", "thinking", "analysis:"}:
        return True
    if lowered.startswith((
        "here is", "here's", "i would", "i will", "let me", "we need to",
        "analyze ", "analyse ", "determine ", "draft ", "format ",
    )):
        return True
    if re.match(r"^(?:plan|analysis|reasoning|thinking)\s*:", lowered):
        return True
    return False


def is_usable_ai_subject(subject: str) -> bool:
    candidate = normalize_ai_subject_candidate(subject)
    lowered = candidate.lower()
    if looks_like_ai_reasoning(candidate):
        return False
    if len(candidate) < 8 or len(candidate) > 120:
        return False
    if not re.search(r"[A-Za-z]", candidate):
        return False
    if "\n" in candidate:
        return False
    if re.search(r"\b\w+\.(?:cpp|cxx|cc|h|hpp|td|py|txt|md)\b", candidate):
        return False
    if "/" in candidate or "\\" in candidate:
        return False
    return True


def find_subject_after_marker(lines: list[str], start_index: int) -> tuple[str, int] | None:
    for idx in range(start_index + 1, min(len(lines), start_index + 8)):
        candidate = normalize_ai_subject_candidate(lines[idx])
        if not candidate or looks_like_ai_reasoning(candidate):
            continue
        if is_usable_ai_subject(candidate):
            return candidate, idx
    return None


def find_best_untagged_subject(lines: list[str]) -> tuple[str, int] | None:
    scored: list[tuple[int, int, str]] = []
    for idx, line in enumerate(lines):
        candidate = normalize_ai_subject_candidate(line)
        if not is_usable_ai_subject(candidate):
            continue

        score = 0
        if re.match(r"^[A-Za-z0-9][A-Za-z0-9_.+/ -]{0,40}:\s+\S", candidate):
            score += 20
        if candidate.lower().startswith("clang-mg:"):
            score += 20
        if not candidate.endswith("."):
            score += 4
        word_count = len(candidate.split())
        if 3 <= word_count <= 10:
            score += 4
        if idx > 0 and is_ai_reasoning_label(lines[idx - 1]):
            score += 10
        if re.search(r"\b(add|update|fix|remove|rename|split|merge|parse|handle|support|preserve|avoid)\b", candidate, re.IGNORECASE):
            score += 3
        scored.append((score, idx, candidate))

    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    score, idx, candidate = scored[0]
    if score < 4:
        return None
    return candidate, idx


def parse_tagged_ai_message(lines: list[str]) -> tuple[str, str]:
    subject = ""
    desc_lines: list[str] = []
    in_description = False

    for line in lines:
        if re.match(r"^\s*subject\s*:", line, flags=re.IGNORECASE):
            subject = re.sub(r"^\s*subject\s*:\s*", "", line, flags=re.IGNORECASE).strip()
            in_description = False
            continue
        if re.match(r"^\s*(description|body)\s*:", line, flags=re.IGNORECASE):
            after = re.sub(r"^\s*(description|body)\s*:\s*", "", line, flags=re.IGNORECASE).strip()
            if after:
                desc_lines.append(after)
            in_description = True
            continue
        if in_description:
            if re.match(r"^\s*(changes|diff|analysis|plan)\s*:", line, flags=re.IGNORECASE):
                break
            desc_lines.append(line)

    return normalize_ai_subject_candidate(subject), "\n".join(desc_lines).strip()


def parse_ai_message(text: str) -> tuple[str, str]:
    cleaned = text.strip()
    if not cleaned:
        return "", ""

    parsed_json = parse_json_ai_message(cleaned)
    if parsed_json is not None:
        subject, description = parsed_json
        return normalize_ai_subject_candidate(subject), description.strip()

    lines = cleaned.splitlines()
    subject, description = parse_tagged_ai_message(lines)
    if is_usable_ai_subject(subject):
        return subject, description

    # Many local models ignore formatting instructions and preface the useful
    # answer with "Thinking...", "Plan:", or "Analysis:". Never allow those
    # scaffolding lines to become the default commit subject.
    for idx, line in enumerate(lines):
        if is_ai_reasoning_label(line):
            found = find_subject_after_marker(lines, idx)
            if found is not None:
                subject, subject_idx = found
                if not description:
                    description = "\n".join(
                        l for l in lines[subject_idx + 1:]
                        if l.strip() and not looks_like_ai_reasoning(l)
                    ).strip()
                return subject, description

    found = find_best_untagged_subject(lines)
    if found is not None:
        subject, subject_idx = found
        if not description:
            description = "\n".join(
                l for l in lines[subject_idx + 1:]
                if l.strip() and not looks_like_ai_reasoning(l)
            ).strip()
        return subject, description

    return "", ""


def generate_ai_message(llvm_dir: Path, model: str) -> tuple[str, str]:
    change_summary = collect_change_summary(llvm_dir)
    prompt = f"""You are helping maintain a patch stack for a modified LLVM/Clang fork named clang-mg.
Create a concise Git patch commit message for the uncommitted changes below.

Rules:
- Output only the commit message fields.
- Do not output thinking, reasoning, analysis, a plan, markdown, or commentary.
- The first non-empty line must begin with "Subject:".
- The subject must be one line, imperative mood, no trailing period.
- Prefer the prefix "clang-mg:" unless a narrower prefix is obvious.
- The description should be 2-5 short lines explaining what changed and why.
- Do not invent details not supported by the diff.
- Return exactly this format:
Subject: <subject>
Description:
<description>

Changes:
{change_summary}
"""
    try:
        cp = run_cmd(["ollama", "run", model, prompt], timeout=120)
    except subprocess.TimeoutExpired:
        print("Ollama timed out while generating a message.")
        return "", ""
    except Exception as exc:
        print(f"Ollama failed: {exc}")
        return "", ""
    if cp.returncode != 0:
        print("Ollama failed to generate a message.")
        if cp.stderr:
            print(cp.stderr.strip())
        return "", ""

    raw_output = cp.stdout or ""
    subject, description = parse_ai_message(raw_output)
    if is_usable_ai_subject(subject):
        return subject, description

    retry_prompt = f"""Your previous response did not follow the requested commit-message format.
Return only the final commit message fields now.
Do not output thinking, reasoning, analysis, a plan, markdown, or commentary.
The first non-empty line must begin with "Subject:".

Required format:
Subject: <one-line imperative subject>
Description:
<2-5 short lines>

Previous response to fix:
{truncate(raw_output, 6000)}

Changes:
{change_summary}
"""
    try:
        retry = run_cmd(["ollama", "run", model, retry_prompt], timeout=90)
    except Exception:
        return "", ""
    if retry.returncode != 0:
        return "", ""
    subject, description = parse_ai_message(retry.stdout or "")
    if is_usable_ai_subject(subject):
        return subject, description
    return "", ""


def maybe_generate_ai_message(llvm_dir: Path) -> tuple[str, str]:
    if not sys.stdin.isatty():
        return "", ""
    if not ollama_installed():
        print("Ollama is not installed or is not available in PATH.")
        return "", ""

    if not ollama_running():
        print("Ollama is installed, but it does not appear to be running.")
        if prompt_yes_no("Start Ollama now?", default=False):
            if not start_ollama_server():
                return "", ""
        else:
            return "", ""

    if not ollama_running():
        return "", ""

    if not prompt_yes_no("Use an AI-generated commit message as a starting point?", default=True):
        return "", ""

    models = installed_ollama_models()
    if not models:
        print("No installed Ollama models were found. Continuing with manual entry.")
        return "", ""

    model = select_ollama_model(models)
    print()
    print(f"Generating commit message with Ollama model: {model}")
    subject, description = generate_ai_message(llvm_dir, model)
    if subject or description:
        print()
        print("AI-generated starting point:")
        print("---")
        if subject:
            print(subject)
        if description:
            print()
            print(description)
        print("---")
    return subject, description


def sanitize_subject(subject: str) -> str:
    subject = subject.strip()
    if not subject:
        return "clang-mg: update patches"
    return subject


def slugify(subject: str) -> str:
    text = subject.lower()
    text = re.sub(r"^\[patch[^\]]*\]\s*", "", text)
    text = re.sub(r"^clang-mg:\s*", "", text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:70].strip("-") or "update-patches"


def existing_patch_numbers(patch_root: Path) -> list[int]:
    numbers: list[int] = []
    for p in patch_root.glob("*.patch"):
        m = re.match(r"^(\d+)-", p.name)
        if m:
            try:
                numbers.append(int(m.group(1)))
            except ValueError:
                pass
    return numbers


def next_patch_name(patch_root: Path, subject: str) -> str:
    numbers = existing_patch_numbers(patch_root)
    width = 4
    for p in patch_root.glob("*.patch"):
        m = re.match(r"^(\d+)-", p.name)
        if m:
            width = max(width, len(m.group(1)))
    next_num = (max(numbers) + 1) if numbers else 1
    return f"{next_num:0{width}d}-{slugify(subject)}.patch"


def write_commit_message_file(subject: str, description: str) -> Path:
    tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, prefix="clang-mg-commit-", suffix=".txt")
    with tmp:
        tmp.write(subject.strip() + "\n")
        if description.strip():
            tmp.write("\n")
            tmp.write(description.strip() + "\n")
    return Path(tmp.name)


def save_current_changes(root_dir: Path, llvm_dir: Path) -> int:
    patch_root = root_dir / "patches"

    print("=== save clang-mg patch ===")
    print(f"Root dir:   {root_dir}")
    print(f"LLVM dir:   {llvm_dir}")
    print(f"Patch root: {patch_root}")
    print()

    if not (llvm_dir / ".git").is_dir():
        return fail(f"ERROR: LLVM repo is not cloned:\n{llvm_dir}")
    if not ensure_no_git_operation_in_progress(llvm_dir):
        return 1

    status = git_output(["status", "--porcelain"], cwd=llvm_dir, check=False)
    if not status:
        print("LLVM has no uncommitted changes to save.")
        return 0

    print("Changes waiting to be saved:")
    print(status)
    print()

    ai_subject, ai_description = maybe_generate_ai_message(llvm_dir)

    print()
    subject = sanitize_subject(prompt_text("Commit message", ai_subject.strip()))
    description = prompt_multiline("Commit description", ai_description.strip())

    patch_root.mkdir(parents=True, exist_ok=True)
    message_file = write_commit_message_file(subject, description)
    tmp_dir = Path(tempfile.mkdtemp(prefix=".patch-save.", dir=str(root_dir)))
    try:
        git(["add", "-A"], cwd=llvm_dir)
        git(["commit", "-F", str(message_file)], cwd=llvm_dir)

        cp = git([
            "format-patch",
            "--zero-commit",
            "--no-stat",
            "-1",
            "--output-directory",
            str(tmp_dir),
        ], cwd=llvm_dir, check=False, quiet=True)
        if cp.returncode != 0:
            print("ERROR: Failed to generate patch from the new commit.")
            return 1

        generated = sorted(tmp_dir.glob("*.patch"))
        if not generated:
            print("ERROR: No patch was generated from the new commit.")
            return 1

        patch_name = next_patch_name(patch_root, subject)
        target = patch_root / patch_name
        while target.exists():
            stem = target.stem
            target = patch_root / f"{stem}-new.patch"
        shutil.copy2(generated[0], target)

        print()
        print("Saved patch:")
        print(f"  {target}")
        print()
        print("Created LLVM commit:")
        print(f"  {git_output(['rev-parse', '--short', 'HEAD'], cwd=llvm_dir, check=False)} {subject}")
        return 0
    finally:
        try:
            message_file.unlink()
        except FileNotFoundError:
            pass
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main(argv: list[str]) -> int:
    root_dir = root_from_script(__file__)
    work_dir = Path(env_or_default("WORK_DIR", root_dir / "work"))
    llvm_dir = Path(env_or_default("LLVM_DIR", work_dir / "llvm-project"))

    if argv and argv[0] in {"-h", "--help"}:
        print("Usage:")
        print("  scripts/save-patches.py")
        print()
        print("Normally, use:")
        print("  python build.py save")
        return 0

    return save_current_changes(root_dir, llvm_dir)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
