#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
from clang_mg_common import (default_jobs, detect_target_triple, env_or_default, git, has_cmd,
                             list_patch_features, parse_enabled, read_feature_config, run, truthy,
                             write_default_feature_config)


def print_header(command: str, llvm_ref: str, llvm_dir: Path, target_triple: str, build_dir: Path, build_type: str, jobs: str) -> None:
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
    print("  bootstrap                  Clone/update LLVM if needed, apply all enabled patches, then build")
    print("  install                    Clone/update LLVM, reset clean, apply all enabled patches, build, then add clang-mg to PATH")
    print("  clone                      Clone LLVM only")
    print("  update                     Update LLVM only if the checkout is clean")
    print("  reset                      Reset LLVM checkout to LLVM_REF / origin ref")
    print("  apply                      Apply all enabled clang-mg patches")
    print("  apply <feature-name...>    Apply one or more specific feature patch stacks")
    print("  refresh-feature <feature>  Reset, apply this feature's dependencies, apply the feature, then refresh its patches")
    print("  enable <feature-name...>   Enable one or more feature patch stacks")
    print("  disable <feature-name...>  Disable one or more feature patch stacks")
    print("  build                      Build current LLVM tree only")
    print("  fresh                      Reset LLVM, apply all enabled patches, then build")
    print("  rebuild                    Same as fresh")
    print("  save <feature-name>        Save current LLVM changes as patches for a feature")
    print("  help                       Show this help menu")
    print()
    print("Examples:")
    print("  python build.py")
    print("  python build.py bootstrap")
    print("  python build.py install")
    print("  python build.py apply")
    print("  python build.py apply change-bin-name")
    print("  python build.py apply change-bin-name curlinclude")
    print("  python build.py refresh-feature traits")
    print("  python build.py enable if-constexpr-members")
    print("  python build.py disable curlinclude")
    print("  python build.py enable core if-constexpr-members")
    print("  python build.py build")
    print("  python build.py fresh")
    print("  python build.py save curlinclude")
    print()
    print("Environment variables:")
    print("  LLVM_REF=main")
    print("  LLVM_URL=https://github.com/llvm/llvm-project.git")
    print(f"  WORK_DIR={root_dir}/work")
    print(f"  LLVM_DIR={root_dir}/work/llvm-project")
    print("  BUILD_TARGET_TRIPLE=x86_64-pc-linux-gnu")
    print(f"  BUILD_DIR={root_dir}/work/build-$BUILD_TARGET_TRIPLE")
    print("  BUILD_TYPE=Debug")
    print("  JOBS=4")
    print("  FEATURE_CONFIG_NAME=feature.conf")
    print("  INTERACTIVE=1")


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


def ensure_feature_config(patch_root: Path, feature_config_name: str, feature_name: str) -> Path:
    if not feature_name.strip():
        print("ERROR: Missing feature name.")
        print()
        print("Usage:")
        print("  python build.py enable <feature-name...>")
        print("  python build.py disable <feature-name...>")
        raise SystemExit(1)
    feature_dir = patch_root / feature_name
    config_file = feature_dir / feature_config_name
    if not feature_dir.is_dir():
        print("ERROR: Feature patch directory does not exist:")
        print(f"  {feature_dir}")
        print()
        print("Available features:")
        for f in list_patch_features(patch_root):
            print(f"  {f}")
        raise SystemExit(1)
    if not config_file.is_file():
        print("Generating missing config:")
        print(f"  {config_file}")
        write_default_feature_config(config_file, feature_name)
    return config_file


def set_feature_enabled(patch_root: Path, feature_config_name: str, feature_name: str, enabled_value: str) -> None:
    config_file = ensure_feature_config(patch_root, feature_config_name, feature_name)
    lines = config_file.read_text(encoding="utf-8", errors="replace").splitlines()
    replaced = False
    new_lines: list[str] = []
    for line in lines:
        if not replaced and re.match(r"^\s*ENABLED\s*=", line):
            new_lines.append(f"ENABLED={enabled_value}")
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        new_lines.append("")
        new_lines.append("# Whether this feature should be applied.")
        new_lines.append(f"ENABLED={enabled_value}")
    config_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    if enabled_value == "1":
        print(f"Enabled feature:  {feature_name}")
    else:
        print(f"Disabled feature: {feature_name}")
    print("Config:")
    print(f"  {config_file}")


def run_set_feature_enabled(patch_root: Path, feature_config_name: str, enabled_value: str, feature_names: list[str]) -> None:
    if not feature_names:
        print("ERROR: Missing feature name.")
        print()
        if enabled_value == "1":
            print("Usage:")
            print("  python build.py enable <feature-name...>")
        else:
            print("Usage:")
            print("  python build.py disable <feature-name...>")
        print()
        print("Available features:")
        for f in list_patch_features(patch_root):
            print(f"  {f}")
        raise SystemExit(1)
    for feature_name in feature_names:
        set_feature_enabled(patch_root, feature_config_name, feature_name, enabled_value)
        print()


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


def run_apply_features(root_dir: Path, scripts_dir: Path, llvm_dir: Path, feature_names: list[str]) -> None:
    require_llvm_repo(llvm_dir)
    if not feature_names:
        run_apply_patches(root_dir, scripts_dir, llvm_dir)
        return
    for feature_name in feature_names:
        env = os.environ.copy()
        env["LLVM_DIR"] = str(llvm_dir)
        env["REFRESH_PATCHES"] = "0"
        run([sys.executable, str(scripts_dir / "apply-feature.py"), feature_name], env=env)


