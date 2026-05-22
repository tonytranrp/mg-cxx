#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

has_cmd() {
    command -v "$1" >/dev/null 2>&1
}

detect_target_triple() {
    if [[ -n "${BUILD_TARGET_TRIPLE:-}" ]]; then
        echo "$BUILD_TARGET_TRIPLE"
        return 0
    fi

    local triple=""

    if has_cmd clang; then
        triple="$(clang -dumpmachine 2>/dev/null || true)"
    elif has_cmd cc; then
        triple="$(cc -dumpmachine 2>/dev/null || true)"
    fi

    if [[ -n "$triple" ]]; then
        echo "$triple"
        return 0
    fi

    local os
    local arch

    os="$(uname -s)"
    arch="$(uname -m)"

    case "$os" in
        Darwin)
            case "$arch" in
                arm64|aarch64)
                    echo "arm64-apple-darwin"
                    ;;
                x86_64)
                    echo "x86_64-apple-darwin"
                    ;;
                *)
                    echo "$arch-apple-darwin"
                    ;;
            esac
            ;;

        Linux)
            case "$arch" in
                x86_64)
                    echo "x86_64-pc-linux-gnu"
                    ;;
                aarch64|arm64)
                    echo "aarch64-unknown-linux-gnu"
                    ;;
                armv7l)
                    echo "armv7-unknown-linux-gnueabihf"
                    ;;
                *)
                    echo "$arch-unknown-linux-gnu"
                    ;;
            esac
            ;;

        MINGW*|MSYS*|CYGWIN*)
            case "$arch" in
                x86_64)
                    echo "x86_64-pc-windows-msvc"
                    ;;
                aarch64|arm64)
                    echo "aarch64-pc-windows-msvc"
                    ;;
                *)
                    echo "$arch-pc-windows-msvc"
                    ;;
            esac
            ;;

        *)
            echo "$arch-unknown-$os"
            ;;
    esac
}

LLVM_URL="${LLVM_URL:-https://github.com/llvm/llvm-project.git}"
LLVM_REF="${LLVM_REF:-main}"

WORK_DIR="${WORK_DIR:-$ROOT_DIR/work}"
LLVM_DIR="${LLVM_DIR:-$WORK_DIR/llvm-project}"

BUILD_TARGET_TRIPLE="${BUILD_TARGET_TRIPLE:-$(detect_target_triple)}"
BUILD_DIR="${BUILD_DIR:-$WORK_DIR/build-$BUILD_TARGET_TRIPLE}"

BUILD_TYPE="${BUILD_TYPE:-Release}"
JOBS="${JOBS:-$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 4)}"

CLONE_SCRIPT="$ROOT_DIR/scripts/clone-llvm.sh"
UPDATE_SCRIPT="$ROOT_DIR/scripts/update-llvm.sh"
RESET_SCRIPT="$ROOT_DIR/scripts/reset-llvm.sh"
APPLY_PATCHES_SCRIPT="$ROOT_DIR/scripts/apply-patches.sh"
APPLY_FEATURE_SCRIPT="$ROOT_DIR/scripts/apply-feature.sh"
BUILD_LLVM_SCRIPT="$ROOT_DIR/scripts/build-llvm.sh"
SAVE_FEATURE_SCRIPT="$ROOT_DIR/scripts/save-feature.sh"
INSTALL_SCRIPT="$ROOT_DIR/scripts/install-clang-mg.sh"

PATCH_ROOT="$ROOT_DIR/patches"
FEATURE_CONFIG_NAME="${FEATURE_CONFIG_NAME:-feature.conf}"

COMMAND="${1:-bootstrap}"

INTERACTIVE="${INTERACTIVE:-0}"

if [[ "${2:-}" == "--interactive" || "${1:-}" == "--interactive" ]]; then
    INTERACTIVE=1
fi

print_header() {
    echo "=== clang-mg ==="
    echo "Command:       $COMMAND"
    echo "LLVM ref:      $LLVM_REF"
    echo "LLVM dir:      $LLVM_DIR"
    echo "Target triple: $BUILD_TARGET_TRIPLE"
    echo "Build dir:     $BUILD_DIR"
    echo "Build type:    $BUILD_TYPE"
    echo "Jobs:          $JOBS"
    echo
}

