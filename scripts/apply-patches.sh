#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LLVM_DIR="${2:-$ROOT_DIR/work/llvm-project}"

PATCH_ROOT="$ROOT_DIR/patches"
APPLY_FEATURE_SCRIPT="$ROOT_DIR/scripts/apply-feature.sh"
FEATURE_CONFIG_NAME="${FEATURE_CONFIG_NAME:-feature.conf}"

declare -A FEATURE_DIRS
declare -A FEATURE_HAS_PATCHES
declare -A FEATURE_ENABLED
declare -A FEATURE_DEPS
declare -A FEATURE_BEFORE
declare -A FEATURE_IN_DEGREE
declare -A FEATURE_EDGES
declare -A ENABLED_FEATURE_MAP

FEATURES=()
ENABLED_FEATURES=()
ORDERED_FEATURES=()
QUEUE=()

echo "=== apply all clang-mg patches ==="
echo "Root dir:   $ROOT_DIR"
echo "LLVM dir:   $LLVM_DIR"
echo "Patch root: $PATCH_ROOT"
echo

if [ ! -d "$LLVM_DIR/.git" ]; then
    echo "ERROR: LLVM repo is not cloned:"
    echo "$LLVM_DIR"
    exit 1
fi

if [ ! -d "$PATCH_ROOT" ]; then
    echo "ERROR: Patch directory does not exist:"
    echo "$PATCH_ROOT"
    exit 1
fi

if [ ! -x "$APPLY_FEATURE_SCRIPT" ]; then
    echo "ERROR: apply-feature script not found or not executable:"
    echo "$APPLY_FEATURE_SCRIPT"
    echo
    echo "Try:"
    echo "  chmod +x \"$APPLY_FEATURE_SCRIPT\""
    exit 1
fi

cd "$LLVM_DIR"

if [ -n "$(git status --porcelain)" ]; then
    echo "ERROR: LLVM has uncommitted changes."
    echo
    echo "Save, commit, or reset your changes before applying patches."
    echo
    echo "Useful commands:"
    echo "  git status"
    echo "  git diff"
    echo "  git add ."
    echo "  git commit -m \"clang-mg: describe current work\""
    echo
    echo "Apply cancelled."
    exit 1
fi

find_feature_dirs() {
    find "$PATCH_ROOT" \
        -mindepth 1 \
        -maxdepth 1 \
        -type d \
        ! -name "*.backup.*" \
        ! -name "*.bak.*" \
        ! -name "*.old.*" \
        ! -name ".backup" \
        ! -name ".backups" \
        ! -name "backup" \
        ! -name "backups" \
        ! -name ".patch-refresh-*" \
        ! -name "*~" \
        | sort
}

find_ignored_patch_dirs() {
    find "$PATCH_ROOT" \
        -mindepth 1 \
        -maxdepth 1 \
        -type d \
        \( \
            -name "*.backup.*" \
            -o -name "*.bak.*" \
            -o -name "*.old.*" \
            -o -name ".backup" \
            -o -name ".backups" \
            -o -name "backup" \
            -o -name "backups" \
            -o -name ".patch-refresh-*" \
            -o -name "*~" \
        \) \
        | sort
}

is_enabled_value() {
    case "$1" in
        1|true|TRUE|yes|YES|on|ON|enabled|ENABLED)
            return 0
            ;;
        0|false|FALSE|no|NO|off|OFF|disabled|DISABLED)
            return 1
            ;;
        *)
            echo "ERROR: Invalid ENABLED value: $1"
            echo "Use one of: 1, 0, true, false, yes, no, on, off"
            exit 1
            ;;
    esac
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

load_feature_configs() {
    local feature_dir
    local feature_name
    local config_file

    local ignored_dirs=()
    mapfile -t ignored_dirs < <(find_ignored_patch_dirs)

    if [ "${#ignored_dirs[@]}" -gt 0 ]; then
        echo "Ignoring backup/temp patch directories:"
        printf '  %s\n' "${ignored_dirs[@]}"
        echo
    fi

    while IFS= read -r feature_dir; do
        feature_name="$(basename "$feature_dir")"
        config_file="$feature_dir/$FEATURE_CONFIG_NAME"

        if [[ "$feature_name" =~ [[:space:]] ]]; then
            echo "ERROR: Feature directory names cannot contain whitespace:"
            echo "  $feature_name"
            exit 1
        fi

        if [ ! -f "$config_file" ]; then
            echo "Generating missing config:"
            echo "  $config_file"
            write_default_feature_config "$config_file" "$feature_name"
        fi

        FEATURES+=("$feature_name")
        FEATURE_DIRS["$feature_name"]="$feature_dir"

        if compgen -G "$feature_dir/*.patch" > /dev/null; then
            FEATURE_HAS_PATCHES["$feature_name"]=1
        else
            FEATURE_HAS_PATCHES["$feature_name"]=0
        fi

        ENABLED=1
        DEPENDS=()
        BEFORE=()

        # shellcheck source=/dev/null
        source "$config_file"

        if is_enabled_value "$ENABLED"; then
            FEATURE_ENABLED["$feature_name"]=1
        else
            FEATURE_ENABLED["$feature_name"]=0
        fi

        FEATURE_DEPS["$feature_name"]="${DEPENDS[*]:-}"
        FEATURE_BEFORE["$feature_name"]="${BEFORE[*]:-}"

    done < <(find_feature_dirs)
}

