#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

CLANG_SUFFIX="${CLANG_SUFFIX:-mg}"
WORK_DIR="${WORK_DIR:-$ROOT_DIR/work}"
BUILD_DIR="${BUILD_DIR:-$WORK_DIR/build}"

PROFILE_FILE="${PROFILE_FILE:-}"

MARKER_BEGIN="# >>> clang-mg path >>>"
MARKER_END="# <<< clang-mg path <<<"
LEGACY_MARKER="# Added by build-clang-mg.sh"

usage() {
    cat <<EOF
Usage:
  scripts/install-clang-mg.sh [clang-mg-executable | build-dir | bin-dir]

Examples:
  scripts/install-clang-mg.sh
  scripts/install-clang-mg.sh work/build/bin/clang-mg
  scripts/install-clang-mg.sh work/build
  scripts/install-clang-mg.sh work/build/bin

Environment variables:
  CLANG_SUFFIX=mg
  BUILD_DIR=$BUILD_DIR
  PROFILE_FILE=<custom shell profile path>
EOF
}

has_cmd() {
    command -v "$1" >/dev/null 2>&1
}

detect_shell_profile() {
    local shell_name
    shell_name="$(basename "${SHELL:-}")"

    case "$shell_name" in
        zsh)
            echo "${HOME}/.zshrc"
            ;;
        bash)
            if [[ "$(uname -s)" == "Darwin" ]]; then
                echo "${HOME}/.bash_profile"
            else
                echo "${HOME}/.bashrc"
            fi
            ;;
        fish)
            echo "${HOME}/.config/fish/config.fish"
            ;;
        *)
            echo "${HOME}/.profile"
            ;;
    esac
}

resolve_exe_path() {
    local input_path="${1:-}"

    if [[ -z "$input_path" ]]; then
        echo "$BUILD_DIR/bin/clang-${CLANG_SUFFIX}"
        return 0
    fi

    if [[ -d "$input_path/bin" && -x "$input_path/bin/clang-${CLANG_SUFFIX}" ]]; then
        echo "$input_path/bin/clang-${CLANG_SUFFIX}"
        return 0
    fi

    if [[ -d "$input_path" && -x "$input_path/clang-${CLANG_SUFFIX}" ]]; then
        echo "$input_path/clang-${CLANG_SUFFIX}"
        return 0
    fi

    echo "$input_path"
}

make_absolute_path() {
    local path="$1"
    local dir
    local file

    dir="$(dirname "$path")"
    file="$(basename "$path")"

    if [[ ! -d "$dir" ]]; then
        echo "$path"
        return 0
    fi

    dir="$(cd "$dir" && pwd -P)"
    echo "$dir/$file"
}

remove_old_blocks() {
    local profile_file="$1"
    local tmp_file
    local shell_name

    shell_name="$(basename "${SHELL:-}")"
    tmp_file="$(mktemp)"

    awk \
        -v begin="$MARKER_BEGIN" \
        -v end="$MARKER_END" \
        -v legacy="$LEGACY_MARKER" \
        -v shell_name="$shell_name" '
        BEGIN {
            in_managed = 0
            in_legacy = 0
            legacy_depth = 0
        }

        $0 == begin {
            in_managed = 1
            next
        }

        $0 == end {
            in_managed = 0
            next
        }

        in_managed {
            next
        }

        $0 == legacy {
            in_legacy = 1
            legacy_depth = 0
            next
        }

        in_legacy {
            if (shell_name == "fish") {
                if ($0 ~ /^[[:space:]]*if[[:space:]]+/) {
                    legacy_depth++
                }

                if ($0 ~ /^[[:space:]]*end[[:space:]]*$/) {
                    legacy_depth--

                    if (legacy_depth <= 0) {
                        in_legacy = 0
                    }
                }

                next
            }

            if ($0 ~ /^[[:space:]]*if[[:space:]]+\[/) {
                legacy_depth = 1
            }

            if ($0 ~ /^[[:space:]]*fi[[:space:]]*$/ && legacy_depth == 1) {
                in_legacy = 0
                next
            }

            next
        }

        {
            print
        }
    ' "$profile_file" > "$tmp_file"

    mv "$tmp_file" "$profile_file"
}

append_path_block() {
    local profile_file="$1"
    local bin_dir="$2"
    local shell_name

    shell_name="$(basename "${SHELL:-}")"

    if [[ "$shell_name" == "fish" ]]; then
        cat >> "$profile_file" <<EOF

$MARKER_BEGIN
# Added by install-clang-mg.sh
if test -d "$bin_dir"
    if not contains "$bin_dir" \$PATH
        set -gx PATH "$bin_dir" \$PATH
    end
end
$MARKER_END
EOF
    else
        cat >> "$profile_file" <<EOF

$MARKER_BEGIN
# Added by install-clang-mg.sh
if [ -d "$bin_dir" ]; then
    case ":\$PATH:" in
        *":$bin_dir:"*) ;;
        *) export PATH="$bin_dir:\$PATH" ;;
    esac
fi
$MARKER_END
EOF
    fi
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

exe_path="$(resolve_exe_path "${1:-}")"
exe_path="$(make_absolute_path "$exe_path")"

if [[ ! -x "$exe_path" ]]; then
    echo "ERROR: Could not find executable clang-${CLANG_SUFFIX}:"
    echo "  $exe_path"
    echo
    echo "Build clang-mg first, or pass the executable/build directory:"
    echo "  scripts/install-clang-mg.sh work/build/bin/clang-${CLANG_SUFFIX}"
    echo "  scripts/install-clang-mg.sh work/build"
    exit 1
fi

bin_dir="$(cd "$(dirname "$exe_path")" && pwd -P)"

if [[ -z "$PROFILE_FILE" ]]; then
    PROFILE_FILE="$(detect_shell_profile)"
fi

mkdir -p "$(dirname "$PROFILE_FILE")"
touch "$PROFILE_FILE"

echo "=== install clang-mg ==="
echo "Executable:   $exe_path"
echo "Binary dir:   $bin_dir"
echo "Profile file: $PROFILE_FILE"
echo

echo "Checking clang-mg..."
"$exe_path" --version || true

echo
echo "Removing old clang-mg PATH block if present..."
remove_old_blocks "$PROFILE_FILE"

echo "Adding updated clang-mg PATH block..."
append_path_block "$PROFILE_FILE" "$bin_dir"

echo
echo "Installed clang-mg PATH entry."
echo
echo "Open a new terminal, or run:"
echo "  export PATH=\"$bin_dir:\$PATH\""
echo
echo "Then check:"
echo "  clang-${CLANG_SUFFIX} --version"