usage() {
    cat <<EOF
Usage:
  ./build.sh [command]

Commands:
  bootstrap                  Clone/update LLVM if needed, apply all enabled patches, then build
  install                    Clone/update LLVM, reset clean, apply all enabled patches, build, then add clang-mg to PATH
  clone                      Clone LLVM only
  update                     Update LLVM only if the checkout is clean
  reset                      Reset LLVM checkout to LLVM_REF / origin ref
  apply                      Apply all enabled clang-mg patches
  apply <feature-name...>    Apply one or more specific feature patch stacks
  enable <feature-name...>   Enable one or more feature patch stacks
  disable <feature-name...>  Disable one or more feature patch stacks
  build                      Build current LLVM tree only
  fresh                      Reset LLVM, apply all enabled patches, then build
  rebuild                    Same as fresh
  save <feature-name>        Save current LLVM changes as patches for a feature
  help                       Show this help menu

Examples:
  ./build.sh
  ./build.sh bootstrap
  ./build.sh install
  ./build.sh apply
  ./build.sh apply change-bin-name
  ./build.sh apply change-bin-name curlinclude
  ./build.sh enable if-constexpr-members
  ./build.sh disable curlinclude
  ./build.sh enable core if-constexpr-members
  ./build.sh build
  ./build.sh fresh
  ./build.sh save curlinclude

Environment variables:
  LLVM_REF=main
  LLVM_URL=https://github.com/llvm/llvm-project.git
  WORK_DIR=$ROOT_DIR/work
  LLVM_DIR=$ROOT_DIR/work/llvm-project
  BUILD_TARGET_TRIPLE=x86_64-pc-linux-gnu
  BUILD_DIR=$ROOT_DIR/work/build-\$BUILD_TARGET_TRIPLE
  BUILD_TYPE=Debug
  JOBS=4
  FEATURE_CONFIG_NAME=feature.conf
EOF
}

require_llvm_repo() {
    if [ ! -d "$LLVM_DIR/.git" ]; then
        echo "ERROR: LLVM is not cloned yet."
        echo
        echo "Run:"
        echo "  ./build.sh clone"
        echo
        echo "or:"
        echo "  ./build.sh bootstrap"
        exit 1
    fi
}

list_patch_features() {
    if [ ! -d "$PATCH_ROOT" ]; then
        echo "No patches directory found:"
        echo "  $PATCH_ROOT"
        return 0
    fi

    find "$PATCH_ROOT" \
        -mindepth 1 \
        -maxdepth 1 \
        -type d \
        -exec basename {} \; \
        | sort
}

write_default_feature_config() {
    local config_file="$1"
    local feature_name="$2"

    mkdir -p "$(dirname "$config_file")"

    cat > "$config_file" <<EOF
# Auto-generated config for clang-mg feature: $feature_name

# Whether this feature should be applied.
# Valid values: 1, 0, true, false, yes, no, on, off
ENABLED=1

# Features that must be applied before this feature.
# Example:
#   DEPENDS=(core)
DEPENDS=()

# Features that this feature must be applied before.
# Usually DEPENDS is enough, but this is useful for ordering from the other side.
# Example:
#   BEFORE=(if-constexpr-members)
BEFORE=()
EOF
}

ensure_feature_config() {
    local feature_name="$1"
    local feature_dir="$PATCH_ROOT/$feature_name"
    local config_file="$feature_dir/$FEATURE_CONFIG_NAME"

    if [ -z "$feature_name" ]; then
        echo "ERROR: Missing feature name." >&2
        echo >&2
        echo "Usage:" >&2
        echo "  ./build.sh enable <feature-name...>" >&2
        echo "  ./build.sh disable <feature-name...>" >&2
        exit 1
    fi

    if [ ! -d "$feature_dir" ]; then
        echo "ERROR: Feature patch directory does not exist:" >&2
        echo "  $feature_dir" >&2
        echo >&2
        echo "Available features:" >&2
        list_patch_features | sed 's/^/  /' >&2
        exit 1
    fi

    if [ ! -f "$config_file" ]; then
        echo "Generating missing config:" >&2
        echo "  $config_file" >&2
        write_default_feature_config "$config_file" "$feature_name"
    fi

    printf '%s\n' "$config_file"
}

set_feature_enabled() {
    local feature_name="$1"
    local enabled_value="$2"

    local config_file
    config_file="$(ensure_feature_config "$feature_name")"

    local tmp_file
    tmp_file="$(mktemp)"

    awk -v enabled_value="$enabled_value" '
        BEGIN {
            replaced = 0
        }

        /^[[:space:]]*ENABLED[[:space:]]*=/ && replaced == 0 {
            print "ENABLED=" enabled_value
            replaced = 1
            next
        }

        {
            print
        }

        END {
            if (replaced == 0) {
                print ""
                print "# Whether this feature should be applied."
                print "ENABLED=" enabled_value
            }
        }
    ' "$config_file" > "$tmp_file"

    mv "$tmp_file" "$config_file"

    if [ "$enabled_value" = "1" ]; then
        echo "Enabled feature:  $feature_name"
    else
        echo "Disabled feature: $feature_name"
    fi

    echo "Config:"
    echo "  $config_file"
}