sort_queue() {
    local sorted=()

    if [ "${#QUEUE[@]}" -eq 0 ]; then
        return 0
    fi

    while IFS= read -r item; do
        sorted+=("$item")
    done < <(printf '%s\n' "${QUEUE[@]}" | sort)

    QUEUE=("${sorted[@]}")
}

add_edge() {
    local from="$1"
    local to="$2"

    FEATURE_EDGES["$from"]="${FEATURE_EDGES[$from]:-} $to"
    FEATURE_IN_DEGREE["$to"]=$(( ${FEATURE_IN_DEGREE[$to]} + 1 ))
}

validate_dependency() {
    local feature="$1"
    local dep="$2"

    if [ -z "${FEATURE_DIRS[$dep]+set}" ]; then
        echo "ERROR: Feature '$feature' depends on unknown feature '$dep'."
        echo
        echo "Known features:"
        printf '  %s\n' "${FEATURES[@]}"
        exit 1
    fi

    if [ "${FEATURE_HAS_PATCHES[$dep]}" != "1" ]; then
        echo "ERROR: Feature '$feature' depends on '$dep', but '$dep' has no .patch files."
        exit 1
    fi

    if [ "${FEATURE_ENABLED[$dep]}" != "1" ]; then
        echo "ERROR: Feature '$feature' depends on '$dep', but '$dep' is disabled."
        echo
        echo "Either enable '$dep' or disable '$feature'."
        exit 1
    fi
}

build_feature_order() {
    local feature
    local dep
    local before
    local next
    local remaining=0

    for feature in "${FEATURES[@]}"; do
        if [ "${FEATURE_HAS_PATCHES[$feature]}" != "1" ]; then
            echo "Skipping feature with no .patch files: $feature"
            continue
        fi

        if [ "${FEATURE_ENABLED[$feature]}" != "1" ]; then
            echo "Skipping disabled feature: $feature"
            continue
        fi

        ENABLED_FEATURES+=("$feature")
        ENABLED_FEATURE_MAP["$feature"]=1
        FEATURE_IN_DEGREE["$feature"]=0
        FEATURE_EDGES["$feature"]=""
    done

    for feature in "${ENABLED_FEATURES[@]}"; do
        for dep in ${FEATURE_DEPS[$feature]:-}; do
            validate_dependency "$feature" "$dep"
            add_edge "$dep" "$feature"
        done

        for before in ${FEATURE_BEFORE[$feature]:-}; do
            if [ -z "${FEATURE_DIRS[$before]+set}" ]; then
                echo "ERROR: Feature '$feature' has BEFORE entry for unknown feature '$before'."
                exit 1
            fi

            if [ -z "${ENABLED_FEATURE_MAP[$before]+set}" ]; then
                echo "NOTE: '$feature' says it should run before '$before', but '$before' is not enabled. Ignoring."
                continue
            fi

            add_edge "$feature" "$before"
        done
    done

    for feature in "${ENABLED_FEATURES[@]}"; do
        if [ "${FEATURE_IN_DEGREE[$feature]}" -eq 0 ]; then
            QUEUE+=("$feature")
        fi
    done

    sort_queue

    while [ "${#QUEUE[@]}" -gt 0 ]; do
        feature="${QUEUE[0]}"
        QUEUE=("${QUEUE[@]:1}")

        ORDERED_FEATURES+=("$feature")

        for next in ${FEATURE_EDGES[$feature]:-}; do
            FEATURE_IN_DEGREE["$next"]=$(( ${FEATURE_IN_DEGREE[$next]} - 1 ))

            if [ "${FEATURE_IN_DEGREE[$next]}" -eq 0 ]; then
                QUEUE+=("$next")
                sort_queue
            fi
        done
    done

    if [ "${#ORDERED_FEATURES[@]}" -ne "${#ENABLED_FEATURES[@]}" ]; then
        echo "ERROR: Dependency cycle detected between enabled features."
        echo
        echo "Features still blocked:"

        for feature in "${ENABLED_FEATURES[@]}"; do
            if [ "${FEATURE_IN_DEGREE[$feature]}" -gt 0 ]; then
                echo "  $feature"
                remaining=1
            fi
        done

        if [ "$remaining" -eq 0 ]; then
            echo "  unknown"
        fi

        exit 1
    fi
}

apply_loose_patches() {
    local loose_patches=()

    mapfile -t loose_patches < <(
        find "$PATCH_ROOT" \
            -maxdepth 1 \
            -type f \
            -name "*.patch" \
            ! -name "*.backup.patch" \
            ! -name "*.bak.patch" \
            ! -name "*.old.patch" \
            ! -name "*~" \
            | sort
    )

    if [ "${#loose_patches[@]}" -eq 0 ]; then
        echo "No loose top-level patches found."
        return 0
    fi

    echo
    echo "Applying loose top-level patches from:"
    echo "$PATCH_ROOT"

    git am --3way "${loose_patches[@]}"
}

apply_ordered_features() {
    local feature_name

    echo
    echo "Feature apply order:"

    if [ "${#ORDERED_FEATURES[@]}" -eq 0 ]; then
        echo "  No enabled feature patch directories found."
        return 0
    fi

    printf '  %s\n' "${ORDERED_FEATURES[@]}"

    for feature_name in "${ORDERED_FEATURES[@]}"; do
        echo
        echo "Applying feature: $feature_name"

        LLVM_DIR="$LLVM_DIR" \
            "$APPLY_FEATURE_SCRIPT" "$feature_name"
    done
}

load_feature_configs
build_feature_order

apply_loose_patches
apply_ordered_features

echo
echo "All enabled clang-mg patches applied."