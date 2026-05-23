#!/usr/bin/env bash
set -euo pipefail

LLVM_DIR="${1:-}"
BUILD_DIR="${2:-}"
BUILD_TYPE="${3:-Release}"
JOBS="${4:-$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 4)}"

INTERACTIVE=0

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

shift $(( $# >= 4 ? 4 : $# ))

while [[ $# -gt 0 ]]; do
    case "$1" in
        --interactive)
            INTERACTIVE=1
            shift
            ;;
        -h|--help)
            echo "Usage: $0 <llvm-dir> [build-dir] [build-type] [jobs] [--interactive]"
            echo
            echo "Examples:"
            echo "  $0 work/llvm-project"
            echo "  $0 work/llvm-project work/build-x86_64-pc-linux-gnu"
            echo "  $0 work/llvm-project work/build-x86_64-pc-linux-gnu Release 8"
            echo "  $0 work/llvm-project work/build-x86_64-pc-linux-gnu Release 8 --interactive"
            exit 0
            ;;
        *)
            echo "ERROR: Unknown option: $1"
            exit 1
            ;;
    esac
done

if [[ -z "$LLVM_DIR" ]]; then
    echo "ERROR: Missing LLVM source directory."
    echo
    echo "Usage:"
    echo "  $0 <llvm-dir> [build-dir] [build-type] [jobs] [--interactive]"
    exit 1
fi

if [[ ! -d "$LLVM_DIR/llvm" ]]; then
    echo "ERROR: Could not find LLVM source directory:"
    echo "  $LLVM_DIR/llvm"
    exit 1
fi

BUILD_TARGET_TRIPLE="${BUILD_TARGET_TRIPLE:-$(detect_target_triple)}"

if [[ -z "$BUILD_DIR" ]]; then
    LLVM_PARENT_DIR="$(cd "$(dirname "$LLVM_DIR")" && pwd)"
    BUILD_DIR="$LLVM_PARENT_DIR/build-$BUILD_TARGET_TRIPLE"
fi

prompt_debug_build() {
    local answer

    if [[ "$INTERACTIVE" -ne 1 ]]; then
        return 0
    fi

    if [[ ! -t 0 ]]; then
        echo "Interactive mode requested, but stdin is not a terminal. Using Release build."
        BUILD_TYPE="Release"
        return 0
    fi

    echo
    printf "Build an unoptimized Debug build instead of optimized Release? [y/N]: "
    read -r answer || answer=""

    case "$answer" in
        y|Y|yes|YES|Yes)
            BUILD_TYPE="Debug"
            ;;
        *)
            BUILD_TYPE="Release"
            ;;
    esac
}

prompt_debug_build

if has_cmd ninja; then
    GENERATOR="Ninja"
elif has_cmd ninja-build; then
    GENERATOR="Ninja"
else
    echo "ERROR: Ninja was not found."
    echo "Please install ninja or ninja-build."
    exit 1
fi

CMAKE_CONFIGURE_ARGS=(
    -G "$GENERATOR"
    "-DLLVM_ENABLE_PROJECTS=clang"
    "-DCMAKE_BUILD_TYPE=$BUILD_TYPE"
    "-DLLVM_ENABLE_ASSERTIONS=ON"
)

if [[ "$(uname -s)" == "Darwin" ]]; then
    CMAKE_CONFIGURE_ARGS+=(
        "-DCLANG_USE_XCSELECT=ON"
    )
fi

echo
echo "Configuring LLVM build..."
echo "LLVM dir:      $LLVM_DIR"
echo "Target triple: $BUILD_TARGET_TRIPLE"
echo "Build dir:     $BUILD_DIR"
echo "Build type:    $BUILD_TYPE"
echo "Jobs:          $JOBS"
echo "CMake args:    ${CMAKE_CONFIGURE_ARGS[*]}"
echo

cmake -S "$LLVM_DIR/llvm" -B "$BUILD_DIR" \
    "${CMAKE_CONFIGURE_ARGS[@]}"

echo
echo "Building clang..."

cmake --build "$BUILD_DIR" --target clang -- -j "$JOBS"

echo
echo "Build complete."

if [[ -x "$BUILD_DIR/bin/clang-mg" ]]; then
    echo "Built: $BUILD_DIR/bin/clang-mg"
    "$BUILD_DIR/bin/clang-mg" --version || true
elif [[ -x "$BUILD_DIR/bin/clang" ]]; then
    echo "Built: $BUILD_DIR/bin/clang"
    "$BUILD_DIR/bin/clang" --version || true
else
    echo "WARNING: Build finished, but no clang or clang-mg binary was found in:"
    echo "  $BUILD_DIR/bin"
fi