def collect_feature_dependency_order(patch_root: Path, feature_config_name: str, feature_name: str) -> list[str]:
    """Return recursive DEPENDS order followed by feature_name.

    This intentionally ignores ENABLED. Refreshing a feature should use the
    feature's declared prerequisites even if the user currently has those
    features disabled for normal apply-all builds.
    """
    ordered: list[str] = []
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            cycle = " -> ".join([*visiting, name])
            print("ERROR: Dependency cycle detected while resolving refresh order.")
            print(f"  {cycle}")
            raise SystemExit(1)
        feature_dir = patch_root / name
        if not feature_dir.is_dir():
            print("ERROR: Feature patch directory does not exist:")
            print(f"  {feature_dir}")
            print()
            print("Available features:")
            for f in list_patch_features(patch_root):
                print(f"  {f}")
            raise SystemExit(1)
        config_file = ensure_feature_config(patch_root, feature_config_name, name)
        cfg = read_feature_config(config_file)
        visiting.add(name)
        for dep in list(cfg["depends"]):  # type: ignore[index]
            dep_name = str(dep).strip()
            if dep_name:
                visit(dep_name)
        visiting.remove(name)
        visited.add(name)
        ordered.append(name)

    visit(feature_name)
    return ordered


def run_refresh_feature(root_dir: Path, scripts_dir: Path, llvm_dir: Path, llvm_ref: str,
                        patch_root: Path, feature_config_name: str, feature_name: str) -> None:
    require_llvm_repo(llvm_dir)
    if not feature_name.strip():
        print("ERROR: Missing feature name.")
        print()
        print("Usage:")
        print("  python build.py refresh-feature <feature-name>")
        print()
        print("Available features:")
        for f in list_patch_features(patch_root):
            print(f"  {f}")
        raise SystemExit(1)

    feature_dir = patch_root / feature_name
    if not feature_dir.is_dir():
        print("ERROR: Feature patch directory does not exist:")
        print(f"  {feature_dir}")
        print()
        print("Available features:")
        for f in list_patch_features(patch_root):
            print(f"  {f}")
        raise SystemExit(1)

    print("=== refresh feature plan ===")
    print(f"Feature: {feature_name}")
    print("Action:  reset LLVM, let apply-feature apply missing dependencies, then refresh this feature")
    print()

    run_reset(scripts_dir, llvm_dir, llvm_ref)

    env = os.environ.copy()
    env["LLVM_DIR"] = str(llvm_dir)
    env["REFRESH_PATCHES"] = "1"
    env["APPLY_FEATURE_DEPS"] = "1"
    run([sys.executable, str(scripts_dir / "apply-feature.py"), feature_name], env=env)


def run_build(scripts_dir: Path, llvm_dir: Path, build_dir: Path, build_type: str, jobs: str, build_target_triple: str, interactive: bool) -> None:
    require_llvm_repo(llvm_dir)
    env = os.environ.copy()
    env["BUILD_TARGET_TRIPLE"] = build_target_triple
    args = [sys.executable, str(scripts_dir / "build-llvm.py"), str(llvm_dir), str(build_dir), build_type, str(jobs)]
    if interactive:
        args.append("--interactive")
    run(args, env=env)


def run_install_path(scripts_dir: Path, llvm_dir: Path, build_dir: Path) -> None:
    require_llvm_repo(llvm_dir)
    env = os.environ.copy()
    env["BUILD_DIR"] = str(build_dir)
    run([sys.executable, str(scripts_dir / "install-clang-mg.py"), str(build_dir)], env=env)


def run_save_feature(scripts_dir: Path, llvm_dir: Path, llvm_ref: str, feature_name: str) -> None:
    require_llvm_repo(llvm_dir)
    if not feature_name.strip():
        print("ERROR: Missing feature name.")
        print()
        print("Usage:")
        print("  python build.py save <feature-name>")
        print()
        print("Example:")
        print("  python build.py save curlinclude")
        raise SystemExit(1)
    cp = git(["-C", str(llvm_dir), "rev-parse", "--verify", f"origin/{llvm_ref}"], check=False, quiet=True)
    base = f"origin/{llvm_ref}" if cp.returncode == 0 else llvm_ref
    run([sys.executable, str(scripts_dir / "save-feature.py"), feature_name, base], cwd=llvm_dir)


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
    patch_root = root_dir / "patches"
    feature_config_name = env_or_default("FEATURE_CONFIG_NAME", "feature.conf")

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

    if command in {"help", "-h", "--help"}:
        usage(root_dir)
    elif command == "clone":
        run_clone(scripts_dir, llvm_url, llvm_ref, llvm_dir)
    elif command == "update":
        run_update(scripts_dir, llvm_url, llvm_ref, work_dir, llvm_dir)
    elif command == "reset":
        run_reset(scripts_dir, llvm_dir, llvm_ref)
    elif command == "apply":
        run_apply_features(root_dir, scripts_dir, llvm_dir, rest)
    elif command == "refresh-feature":
        feature_name = rest[0] if rest else ""
        run_refresh_feature(root_dir, scripts_dir, llvm_dir, llvm_ref, patch_root, feature_config_name, feature_name)
    elif command == "enable":
        run_set_feature_enabled(patch_root, feature_config_name, "1", rest)
    elif command == "disable":
        run_set_feature_enabled(patch_root, feature_config_name, "0", rest)
    elif command == "build":
        run_build(scripts_dir, llvm_dir, build_dir, build_type, jobs, build_target_triple, interactive)
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
    elif command == "save":
        feature_name = rest[0] if rest else ""
        run_save_feature(scripts_dir, llvm_dir, llvm_ref, feature_name)
    else:
        print(f"ERROR: Unknown command: {command}")
        print()
        usage(root_dir)
        return 1

    print()
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
