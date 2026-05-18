#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

WORK_DIR="${WORK_DIR:-$ROOT_DIR/work}"
LLVM_DIR="${LLVM_DIR:-$WORK_DIR/llvm-project}"

FEATURE_NAME="${1:-}"

# Set to 0 if you want to apply patches without rewriting the patch files.
REFRESH_PATCHES="${REFRESH_PATCHES:-1}"

usage() {
    cat <<EOF
Usage:
  scripts/apply-feature.sh <feature-name>

Examples:
  scripts/apply-feature.sh core
  scripts/apply-feature.sh curlinclude
  scripts/apply-feature.sh if-constexpr-members

Environment variables:
  LLVM_DIR=$LLVM_DIR
  WORK_DIR=$WORK_DIR
  REFRESH_PATCHES=$REFRESH_PATCHES

Conflict workflow:
  If git am hits a conflict, this script will pause.
  Resolve the conflict, run git add on the fixed files, then choose continue.
  After all patches apply, the feature patch directory is refreshed automatically.
EOF
}

list_features() {
    echo "Available features:"

    if [ ! -d "$ROOT_DIR/patches" ]; then
        echo "  No patches directory found."
        return
    fi

    find "$ROOT_DIR/patches" \
        -mindepth 1 \
        -maxdepth 1 \
        -type d \
        -exec basename {} \; \
        | sort \
        | sed 's/^/  /'
}

show_conflict_help() {
    cat <<EOF

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

EOF
}

open_shell() {
    echo
    echo "Opening a shell in:"
    echo "  $LLVM_DIR"
    echo
    echo "When done resolving conflicts, exit the shell to return here."
    echo

    "${SHELL:-/bin/bash}" < /dev/tty > /dev/tty 2>&1
}

continue_am() {
    echo
    echo "Continuing git am..."
    if git am --continue; then
        echo "git am completed successfully."
        return 0
    fi

    echo
    echo "git am still needs attention."
    return 1
}

skip_am() {
    echo
    echo "Skipping current patch..."
    if git am --skip; then
        echo "Patch skipped and git am completed successfully."
        return 0
    fi

    echo
    echo "git am still needs attention."
    return 1
}

interactive_am_resolution() {
    show_conflict_help

    if [ ! -r /dev/tty ] || [ ! -w /dev/tty ]; then
        echo "ERROR: No interactive terminal is available."
        echo
        echo "Resolve manually inside LLVM with:"
        echo "  git status"
        echo "  git diff --name-only --diff-filter=U"
        echo "  git add <files>"
        echo "  git am --continue"
        echo
        echo "Or abort with:"
        echo "  git am --abort"
        return 1
    fi

    while [ -d ".git/rebase-apply" ]; do
        cat > /dev/tty <<EOF

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

EOF

        printf "Choose an action: " > /dev/tty

        if ! IFS= read -r choice < /dev/tty; then
            echo
            echo "ERROR: Could not read from terminal."
            echo "Leaving git am in progress so you can resolve it manually."
            return 1
        fi

        case "$choice" in
            s)
                git status
                ;;
            u)
                git diff --name-only --diff-filter=U
                ;;
            p)
                git am --show-current-patch=diff | ${PAGER:-less}
                ;;
            d)
                git diff
                ;;
            a)
                git add -A
                echo "Staged all changes."
                ;;
            c)
                if continue_am; then
                    return 0
                fi
                ;;
            k)
                if skip_am; then
                    return 0
                fi
                ;;
            x)
                echo
                echo "Aborting git am..."
                git am --abort
                return 1
                ;;
            sh)
                open_shell
                ;;
            "")
                echo "No option entered."
                ;;
            *)
                echo "Unknown option: $choice"
                ;;
        esac
    done

    return 0
}

