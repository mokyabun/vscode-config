#!/bin/sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TARGET=all
PROFILE=all
DRY_RUN=0
SKIP_EXTENSIONS=0

usage() {
    cat <<'EOF'
Usage: ./apply.sh [options]

Options:
  --target vscode|code-server|all  Target editor (default: all)
  --profile NAME|all              Apply one profile (default: all)
  --dry-run                       Show changes without writing/installing
  --skip-extensions               Do not install extensions
  -h, --help                      Show this help

Environment:
  VSCODE_USER_DIR, CODE_SERVER_USER_DIR
  VSCODE_BIN (default: code), CODE_SERVER_BIN (default: code-server)
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --target)
            [ "$#" -ge 2 ] || { echo "error: --target requires a value" >&2; exit 2; }
            TARGET=$2
            shift 2
            ;;
        --profile)
            [ "$#" -ge 2 ] || { echo "error: --profile requires a value" >&2; exit 2; }
            PROFILE=$2
            shift 2
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --skip-extensions)
            SKIP_EXTENSIONS=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "error: unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

case "$TARGET" in
    vscode|code-server|all) ;;
    *) echo "error: invalid target: $TARGET" >&2; exit 2 ;;
esac

command -v python3 >/dev/null 2>&1 || {
    echo "error: Python 3 is required" >&2
    exit 1
}

case $(uname -s) in
    Darwin)
        DEFAULT_VSCODE_USER_DIR="$HOME/Library/Application Support/Code/User"
        ;;
    *)
        DEFAULT_VSCODE_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/Code/User"
        ;;
esac

DEFAULT_CODE_SERVER_USER_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/code-server/User"
VSCODE_USER_DIR=${VSCODE_USER_DIR:-$DEFAULT_VSCODE_USER_DIR}
CODE_SERVER_USER_DIR=${CODE_SERVER_USER_DIR:-$DEFAULT_CODE_SERVER_USER_DIR}
VSCODE_BIN=${VSCODE_BIN:-code}
CODE_SERVER_BIN=${CODE_SERVER_BIN:-code-server}

run_target() {
    kind=$1
    user_dir=$2
    editor_bin=$3

    set -- python3 "$SCRIPT_DIR/scripts/apply.py" \
        --repo "$SCRIPT_DIR" \
        --target "$kind" \
        --user-dir "$user_dir" \
        --command "$editor_bin" \
        --profile "$PROFILE"
    [ "$DRY_RUN" -eq 0 ] || set -- "$@" --dry-run
    [ "$SKIP_EXTENSIONS" -eq 0 ] || set -- "$@" --skip-extensions
    "$@"
}

status=0

if [ "$TARGET" = vscode ] || [ "$TARGET" = all ]; then
    if [ "$TARGET" = vscode ] || command -v "$VSCODE_BIN" >/dev/null 2>&1 || [ -d "$VSCODE_USER_DIR" ]; then
        run_target vscode "$VSCODE_USER_DIR" "$VSCODE_BIN" || status=$?
    else
        echo "skip: VS Code was not detected"
    fi
fi

if [ "$TARGET" = code-server ] || [ "$TARGET" = all ]; then
    if [ "$TARGET" = all ] && [ "$PROFILE" != all ] && [ "$PROFILE" != default ]; then
        echo "skip: code-server has no '$PROFILE' profile (default only)"
    elif [ "$TARGET" = code-server ] || command -v "$CODE_SERVER_BIN" >/dev/null 2>&1 || [ -d "$CODE_SERVER_USER_DIR" ]; then
        run_target code-server "$CODE_SERVER_USER_DIR" "$CODE_SERVER_BIN" || status=$?
    else
        echo "skip: code-server was not detected"
    fi
fi

exit "$status"
