#!/usr/bin/env bash
# Vanta Linux installer. Re-run to update; game data is kept in ~/.Vanta.
set -euo pipefail
fail() { printf 'Vanta: %s\n' "$*" >&2; exit 1; }
[[ $(uname -s) == Linux ]] || fail 'This installer is for Linux.'
[[ $EUID -ne 0 ]] || fail 'Run as your normal desktop user, without sudo.'
case "${1:-}" in
  --help) printf 'Usage: install.sh [--install-deps]\nInstalls per-user. --install-deps uses sudo for distribution packages.\n'; exit 0 ;;
  ''|--install-deps) ;;
  *) fail 'Unknown option. Use --help.' ;;
esac
if [[ ${1:-} == --install-deps ]]; then
  if command -v apt-get >/dev/null; then
    sudo apt-get update
    sudo apt-get install -y python3 python3-venv python3-pip git curl util-linux libfontconfig1 fonts-dejavu-core libdbus-1-3 libegl1 libgl1 libglib2.0-0 libxkbcommon0 libxkbcommon-x11-0 libxcb-cursor0 libxcb-icccm4 libxcb-keysyms1 libxcb-shape0 libxcb-xinerama0 libxcb-render-util0 xdg-utils
  elif command -v pacman >/dev/null; then
    # Do not refresh package databases independently: Arch partial upgrades are unsupported.
    sudo pacman -S --needed python python-pip git curl util-linux glib2 fontconfig ttf-dejavu dbus mesa libglvnd libxkbcommon-x11 xcb-util-cursor xcb-util-wm xcb-util-keysyms xcb-util-image xcb-util-renderutil xdg-utils
  elif command -v dnf >/dev/null; then
    sudo dnf install -y python3 python3-pip git curl util-linux fontconfig dejavu-sans-fonts dbus-libs mesa-libEGL mesa-libGL libxkbcommon-x11 xcb-util-cursor xcb-util-wm xcb-util-keysyms xcb-util-image xcb-util-renderutil xdg-utils
  else
    fail 'Automatic packages support apt, pacman and dnf. Install Python 3.10+, venv, git and Qt6 desktop libraries, then run without --install-deps.'
  fi
fi
for tool in python3 git flock; do command -v "$tool" >/dev/null || fail "Missing $tool. Re-run with --install-deps."; done
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
[[ "$DATA_HOME" == /* ]] || fail 'XDG_DATA_HOME must be an absolute path.'
BASE="$DATA_HOME/vanta"
mkdir -p "$BASE/releases" "$DATA_HOME/applications" "$DATA_HOME/icons/hicolor/256x256/apps"
exec 8>"$BASE/install.lock"
flock 8
STAGE=$(mktemp -d "$BASE/releases/install.XXXXXXXX")
cleanup() { [[ -z ${STAGE:-} ]] || rm -rf -- "$STAGE"; }
trap cleanup EXIT
printf 'Downloading Vanta for Linux…\n'
git clone --depth 1 --quiet https://github.com/inpriv/vanta.git "$STAGE"
"$STAGE/start.sh" --setup
"$STAGE/start.sh" --doctor
# Keep the release in its original path: virtualenv scripts contain absolute paths.
LINK="$BASE/current.new.$$"
ln -s "$STAGE" "$LINK"
mv -Tf "$LINK" "$BASE/current"
STAGE=''
cp "$BASE/current/icons/icon.png" "$DATA_HOME/icons/hicolor/256x256/apps/xyz.getvanta.Vanta.png"
# Escape paths according to the Desktop Entry string and Exec quoting rules.
python3 - "$BASE/current/start.sh" "$DATA_HOME/applications/xyz.getvanta.Vanta.desktop" <<'PY'
import pathlib, sys
launcher, destination = sys.argv[1:]
quoted = launcher.replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%')
# Desktop Entry string escapes are decoded before Exec quoting.
quoted = quoted.replace('\\', '\\\\')
pathlib.Path(destination).write_text('[Desktop Entry]\nType=Application\nName=Vanta Launcher\nComment=Play Minecraft with Vanta\nExec="' + quoted + '"\nIcon=xyz.getvanta.Vanta\nTerminal=false\nCategories=Game;\nStartupWMClass=Vanta Launcher\n', encoding='utf-8')
PY
if command -v update-desktop-database >/dev/null; then update-desktop-database "$DATA_HOME/applications" || true; fi
printf '\nVanta installed. Open Vanta Launcher from your application menu.\nRun this installer again to update. Game data stays in ~/.Vanta.\n'
printf 'Terminal launch: %q\n' "$BASE/current/start.sh"