refresh_feature_patches() {
    local start_commit="$1"

    if [ "$REFRESH_PATCHES" != "1" ]; then
        echo
        echo "Skipping patch refresh because REFRESH_PATCHES=$REFRESH_PATCHES"
        return 0
    fi

    echo
    echo "Refreshing feature patches from applied commits..."

    local tmp_dir
    tmp_dir="$(mktemp -d "$ROOT_DIR/.patch-refresh-${FEATURE_NAME}.XXXXXX")"

    if ! git format-patch --no-stat --output-directory "$tmp_dir" "$start_commit..HEAD" >/dev/null; then
        echo "ERROR: Failed to regenerate patches."
        rm -rf "$tmp_dir"
        return 1
    fi

    if ! compgen -G "$tmp_dir/*.patch" > /dev/null; then
        echo "ERROR: No regenerated patches were produced."
        rm -rf "$tmp_dir"
        return 1
    fi

    local backup_dir
    backup_dir="$PATCH_DIR.backup.$(date +%Y%m%d-%H%M%S)"

    mkdir -p "$backup_dir"
    cp "$PATCH_DIR"/*.patch "$backup_dir"/

    rm -f "$PATCH_DIR"/*.patch
    cp "$tmp_dir"/*.patch "$PATCH_DIR"/
    rm -rf "$tmp_dir"

    echo
    echo "Updated patch collection:"
    echo "  $PATCH_DIR"
    echo
    echo "Backup of old patches:"
    echo "  $backup_dir"
}

if [ "$FEATURE_NAME" = "-h" ] || [ "$FEATURE_NAME" = "--help" ] || [ -z "$FEATURE_NAME" ]; then
    usage
    echo
    list_features
    exit 0
fi

if [ "$FEATURE_NAME" = "list" ] || [ "$FEATURE_NAME" = "--list" ]; then
    list_features
    exit 0
fi

PATCH_DIR="$ROOT_DIR/patches/$FEATURE_NAME"

echo "=== apply feature ==="
echo "Feature:   $FEATURE_NAME"
echo "Patch dir: $PATCH_DIR"
echo "LLVM dir:  $LLVM_DIR"
echo

if [ ! -d "$LLVM_DIR/.git" ]; then
    echo "ERROR: LLVM repo is not cloned:"
    echo "$LLVM_DIR"
    echo
    echo "Run:"
    echo "  ./build.sh clone"
    echo
    echo "or:"
    echo "  ./build.sh bootstrap"
    exit 1
fi

if [ ! -d "$PATCH_DIR" ]; then
    echo "ERROR: Feature patch directory does not exist:"
    echo "$PATCH_DIR"
    echo
    list_features
    exit 1
fi

if ! compgen -G "$PATCH_DIR/*.patch" > /dev/null; then
    echo "ERROR: No .patch files found in:"
    echo "$PATCH_DIR"
    exit 1
fi

cd "$LLVM_DIR"

if [ -d ".git/rebase-merge" ]; then
    echo "ERROR: A rebase is currently in progress."
    echo "Finish or abort it before applying feature patches."
    exit 1
fi

if [ -d ".git/rebase-apply" ]; then
    echo "ERROR: A patch application or rebase is already in progress."
    echo
    echo "Run one of these inside LLVM first:"
    echo "  git am --continue"
    echo "  git am --abort"
    echo "  git rebase --continue"
    echo "  git rebase --abort"
    exit 1
fi

if [ -f ".git/MERGE_HEAD" ]; then
    echo "ERROR: A merge is currently in progress."
    echo "Finish or abort it before applying feature patches."
    exit 1
fi

if [ -f ".git/CHERRY_PICK_HEAD" ]; then
    echo "ERROR: A cherry-pick is currently in progress."
    echo "Finish or abort it before applying feature patches."
    exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
    echo "ERROR: LLVM has uncommitted changes."
    echo
    echo "Save or commit your current work before applying feature patches."
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

mapfile -t PATCHES < <(find "$PATCH_DIR" -maxdepth 1 -type f -name "*.patch" | sort)

START_COMMIT="$(git rev-parse HEAD)"

echo "Applying feature patches..."

if ! git am --3way "${PATCHES[@]}"; then
    if [ -d ".git/rebase-apply" ]; then
        if ! interactive_am_resolution; then
            echo
            echo "Apply cancelled."
            exit 1
        fi
    else
        echo
        echo "ERROR: git am failed, but no patch application state was found."
        exit 1
    fi
fi

echo
echo "Applied feature successfully: $FEATURE_NAME"

refresh_feature_patches "$START_COMMIT"

echo
echo "Recent commits:"
git --no-pager log --oneline -5