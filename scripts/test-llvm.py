#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
import textwrap
from pathlib import Path

from clang_mg_common import default_jobs, detect_target_triple, has_cmd, is_windows, run


def show_usage(script_name: str) -> None:
    print(f"Usage: {script_name} <llvm-dir> <build-dir> <build-type> <jobs> [test-target] [test-path ...]")
    print()
    print("Runs LLVM-project lit tests from this repository's configured build tree.")
    print()
    print("Examples:")
    print(f"  {script_name} work/llvm-project work/build-x86_64-pc-linux-gnu Release 16")
    print(f"  {script_name} work/llvm-project work/build-x86_64-pc-linux-gnu Release 16 clang")
    print(f"  {script_name} work/llvm-project work/build-x86_64-pc-linux-gnu Release 16 clang CXXMG/traits")
    print(f"  {script_name} work/llvm-project work/build-x86_64-pc-linux-gnu Release 16 clang cxxmg")
    print(f"  {script_name} work/llvm-project work/build-x86_64-pc-linux-gnu Release 16 clang C/C99/block-scopes.c")
    print()
    print("Arguments after the test target are relative to <llvm-dir>/<target>/test.")
    print("Path matching is case-insensitive by component, so `clang cxxmg` resolves to `clang/test/CXXMG`.")
    print()
    print("Environment variables:")
    print("  CLANG=<path>                 Override compiler used by LLVM lit substitutions")
    print("  CLANG_MG_TEST_BIN_DIR=<dir>  Preferred directory for llvm-lit and test tools")
    print("  CLANG_MG_LIT_OPTS='-v'       Extra arguments passed to llvm-lit")
    print("  CLANG_MG_USE_UPSTREAM_LIT=1     Use upstream clang/test lit.cfg.py even for CXXMG subsets")
    print("  LIT_OPTS='-v'                Extra options parsed by lit itself")
    print("  BUILD_TARGET_TRIPLE=...     Used only when this script must configure a missing build")
    print("  LLVM_ENABLE_PROJECTS=clang  Used only when this script must configure a missing build")


