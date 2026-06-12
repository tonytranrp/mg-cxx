#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
from clang_mg_common import default_jobs, detect_target_triple, env_or_default, git, run, truthy


def print_header(command: str, llvm_ref: str, llvm_dir: Path, target_triple: str,
                 build_dir: Path, build_type: str, jobs: str) -> None:
    print("=== clang-mg ===")
    print(f"Command:       {command}")
    print(f"LLVM ref:      {llvm_ref}")
    print(f"LLVM dir:      {llvm_dir}")
    print(f"Target triple: {target_triple}")
    print(f"Build dir:     {build_dir}")
    print(f"Build type:    {build_type}")
    print(f"Jobs:          {jobs}")
    print()


def usage(root_dir: Path) -> None:
    print("Usage:")
    print("  python build.py [command]")
    print()
    print("Commands:")
    print("  bootstrap            Clone/update LLVM if needed, apply the flat patch stack, then build")
    print("  install              Clone/update LLVM, reset clean, apply the patch stack, build, then add clang-mg to PATH")
    print("  clone                Clone LLVM only")
    print("  update               Update LLVM only if the checkout is clean")
    print("  reset                Reset LLVM checkout to LLVM_REF / origin ref")
    print("  apply                Apply all top-level patches from patches/")
    print("  refresh [start-ref]  Regenerate all top-level patches from start-ref..HEAD")
    print("  export [base-ref]   Write one net patch for the applied LLVM changes into work/")
    print("  collect [base-ref]  Alias for export")
    print("  save                 Save current LLVM working-tree changes as a new patch")
    print("  build                Build current LLVM tree only")
    print("  test [target] [path ...]  Build test dependencies and run LLVM lit tests")
    print("  fresh                Reset LLVM, apply all patches, then build")
    print("  rebuild              Same as fresh")
    print("  help                 Show this help menu")
    print()
    print("Examples:")
    print("  python build.py")
    print("  python build.py bootstrap")
    print("  python build.py apply")
    print("  python build.py save")
    print("  python build.py refresh")
    print("  python build.py refresh origin/main")
    print("  python build.py export")
    print("  python build.py export origin/main")
    print("  python build.py build")
    print("  python build.py test")
    print("  python build.py test clang")
    print("  python build.py test clang CXXMG/traits")
    print("  python build.py test clang cxxmg")
    print("  python build.py fresh")
    print()
    print("Patch workflow:")
    print("  patches/*.patch is the canonical clang-mg patch stack.")
    print("  apply   = apply every top-level patch in lexical order")
    print("  save    = commit current LLVM changes and append one new patch")
    print("  refresh = rewrite the whole flat patch stack from LLVM git history")
    print("  export  = write one collapsed net diff of the applied LLVM changes")
    print()
    print("Environment variables:")
    print("  LLVM_REF=main")
    print("  LLVM_URL=https://github.com/llvm/llvm-project.git")
    print(f"  WORK_DIR={root_dir}/work")
    print(f"  LLVM_DIR={root_dir}/work/llvm-project")
    print("  BUILD_TARGET_TRIPLE=x86_64-pc-linux-gnu")
    print(f"  BUILD_DIR={root_dir}/work/build-$BUILD_TARGET_TRIPLE")
    print("  BUILD_TYPE=Release")
    print("  LLVM_ENABLE_PROJECTS=clang")
    print("  JOBS=16")
    print("  INTERACTIVE=1")
    print("  OLLAMA_MODEL=gemma3:4b")


def test_llvm_repo(llvm_dir: Path) -> bool:
    if not llvm_dir.exists():
        return False
    cp = git(["-C", str(llvm_dir), "rev-parse", "--is-inside-work-tree"], check=False, quiet=True)
    return cp.returncode == 0


def require_llvm_repo(llvm_dir: Path) -> None:
    if not test_llvm_repo(llvm_dir):
        print("ERROR: LLVM is not cloned yet.")
        print()
        print("Run:")
        print("  python build.py clone")
        print()
        print("or:")
        print("  python build.py bootstrap")
        raise SystemExit(1)


def run_clone(scripts_dir: Path, llvm_url: str, llvm_ref: str, llvm_dir: Path) -> None:
    run([sys.executable, str(scripts_dir / "clone-llvm.py"), llvm_url, llvm_ref, str(llvm_dir)])


def run_update(scripts_dir: Path, llvm_url: str, llvm_ref: str, work_dir: Path, llvm_dir: Path) -> None:
    env = os.environ.copy()
    env.update({"LLVM_URL": llvm_url, "LLVM_REF": llvm_ref, "WORK_DIR": str(work_dir), "LLVM_DIR": str(llvm_dir)})
    run([sys.executable, str(scripts_dir / "update-llvm.py")], env=env)


