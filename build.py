"""
Nuitka build script for the Vanta Launcher (Inpriv Labs).

Compiles the launcher into a single native executable (dist/Vanta.exe)
using Nuitka instead of PyInstaller. PyInstaller's onefile bootloader
unpacks the Python runtime into a random %TEMP%\\_MEIxxxx folder on every
start, a strong static ML signature for antivirus engines (Wacapew.C!ml).
Nuitka compiles Python to C and produces a normal native binary with a
stable, cached onefile payload location.

Usage:
    python build.py            # release build -> dist/Vanta.exe
    python build.py --debug    # keep a console window for troubleshooting
"""

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
ENTRY = "main.py"
DIST_DIR = os.path.join(ROOT, "dist")

COMPANY = "Inpriv Labs"
PRODUCT = "Vanta Launcher"
VERSION = "1.7.0"
FILE_VERSION = "1.7.0.0"
DESCRIPTION = "Vanta Minecraft Launcher"
COPYRIGHT = "Copyright (c) 2026 Inpriv Labs"
ICON = "icons/icon.ico"

BUILD_DEPS = ("nuitka", "zstandard")


def ensure_build_dependencies() -> None:
    missing = []
    for dep in BUILD_DEPS:
        try:
            __import__(dep)
        except ImportError:
            missing.append(dep)
    if missing:
        print(f"[build] Installing missing build dependencies: {', '.join(missing)}")
        subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])


def check_compiler() -> None:
    if sys.platform != "win32":
        return
    if shutil.which("cl.exe"):
        print("[build] C compiler: MSVC (cl.exe)")
    elif shutil.which("gcc"):
        print("[build] C compiler: MinGW64 (gcc)")
    else:
        print(
            "[build] No C compiler on PATH. Nuitka will download a private "
            "MinGW64 toolchain automatically (--assume-yes-for-downloads)."
        )


def build(debug: bool = False) -> int:
    if sys.version_info < (3, 10):
        print("[build] Python 3.10 or higher is required.", file=sys.stderr)
        return 1

    ensure_build_dependencies()
    check_compiler()

    cmd = [
        sys.executable, "-m", "nuitka",
        "--onefile",
        "--standalone",
        "--enable-plugin=pyqt6",
        "--include-data-dir=icons=icons",
        "--assume-yes-for-downloads",
        "--output-dir=dist",
        # Stable, version-keyed onefile unpack location instead of a random
        # %TEMP%\\_MEIxxxx directory; cached across runs and re-extracted
        # exactly once per version.
        "--onefile-tempdir-spec={CACHE_DIR}/{PRODUCT}/{VERSION}",
        f"--company-name={COMPANY}",
        f"--product-name={PRODUCT}",
        f"--file-version={FILE_VERSION}",
        f"--product-version={VERSION}",
        f"--file-description={DESCRIPTION}",
        f"--copyright={COPYRIGHT}",
    ]

    if sys.platform == "win32":
        cmd += [
            f"--windows-icon-from-ico={ICON}",
            "--windows-console-mode=force" if debug else "--windows-console-mode=disable",
            "--output-filename=Vanta.exe",
        ]

    cmd.append(ENTRY)

    print("[build] Running:", " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT)

    exe_name = "Vanta.exe" if sys.platform == "win32" else "main.bin"
    exe_path = os.path.join(DIST_DIR, exe_name)
    if result.returncode == 0 and os.path.exists(exe_path):
        size_mib = os.path.getsize(exe_path) / (1024 * 1024)
        print(f"[build] OK: {exe_path} ({size_mib:.1f} MiB)")
    else:
        print(f"[build] FAILED: expected output not found at {exe_path}", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(build(debug="--debug" in sys.argv))
