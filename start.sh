#!/usr/bin/env bash
# Run Vanta on Linux without modifying the system Python installation.
set -euo pipefail
APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
fail() { printf 'Vanta: %s\n' "$*" >&2; exit 1; }
[[ $(uname -s) == Linux ]] || fail 'This launcher script is for Linux.'
case "${1:-}" in
  --help) printf 'Usage: ./start.sh [--setup | --doctor | launcher arguments]\n'; exit 0 ;;
esac
PYTHON="${VANTA_PYTHON:-python3}"
command -v "$PYTHON" >/dev/null || fail 'Install Python 3.10+ and python3-venv (Ubuntu/Debian) or python (Arch).'
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))' || fail 'Python 3.10 or newer is required.'
VENV="$APP_DIR/.venv"
command -v flock >/dev/null || fail 'Install util-linux (flock is needed for safe first-run setup).'
# Concurrent launches must not modify the same virtual environment.
exec 9>"$APP_DIR/.setup.lock"
flock 9
if [[ ! -x "$VENV/bin/python" ]] || ! "$VENV/bin/python" -c 'import sys; assert sys.version_info[:2] == tuple(map(int, sys.argv[1].split(".")))' "$("$PYTHON" -c 'import sys; print("%s.%s" % sys.version_info[:2])')" 2>/dev/null; then
  "$PYTHON" -m venv --clear "$VENV" || fail 'Cannot create a virtual environment. On Ubuntu/Debian: sudo apt install python3-venv'
fi
REQ_HASH=$(sha256sum "$APP_DIR/requirements-linux.txt" | cut -d ' ' -f 1)
if [[ ! -f "$VENV/.requirements.sha256" ]] || [[ $(cat "$VENV/.requirements.sha256") != "$REQ_HASH" ]]; then
  printf 'Preparing Vanta dependencies (first run or requirements changed)…\n'
  "$VENV/bin/python" -m pip install --disable-pip-version-check -r "$APP_DIR/requirements-linux.txt"
  "$VENV/bin/python" -c 'import PyQt6.QtWidgets, minecraft_launcher_lib, requests, psutil, pypresence'
  printf '%s\n' "$REQ_HASH" > "$VENV/.requirements.sha256"
fi
flock -u 9
exec 9>&-
[[ ${1:-} != --setup ]] || exit 0
# Foreign Qt plugins (e.g. from an activated Conda environment) can crash Qt.
unset QT_PLUGIN_PATH QT_QPA_PLATFORM_PLUGIN_PATH
QT_DIR=$("$VENV/bin/python" -c 'from PyQt6.QtCore import QLibraryInfo; print(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))')
check_plugin() {
  local plugin="$QT_DIR/platforms/$1" missing
  [[ -f "$plugin" ]] || return 1
  missing=$(ldd "$plugin" 2>/dev/null | awk '/not found/ {print $1}')
  if [[ -n "$missing" ]]; then
    printf 'Missing libraries for %s:\n%s\n' "$1" "$missing" >&2
    return 1
  fi
}
if [[ ${1:-} == --doctor ]]; then
  "$VENV/bin/python" --version
  printf 'Desktop: %s / %s\n' "${XDG_CURRENT_DESKTOP:-unknown}" "${XDG_SESSION_TYPE:-unknown}"
  check_plugin libqxcb.so || exit 1
  printf 'X11 plugin dependencies OK.\n'
  if [[ -n ${WAYLAND_DISPLAY:-} ]]; then check_plugin libqwayland.so || exit 1; fi
  printf 'Diagnostics OK. Game data: ~/.Vanta; Minecraft runtime: ~/.minecraft\n'
  exit 0
fi
[[ -n ${DISPLAY:-} || -n ${WAYLAND_DISPLAY:-} || ${QT_QPA_PLATFORM:-} == offscreen ]] || fail 'No graphical session found. Run Vanta from your desktop terminal.'
if [[ -z ${QT_QPA_PLATFORM:-} ]]; then
  if [[ -n ${WAYLAND_DISPLAY:-} ]] && check_plugin libqwayland.so; then
    export QT_QPA_PLATFORM='wayland;xcb'
  else
    check_plugin libqxcb.so || fail 'Install desktop libraries: see the Linux troubleshooting section in README.md.'
    export QT_QPA_PLATFORM=xcb
  fi
fi
cd "$APP_DIR"
exec "$VENV/bin/python" "$APP_DIR/main.py" "$@"