def resolve_reset_ref(llvm_dir: Path, llvm_ref: str) -> str:
    require_llvm_repo(llvm_dir)
    git(["fetch", "origin", "--tags"], cwd=llvm_dir)
    cp = git(["show-ref", "--verify", "--quiet", f"refs/remotes/origin/{llvm_ref}"], cwd=llvm_dir, check=False, quiet=True)
    return f"origin/{llvm_ref}" if cp.returncode == 0 else llvm_ref


def run_reset(scripts_dir: Path, llvm_dir: Path, llvm_ref: str) -> None:
    require_llvm_repo(llvm_dir)
    reset_ref = resolve_reset_ref(llvm_dir, llvm_ref)
    run([sys.executable, str(scripts_dir / "reset-llvm.py"), reset_ref], cwd=llvm_dir)


def run_apply_patches(root_dir: Path, scripts_dir: Path, llvm_dir: Path) -> None:
    require_llvm_repo(llvm_dir)
    run([sys.executable, str(scripts_dir / "apply-patches.py"), str(root_dir), str(llvm_dir)])


def run_refresh_patches(scripts_dir: Path, llvm_dir: Path, start_ref: str | None = None) -> None:
    require_llvm_repo(llvm_dir)
    args = [sys.executable, str(scripts_dir / "refresh-patches.py")]
    if start_ref:
        args.append(start_ref)
    run(args, cwd=llvm_dir)


def run_save_patches(scripts_dir: Path, llvm_dir: Path) -> None:
    require_llvm_repo(llvm_dir)
    run([sys.executable, str(scripts_dir / "save-patches.py")], cwd=llvm_dir)


def run_export_patch(scripts_dir: Path, work_dir: Path, llvm_dir: Path, llvm_ref: str,
                     base_ref: str | None = None) -> None:
    require_llvm_repo(llvm_dir)
    args = [sys.executable, str(scripts_dir / "export-patch.py")]
    if base_ref:
        args.append(base_ref)
    env = os.environ.copy()
    env.update({"WORK_DIR": str(work_dir), "LLVM_DIR": str(llvm_dir), "LLVM_REF": llvm_ref})
    run(args, env=env)


def run_build(scripts_dir: Path, llvm_dir: Path, build_dir: Path, build_type: str,
              jobs: str, target_triple: str, interactive: bool) -> None:
    require_llvm_repo(llvm_dir)
    args = [
        sys.executable,
        str(scripts_dir / "build-llvm.py"),
        str(llvm_dir),
        str(build_dir),
        build_type,
        jobs,
    ]
    env = os.environ.copy()
    env["BUILD_TARGET_TRIPLE"] = target_triple
    if interactive:
        env["INTERACTIVE"] = "1"
    run(args, env=env)


def load_script_module(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"ERROR: Could not load script module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_tests(scripts_dir: Path, llvm_dir: Path, build_dir: Path, build_type: str,
              jobs: str, test_args: list[str], work_dir: Path, target_triple: str) -> int | None:
    require_llvm_repo(llvm_dir)
    args = [
        str(llvm_dir),
        str(build_dir),
        build_type,
        jobs,
        *test_args,
    ]
    old_work_dir = os.environ.get("WORK_DIR")
    old_target_triple = os.environ.get("BUILD_TARGET_TRIPLE")
    os.environ["WORK_DIR"] = str(work_dir)
    os.environ["BUILD_TARGET_TRIPLE"] = target_triple
    try:
        test_llvm = load_script_module(scripts_dir / "test-llvm.py", "clang_mg_test_llvm")
        return test_llvm.run_test_llvm(args)
    finally:
        if old_work_dir is None:
            os.environ.pop("WORK_DIR", None)
        else:
            os.environ["WORK_DIR"] = old_work_dir
        if old_target_triple is None:
            os.environ.pop("BUILD_TARGET_TRIPLE", None)
        else:
            os.environ["BUILD_TARGET_TRIPLE"] = old_target_triple


def final_message_for_passed_tests(passed_tests: int | None) -> str:
    if passed_tests == 69:
        return "Nice."
    if passed_tests == 420:
        return "Blaze It!"
    return "Done."


def run_install_path(scripts_dir: Path, llvm_dir: Path, build_dir: Path) -> None:
    run([sys.executable, str(scripts_dir / "install-clang-mg.py"), str(llvm_dir), str(build_dir)])


