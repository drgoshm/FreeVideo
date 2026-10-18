#!/bin/bash
# Run with /bin/bash, or open in Terminal on macOS.
set -euo pipefail
umask 077

finish() {
    code=$?
    if [ "$code" -ne 0 ]; then
        printf '\nInstallation stopped. See the error above.\n'
    fi
    if [ -t 0 ]; then
        read -r -p 'Press Enter to close this window...' ignored || true
    fi
    exit "$code"
}
trap finish EXIT

if [ "$(uname -s)" != Darwin ]; then
    printf 'This installer is for macOS only.\n' >&2
    exit 1
fi

source_dir=$(cd -- "$(dirname -- "$0")" && pwd)
base_dir="${FREEVIDEO_MCP_HOME:-$HOME/.local/share/freevideo-mcp}"
credential_dir="$HOME/.config/freevideo"
for filename in access-key.txt FreeVideo-LAN-CA.crt; do
    if [ ! -f "$credential_dir/$filename" ] && [ ! -f "$source_dir/$filename" ] && [ ! -f "$HOME/Downloads/$filename" ]; then
        printf 'Missing %s. Copy it to %s and run this installer again.\n' "$filename" "$credential_dir" >&2
        exit 1
    fi
done
if [ ! -f "$source_dir/freevideo_engine/agent/mcp.py" ]; then
    printf 'Extract the whole archive before running the installer.\n' >&2
    exit 1
fi

mkdir -p "$base_dir/bin" "$base_dir/releases"
base_dir=$(cd -- "$base_dir" && pwd)
export UV_PYTHON_INSTALL_DIR="$base_dir/python"
export UV_PYTHON_BIN_DIR="$base_dir/bin"
export UV_CACHE_DIR="$base_dir/cache"
export UV_PYTHON_PREFERENCE=only-managed
export UV_PYTHON_INSTALL_BIN=0

uv_exe="$base_dir/bin/uv"
if [ ! -x "$uv_exe" ]; then
    printf 'Installing a private copy of uv...\n'
    uv_installer=$(mktemp "$base_dir/uv-install.XXXXXX")
    /usr/bin/curl --proto '=https' --tlsv1.2 --fail --show-error --location \
        https://astral.sh/uv/install.sh --output "$uv_installer"
    UV_UNMANAGED_INSTALL="$base_dir/bin" /bin/sh "$uv_installer"
    /bin/rm -f -- "$uv_installer"
fi

printf 'Preparing Python 3.12 and MCP dependencies...\n'
"$uv_exe" --no-config python install 3.12
# A new release keeps the previously configured/running client intact on failure.
release_dir=$(mktemp -d "$base_dir/releases/$(date +%Y%m%d-%H%M%S).XXXXXX")
"$uv_exe" --no-config venv --python 3.12 "$release_dir/.venv"
"$uv_exe" --no-config pip install --python "$release_dir/.venv/bin/python" \
    -r "$source_dir/requirements-installer.txt"

"$release_dir/.venv/bin/python" "$source_dir/install_mcp.py" \
    --source "$source_dir" --release "$release_dir" "$@"