run_set_feature_enabled() {
    local enabled_value="$1"
    shift

    if [ "$#" -eq 0 ]; then
        echo "ERROR: Missing feature name."

        if [ "$enabled_value" = "1" ]; then
            echo
            echo "Usage:"
            echo "  ./build.sh enable <feature-name...>"
        else
            echo
            echo "Usage:"
            echo "  ./build.sh disable <feature-name...>"
        fi

        echo
        echo "Available features:"
        list_patch_features | sed 's/^/  /'
        exit 1
    fi

    local feature_name

    for feature_name in "$@"; do
        set_feature_enabled "$feature_name" "$enabled_value"
        echo
    done
}

run_clone() {
    "$CLONE_SCRIPT" \
        "$LLVM_URL" \
        "$LLVM_REF" \
        "$LLVM_DIR"
}

run_update() {
    LLVM_URL="$LLVM_URL" \
    LLVM_REF="$LLVM_REF" \
    WORK_DIR="$WORK_DIR" \
    LLVM_DIR="$LLVM_DIR" \
        "$UPDATE_SCRIPT"
}

resolve_reset_ref() {
    require_llvm_repo

    cd "$LLVM_DIR"

    git fetch origin --tags

    if git show-ref --verify --quiet "refs/remotes/origin/$LLVM_REF"; then
        echo "origin/$LLVM_REF"
    else
        echo "$LLVM_REF"
    fi
}

run_reset() {
    require_llvm_repo

    local reset_ref
    reset_ref="$(resolve_reset_ref)"

    cd "$LLVM_DIR"

    "$RESET_SCRIPT" "$reset_ref"
}

run_apply_patches() {
    require_llvm_repo

    "$APPLY_PATCHES_SCRIPT" \
        "$ROOT_DIR" \
        "$LLVM_DIR"
}

run_apply_features() {
    require_llvm_repo

    if [ "$#" -eq 0 ]; then
        run_apply_patches
        return 0
    fi

    local feature_name

    for feature_name in "$@"; do
        LLVM_DIR="$LLVM_DIR" \
            "$APPLY_FEATURE_SCRIPT" "$feature_name"
    done
}

run_build() {
    require_llvm_repo

    if [[ "$INTERACTIVE" -eq 1 ]]; then
        "$BUILD_LLVM_SCRIPT" \
            "$LLVM_DIR" \
            "$BUILD_DIR" \
            "$BUILD_TYPE" \
            "$JOBS" \
            --interactive
    else
        "$BUILD_LLVM_SCRIPT" \
            "$LLVM_DIR" \
            "$BUILD_DIR" \
            "$BUILD_TYPE" \
            "$JOBS"
    fi
}

run_install_path() {
    require_llvm_repo

    BUILD_DIR="$BUILD_DIR" \
        "$INSTALL_SCRIPT" "$BUILD_DIR"
}

run_save_feature() {
    require_llvm_repo

    local feature_name="${1:-}"

    if [ -z "$feature_name" ]; then
        echo "ERROR: Missing feature name."
        echo
        echo "Usage:"
        echo "  ./build.sh save <feature-name>"
        echo
        echo "Example:"
        echo "  ./build.sh save curlinclude"
        exit 1
    fi

    cd "$LLVM_DIR"

    if git -C "$LLVM_DIR" rev-parse --verify "origin/$LLVM_REF" >/dev/null 2>&1; then
        "$SAVE_FEATURE_SCRIPT" "$feature_name" "origin/$LLVM_REF"
    else
        "$SAVE_FEATURE_SCRIPT" "$feature_name" "$LLVM_REF"
    fi
}

print_header

case "$COMMAND" in
    help|-h|--help)
        usage
        ;;

    clone)
        run_clone
        ;;

    update)
        run_update
        ;;

    reset)
        run_reset
        ;;

    apply)
        shift
        run_apply_features "$@"
        ;;

    enable)
        shift
        run_set_feature_enabled 1 "$@"
        ;;

    disable)
        shift
        run_set_feature_enabled 0 "$@"
        ;;

    build)
        run_build
        ;;

    bootstrap)
        run_update
        run_apply_patches
        run_build
        ;;

    install)
        run_update

        # Make sure we are applying patches onto a clean LLVM base.
        # This prevents accidentally applying the same patch stack twice.
        run_reset

        run_apply_patches
        run_build
        run_install_path
        ;;

    fresh|rebuild)
        if [ ! -d "$LLVM_DIR/.git" ]; then
            run_clone
        else
            run_reset
        fi

        run_apply_patches
        run_build
        ;;

    save)
        shift
        run_save_feature "${1:-}"
        ;;

    *)
        echo "ERROR: Unknown command: $COMMAND"
        echo
        usage
        exit 1
        ;;
esac

echo
echo "Done."