def split_user_test_paths(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    # Accept either shell-like whitespace or comma-separated lists in the prompt.
    return [part for item in shlex.split(text) for part in item.split(",") if part]


def discover_test_projects(llvm_dir: Path) -> list[str]:
    if not llvm_dir.is_dir():
        return []
    projects = []
    for child in llvm_dir.iterdir():
        if child.is_dir() and (child / "test").is_dir():
            projects.append(child.name)
    return sorted(projects, key=lambda name: (name.lower() != "clang", name.lower()))


def resolve_project_name(llvm_dir: Path, requested: str) -> str:
    projects = discover_test_projects(llvm_dir)
    if not projects:
        raise SystemExit(f"ERROR: No LLVM project test directories were found under: {llvm_dir}")

    for project in projects:
        if project == requested:
            return project
    for project in projects:
        if project.lower() == requested.lower():
            return project

    print(f"ERROR: Unknown LLVM test target: {requested}")
    print()
    print("Available test targets:")
    for project in projects:
        print(f"  {project}")
    raise SystemExit(1)


def prompt_for_tests(llvm_dir: Path) -> tuple[str, list[str]]:
    default_project = "clang" if (llvm_dir / "clang" / "test").is_dir() else "llvm"
    projects = discover_test_projects(llvm_dir)

    if not sys.stdin.isatty():
        return default_project, []

    print("Available LLVM test targets:")
    if projects:
        print("  " + ", ".join(projects))
    else:
        print("  <none found>")
    print()
    print("Press Enter twice to run all Clang tests.")
    project = input(f"Test target [{default_project}]: ").strip() or default_project
    project = resolve_project_name(llvm_dir, project)
    test_text = input(f"Test path(s) relative to {project}/test [all]: ")
    return project, split_user_test_paths(test_text)


def resolve_case_insensitive_path(base: Path, requested: str) -> Path:
    if not requested.strip():
        return base

    normalized = requested.replace("\\", "/").strip("/")
    raw_parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if not raw_parts:
        return base
    if any(part == ".." for part in raw_parts):
        raise SystemExit(f"ERROR: Test paths must stay inside the test directory: {requested}")
    if Path(requested).is_absolute():
        raise SystemExit(f"ERROR: Test path must be relative to the project test directory: {requested}")

    current = base
    for part in raw_parts:
        exact = current / part
        if exact.exists():
            current = exact
            continue
        if not current.is_dir():
            raise SystemExit(f"ERROR: Could not resolve test path component `{part}` in: {current}")

        matches = [child for child in current.iterdir() if child.name.lower() == part.lower()]
        if len(matches) == 1:
            current = matches[0]
            continue
        if len(matches) > 1:
            print(f"ERROR: Ambiguous case-insensitive test path component `{part}` in: {current}")
            for match in matches:
                print(f"  {match.name}")
            raise SystemExit(1)

        raise SystemExit(f"ERROR: Test path does not exist under {base}: {requested}")
    return current


def configure_build_if_needed(llvm_dir: Path, build_dir: Path, build_type: str, jobs: str, project: str) -> None:
    if (build_dir / "CMakeCache.txt").is_file():
        return

    print("LLVM build directory is not configured yet.")
    print("Configuring and building clang first so test targets are available...")
    scripts_dir = Path(__file__).resolve().parent
    env = os.environ.copy()
    env.setdefault("BUILD_TARGET_TRIPLE", detect_target_triple())
    if project.lower() != "llvm":
        configured_projects = [item for item in re.split(r"[;,]", env.get("LLVM_ENABLE_PROJECTS", "clang")) if item.strip()]
        if not any(item.lower() == project.lower() for item in configured_projects):
            configured_projects.append(project)
        env["LLVM_ENABLE_PROJECTS"] = ";".join(configured_projects)
    run([
        sys.executable,
        str(scripts_dir / "build-llvm.py"),
        str(llvm_dir),
        str(build_dir),
        build_type,
        jobs,
    ], env=env)


def inferred_work_dir(build_dir: Path) -> Path:
    env_work_dir = os.environ.get("WORK_DIR", "").strip()
    if env_work_dir:
        return Path(env_work_dir).resolve()
    return build_dir.resolve().parent


def inferred_target_triple(build_dir: Path) -> str:
    env_triple = os.environ.get("BUILD_TARGET_TRIPLE", "").strip()
    if env_triple:
        return env_triple
    name = build_dir.name
    if name.startswith("build-") and len(name) > len("build-"):
        return name[len("build-"):]
    return ""


def executable_names(base_name: str) -> list[str]:
    if is_windows():
        return [
            f"{base_name}.exe",
            f"{base_name}.cmd",
            f"{base_name}.bat",
            f"{base_name}.py",
            base_name,
        ]
    return [base_name, f"{base_name}.py"]


def preferred_tool_bin_dirs(build_dir: Path) -> list[Path]:
    dirs: list[Path] = []

    def add(directory: Path) -> None:
        resolved = directory.resolve(strict=False)
        key = str(resolved).lower() if is_windows() else str(resolved)
        if key not in seen:
            seen.add(key)
            dirs.append(resolved)

    seen: set[str] = set()

    explicit = os.environ.get("CLANG_MG_TEST_BIN_DIR", "").strip()
    if explicit:
        add(Path(explicit))

    triple = inferred_target_triple(build_dir)
    if triple:
        # clang-mg's preferred installed/tool directory, e.g.
        #   work/x86_64-pc-linux-gnu/bin
        add(inferred_work_dir(build_dir) / triple / "bin")

    # LLVM build trees normally place lit and freshly-built test tools here.
    # Keep this after work/<triple>/bin so the clang-mg tool directory wins,
    # but before the inherited PATH so focused lit runs work before install.
    add(build_dir / "bin")
    return dirs


def find_executable_with_path(base_name: str, build_dir: Path, path_value: str | None = None, *, include_path: bool = True) -> Path | None:
    for directory in preferred_tool_bin_dirs(build_dir):
        if not directory.is_dir():
            continue
        for name in executable_names(base_name):
            candidate = directory / name
            if candidate.is_file():
                return candidate

    if not include_path:
        return None

    search_path = os.environ.get("PATH", "") if path_value is None else path_value
    for name in executable_names(base_name):
        found = shutil.which(name, path=search_path)
        if found:
            return Path(found)
    return None


def configure_lit_clang_environment(env: dict[str, str], build_dir: Path) -> None:
    # LLVM's lit config intentionally allows CLANG to override the executable
    # used for %clang/%clang_cc1/%clangxx. Honor an explicit user override.
    if env.get("CLANG", "").strip():
        return

    search_path = env.get("PATH", os.environ.get("PATH", ""))

    # This runner is for the clang-mg fork, so prefer the installed clang-mg
    # tools over upstream-named tools. Do not synthesize a clang wrapper: pass
    # lit the real executable that was selected.
    preferred_names = ("clang-mg++", "clang-mg", "clang")

    # First search only the clang-mg/LLVM tool directories. This keeps test runs
    # anchored to work/<triple>/bin whenever the installed fork tools are there,
    # with the build tree's bin directory as the next configured-tree fallback.
    for name in preferred_names:
        candidate = find_executable_with_path(name, build_dir, search_path, include_path=False)
        if candidate is not None:
            env["CLANG"] = str(candidate)
            return

    # Last resort: search PATH with the same fork-first order.
    for name in preferred_names:
        candidate = find_executable_with_path(name, build_dir, search_path, include_path=True)
        if candidate is not None:
            env["CLANG"] = str(candidate)
            return


def env_with_preferred_tools(build_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PATH", "")
    prefix = [str(directory) for directory in preferred_tool_bin_dirs(build_dir) if directory.is_dir()]
    if prefix:
        env["PATH"] = os.pathsep.join(prefix + ([existing] if existing else []))
    configure_lit_clang_environment(env, build_dir)
    return env


def find_executable(base_name: str, build_dir: Path) -> Path | None:
    env = env_with_preferred_tools(build_dir)
    return find_executable_with_path(base_name, build_dir, env.get("PATH"))


def command_for_executable(path: Path) -> list[str]:
    suffix = path.suffix.lower()
    if suffix == ".py":
        return [sys.executable, str(path)]
    return [str(path)]


def cmake_build(build_dir: Path, build_type: str, jobs: str, target: str) -> None:
    print()
    print(f"Building test target: {target}")
    run([
        "cmake",
        "--build",
        ".",
        "--config",
        build_type,
        "--target",
        target,
        "--parallel",
        str(jobs),
    ], cwd=build_dir, env=env_with_preferred_tools(build_dir))


def available_build_targets(build_dir: Path) -> set[str]:
    cp = run(["cmake", "--build", ".", "--target", "help"], cwd=build_dir, env=env_with_preferred_tools(build_dir), check=False, capture=True)
    text = (cp.stdout or "") + "\n" + (cp.stderr or "")
    if cp.returncode != 0 or not text.strip():
        return set()

    targets: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Ninja commonly prints: "... check-clang: phony".
        m = re.match(r"^(?:\.\.\.\s*)?([^:\s]+)\s*:", stripped)
        if m:
            targets.add(m.group(1))
            continue
        # Makefile generators commonly print bare target names.
        if re.match(r"^[A-Za-z0-9_.+\-/]+$", stripped):
            targets.add(stripped)
    return targets


def build_minimal_clang_mg_test_targets(build_dir: Path, build_type: str, jobs: str) -> None:
    """Build only the tools needed by the clang-mg focused lit suite.

    This intentionally avoids clang-test-depends because upstream clang/test/lit.cfg.py
    can run global clang-repl feature probes before any focused CXXMG test executes.
    Those probes are unrelated to clang-mg language tests and can be very expensive
    or hang on some machines.
    """
    targets = available_build_targets(build_dir)
    wanted: list[str] = []

    def maybe_add(name: str) -> None:
        if not targets or name in targets:
            if name not in wanted:
                wanted.append(name)

    for name in (
        "clang",
        "clang-resource-headers",
        "FileCheck",
        "not",
        "count",
        "split-file",
    ):
        maybe_add(name)

    if not wanted:
        raise SystemExit(
            f"ERROR: Could not find minimal Clang test build targets in {build_dir}.\n"
            "Make sure this is a configured LLVM build directory."
        )

    for target in wanted:
        cmake_build(build_dir, build_type, jobs, target)


def build_required_subset_targets(build_dir: Path, build_type: str, jobs: str, project: str) -> None:
    targets = available_build_targets(build_dir)
    wanted: list[str] = []

    def add(name: str) -> None:
        if name not in wanted:
            wanted.append(name)

    def maybe_add(name: str) -> None:
        if not targets or name in targets:
            add(name)

    test_dep_target = f"{project}-test-depends"
    if not targets or test_dep_target in targets:
        add(test_dep_target)
    elif project.lower() == "clang":
        # clang-test-depends is the authoritative target for Clang's configured
        # test tools. Keep a conservative fallback for older or unusual trees.
        for name in (
            "clang",
            "clang-resource-headers",
            "FileCheck",
            "count",
            "not",
            "split-file",
            "llvm-config",
        ):
            maybe_add(name)
    else:
        raise SystemExit(
            f"ERROR: Build target `{test_dep_target}` is not available in {build_dir}.\n"
            f"Make sure the build was configured with LLVM_ENABLE_PROJECTS including `{project}`."
        )

    for target in wanted:
        cmake_build(build_dir, build_type, jobs, target)


def run_full_project_tests(build_dir: Path, build_type: str, jobs: str, project: str) -> None:
    check_target = f"check-{project}"
    targets = available_build_targets(build_dir)
    if targets and check_target not in targets:
        raise SystemExit(
            f"ERROR: Build target `{check_target}` is not available in {build_dir}.\n"
            f"Make sure the build was configured with LLVM_ENABLE_PROJECTS including `{project}`."
        )
    cmake_build(build_dir, build_type, jobs, check_target)


def find_llvm_lit_command(build_dir: Path) -> list[str]:
    candidate = find_executable("llvm-lit", build_dir)
    if candidate is not None:
        return command_for_executable(candidate)

    searched = "\n".join(f"  {directory}" for directory in preferred_tool_bin_dirs(build_dir))
    raise SystemExit(
        "ERROR: Could not find llvm-lit.\n"
        "Searched preferred clang-mg/LLVM tool directories first, then PATH.\n"
        f"Preferred directories:\n{searched}\n\n"
        "Build the project test dependencies first, or set CLANG_MG_TEST_BIN_DIR to the directory containing llvm-lit."
    )


def is_truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def tests_are_under(path_root: Path, test_paths: list[Path]) -> bool:
    root = path_root.resolve()
    for path in test_paths:
        try:
            path.resolve().relative_to(root)
        except ValueError:
            return False
    return True


def should_use_minimal_clang_mg_lit(project: str, project_test_dir: Path, resolved_tests: list[Path]) -> bool:
    if project.lower() != "clang":
        return False
    if is_truthy_env("CLANG_MG_USE_UPSTREAM_LIT"):
        return False
    cxxmg_root = project_test_dir / "CXXMG"
    if not cxxmg_root.is_dir():
        return False
    return tests_are_under(cxxmg_root, resolved_tests)


def escape_lit_string(value: str) -> str:
    # repr() is sufficient for Python string literals in the generated lit config.
    return repr(value)


def selected_clang_for_lit(build_dir: Path) -> str:
    env = env_with_preferred_tools(build_dir)
    clang = env.get("CLANG", "").strip()
    if not clang:
        raise SystemExit(
            "ERROR: Could not find clang-mg++, clang-mg, or clang for lit.\n"
            "Build clang first or set CLANG to the compiler executable to use."
        )
    return clang


def create_minimal_clang_mg_lit_suite(build_dir: Path, source_cxxmg_dir: Path) -> Path:
    suite_dir = build_dir / "test" / "clang-mg-lit" / "CXXMG"
    suite_dir.mkdir(parents=True, exist_ok=True)

    # Put the minimal config directly inside the synthetic CXXMG root. Tests are
    # exposed through a symlink named "src" so lit never walks up to clang/test
    # and therefore never loads upstream clang/test/lit.cfg.py.
    src_link = suite_dir / "src"
    if src_link.exists() or src_link.is_symlink():
        if src_link.is_symlink() or src_link.is_file():
            src_link.unlink()
        elif src_link.is_dir() and not src_link.is_symlink():
            shutil.rmtree(src_link)
    try:
        src_link.symlink_to(source_cxxmg_dir.resolve(), target_is_directory=True)
    except OSError:
        # Windows without developer mode may not allow symlinks. Fall back to a
        # lightweight copy of the test tree; CXXMG tests are small enough that
        # this is acceptable and keeps the behavior portable.
        shutil.copytree(source_cxxmg_dir, src_link, dirs_exist_ok=True)

    env = env_with_preferred_tools(build_dir)
    path_value = env.get("PATH", os.environ.get("PATH", ""))
    clang = selected_clang_for_lit(build_dir)
    tool_dirs = [str(directory) for directory in preferred_tool_bin_dirs(build_dir) if directory.is_dir()]
    python_exe = sys.executable
    test_exec_root = build_dir / "test" / "clang-mg-lit-exec"
    test_exec_root.mkdir(parents=True, exist_ok=True)

    cfg = f"""
# Auto-generated by scripts/test-llvm.py.
# This focused config is intentionally minimal: it avoids loading upstream
# clang/test/lit.cfg.py so CXXMG-only runs do not execute global clang-repl
# CUDA/JIT feature probes before the actual clang-mg tests run.
import os
import lit.formats
import lit.llvm

config.name = "ClangMG-CXXMG"
config.test_format = lit.formats.ShTest(execute_external=False)
config.suffixes = [".c", ".cpp", ".i", ".m", ".mm", ".cu", ".hlsl", ".ll", ".cl", ".s", ".S", ".test"]
config.excludes = ["Inputs", "CMakeLists.txt", "README.txt", "LICENSE.txt"]
config.test_source_root = os.path.dirname(__file__)
config.test_exec_root = {escape_lit_string(str(test_exec_root))}
config.llvm_tools_dir = {escape_lit_string(str(build_dir / "bin"))}
config.clang_tools_dir = {escape_lit_string(str(build_dir / "bin"))}
config.python_executable = {escape_lit_string(python_exe)}
config.target_triple = {escape_lit_string(inferred_target_triple(build_dir) or "unknown-unknown-unknown")}
config.host_triple = config.target_triple
config.environment["PATH"] = {escape_lit_string(path_value)}
config.environment["CLANG_NO_DEFAULT_CONFIG"] = "1"

# Standalone synthetic lit configs are not loaded through LLVM's generated
# lit.site.cfg.py, so lit.llvm.llvm_config is still None here until we
# initialize it explicitly. This keeps the useful LLVM substitutions without
# importing clang/test/lit.cfg.py and triggering global clang-repl probes.
lit.llvm.initialize(lit_config, config)
llvm_config = lit.llvm.llvm_config
llvm_config.use_default_substitutions()
config.substitutions.append(("%PATH%", config.environment["PATH"]))
config.substitutions.append(("%target_triple", config.target_triple))

_clang = {escape_lit_string(clang)}
config.substitutions.append(("%clang", _clang))
config.substitutions.append(("%clang_cc1", _clang + " -cc1"))
config.substitutions.append(("%clangxx", _clang))
config.substitutions.append(("%clang_cpp", _clang + " -E"))
config.substitutions.append(("%clang_analyze_cc1", _clang + " -cc1 -analyze"))

tool_dirs = {tool_dirs!r}
llvm_config.add_tool_substitutions(["FileCheck", "not", "count", "split-file"], tool_dirs)
"""
    (suite_dir / "lit.cfg.py").write_text(textwrap.dedent(cfg).lstrip(), encoding="utf-8")
    return suite_dir


def map_to_minimal_clang_mg_suite(project_test_dir: Path, suite_dir: Path, test_paths: list[Path]) -> list[Path]:
    cxxmg_root = (project_test_dir / "CXXMG").resolve()
    mapped: list[Path] = []
    for path in test_paths:
        rel = path.resolve().relative_to(cxxmg_root)
        mapped.append(suite_dir / "src" / rel)
    return mapped


def run_minimal_clang_mg_lit(build_dir: Path, jobs: str, project_test_dir: Path, test_paths: list[Path]) -> None:
    lit_cmd = find_llvm_lit_command(build_dir)
    suite_dir = create_minimal_clang_mg_lit_suite(build_dir, project_test_dir / "CXXMG")
    mapped_tests = map_to_minimal_clang_mg_suite(project_test_dir, suite_dir, test_paths)
    extra_args = shlex.split(os.environ.get("CLANG_MG_LIT_OPTS", ""))
    args = [*lit_cmd, "-j", str(jobs), *extra_args, *[str(path) for path in mapped_tests]]
    env = env_with_preferred_tools(build_dir)
    print()
    print("Running llvm-lit with minimal CXXMG config:")
    print(f"  Config root: {suite_dir}")
    if env.get("CLANG"):
        print(f"  CLANG={env['CLANG']}")
    print("  " + " ".join(args))
    run(args, env=env)

def run_lit(build_dir: Path, jobs: str, test_paths: list[Path]) -> None:
    lit_cmd = find_llvm_lit_command(build_dir)
    extra_args = shlex.split(os.environ.get("CLANG_MG_LIT_OPTS", ""))
    args = [*lit_cmd, "-j", str(jobs), *extra_args, *[str(path) for path in test_paths]]
    env = env_with_preferred_tools(build_dir)
    print()
    print("Running llvm-lit:")
    if env.get("CLANG"):
        print(f"  CLANG={env['CLANG']}")
    print("  " + " ".join(args))
    run(args, env=env)


def main(argv: list[str]) -> int:
    if any(arg in {"-h", "--help"} for arg in argv):
        show_usage(str(Path(__file__)))
        return 0
    if len(argv) < 4:
        print("ERROR: Missing required arguments.")
        print()
        show_usage(str(Path(__file__)))
        return 1

    llvm_dir = Path(argv[0]).resolve()
    build_dir = Path(argv[1]).resolve()
    build_type = argv[2]
    jobs = argv[3] or default_jobs()
    rest = argv[4:]

    if not llvm_dir.is_dir():
        print(f"ERROR: LLVM checkout does not exist: {llvm_dir}")
        return 1
    if not (llvm_dir / "llvm").is_dir():
        print(f"ERROR: This does not look like an llvm-project checkout: {llvm_dir}")
        return 1
    if not has_cmd("cmake"):
        print("ERROR: CMake was not found. Please install CMake and make sure it is available in PATH.")
        return 1

    if rest:
        project = resolve_project_name(llvm_dir, rest[0])
        requested_tests = rest[1:]
    else:
        project, requested_tests = prompt_for_tests(llvm_dir)
        project = resolve_project_name(llvm_dir, project)

    project_test_dir = llvm_dir / project / "test"
    if not project_test_dir.is_dir():
        print(f"ERROR: Test directory does not exist: {project_test_dir}")
        return 1

    configure_build_if_needed(llvm_dir, build_dir, build_type, jobs, project)

    print()
    print("=== test llvm ===")
    print(f"Test target:   {project}")
    print(f"Test dir:      {project_test_dir}")
    print(f"Build dir:     {build_dir}")
    print(f"Build type:    {build_type}")
    print(f"Jobs:          {jobs}")
    print("Tool bin priority:")
    for directory in preferred_tool_bin_dirs(build_dir):
        print(f"  {directory}")
    print("  PATH")
    lit_env = env_with_preferred_tools(build_dir)
    if lit_env.get("CLANG"):
        source = "user" if os.environ.get("CLANG", "").strip() else "auto"
        print(f"Lit CLANG ({source}): {lit_env['CLANG']}")
    if requested_tests:
        print("Requested tests:")
        for test in requested_tests:
            print(f"  {test}")
    else:
        print("Requested tests: all")

    if not requested_tests:
        run_full_project_tests(build_dir, build_type, jobs, project)
        return 0

    resolved_tests = [resolve_case_insensitive_path(project_test_dir, test) for test in requested_tests]
    print("Resolved tests:")
    for path in resolved_tests:
        print(f"  {path}")

    if should_use_minimal_clang_mg_lit(project, project_test_dir, resolved_tests):
        print("Lit config:    minimal CXXMG config (skips upstream clang-repl probes)")
        build_minimal_clang_mg_test_targets(build_dir, build_type, jobs)
        run_minimal_clang_mg_lit(build_dir, jobs, project_test_dir, resolved_tests)
    else:
        build_required_subset_targets(build_dir, build_type, jobs, project)
        run_lit(build_dir, jobs, resolved_tests)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
