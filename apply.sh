#!/bin/sh

set -eu

REPOSITORY_ARCHIVE_URL="https://codeload.github.com/mokyabun/vscode-config/tar.gz/refs/heads/main"

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

die() {
    echo "error: $*" >&2
    exit 1
}

parse_options() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --target)
                [ "$#" -ge 2 ] || die "--target requires a value"
                TARGET=$2
                shift 2
                ;;
            --profile)
                [ "$#" -ge 2 ] || die "--profile requires a value"
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
                usage >&2
                die "unknown option: $1"
                ;;
        esac
    done

    case "$TARGET" in
        vscode|code-server|all) ;;
        *) die "invalid target: $TARGET" ;;
    esac
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "$1 is required"
}

find_repository_root() {
    case "$0" in
        */*) script_path=$0 ;;
        *) script_path=$(command -v "$0" 2>/dev/null) || return 1 ;;
    esac

    [ -f "$script_path" ] || return 1
    script_dir=$(CDPATH= cd -- "$(dirname -- "$script_path")" && pwd)
    [ -f "$script_dir/scripts/apply.py" ] || return 1
    [ -f "$script_dir/profiles/_base/settings.jsonc" ] || return 1
    printf '%s\n' "$script_dir"
}

download_repository() {
    destination=$1

    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$REPOSITORY_ARCHIVE_URL" -o "$destination"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$destination" "$REPOSITORY_ARCHIVE_URL"
    else
        die "curl or wget is required"
    fi
}

bootstrap() (
    require_command tar

    bootstrap_dir=$(mktemp -d "${TMPDIR:-/tmp}/vscode-config.XXXXXX")
    cleanup() {
        rm -rf -- "$bootstrap_dir"
    }
    trap cleanup 0
    trap 'exit 1' HUP INT TERM

    archive="$bootstrap_dir/vscode-config.tar.gz"
    repository="$bootstrap_dir/repository"

    download_repository "$archive"
    mkdir "$repository"
    tar -xzf "$archive" -C "$repository" --strip-components=1
    sh "$repository/apply.sh" "$@"
)

configure_environment() {
    case $(uname -s) in
        Darwin)
            default_vscode_user_dir="$HOME/Library/Application Support/Code/User"
            ;;
        *)
            default_vscode_user_dir="${XDG_CONFIG_HOME:-$HOME/.config}/Code/User"
            ;;
    esac

    default_code_server_user_dir="${XDG_DATA_HOME:-$HOME/.local/share}/code-server/User"
    VSCODE_USER_DIR=${VSCODE_USER_DIR:-$default_vscode_user_dir}
    CODE_SERVER_USER_DIR=${CODE_SERVER_USER_DIR:-$default_code_server_user_dir}
    VSCODE_BIN=${VSCODE_BIN:-code}
    CODE_SERVER_BIN=${CODE_SERVER_BIN:-code-server}
}

run_target() {
    kind=$1
    user_dir=$2
    editor_bin=$3

    set -- python3 "$REPOSITORY_ROOT/scripts/apply.py" \
        --repo "$REPOSITORY_ROOT" \
        --target "$kind" \
        --user-dir "$user_dir" \
        --command "$editor_bin" \
        --profile "$PROFILE"
    [ "$DRY_RUN" -eq 0 ] || set -- "$@" --dry-run
    [ "$SKIP_EXTENSIONS" -eq 0 ] || set -- "$@" --skip-extensions
    "$@"
}

apply_vscode() {
    if [ "$TARGET" = vscode ] || command -v "$VSCODE_BIN" >/dev/null 2>&1 || [ -d "$VSCODE_USER_DIR" ]; then
        run_target vscode "$VSCODE_USER_DIR" "$VSCODE_BIN"
    else
        echo "skip: VS Code was not detected"
    fi
}

apply_code_server() {
    if [ "$TARGET" = all ] && [ "$PROFILE" != all ] && [ "$PROFILE" != default ]; then
        echo "skip: code-server has no '$PROFILE' profile (default only)"
    elif [ "$TARGET" = code-server ] || command -v "$CODE_SERVER_BIN" >/dev/null 2>&1 || [ -d "$CODE_SERVER_USER_DIR" ]; then
        run_target code-server "$CODE_SERVER_USER_DIR" "$CODE_SERVER_BIN"
    else
        echo "skip: code-server was not detected"
    fi
}

apply_configurations() {
    status=0

    if [ "$TARGET" = vscode ] || [ "$TARGET" = all ]; then
        apply_vscode || status=$?
    fi
    if [ "$TARGET" = code-server ] || [ "$TARGET" = all ]; then
        apply_code_server || status=$?
    fi

    return "$status"
}

main() {
    parse_options "$@"

    if ! REPOSITORY_ROOT=$(find_repository_root); then
        bootstrap "$@"
        return
    fi

    require_command python3
    configure_environment
    apply_configurations
}

main "$@"