def main(argv: list[str]) -> int:
    root_dir = Path(__file__).resolve().parent
    scripts_dir = root_dir / "scripts"

    llvm_url = env_or_default("LLVM_URL", "https://github.com/llvm/llvm-project.git")
    llvm_ref = env_or_default("LLVM_REF", "main")
    work_dir = Path(env_or_default("WORK_DIR", root_dir / "work"))
    llvm_dir = Path(env_or_default("LLVM_DIR", work_dir / "llvm-project"))
    build_target_triple = detect_target_triple()
    build_dir = Path(env_or_default("BUILD_DIR", work_dir / f"build-{build_target_triple}"))
    build_type = env_or_default("BUILD_TYPE", "Release")
    jobs = default_jobs()

    interactive = truthy(env_or_default("INTERACTIVE", "0"))
    filtered: list[str] = []
    for arg in argv:
        if arg == "--interactive":
            interactive = True
        else:
            filtered.append(arg)
    command = filtered[0] if filtered else "bootstrap"
    rest = filtered[1:]

    print_header(command, llvm_ref, llvm_dir, build_target_triple, build_dir, build_type, jobs)
    final_message = "Done."

    if command in {"help", "-h", "--help"}:
        usage(root_dir)
    elif command == "clone":
        run_clone(scripts_dir, llvm_url, llvm_ref, llvm_dir)
    elif command == "update":
        run_update(scripts_dir, llvm_url, llvm_ref, work_dir, llvm_dir)
    elif command == "reset":
        run_reset(scripts_dir, llvm_dir, llvm_ref)
    elif command == "apply":
        if rest:
            print("ERROR: `apply` no longer accepts feature names.")
            print("The new workflow applies every top-level patch in patches/.")
            print()
            usage(root_dir)
            return 1
        run_apply_patches(root_dir, scripts_dir, llvm_dir)
    elif command == "refresh":
        if len(rest) > 1:
            print("ERROR: refresh accepts at most one optional start ref.")
            print("Usage:")
            print("  python build.py refresh [start-ref]")
            return 1
        run_refresh_patches(scripts_dir, llvm_dir, rest[0] if rest else None)
    elif command in {"export", "collect"}:
        if len(rest) > 1:
            print(f"ERROR: `{command}` accepts at most one optional base ref.")
            print("Usage:")
            print("  python build.py export [base-ref]")
            print("  python build.py collect [base-ref]")
            return 1
        run_export_patch(scripts_dir, work_dir, llvm_dir, llvm_ref, rest[0] if rest else None)
    elif command == "save":
        if rest:
            print("ERROR: `save` no longer accepts a feature name.")
            print("The new workflow saves one new patch into the flat patches/ stack.")
            print()
            usage(root_dir)
            return 1
        run_save_patches(scripts_dir, llvm_dir)
    elif command == "build":
        run_build(scripts_dir, llvm_dir, build_dir, build_type, jobs, build_target_triple, interactive)
    elif command == "test":
        passed_tests = run_tests(scripts_dir, llvm_dir, build_dir, build_type, jobs, rest, work_dir, build_target_triple)
        final_message = final_message_for_passed_tests(passed_tests)
    elif command == "bootstrap":
        run_update(scripts_dir, llvm_url, llvm_ref, work_dir, llvm_dir)
        run_apply_patches(root_dir, scripts_dir, llvm_dir)
        run_build(scripts_dir, llvm_dir, build_dir, build_type, jobs, build_target_triple, interactive)
    elif command == "install":
        run_update(scripts_dir, llvm_url, llvm_ref, work_dir, llvm_dir)
        run_reset(scripts_dir, llvm_dir, llvm_ref)
        run_apply_patches(root_dir, scripts_dir, llvm_dir)
        run_build(scripts_dir, llvm_dir, build_dir, build_type, jobs, build_target_triple, interactive)
        run_install_path(scripts_dir, llvm_dir, build_dir)
    elif command in {"fresh", "rebuild"}:
        if not test_llvm_repo(llvm_dir):
            run_clone(scripts_dir, llvm_url, llvm_ref, llvm_dir)
        else:
            run_reset(scripts_dir, llvm_dir, llvm_ref)
        run_apply_patches(root_dir, scripts_dir, llvm_dir)
        run_build(scripts_dir, llvm_dir, build_dir, build_type, jobs, build_target_triple, interactive)
    elif command in {"enable", "disable", "refresh-feature"}:
        print(f"ERROR: `{command}` was removed by the flat patch-stack workflow.")
        print("Use `python build.py apply`, `python build.py save`, or `python build.py refresh`.")
        return 1
    else:
        print(f"ERROR: Unknown command: {command}")
        print()
        usage(root_dir)
        return 1

    print()
    print(final_message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
