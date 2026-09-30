import sys
import os
import uuid
import json
import subprocess
import shutil
import re
import base64
import glob
import time
import hashlib
import math
import urllib.parse
import webbrowser
import zipfile
import requests
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

try:
    from pypresence import Presence
except ImportError:
    Presence = None

try:
    import psutil
except ImportError:
    psutil = None

from PyQt6.QtCore import (
    QThread, pyqtSignal, Qt, QSettings, QPoint, QPropertyAnimation,
    QEasingCurve, QEvent, QParallelAnimationGroup, QRect, QRectF, QTimer,
    QVariantAnimation, QBuffer, QByteArray, QIODevice, QElapsedTimer, QObject
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QComboBox, QPushButton, QMessageBox, QFrame, QLabel,
    QStackedWidget, QSlider, QCheckBox, QListWidget, QListWidgetItem, QProgressBar, QScrollArea,
    QGraphicsDropShadowEffect, QDialog, QAbstractItemView, QGraphicsBlurEffect,
    QGraphicsOpacityEffect
)
from PyQt6.QtGui import (
    QColor, QFont, QFontMetrics, QImage, QPainter, QBrush, QPolygon, QIcon, QPixmap,
    QLinearGradient, QPainterPath, QPen
)

import minecraft_launcher_lib
import minecraft_launcher_lib.runtime
import minecraft_launcher_lib.fabric

APP_VERSION = "2.1"

# version.json is the single source of truth for the launcher's version.
# It's bundled into the executable by build.py (--include-data-file) and
# read at startup so the displayed version always matches the update
# manifest's `latest` field. APP_VERSION above is only the dev-mode fallback
# when running from source (version.json next to main.py).
VERSION_FILENAME = "version.json"


def _load_bundled_version() -> str:
    """Read the launcher's own version from the bundled version.json.

    Falls back to APP_VERSION if the file can't be read (dev runs, broken
    bundle, etc.) so the launcher never ends up with an empty version
    string in the UI.
    """
    try:
        import json as _json
        # _resource_base() resolves the bundled-data path in both source
        # and frozen/onefile modes.
        path = os.path.join(_resource_base(), VERSION_FILENAME)
        with open(path, "r", encoding="utf-8") as f:
            data = _json.load(f)
        latest = data.get("latest", "")
        # `latest` is stored as "vX.Y"; strip the "v" so it matches the
        # shape APP_VERSION expects (just the version digits).
        return str(latest).lstrip("v") or APP_VERSION
    except Exception:
        return APP_VERSION


APP_VERSION_RUNTIME = _load_bundled_version()

# Seconds the launch watchdog waits before considering the game confirmed running.
STARTUP_CONFIRM_SECONDS = 90

API_HEADERS = {
    "User-Agent": f"VantaLauncher/{APP_VERSION_RUNTIME} (+https://github.com/inpriv/vanta; support@getvanta.xyz)"
}

UPDATE_MANIFEST_URL = "https://raw.githubusercontent.com/inpriv/vanta/refs/heads/main/version.json"
UPDATE_CHECK_DELAY_MS = 3000
UPDATE_CHECK_RETRY_MS = 30000


def _build_http_session() -> requests.Session:
    """Shared keep-alive session for every HTTP call the launcher makes.

    Reusing pooled TLS connections avoids a fresh DNS + TCP + TLS handshake
    per request, which is the dominant cost of the small Modrinth API calls
    (search, version lookups, icons). urllib3's pool is thread-safe, so the
    QThread workers and the icon thread pool can all share it.
    """
    session = requests.Session()
    session.headers.update(API_HEADERS)
    try:
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        retry = Retry(total=2, connect=2, read=1, backoff_factor=0.3,
                      status_forcelist=(502, 503, 504), allowed_methods=None)
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
    except Exception:
        pass
    return session


HTTP = _build_http_session()


def _patch_natives_extraction() -> None:
    """
    Replace minecraft_launcher_lib's natives extractor with a tolerant one.

    The stock extractor crashes with WinError 183 ("Cannot create a file when
    that file already exists") on jars that contain both a file and directory
    entry at the same path (e.g. META-INF/versions/9), which leaves Fabric
    installs broken and aborts launches.
    """
    try:
        from minecraft_launcher_lib import natives as _natives
    except Exception:
        return
    if getattr(_natives, "_vanta_safe_extract", False):
        return

    def _safe_extract_natives_file(filename, extract_path, extract_data):
        try:
            os.makedirs(extract_path, exist_ok=True)
        except OSError:
            pass
        excludes = (extract_data or {}).get("exclude", [])
        with zipfile.ZipFile(filename, "r") as zf:
            for name in zf.namelist():
                if any(name.startswith(e) for e in excludes):
                    continue
                target = os.path.join(extract_path, *name.split("/"))
                if name.endswith("/"):
                    if os.path.isfile(target):
                        try:
                            os.remove(target)
                        except OSError:
                            pass
                    try:
                        os.makedirs(target, exist_ok=True)
                    except OSError:
                        pass
                    continue
                parent = os.path.dirname(target)
                try:
                    os.makedirs(parent, exist_ok=True)
                except FileExistsError:
                    try:
                        os.remove(parent)
                        os.makedirs(parent, exist_ok=True)
                    except OSError:
                        continue
                except OSError:
                    continue
                if os.path.isdir(target):
                    try:
                        shutil.rmtree(target)
                    except OSError:
                        continue
                try:
                    with zf.open(name) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                except OSError:
                    continue

    _natives.extract_natives_file = _safe_extract_natives_file
    _natives._vanta_safe_extract = True


_patch_natives_extraction()


def _to_bool(val, default: bool = True) -> bool:
    """Safely convert various QSettings values (str, int, bool) to Python bool."""
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes", "on")
    return default


def _install_marker_path(minecraft_dir: str, version_id: str) -> str:
    """Path of the marker file written after a fully successful install."""
    return os.path.join(minecraft_dir, "versions", version_id, "vanta.ok")


def _install_complete(minecraft_dir: str, version_id: str) -> bool:
    """True if the version was fully installed before AND its files still exist."""
    version_dir = os.path.join(minecraft_dir, "versions", version_id)
    json_path = os.path.join(version_dir, f"{version_id}.json")
    jar = os.path.join(version_dir, f"{version_id}.jar")
    has_meta = os.path.exists(_install_marker_path(minecraft_dir, version_id)) and os.path.exists(json_path)
    return has_meta and (os.path.exists(jar) or version_id.startswith("fabric-"))


def _mark_install_complete(minecraft_dir: str, version_id: str) -> None:
    try:
        os.makedirs(os.path.dirname(_install_marker_path(minecraft_dir, version_id)), exist_ok=True)
        with open(_install_marker_path(minecraft_dir, version_id), "w", encoding="utf-8") as f:
            f.write("ok\n")
    except OSError:
        pass


def _read_log_tail(log_path: str, max_lines: int = 30, max_chars: int = 4000) -> str:
    """Return the last lines of a log file, or an empty string if unavailable."""
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        tail = "".join(lines[-max_lines:])
        return tail[-max_chars:]
    except OSError:
        return ""


def _resource_base() -> str:
    """
    Directory that contains bundled read-only assets (the icons/ folder).

    - PyInstaller onefile: sys._MEIPASS (kept for backwards compatibility).
    - Nuitka standalone/onefile: the compiled main module's directory,
      where --include-data-dir places data files (for onefile builds this
      is the stable, cached unpack dir).
    - Source runs: this source file's directory.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return str(meipass)
    return os.path.dirname(os.path.abspath(__file__))


class GameStartupWatcher:
    """
    Confirms Minecraft startup from the game's own log output.

    Replaces the former Win32 window enumeration (ctypes EnumWindows /
    GetWindowTextW), which raw-API antivirus heuristics flagged. The game
    process writes its stdout/stderr to the instance log; once the render
    window has been created, Minecraft logs its OpenAL / sound-engine
    initialization. Combined with the standard subprocess poll() status
    checks in the launch loop, this provides equivalent confirmation using
    only portable Python mechanisms.
    """

    # Lowercase markers emitted after the render window exists (vanilla
    # and Fabric log formats, all versions with LWJGL 3).
    _WINDOW_READY_MARKERS = (
        "openal initialized",
        "sound engine started",
    )

    _MAX_BUFFER_CHARS = 65536

    def __init__(self, log_path: str) -> None:
        self._log_path = log_path
        self._handle = None
        self._pos = 0
        self._buffer = ""

    def window_detected(self) -> bool:
        """True once the log shows the game's render/audio stack came up."""
        self._buffer = (self._buffer + self._read_new_output())[-self._MAX_BUFFER_CHARS:]
        lowered = self._buffer.lower()
        return any(marker in lowered for marker in self._WINDOW_READY_MARKERS)

    def _read_new_output(self) -> str:
        try:
            if self._handle is None:
                self._handle = open(self._log_path, "r", encoding="utf-8", errors="replace")
            self._handle.seek(self._pos)
            chunk = self._handle.read()
            self._pos = self._handle.tell()
            return chunk
        except OSError:
            return ""

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None


def silence_asyncio_windows_bugs() -> None:
    """
    Patch asyncio transport destructors on Windows to suppress benign errors
    during process exit (ValueError/RuntimeError on closed pipe).
    """
    if sys.platform != "win32":
        return
    try:
        import asyncio
        from asyncio import proactor_events, base_subprocess

        if hasattr(proactor_events, "_ProactorBasePipeTransport"):
            original_del = proactor_events._ProactorBasePipeTransport.__del__

            def patched_pipe_del(self):
                try:
                    original_del(self)
                except (ValueError, OSError, RuntimeError):
                    pass

            proactor_events._ProactorBasePipeTransport.__del__ = patched_pipe_del

        if hasattr(base_subprocess, "BaseSubprocessTransport"):
            original_sub_del = base_subprocess.BaseSubprocessTransport.__del__

            def patched_sub_del(self):
                try:
                    original_sub_del(self)
                except (ValueError, OSError, RuntimeError):
                    pass

            base_subprocess.BaseSubprocessTransport.__del__ = patched_sub_del
    except Exception:
        pass


def is_fabric_compatible(version: str) -> bool:
    """Fabric loader requires Minecraft 1.14 or higher."""
    try:
        if version.startswith("fabric-loader-"):
            version = version.split("-")[-1]
        core = version.split("-")[0]
        parts = [int(p) for p in core.split(".") if p.isdigit()]
        if len(parts) < 2:
            return False
        major, minor = parts[0], parts[1]
        return (major > 1) or (major == 1 and minor >= 14)
    except (ValueError, IndexError):
        return False


def get_expected_runtime_name(version: str) -> str:
    """Derive standard Mojang Java runtime component name from Minecraft version string."""
    try:
        if version.startswith("fabric-loader-"):
            version = version.split("-")[-1]
        core = version.split("-")[0]
        parts = [int(p) for p in core.split(".") if p.isdigit()]
        if len(parts) >= 2:
            major, minor = parts[0], parts[1]
            patch = parts[2] if len(parts) > 2 else 0
            if major > 1:
                return "java-runtime-epsilon"  # Java 25 (year-based 26.x+)
            if major == 1 and (minor > 20 or (minor == 20 and patch >= 5)):
                return "java-runtime-delta"  # Java 21
            if major == 1 and minor >= 18:
                return "java-runtime-gamma"  # Java 17
            if major == 1 and minor == 17:
                return "java-runtime-alpha"  # Java 16
    except Exception:
        pass
    return "jre-legacy"  # Java 8


RUNTIME_JAVA_MAJOR = {
    "java-runtime-epsilon": 25,
    "java-runtime-delta": 21,
    "java-runtime-gamma": 17,
    "java-runtime-alpha": 16,
    "jre-legacy": 8,
}

# Mojang shipped the native Vulkan renderer in Minecraft 26.2 "Chaos Cubed"
# (released June 16, 2026, alongside the "Vibrant Visuals" transition). Older
# versions only have an OpenGL path in the official client, so this switch is
# meaningful from 26.2 onward. The launcher enables it by writing the new
# `preferredGraphicsBackend:vulkan` line into options.txt; Mojang's renderer
# loads its own LWJGL3 Vulkan bindings, so no special JVM flag is needed.
NATIVE_VULKAN_MIN_VERSION = (26, 2, 0)

# Default version selected when there's no saved preference (fresh
# install, cleared settings, etc.). 26.2 is Mojang's current stable
# release and the first version with the native Vulkan renderer.
_DEFAULT_VERSION = "26.2"


def is_native_vulkan_compatible(version: str) -> bool:
    """True if the vanilla client for `version` ships the native Vulkan renderer.

    Handles both legacy "1.21.4" versioning and the new year-based "26.2"
    scheme Mojang adopted in 2026. Fabric-loader wrappers like
    "fabric-loader-0.16.5-26.2" are reduced to "26.2" before comparison.
    """
    if not version:
        return False
    try:
        if version.startswith("fabric-loader-"):
            version = version.split("-")[-1]
        core = version.split("-")[0]
        parts = [int(p) for p in core.split(".") if p.isdigit()]
        if len(parts) < 2:
            return False
        # Compare against the configured minimum. Missing patch -> 0.
        parsed = (parts[0], parts[1], parts[2] if len(parts) > 2 else 0)
        return parsed >= NATIVE_VULKAN_MIN_VERSION
    except (ValueError, IndexError):
        return False


def _parse_java_major_version(text: str) -> Optional[int]:
    """Extract the major Java version from `java -version` output (e.g. 21, 17, 8)."""
    match = re.search(r'(?:openjdk|java) version "([^"]+)"', text)
    if not match:
        return None
    parts = match.group(1).split(".")
    try:
        if parts[0] == "1" and len(parts) > 1:
            return int(re.match(r"\d+", parts[1]).group())
        return int(re.match(r"\d+", parts[0]).group())
    except (ValueError, IndexError, AttributeError):
        return None


_JAVA_PROBE_CACHE: dict = {}
_JAVA_PROBE_LOCK = threading.Lock()


def probe_java_executable(java_path: str) -> Optional[int]:
    """Return the major Java version if `java_path` actually executes, else None.

    This is the ground-truth check: on Windows, `shutil.which("java")` can find
    the Microsoft Store alias stub (a non-executable reparse point), so the only
    reliable test is running the binary.

    Results are memoised per (path, mtime, size): spawning a JVM costs
    100-500 ms, and a second Play click would otherwise re-probe every
    candidate. A replaced/updated binary changes mtime and is re-probed.
    """
    try:
        st = os.stat(java_path)
        key = (os.path.normcase(os.path.abspath(java_path)), st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    with _JAVA_PROBE_LOCK:
        if key in _JAVA_PROBE_CACHE:
            return _JAVA_PROBE_CACHE[key]
    result = _probe_java_uncached(java_path)
    with _JAVA_PROBE_LOCK:
        _JAVA_PROBE_CACHE[key] = result
    return result


def _probe_java_uncached(java_path: str) -> Optional[int]:
    try:
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        result = subprocess.run(
            [java_path, "-version"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    output = (result.stderr or "") + (result.stdout or "")
    return _parse_java_major_version(output)


def find_system_java(min_major: Optional[int] = None) -> Optional[str]:
    """Locate a *working* system Java installation and return its java executable path.

    Never trusts `shutil.which` alone: every candidate is probe-executed with
    `java -version`. If `min_major` is given, only installations whose major
    version is >= min_major are accepted. Among acceptable candidates the one
    with the highest version wins.
    """
    candidates: List[str] = []

    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidates.append(os.path.join(java_home, "bin", "java.exe"))
        candidates.append(os.path.join(java_home, "bin", "java"))

    for name in ("java", "javaw"):
        found = shutil.which(name)
        if found:
            candidates.append(found)

    program_bases = [
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("LOCALAPPDATA"),
        os.environ.get("ProgramData"),
    ]
    glob_patterns = [
        os.path.join("Java", "*", "bin", "java.exe"),
        os.path.join("Eclipse Adoptium", "*", "bin", "java.exe"),
        os.path.join("Microsoft", "jdk*", "bin", "java.exe"),
        os.path.join("Zulu", "*", "bin", "java.exe"),
        os.path.join("Amazon Corretto", "*", "bin", "java.exe"),
        os.path.join("BellSoft", "*", "bin", "java.exe"),
        os.path.join("AdoptOpenJDK", "*", "bin", "java.exe"),
        os.path.join("Programs", "Eclipse Adoptium", "*", "bin", "java.exe"),
        os.path.join("Programs", "Java", "*", "bin", "java.exe"),
    ]
    for base in program_bases:
        if not base or not os.path.isdir(base):
            continue
        for pattern in glob_patterns:
            try:
                for hit in glob.glob(os.path.join(base, pattern)):
                    candidates.append(hit)
            except OSError:
                continue

    best_path: Optional[str] = None
    best_major = -1
    seen = set()
    for candidate in candidates:
        normalized = os.path.normpath(candidate)
        if normalized in seen or not os.path.exists(normalized):
            continue
        seen.add(normalized)
        major = probe_java_executable(normalized)
        if major is None:
            continue
        if min_major is not None and major < min_major:
            continue
        if major > best_major:
            best_major = major
            best_path = normalized

    return best_path


def matches_mod(filename: str, mod_id: str) -> bool:
    """Check if the filename matches a specific mod ID, handling overlaps like sodium vs sodium-extra."""
    if not filename or not mod_id:
        return False
    fn = filename.lower().replace("-", "").replace("_", "")
    m = mod_id.lower().replace("-", "").replace("_", "")
    if m == "sodium":
        return "sodium" in fn and "extra" not in fn
    return m in fn


def get_vanta_dir() -> str:
    """Return the Vanta data directory for the current platform."""
    base = os.environ.get("APPDATA") if sys.platform == "win32" else None
    if not base:
        base = os.path.expanduser("~")
    return os.path.join(base, ".Vanta")


def safe_instance_name(version: str) -> str:
    """Sanitize a version id into a safe, unique instance folder name."""
    name = "".join(c if (c.isalnum() or c in ".-_") else "_" for c in version).strip(". ")
    if not name:
        name = "unknown"
    if sys.platform == "win32" and name.upper() in {
        "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4",
        "COM5", "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3"
    }:
        name = f"_{name}"
    return name


def sanitize_mod_filename(filename: str) -> Optional[str]:
    """Return a safe bare filename for a Modrinth download, or None if unsafe."""
    if not filename:
        return None
    name = os.path.basename(filename.replace("\\", "/")).strip()
    if not name or name in (".", ".."):
        return None
    if "/" in name or "\\" in name or any(ord(c) < 32 for c in name):
        return None
    return name


def download_mod_file(url: str, expected_sha1: Optional[str], dest_path: str,
                      on_progress=None) -> None:
    """Stream a mod jar to disk atomically, verifying its SHA-1 hash.

    ``on_progress(done_bytes, total_bytes)`` is called per chunk when given
    (total is 0 if the server sent no Content-Length).
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"Refusing non-HTTPS download URL: {url}")
    tmp_path = dest_path + ".part"
    sha1 = hashlib.sha1()
    try:
        with HTTP.get(url, headers=API_HEADERS, stream=True, timeout=(5, 60)) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length", 0) or 0)
            done = 0
            with open(tmp_path, "wb") as out:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        sha1.update(chunk)
                        out.write(chunk)
                        done += len(chunk)
                        if on_progress is not None:
                            on_progress(done, total)
        if expected_sha1 and sha1.hexdigest().lower() != expected_sha1.lower():
            raise ValueError(f"SHA-1 mismatch for {os.path.basename(dest_path)}")
        os.replace(tmp_path, dest_path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _get_arrow_image_path() -> str:
    """Generate and cache the combo box arrow PNG file on disk for reliable QSS loading."""
    try:
        vdir = get_vanta_dir()
        os.makedirs(vdir, exist_ok=True)
        arrow_path = os.path.join(vdir, "arrow.png")
        if not os.path.exists(arrow_path):
            image = QImage(12, 8, QImage.Format.Format_ARGB32)
            image.fill(Qt.GlobalColor.transparent)

            painter = QPainter(image)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setBrush(QBrush(QColor("#FFFFFF")))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawPolygon(QPolygon([QPoint(1, 2), QPoint(11, 2), QPoint(6, 7)]))
            painter.end()

            image.save(arrow_path, "PNG")
        return arrow_path.replace("\\", "/")
    except Exception as e:
        sys.stderr.write(f"Failed to generate arrow image: {e}\n")
        return ""


def _generate_settings_image() -> QPixmap:
    """Generate a hamburger menu icon for the settings button (in-memory)."""
    image = QImage(16, 16, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor("#A0A0A2")))

    painter.drawRoundedRect(QRect(1, 3, 14, 2), 1, 1)
    painter.drawRoundedRect(QRect(1, 7, 14, 2), 1, 1)
    painter.drawRoundedRect(QRect(1, 11, 14, 2), 1, 1)

    painter.end()
    return QPixmap.fromImage(image)


def _generate_search_icon() -> QPixmap:
    """Small magnifier glyph for the mod search field (in-memory)."""
    image = QImage(32, 32, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor("#8E8E93"), 3.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(QRectF(6, 6, 15, 15))
    painter.drawLine(19, 19, 26, 26)
    painter.end()
    pixmap = QPixmap.fromImage(image)
    pixmap.setDevicePixelRatio(2.0)
    return pixmap


class VersionFetchWorker(QThread):
    versions_fetched = pyqtSignal(list)
    error_occurred = pyqtSignal(str)

    def run(self) -> None:
        try:
            version_list = minecraft_launcher_lib.utils.get_version_list()
            releases = [v["id"] for v in version_list if v["type"] == "release"]
            if not releases:
                raise ValueError("No release versions returned from API.")
            self.versions_fetched.emit(releases)
        except Exception as e:
            self.error_occurred.emit(str(e))


def _parse_version_tag(tag: str) -> tuple:
    """Convert a version tag like 'v1.6' or '1.10.2' into a comparable tuple."""
    parts = []
    for chunk in (tag or "").strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts) if parts else (0,)


def _cleanup_stale_update() -> None:
    """Remove the '.old' executable left behind by a previous auto-update."""
    if not getattr(sys, "frozen", False):
        return
    try:
        backup = os.path.abspath(sys.executable) + ".old"
        if os.path.exists(backup):
            os.remove(backup)
    except OSError:
        pass


def _apply_update(new_exe_path: str) -> None:
    """
    Swap the running executable with the downloaded update and relaunch.

    Windows locks the file of a running exe against deletion/overwrite but
    allows renaming it, so the current exe is moved to '.old', the new one
    takes its place, and the launcher restarts from the new binary.
    """
    current = os.path.abspath(sys.executable)
    backup = current + ".old"
    try:
        if os.path.exists(backup):
            os.remove(backup)
    except OSError:
        pass
    os.rename(current, backup)
    try:
        shutil.move(new_exe_path, current)
    except OSError:
        os.rename(backup, current)
        raise
    subprocess.Popen(
        [current],
        cwd=os.path.dirname(current),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )


class UpdateCheckWorker(QThread):
    update_available = pyqtSignal(str, str)  # latest_tag, download_url
    check_failed = pyqtSignal(str)

    def run(self) -> None:
        try:
            r = HTTP.get(UPDATE_MANIFEST_URL, headers=API_HEADERS, timeout=(5, 10))
            if r.status_code != 200:
                raise ValueError(f"Update check failed (HTTP {r.status_code}).")
            data = r.json()
            latest = str(data.get("latest", "")).strip()
            if not latest:
                raise ValueError("version.json is missing the 'latest' field.")
            if _parse_version_tag(latest) > _parse_version_tag(APP_VERSION_RUNTIME):
                url = str(data.get("download_url", "")).strip()
                if not url:
                    raise ValueError("version.json is missing 'download_url'.")
                url = url.replace("{version}", latest)
                self.update_available.emit(latest, url)
        except Exception as e:
            self.check_failed.emit(str(e))


class UpdateDownloadWorker(QThread):
    progress = pyqtSignal(int)
    ready_to_install = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self) -> None:
        tmp_path = ""
        try:
            parsed = urllib.parse.urlparse(self.url)
            if parsed.scheme != "https":
                raise ValueError(f"Refusing non-HTTPS update URL: {self.url}")
            exe_dir = os.path.dirname(os.path.abspath(sys.executable))
            tmp_path = os.path.join(exe_dir, ".Vanta.Update.part")
            final_path = os.path.join(exe_dir, ".Vanta.Update.new")
            try:
                if os.path.exists(final_path):
                    os.remove(final_path)
            except OSError:
                pass

            done = 0
            last_pct = -1
            with HTTP.get(self.url, headers=API_HEADERS, stream=True, timeout=(5, 60)) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length", 0) or 0)
                with open(tmp_path, "wb") as out:
                    for chunk in r.iter_content(chunk_size=65536):
                        if not chunk:
                            continue
                        out.write(chunk)
                        done += len(chunk)
                        if total > 0:
                            pct = min(100, int(done * 100 / total))
                            if pct != last_pct:
                                last_pct = pct
                                self.progress.emit(pct)

            with open(tmp_path, "rb") as f:
                if f.read(2) != b"MZ":
                    raise ValueError("Downloaded file is not a valid Windows executable.")
            os.replace(tmp_path, final_path)
            tmp_path = ""
            self.ready_to_install.emit(final_path)
        except Exception as e:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            self.error.emit(str(e))


class JavaDownloadWorker(QThread):
    progress = pyqtSignal(str, int)
    completed = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, jvm_version: str, minecraft_dir: str):
        super().__init__()
        self.jvm_version = jvm_version
        self.minecraft_dir = minecraft_dir
        self._max_val = 0

    def run(self) -> None:
        try:
            def set_status(text: str) -> None:
                self.progress.emit(text, -1)

            def set_max(val: int) -> None:
                self._max_val = val

            last_pct = {"v": -1}

            def set_progress(val: int) -> None:
                if self._max_val > 0:
                    percent = max(0, min(100, int((val / self._max_val) * 100)))
                    if percent != last_pct["v"]:
                        last_pct["v"] = percent
                        self.progress.emit("Downloading Java...", percent)

            callbacks = {
                "setStatus": set_status,
                "setProgress": set_progress,
                "setMax": set_max,
            }

            minecraft_launcher_lib.runtime.install_jvm_runtime(
                self.jvm_version, self.minecraft_dir, callback=callbacks
            )
            self.completed.emit()
        except Exception as e:
            self.error.emit(str(e))


class LaunchWorker(QThread):
    progress_updated = pyqtSignal(str, int)
    launch_success = pyqtSignal()
    game_exited = pyqtSignal()
    launch_failed = pyqtSignal(str)
    game_confirmed = pyqtSignal()
    error_occurred = pyqtSignal(str)
    performance_mods_installed = pyqtSignal()
    mods_missing = pyqtSignal(str)

    def __init__(self, username: str, version: str, minecraft_dir: str,
                 ram_gb: int, performance_mode: bool,
                 java_path: Optional[str] = None, use_native_vulkan: bool = False):
        super().__init__()
        self.username = username
        self.version = version
        self.minecraft_dir = minecraft_dir
        self.ram_gb = ram_gb
        self.performance_mode = performance_mode
        self.java_path = java_path
        self.use_native_vulkan = bool(use_native_vulkan) and is_native_vulkan_compatible(version)
        self._max_val = 0
        self.process = None
        self._aborted = False

    def abort(self) -> None:
        self._aborted = True
        proc = self.process
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    def _resolve_java(self) -> Optional[str]:
        """Pick the Java executable for this launch (also used for the Fabric installer).

        Order: the path chosen by the UI, the Mojang runtime the version
        json asks for, the runtime we expect for this version, then a
        probe-verified system Java that is new enough. Never plain "java"
        from PATH (it can be a dead shim - AGENTS.md rule 3).
        """
        if self.java_path and os.path.exists(self.java_path):
            return self.java_path
        try:
            runtime_info = minecraft_launcher_lib.runtime.get_version_runtime_information(
                self.version, self.minecraft_dir
            )
            if runtime_info and runtime_info.get("name"):
                java_exec = minecraft_launcher_lib.runtime.get_executable_path(
                    runtime_info["name"], self.minecraft_dir
                )
                if java_exec and os.path.exists(java_exec):
                    return java_exec
        except Exception:
            pass
        expected = get_expected_runtime_name(self.version)
        try:
            expected_exec = minecraft_launcher_lib.runtime.get_executable_path(expected, self.minecraft_dir)
            if expected_exec and os.path.exists(expected_exec):
                return expected_exec
        except Exception:
            pass
        return find_system_java(RUNTIME_JAVA_MAJOR.get(expected))

    @staticmethod
    def _console_free_java(java_exec: str) -> str:
        """javaw.exe next to java.exe, so the Fabric installer doesn't flash a console window."""
        if sys.platform == "win32" and java_exec.lower().endswith("java.exe"):
            javaw = java_exec[:-len("java.exe")] + "javaw.exe"
            if os.path.exists(javaw):
                return javaw
        return java_exec

    def _latest_stable_loader(self) -> Optional[str]:
        """Newest stable Fabric loader for this Minecraft version (None if offline)."""
        try:
            url = f"https://meta.fabricmc.net/v2/versions/loader/{urllib.parse.quote(self.version)}"
            r = HTTP.get(url, timeout=(4, 8))
            if r.status_code != 200:
                return None
            entries = r.json() or []
            for entry in entries:
                loader = entry.get("loader") or {}
                if loader.get("stable") and loader.get("version"):
                    return str(loader["version"])
            if entries:
                return (entries[0].get("loader") or {}).get("version")
        except Exception:
            pass
        return None

    @staticmethod
    def _new_crash_report(instance_dir: str, since: float) -> str:
        """Summary of a crash report written after `since` (epoch seconds), or ''."""
        folder = os.path.join(instance_dir, "crash-reports")
        try:
            reports = [
                os.path.join(folder, f) for f in os.listdir(folder)
                if f.endswith(".txt") and os.path.getmtime(os.path.join(folder, f)) >= since - 2
            ]
        except OSError:
            return ""
        if not reports:
            return ""
        newest = max(reports, key=os.path.getmtime)
        try:
            with open(newest, "r", encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError:
            return ""
        description = next((l.split(":", 1)[1].strip() for l in lines if l.startswith("Description:")), "")
        try:
            start = next(i for i, l in enumerate(lines) if l.startswith("Description:")) + 1
        except StopIteration:
            start = 0
        trace = [l for l in lines[start:start + 20] if l.strip()][:14]
        summary = f"Reason: {description}" if description else "Minecraft wrote a crash report."
        return (f"{summary}\nReport: {newest}\n\nDetails:\n" + "\n".join(trace)).rstrip()

    def _fabric_version_valid(self, fabric_id: str) -> bool:
        """A fabric version is usable only if its version json exists."""
        json_path = os.path.join(
            self.minecraft_dir, "versions", fabric_id, fabric_id + ".json"
        )
        return os.path.exists(json_path) and os.path.getsize(json_path) > 2

    def _set_native_vulkan_option(self, instance_dir: str) -> None:
        """Pre-set ``preferredGraphicsBackend:vulkan`` in instance options.txt.

        As of Minecraft 26.2 "Chaos Cubed", the graphics API lives in
        ``options.txt`` under the key ``preferredGraphicsBackend`` (with
        lowercase values ``default``, ``opengl`` or ``vulkan``). Setting it
        to ``vulkan`` is equivalent to picking "Prefer Vulkan (Experimental)"
        in Options > Video Settings > Graphics API. We only flip this one
        key and leave every other option (FOV, render distance, etc.)
        alone so the user's previous choices are preserved.
        """
        options_path = os.path.join(instance_dir, "options.txt")
        try:
            os.makedirs(instance_dir, exist_ok=True)
            existing: List[str] = []
            if os.path.exists(options_path):
                try:
                    with open(options_path, "r", encoding="utf-8", errors="replace") as f:
                        existing = f.readlines()
                except OSError:
                    existing = []
            replaced = False
            for i, line in enumerate(existing):
                if line.startswith("preferredGraphicsBackend:"):
                    existing[i] = "preferredGraphicsBackend:vulkan\n"
                    replaced = True
                    break
            if not replaced:
                existing.append("preferredGraphicsBackend:vulkan\n")
            with open(options_path, "w", encoding="utf-8") as f:
                f.writelines(existing)
        except OSError as e:
            # Failing to pre-write the graphics API line is non-fatal: the
            # user can pick Vulkan manually in video settings.
            sys.stderr.write(f"Could not pre-set Vulkan graphics API option: {e}\n")

    def run(self) -> None:
        try:
            instance_subname = safe_instance_name(self.version)

            def set_status(text: str) -> None:
                self.progress_updated.emit(text, -1)

            def set_max(val: int) -> None:
                self._max_val = val

            last_pct = {"v": -1}

            def set_progress(val: int) -> None:
                # minecraft_launcher_lib calls this once per file (thousands
                # of assets); only cross the thread boundary when the visible
                # percentage actually changes.
                if self._max_val > 0:
                    percent = max(0, min(100, int((val / self._max_val) * 100)))
                    if percent != last_pct["v"]:
                        last_pct["v"] = percent
                        self.progress_updated.emit("Installing...", percent)

            callbacks = {
                "setStatus": set_status,
                "setProgress": set_progress,
                "setMax": set_max,
            }

            try:
                installed = [
                    v.get("id", "") for v in minecraft_launcher_lib.utils.get_installed_versions(
                        self.minecraft_dir
                    )
                ]
            except Exception:
                installed = []

            already_installed = self.version in installed and _install_complete(self.minecraft_dir, self.version)
            if already_installed:
                self.progress_updated.emit("Files verified (cached)...", 5)
            else:
                try:
                    self.progress_updated.emit("Checking files...", 0)
                    minecraft_launcher_lib.install.install_minecraft_version(
                        self.version, self.minecraft_dir, callback=callbacks
                    )
                    _mark_install_complete(self.minecraft_dir, self.version)
                except Exception as net_err:
                    if self.version in installed:
                        self.progress_updated.emit("Offline: Launching cached...", 100)
                    else:
                        raise RuntimeError(
                            f"Failed to fetch assets for {self.version}.\n"
                            "Please verify your internet connection."
                        ) from net_err

            self.progress_updated.emit("Preparing launch...", 100)

            java_exec = self._resolve_java()
            if not java_exec:
                self.error_occurred.emit(
                    "Java environment not found.\n\n"
                    "Vanta could not find a working Java installation.\n"
                    "Start the launch again and accept the prompt to download\n"
                    "the Java runtime automatically, or install OpenJDK 17 or 21\n"
                    "(e.g. from https://adoptium.net)."
                )
                return

            vanta_dir = get_vanta_dir()
            instance_dir = os.path.join(vanta_dir, "instances", instance_subname)
            os.makedirs(instance_dir, exist_ok=True)

            def mods_dir_has_custom_jars() -> bool:
                mods_dir = os.path.join(instance_dir, "mods")
                if not os.path.exists(mods_dir):
                    return False
                try:
                    jars = [
                        f for f in os.listdir(mods_dir)
                        if f.endswith(".jar") and "fabric-api" not in f.lower() and "fabric_api" not in f.lower()
                    ]
                    return len(jars) > 0
                except Exception:
                    return False

            target_version = self.version
            use_fabric = (self.performance_mode or mods_dir_has_custom_jars()) and is_fabric_compatible(self.version)

            if use_fabric:
                suffix = "-" + self.version

                def loader_key(version_id: str) -> tuple:
                    core = version_id[len("fabric-loader-"):-len(suffix)]
                    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.+-]", core))

                def valid_loaders(ids) -> list:
                    return sorted(
                        (v for v in ids if v.startswith("fabric-loader-") and v.endswith(suffix)
                         and self._fabric_version_valid(v)),
                        key=loader_key,
                    )

                for broken in (v for v in installed if v.startswith("fabric-loader-") and v.endswith(suffix)
                               and not self._fabric_version_valid(v)):
                    sys.stderr.write(f"Removing broken Fabric installation: {broken}\n")
                    try:
                        shutil.rmtree(os.path.join(self.minecraft_dir, "versions", broken))
                    except OSError as rm_err:
                        sys.stderr.write(f"Could not remove broken Fabric dir: {rm_err}\n")

                cached = valid_loaders(installed)
                latest_loader = self._latest_stable_loader()
                wanted = f"fabric-loader-{latest_loader}{suffix}" if latest_loader else None
                target_version = None
                install_error = None

                if wanted and wanted in cached:
                    target_version = wanted
                    self.progress_updated.emit("Fabric loader (cached)...", 10)
                elif wanted or not cached:
                    try:
                        self.progress_updated.emit(
                            "Updating Fabric loader..." if cached else "Installing Fabric...", 10
                        )
                        minecraft_launcher_lib.fabric.install_fabric(
                            self.version, self.minecraft_dir,
                            loader_version=latest_loader,
                            callback=callbacks,
                            java=self._console_free_java(java_exec),
                        )
                        after = valid_loaders(
                            v.get("id", "") for v in minecraft_launcher_lib.utils.get_installed_versions(self.minecraft_dir)
                        )
                        if wanted and wanted in after:
                            target_version = wanted
                        elif after:
                            target_version = after[-1]
                    except Exception as e:
                        install_error = str(e)
                        sys.stderr.write(f"Fabric installation failed: {e}\n")

                if target_version is None and cached:
                    # Offline or the update failed: the newest cached loader still works.
                    target_version = cached[-1]
                    self.progress_updated.emit("Fabric loader (cached)...", 10)

                if target_version is None:
                    self.error_occurred.emit(
                        "Fabric loader could not be installed.\n\n"
                        "Performance Mode (and Fabric mods) require a working Fabric installation.\n"
                        "Please check your internet connection and try again.\n\n"
                        + (f"Details: {install_error}" if install_error else "No valid Fabric installation was found.")
                    )
                    return

                # The Fabric game provider needs the vanilla jar on disk.
                vanilla_jar = os.path.join(self.minecraft_dir, "versions", self.version, self.version + ".jar")
                if not os.path.exists(vanilla_jar):
                    try:
                        self.progress_updated.emit("Fetching Minecraft jar...", 10)
                        minecraft_launcher_lib.install.install_minecraft_version(
                            self.version, self.minecraft_dir, callback=callbacks
                        )
                        _mark_install_complete(self.minecraft_dir, self.version)
                    except Exception as jar_err:
                        sys.stderr.write(f"Vanilla jar fetch failed: {jar_err}\n")
                    if not os.path.exists(vanilla_jar):
                        self.error_occurred.emit(
                            f"The Minecraft {self.version} client jar is missing or incomplete.\n\n"
                            "Reconnect to the internet and try again so Vanta can finish the download."
                        )
                        return

                try:
                    self._ensure_fabric_api_and_mods(instance_dir, self.performance_mode)
                except Exception as e:
                    sys.stderr.write(f"Fabric performance mods installation error: {e}\n")

                if target_version == self.version and mods_dir_has_custom_jars():
                    self.error_occurred.emit(
                        "Fabric loader could not be installed or found offline, but mods are installed for this instance.\n\n"
                        "Reconnect to the internet and try again, or remove the mods from this instance."
                    )
                    return

            offline_uuid = str(uuid.UUID(bytes=hashlib.md5(f"OfflinePlayer:{self.username}".encode("utf-8")).digest(), version=3))

            heap_initial = f"-Xms{max(512, (self.ram_gb * 1024) // 2)}M"
            heap_max = f"-Xmx{self.ram_gb}G"

            options = {
                "username": self.username,
                "uuid": offline_uuid,
                "token": "",
                "launcherName": "Vanta",
                "launcherVersion": APP_VERSION_RUNTIME,
                "gameDirectory": instance_dir,
                "jvmArguments": [
                    heap_max,
                    heap_initial,
                    "-XX:+UseG1GC",
                    "-XX:+ParallelRefProcEnabled",
                    "-XX:MaxGCPauseMillis=200",
                    "-XX:+UnlockExperimentalVMOptions",
                    "-XX:+DisableExplicitGC",
                    "-XX:G1NewSizePercent=30",
                    "-XX:G1MaxNewSizePercent=40",
                    "-XX:G1HeapRegionSize=8M",
                    "-XX:G1ReservePercent=20",
                    "-XX:G1HeapWastePercent=5",
                    "-XX:G1MixedGCCountTarget=4",
                    "-XX:InitiatingHeapOccupancyPercent=15",
                    "-XX:G1MixedGCLiveThresholdPercent=90",
                    "-XX:G1RSetUpdatingPauseTimePercent=5",
                    "-XX:SurvivorRatio=32",
                    "-XX:+PerfDisableSharedMem",
                    "-XX:MaxTenuringThreshold=1"
                ]
            }

            # Native Vulkan renderer (Mojang, 26.2+) is enabled entirely via
            # the `preferredGraphicsBackend` option in options.txt; the
            # client loads its own LWJGL3 Vulkan bindings when the option
            # selects Vulkan, so no JVM flag is needed.
            if self.use_native_vulkan:
                self._set_native_vulkan_option(instance_dir)

            options["executablePath"] = java_exec

            command = minecraft_launcher_lib.command.get_minecraft_command(
                target_version,
                self.minecraft_dir,
                options
            )

            self.progress_updated.emit("Launching...", 100)

            log_path = os.path.join(instance_dir, "latest.log")
            log_file = open(log_path, "w", encoding="utf-8")
            launch_wall_time = time.time()
            watcher = GameStartupWatcher(log_path)
            try:
                self.process = subprocess.Popen(
                    command,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    cwd=instance_dir,
                    close_fds=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
                self.launch_success.emit()

                start_monotonic = time.monotonic()
                while time.monotonic() - start_monotonic < STARTUP_CONFIRM_SECONDS:
                    if self._aborted:
                        return
                    if self.process.poll() is not None:
                        try:
                            log_file.flush()
                            os.fsync(log_file.fileno())
                        except Exception:
                            pass
                        tail = _read_log_tail(log_path)
                        code = self.process.returncode
                        if code != 0:
                            crash = self._new_crash_report(instance_dir, launch_wall_time)
                            if crash:
                                self.launch_failed.emit(
                                    f"Minecraft crashed during startup (exit code {code}).\n{crash}"
                                )
                            else:
                                self.launch_failed.emit(
                                    f"Minecraft exited during startup (exit code {code}).\n\n"
                                    f"Last log lines:\n{tail if tail else '(log is empty)'}"
                                )
                        else:
                            self.game_exited.emit()
                        return
                    if watcher.window_detected():
                        break
                    time.sleep(0.5)

                if self._aborted:
                    return

                self.game_confirmed.emit()
                try:
                    self.process.wait()
                except Exception:
                    pass
                if not self._aborted:
                    code = self.process.returncode
                    crash = self._new_crash_report(instance_dir, launch_wall_time) if code else ""
                    if crash:
                        self.launch_failed.emit(f"Minecraft crashed (exit code {code}).\n{crash}")
                    else:
                        self.game_exited.emit()
            finally:
                watcher.close()
                log_file.close()

        except FileNotFoundError as file_err:
            self.error_occurred.emit(
                "A required file could not be found:\n\n"
                f"{file_err.filename or file_err}\n\n"
                "If this refers to Java, restart the launcher and accept the\n"
                "prompt to download the Java runtime automatically, or install\n"
                "OpenJDK 17 or 21 (e.g. from https://adoptium.net)."
            )
        except Exception as e:
            if not self._aborted:
                self.error_occurred.emit(str(e))

    def _ensure_fabric_api_and_mods(self, instance_dir: str, download_perf_mods: bool) -> None:
        mods_dir = os.path.join(instance_dir, "mods")
        os.makedirs(mods_dir, exist_ok=True)

        mods_to_download = ["fabric-api"]
        if download_perf_mods:
            mods_to_download.extend(["sodium", "lithium", "ferrite-core", "entityculling"])

        seen = set()
        mods = []
        for m in mods_to_download:
            if m not in seen:
                seen.add(m)
                mods.append(m)

        unresolved = []

        def _query_modrinth(mod: str):
            """Return (file_info, filename) for a mod, or (None, reason) if unavailable."""
            def _pick(data):
                if not data or not isinstance(data, list) or len(data) == 0:
                    return None
                files_list = data[0].get("files", [])
                if not files_list:
                    return None
                file_info = files_list[0]
                for f in files_list:
                    if f.get("primary"):
                        file_info = f
                        break
                return file_info

            params = {
                "loaders": json.dumps(["fabric"]),
                "game_versions": json.dumps([self.version]),
            }
            url = f"https://api.modrinth.com/v2/project/{mod}/version"
            r = HTTP.get(url, headers=API_HEADERS, params=params, timeout=(5, 15))
            if r.status_code == 200:
                info = _pick(r.json())
                if info:
                    return info, None

            # Fallback: query without the game-version filter and match client-side.
            r = HTTP.get(
                url,
                headers=API_HEADERS,
                params={"loaders": json.dumps(["fabric"])},
                timeout=(5, 15),
            )
            if r.status_code == 200:
                versions = r.json() or []
                compatible = [
                    v for v in versions
                    if self.version in (v.get("game_versions") or [])
                ]
                info = _pick(compatible)
                if info:
                    return info, None
                if versions:
                    return None, f"no build available for Minecraft {self.version}"
            return None, "Modrinth API unreachable"

        def fetch_and_download(mod: str) -> None:
            try:
                file_info, reason = _query_modrinth(mod)
                target_filename = None

                if file_info is not None:
                    target_filename = sanitize_mod_filename(file_info.get("filename", ""))
                    if target_filename is None:
                        sys.stderr.write(f"Unsafe or missing filename returned by Modrinth API for {mod}; skipping.\n")
                        unresolved.append(f"{mod} (unsafe filename from API)")
                        return
                else:
                    local_match = False
                    if os.path.exists(mods_dir):
                        for f in os.listdir(mods_dir):
                            f_path = os.path.join(mods_dir, f)
                            if f.endswith(".jar") and os.path.getsize(f_path) > 0 and matches_mod(f, mod):
                                local_match = True
                                break
                    if local_match:
                        return
                    sys.stderr.write(f"Mod {mod} could not be resolved from API and is missing locally ({reason}).\n")
                    unresolved.append(f"{mod} ({reason})")
                    return

                if os.path.exists(mods_dir):
                    for f in os.listdir(mods_dir):
                        if f.endswith(".jar") and matches_mod(f, mod) and f != target_filename:
                            try:
                                os.remove(os.path.join(mods_dir, f))
                            except Exception as del_err:
                                sys.stderr.write(f"Failed to delete old mod version {f}: {del_err}\n")

                dest_path = os.path.join(mods_dir, target_filename)
                if not os.path.exists(dest_path) or os.path.getsize(dest_path) == 0:
                    expected_sha1 = (file_info.get("hashes") or {}).get("sha1")
                    download_mod_file(file_info["url"], expected_sha1, dest_path)
            except Exception as e:
                sys.stderr.write(f"Error checking/downloading {mod}: {e}\n")
                unresolved.append(f"{mod} ({e})")

        missing = []
        for i, mod in enumerate(mods):
            local_found = False
            try:
                if os.path.exists(mods_dir):
                    for f in os.listdir(mods_dir):
                        f_path = os.path.join(mods_dir, f)
                        if f.endswith(".jar") and os.path.getsize(f_path) > 0 and matches_mod(f, mod):
                            local_found = True
                            break
            except Exception:
                pass
            if local_found:
                self.progress_updated.emit(f"{mod}: up to date", int(20 + (i / len(mods)) * 60))
            else:
                missing.append(mod)

        if not missing:
            self.performance_mods_installed.emit()
            return

        self.progress_updated.emit(f"Downloading {len(missing)} mods...", 20)
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(fetch_and_download, mod): mod for mod in missing}
            for i, fut in enumerate(as_completed(futures)):
                self.progress_updated.emit(
                    f"Downloading {futures[fut]}...",
                    int(20 + ((i + 1) / len(missing)) * 60)
                )

        if unresolved:
            self.mods_missing.emit(
                "The following mods could not be installed for Minecraft "
                f"{self.version} and were skipped:\n\n• " + "\n• ".join(unresolved)
            )

        self.performance_mods_installed.emit()


class AvatarLoaderWorker(QThread):
    avatar_loaded = pyqtSignal(str, QImage)

    def __init__(self, username: str):
        super().__init__()
        self.username = username

    def run(self) -> None:
        if not self.username:
            return
        try:
            url = f"https://minotar.net/helm/{self.username}/128.png"
            r = HTTP.get(url, headers=API_HEADERS, timeout=(3, 5))
            if r.status_code == 200:
                image = QImage()
                image.loadFromData(r.content)
                if not image.isNull():
                    self.avatar_loaded.emit(self.username, image)
        except Exception:
            pass


MODRINTH_API = "https://api.modrinth.com/v2"
MOD_PAGE_SIZE = 20
FABRIC_API_PROJECT_ID = "P7dR8mSH"


def _format_count(n) -> str:
    """12345678 -> '12.3M' (compact download counter for mod cards)."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "0"
    for div, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= div:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + suffix
    return str(n)


def _format_size(num_bytes: int) -> str:
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    return f"{max(1, num_bytes // 1024)} KB"


def _pretty_jar_name(filename: str) -> str:
    """Best-effort human name from a jar filename when Modrinth can't identify it.

    'sodium-fabric-0.5.8+mc1.20.4.jar' -> 'Sodium Fabric'
    """
    stem = filename[:-4] if filename.lower().endswith(".jar") else filename
    parts = re.split(r"[-_ ]+", stem)
    words = []
    for part in parts:
        if not part or re.match(r"^(v?\d|mc\d|\d)", part, re.IGNORECASE):
            break
        words.append(part)
    name = " ".join(words) or stem
    return name[:1].upper() + name[1:]


_SHA1_CACHE: dict = {}
_SHA1_LOCK = threading.Lock()


def cached_file_sha1(path: str) -> Optional[str]:
    """SHA-1 of a file, memoised per (path, mtime, size)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (os.path.normcase(path), st.st_mtime_ns, st.st_size)
    with _SHA1_LOCK:
        if key in _SHA1_CACHE:
            return _SHA1_CACHE[key]
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    digest = h.hexdigest()
    with _SHA1_LOCK:
        _SHA1_CACHE[key] = digest
    return digest


# sha1 -> {"project_id", "slug", "title", "icon_url", "version"}; None means
# Modrinth answered and does not know the file (e.g. a hand-copied jar).
_MOD_META_CACHE: dict = {}
_MOD_META_LOCK = threading.Lock()


def identify_installed_mods(mods_dir: str) -> dict:
    """Map installed jar filenames to Modrinth project metadata.

    Uses Modrinth's exact hash lookup (one POST for all jars plus one GET
    for the project titles/icons), so it is immune to the filename
    guesswork that `matches_mod` relies on. Results are cached per hash,
    so re-opening the Mods tab or switching versions costs no requests.
    """
    try:
        names = [f for f in os.listdir(mods_dir) if f.lower().endswith(".jar")]
    except OSError:
        return {}
    hashes = {}
    for name in names:
        digest = cached_file_sha1(os.path.join(mods_dir, name))
        if digest:
            hashes[name] = digest

    with _MOD_META_LOCK:
        unknown = sorted({h for h in hashes.values() if h not in _MOD_META_CACHE})
    if unknown:
        try:
            r = HTTP.post(f"{MODRINTH_API}/version_files",
                          json={"hashes": unknown, "algorithm": "sha1"}, timeout=(5, 10))
            if r.status_code == 200:
                versions = r.json() or {}
                project_ids = sorted({v.get("project_id") for v in versions.values() if v.get("project_id")})
                projects = {}
                if project_ids:
                    r2 = HTTP.get(f"{MODRINTH_API}/projects",
                                  params={"ids": json.dumps(project_ids)}, timeout=(5, 10))
                    if r2.status_code == 200:
                        projects = {p.get("id"): p for p in (r2.json() or [])}
                with _MOD_META_LOCK:
                    for digest in unknown:
                        v = versions.get(digest)
                        p = projects.get(v.get("project_id")) if v else None
                        if p:
                            _MOD_META_CACHE[digest] = {
                                "project_id": p.get("id", ""),
                                "slug": p.get("slug", "") or "",
                                "title": p.get("title", "") or "",
                                "icon_url": p.get("icon_url", "") or "",
                                "version": v.get("version_number", "") or "",
                            }
                        elif not v:
                            _MOD_META_CACHE[digest] = None
        except Exception:
            # Offline / API hiccup: don't cache, just fall back to filenames.
            pass

    result = {}
    with _MOD_META_LOCK:
        for name, digest in hashes.items():
            meta = _MOD_META_CACHE.get(digest)
            if meta:
                result[name] = meta
    return result


def _pick_mod_version(versions: list) -> Optional[dict]:
    """Newest *release* build with files; falls back to the newest build of any type."""
    if not versions or not isinstance(versions, list):
        return None
    for v in versions:
        if v.get("version_type") == "release" and v.get("files"):
            return v
    for v in versions:
        if v.get("files"):
            return v
    return None


def _primary_file(version: dict) -> Optional[dict]:
    files = version.get("files") or []
    for f in files:
        if f.get("primary"):
            return f
    return files[0] if files else None


class ModSearchWorker(QThread):
    results_ready = pyqtSignal(int, int, list, int)  # token, offset, hits, total_hits
    failed = pyqtSignal(int, str)

    def __init__(self, token: int, query: str, mc_version: str, offset: int = 0):
        super().__init__()
        self.token = token
        self.query = query
        self.mc_version = mc_version
        self.offset = offset

    def run(self) -> None:
        facets = [["categories:fabric"], ["project_type:mod"]]
        if self.mc_version:
            # Only show mods that actually have a build for the selected
            # version, so every "Install" button is guaranteed to work.
            facets.append([f"versions:{self.mc_version}"])
        params = {
            "query": self.query,
            "facets": json.dumps(facets),
            "limit": MOD_PAGE_SIZE,
            "offset": self.offset,
            "index": "relevance" if self.query else "downloads",
        }
        try:
            r = HTTP.get(f"{MODRINTH_API}/search", params=params, timeout=(5, 10))
            if r.status_code != 200:
                raise ValueError(f"Modrinth returned HTTP {r.status_code}.")
            data = r.json() or {}
            self.results_ready.emit(self.token, self.offset,
                                    list(data.get("hits") or []),
                                    int(data.get("total_hits") or 0))
        except Exception as e:
            self.failed.emit(self.token, str(e))


class InstalledModsWorker(QThread):
    identified = pyqtSignal(str, dict)  # mods_dir, {filename: meta}

    def __init__(self, mods_dir: str):
        super().__init__()
        self.mods_dir = mods_dir

    def run(self) -> None:
        try:
            result = identify_installed_mods(self.mods_dir)
        except Exception:
            result = {}
        self.identified.emit(self.mods_dir, result)


class ModInstallWorker(QThread):
    """Install one Modrinth project plus its required dependencies."""
    progress = pyqtSignal(str, float)   # key, fraction 0..1 (-1 = indeterminate)
    completed = pyqtSignal(str, list)   # key, installed filenames
    error = pyqtSignal(str, str)        # key, message

    _MAX_DEP_DEPTH = 4

    def __init__(self, project_id: str, slug: str, mc_version: str, instance_dir: str):
        super().__init__()
        self.project_id = project_id
        self.slug = slug or project_id
        self.mc_version = mc_version
        self.instance_dir = instance_dir
        self._installed_projects: dict = {}

    def _fetch_versions(self, project_id: str) -> list:
        params = {
            "loaders": json.dumps(["fabric"]),
            "game_versions": json.dumps([self.mc_version]),
        }
        r = HTTP.get(f"{MODRINTH_API}/project/{project_id}/version", params=params, timeout=(5, 15))
        if r.status_code == 404:
            return []
        if r.status_code != 200:
            raise ValueError(f"Modrinth API error (HTTP {r.status_code}).")
        data = r.json()
        return data if isinstance(data, list) else []

    def _resolve(self, project_id: str, plan: list, visited: set, depth: int, is_main: bool) -> None:
        if project_id in visited or depth > self._MAX_DEP_DEPTH:
            return
        visited.add(project_id)
        # Dependencies are resolved even when already installed: a new mod
        # often needs a newer build of its library (e.g. Fabric API, Sodium
        # for Iris), and an outdated one makes Fabric refuse to start. An
        # up-to-date copy is simply skipped at download time.
        version = _pick_mod_version(self._fetch_versions(project_id))
        if version is None:
            if is_main:
                raise ValueError(f"No Fabric build of this mod supports Minecraft {self.mc_version}.")
            return
        file_info = _primary_file(version)
        if not file_info or "url" not in file_info:
            if is_main:
                raise ValueError("No downloadable file found for this mod version.")
            return
        plan.append((project_id, file_info, is_main))
        for dep in version.get("dependencies") or []:
            dep_id = dep.get("project_id")
            if dep.get("dependency_type") == "required" and dep_id:
                self._resolve(dep_id, plan, visited, depth + 1, False)

    def _safe_remove(self, mods_dir: str, filename: str) -> None:
        name = sanitize_mod_filename(filename)
        if not name:
            return
        try:
            os.remove(os.path.join(mods_dir, name))
        except OSError:
            pass

    def run(self) -> None:
        key = self.project_id
        try:
            mods_dir = os.path.join(self.instance_dir, "mods")
            os.makedirs(mods_dir, exist_ok=True)
            self.progress.emit(key, -1.0)

            # Exact inventory of what's already installed (hash lookup), so
            # dependencies aren't duplicated under a different filename -
            # two copies of the same mod make Fabric refuse to start.
            identified = identify_installed_mods(mods_dir)
            self._installed_projects = {m["project_id"]: fn for fn, m in identified.items()}

            plan: list = []
            self._resolve(self.project_id, plan, set(), 0, True)

            existing = [f for f in os.listdir(mods_dir) if f.lower().endswith(".jar")]
            total_bytes = sum(
                int(f.get("size") or 0) for _, f, _ in plan
                if sanitize_mod_filename(f.get("filename", "")) not in existing
            )
            done_bytes = {"v": 0}
            last_emit = {"v": -1.0}
            installed: List[str] = []
            new_files: List[str] = []
            to_remove: set = set()

            def is_other_project(f: str, project_id: str) -> bool:
                other = identified.get(f)
                return bool(other) and other.get("project_id") != project_id

            try:
                for project_id, file_info, is_main in plan:
                    filename = sanitize_mod_filename(file_info.get("filename", ""))
                    if filename is None:
                        if is_main:
                            raise ValueError("Unsafe or missing filename returned by Modrinth API.")
                        continue
                    dest = os.path.join(mods_dir, filename)

                    if not os.path.exists(dest) or os.path.getsize(dest) == 0:
                        base = done_bytes["v"]

                        def on_progress(done: int, _total: int, base=base) -> None:
                            if total_bytes <= 0:
                                return
                            frac = min(1.0, (base + done) / total_bytes)
                            if frac - last_emit["v"] >= 0.01 or frac >= 1.0:
                                last_emit["v"] = frac
                                self.progress.emit(key, frac)

                        expected_sha1 = (file_info.get("hashes") or {}).get("sha1")
                        download_mod_file(file_info["url"], expected_sha1, dest, on_progress=on_progress)
                        new_files.append(dest)
                        done_bytes["v"] += int(file_info.get("size") or 0)
                    installed.append(filename)

                    # Older copies of this project, removed only after every
                    # download succeeded.
                    old = self._installed_projects.get(project_id)
                    if old:
                        to_remove.add(old)
                    if project_id == FABRIC_API_PROJECT_ID or is_main:
                        slug = "fabric-api" if project_id == FABRIC_API_PROJECT_ID else self.slug
                        for f in existing:
                            if not is_other_project(f, project_id) and (
                                matches_mod(f, slug) or (is_main and matches_mod(f, self.project_id))
                            ):
                                to_remove.add(f)
            except Exception:
                # All-or-nothing: never leave a mod without its dependencies.
                for path in new_files:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                raise

            for f in to_remove - set(installed):
                self._safe_remove(mods_dir, f)

            self.progress.emit(key, 1.0)
            self.completed.emit(key, installed)
        except Exception as e:
            self.error.emit(key, str(e))


class IconLoader(QObject):
    """Async, disk-cached loader for Modrinth project icons.

    Downloads run on a small thread pool (sharing the keep-alive HTTP
    session); decoded + downscaled QImages are handed back to the GUI
    thread through a queued signal and kept in an in-memory pixmap cache.
    """
    icon_ready = pyqtSignal(str, QPixmap)
    _decoded = pyqtSignal(str, QImage)

    _MAX_PX = 96

    def __init__(self, cache_dir: str, parent=None) -> None:
        super().__init__(parent)
        self._cache_dir = cache_dir
        self._pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="vanta-icon")
        self._mem: dict = {}
        self._pending: set = set()
        self._decoded.connect(self._on_decoded)
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except OSError:
            pass

    def get(self, url: str) -> Optional[QPixmap]:
        return self._mem.get(url)

    def request(self, url: str) -> None:
        if not url or url in self._mem or url in self._pending:
            return
        self._pending.add(url)
        try:
            self._pool.submit(self._load, url)
        except RuntimeError:
            self._pending.discard(url)

    def shutdown(self) -> None:
        try:
            self._pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    def _load(self, url: str) -> None:
        path = os.path.join(self._cache_dir, hashlib.sha1(url.encode("utf-8")).hexdigest() + ".img")
        data = None
        try:
            if os.path.exists(path):
                with open(path, "rb") as f:
                    data = f.read()
        except OSError:
            data = None
        if not data:
            try:
                r = HTTP.get(url, timeout=(4, 8))
                if r.status_code == 200 and r.content:
                    data = r.content
                    tmp = path + ".part"
                    with open(tmp, "wb") as f:
                        f.write(data)
                    os.replace(tmp, path)
            except Exception:
                pass
        image = QImage()
        if data:
            image.loadFromData(data)
        if not image.isNull() and (image.width() > self._MAX_PX or image.height() > self._MAX_PX):
            image = image.scaled(self._MAX_PX, self._MAX_PX,
                                 Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
        try:
            self._decoded.emit(url, image)
        except RuntimeError:
            pass  # loader destroyed during shutdown

    def _on_decoded(self, url: str, image: QImage) -> None:
        self._pending.discard(url)
        if image.isNull():
            return
        pixmap = QPixmap.fromImage(image)
        self._mem[url] = pixmap
        self.icon_ready.emit(url, pixmap)


class JavaLocateWorker(QThread):
    """Runs find_system_java() off the GUI thread (it spawns `java -version` per candidate)."""
    located = pyqtSignal(object)

    def __init__(self, min_major: Optional[int]):
        super().__init__()
        self.min_major = min_major

    def run(self) -> None:
        try:
            path = find_system_java(self.min_major)
        except Exception:
            path = None
        self.located.emit(path)


def _lerp_color(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(
        round(a.red() + (b.red() - a.red()) * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue() + (b.blue() - a.blue()) * t),
        round(a.alpha() + (b.alpha() - a.alpha()) * t),
    )


def _ui_font(px: int, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    font = QFont("Segoe UI")
    font.setPixelSize(px)
    font.setWeight(weight)
    return font


class _AnimValue(QObject):
    """One eased float that repaints its owner widget on every step."""

    def __init__(self, owner: QWidget, value: float = 0.0, duration: int = 150,
                 easing: QEasingCurve.Type = QEasingCurve.Type.OutCubic) -> None:
        super().__init__(owner)
        self.value = float(value)
        self._owner = owner
        self._target = float(value)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(duration)
        self._anim.setEasingCurve(easing)
        self._anim.valueChanged.connect(self._set)

    def _set(self, v) -> None:
        self.value = float(v)
        self._owner.update()

    def animate_to(self, target: float) -> None:
        target = float(target)
        if target == self._target and (
            self._anim.state() == QVariantAnimation.State.Running or self.value == target
        ):
            return
        self._target = target
        self._anim.stop()
        self._anim.setStartValue(self.value)
        self._anim.setEndValue(target)
        self._anim.start()

    def set_now(self, v: float) -> None:
        self._anim.stop()
        self.value = self._target = float(v)
        self._owner.update()


class _Ticker(QObject):
    """Shared ~30 fps heartbeat for spinners and skeleton pulses.

    One timer for every animated card instead of one per card; it stops
    itself as soon as nothing is connected.
    """
    tick = pyqtSignal()
    _instance = None

    @classmethod
    def instance(cls) -> "_Ticker":
        if cls._instance is None:
            cls._instance = _Ticker()
        return cls._instance

    def __init__(self) -> None:
        super().__init__()
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._on_timeout)

    def subscribe(self, slot) -> None:
        self.tick.connect(slot)
        if not self._timer.isActive():
            self._timer.start()

    def unsubscribe(self, slot) -> None:
        try:
            self.tick.disconnect(slot)
        except (TypeError, RuntimeError):
            pass

    def _on_timeout(self) -> None:
        if self.receivers(self.tick) == 0:
            self._timer.stop()
            return
        self.tick.emit()


class SmoothButton(QPushButton):
    """Custom-painted accent button with animated hover/press colour and a press-in inset.

    Painted directly instead of via setStyleSheet() per animation frame,
    which forced a full style re-polish of the button ~60 times a second.
    """

    def __init__(self, text="", parent=None, base="#0A84FF", hover="#2F95FF", pressed="#0067C0",
                 radius: int = 10):
        super().__init__(text, parent)
        self._base, self._hover, self._pressed = QColor(base), QColor(hover), QColor(pressed)
        self._radius = radius
        self._current = QColor(base)
        self._font = _ui_font(14, QFont.Weight.Bold)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._apply_color)
        self._press = _AnimValue(self, 0.0, 110)

    def _target_color(self):
        if not self.isEnabled():
            return QColor("#3A3A3C")
        if self.isDown():
            return self._pressed
        if self.underMouse():
            return self._hover
        return self._base

    def _update_target(self):
        target = self._target_color()
        self._anim.stop()
        self._anim.setStartValue(self._current)
        self._anim.setEndValue(target)
        self._anim.start()

    def _apply_color(self, color):
        self._current = QColor(color)
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        inset = 1.5 * self._press.value
        rect = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)
        enabled = self.isEnabled()
        color = self._current if enabled else QColor("#3A3A3C")

        grad = QLinearGradient(0, rect.top(), 0, rect.bottom())
        grad.setColorAt(0.0, color.lighter(114) if enabled else color)
        grad.setColorAt(1.0, color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(grad))
        painter.drawRoundedRect(rect, self._radius, self._radius)

        if enabled:
            # Hairline top highlight gives the button a bit of depth.
            painter.setPen(QPen(QColor(255, 255, 255, 38), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), self._radius, self._radius)

        painter.setPen(QColor("#FFFFFF") if enabled else QColor("#8E8E93"))
        painter.setFont(self._font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.text())

    def enterEvent(self, e):
        self._update_target()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._update_target()
        self._press.animate_to(0.0)
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        super().mousePressEvent(e)
        self._update_target()
        self._press.animate_to(1.0)

    def mouseReleaseEvent(self, e):
        super().mouseReleaseEvent(e)
        self._update_target()
        self._press.animate_to(0.0)

    def changeEvent(self, e):
        if e.type() == QEvent.Type.EnabledChange:
            self._update_target()
        super().changeEvent(e)


class ToggleSwitch(QCheckBox):
    """iOS-style pill-shaped on/off switch.

    Subclasses QCheckBox so the rest of the codebase keeps using the
    standard ``isChecked()`` / ``setChecked()`` / ``toggled`` API. The
    default QCheckBox::indicator styling is replaced by a custom paint
    that draws a rounded track plus a sliding handle; handle position and
    track colour are animated together so the transition reads as a
    physical switch instead of a checkbox tick.

    The whole row is clickable (see ``hitButton``), so users don't have
    to aim at the 38x22 track - the label area also toggles.
    """

    _TRACK_W = 38
    _TRACK_H = 22
    _TRACK_PAD = 2

    def __init__(self, text: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Hide the default indicator entirely; we draw everything ourselves.
        self.setStyleSheet(
            "QCheckBox { background: transparent; }"
            "QCheckBox::indicator { width: 0; height: 0; }"
        )
        self.setMinimumHeight(self._TRACK_H + 4)
        self._handle_pos: float = 1.0 if self.isChecked() else 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(200)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)
        self.stateChanged.connect(self._on_state)

    def _on_state(self, _state: int) -> None:
        target = 1.0 if self.isChecked() else 0.0
        self._anim.stop()
        if not self.isVisible():
            # Programmatic changes while hidden (settings load) snap.
            self._on_anim(target)
            return
        self._anim.setStartValue(self._handle_pos)
        self._anim.setEndValue(target)
        self._anim.start()

    def _on_anim(self, v: float) -> None:
        self._handle_pos = float(v)
        self.update()

    def hitButton(self, pos: QPoint) -> bool:  # type: ignore[override]
        """Make the entire row clickable, not just the track rectangle."""
        return self.rect().contains(pos)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        enabled = self.isEnabled()

        text = self.text()
        if text:
            painter.setPen(QColor("#FFFFFF") if enabled else QColor("#6A6A6E"))
            painter.setFont(self.font())
            text_w = self._TRACK_W + 8
            text_rect = QRect(0, 0, max(0, self.width() - text_w), self.height())
            painter.drawText(
                text_rect,
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                text,
            )

        track_x = self.width() - self._TRACK_W - self._TRACK_PAD
        track_y = (self.height() - self._TRACK_H) // 2

        if enabled:
            off_c, on_c = QColor("#3A3A3C"), QColor("#30D158")
        else:
            off_c, on_c = QColor("#2A2A2C"), QColor("#1F5C2F")
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_lerp_color(off_c, on_c, self._handle_pos))
        painter.drawRoundedRect(
            track_x, track_y, self._TRACK_W, self._TRACK_H,
            self._TRACK_H // 2, self._TRACK_H // 2,
        )

        handle_size = self._TRACK_H - 4
        handle_max = self._TRACK_W - self._TRACK_H
        handle_x = track_x + 2 + self._handle_pos * handle_max
        handle_y = track_y + 2

        painter.setBrush(QColor(0, 0, 0, 50))
        painter.drawEllipse(QRectF(handle_x, handle_y + 1, handle_size, handle_size))
        painter.setBrush(QColor("#FFFFFF") if enabled else QColor("#98989D"))
        painter.drawEllipse(QRectF(handle_x, handle_y, handle_size, handle_size))


class SegmentedControl(QWidget):
    """Pill-style tab switcher whose highlight glides between segments."""
    currentChanged = pyqtSignal(int)

    def __init__(self, labels, parent=None, height: int = 28, font_px: int = 11) -> None:
        super().__init__(parent)
        self._labels = list(labels)
        self._index = 0
        self._hover = -1
        self._pos = _AnimValue(self, 0.0, 260)
        self._font = _ui_font(font_px, QFont.Weight.DemiBold)
        self.setFixedHeight(height)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def currentIndex(self) -> int:
        return self._index

    def setLabel(self, index: int, text: str) -> None:
        if 0 <= index < len(self._labels) and self._labels[index] != text:
            self._labels[index] = text
            self.update()

    def setCurrentIndex(self, index: int, animate: bool = True, emit: bool = False) -> None:
        if index == self._index or not (0 <= index < len(self._labels)):
            return
        self._index = index
        if animate and self.isVisible():
            self._pos.animate_to(float(index))
        else:
            self._pos.set_now(float(index))
        if emit:
            self.currentChanged.emit(index)

    def _segment_at(self, x: float) -> int:
        n = max(1, len(self._labels))
        return max(0, min(n - 1, int(x // (self.width() / n))))

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.setCurrentIndex(self._segment_at(event.position().x()), emit=True)
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        seg = self._segment_at(event.position().x())
        if seg != self._hover:
            self._hover = seg
            self.update()

    def leaveEvent(self, event) -> None:
        self._hover = -1
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        n = max(1, len(self._labels))
        w, h = self.width(), self.height()
        seg_w = w / n

        painter.setPen(QPen(QColor("#38383A"), 1))
        painter.setBrush(QColor("#232325"))
        painter.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 8, 8)

        painter.setPen(Qt.PenStyle.NoPen)
        if 0 <= self._hover < n and self._hover != self._index:
            painter.setBrush(QColor(255, 255, 255, 12))
            painter.drawRoundedRect(QRectF(self._hover * seg_w + 3, 3, seg_w - 6, h - 6), 6, 6)

        pill = QRectF(self._pos.value * seg_w + 3, 3, seg_w - 6, h - 6)
        grad = QLinearGradient(0, pill.top(), 0, pill.bottom())
        grad.setColorAt(0.0, QColor("#2F95FF"))
        grad.setColorAt(1.0, QColor("#0A84FF"))
        painter.setBrush(QBrush(grad))
        painter.drawRoundedRect(pill, 6, 6)

        painter.setFont(self._font)
        idle, active = QColor("#98989D"), QColor("#FFFFFF")
        for i, label in enumerate(self._labels):
            closeness = max(0.0, 1.0 - abs(self._pos.value - i))
            painter.setPen(_lerp_color(idle, active, closeness))
            painter.drawText(QRectF(i * seg_w, 0, seg_w, h), Qt.AlignmentFlag.AlignCenter, label)


class ModCard(QWidget):
    """One row in the mod browser / installed list, fully custom-painted.

    Painting the icon, text and action button in a single paintEvent keeps
    each row to one widget (fast to create, scroll and animate) and lets
    the whole card fade + slide in with a single painter transform.

    ``state`` drives the action button:
      install    - blue "Install"
      queued     - grey "Queued" (hover: "Cancel")
      installing - progress fill with percentage / spinner
      installed  - green check (hover: red "Remove")
      remove     - neutral "Remove" (installed tab)
      required   - locked "Required" (Fabric API)
      skeleton   - loading placeholder, no button
    """
    action_clicked = pyqtSignal(object)
    open_requested = pyqtSignal(object)

    HEIGHT = 60
    _ICON = 38
    _BTN_W = 86
    _BTN_H = 28
    _PALETTE = ("#0A84FF", "#30D158", "#FF9F0A", "#BF5AF2", "#FF375F", "#64D2FF", "#FFD60A", "#5E5CE6")
    _fonts: dict = {}

    def __init__(self, info: dict, parent=None) -> None:
        super().__init__(parent)
        self.info = info
        self.state = "skeleton" if info.get("skeleton") else info.get("state", "install")
        self.progress = -1.0
        self.icon: Optional[QPixmap] = None
        self._hover = _AnimValue(self, 0.0, 150)
        self._btn_hover = _AnimValue(self, 0.0, 130)
        self._appear = _AnimValue(self, 1.0, 360)
        self._prog = _AnimValue(self, 0.0, 240)
        self._flash = _AnimValue(self, 0.0, 900, QEasingCurve.Type.OutQuad)
        self._btn_down = False
        self._over_btn = False
        self._ticking = False
        self.setFixedHeight(self.HEIGHT)
        self.setMouseTracking(True)
        tip = info.get("tooltip")
        if tip:
            self.setToolTip(tip)
        if self.state == "skeleton":
            self._set_ticking(True)

    # ---- fonts / helpers -------------------------------------------------
    @classmethod
    def _font(cls, name: str) -> QFont:
        if not cls._fonts:
            cls._fonts = {
                "title": _ui_font(12, QFont.Weight.DemiBold),
                "meta": _ui_font(10),
                "btn": _ui_font(11, QFont.Weight.DemiBold),
                "letter": _ui_font(16, QFont.Weight.Bold),
            }
        return cls._fonts[name]

    def _btn_rect(self) -> QRectF:
        return QRectF(self.width() - self._BTN_W - 12, (self.HEIGHT - self._BTN_H) / 2,
                      self._BTN_W, self._BTN_H)

    def _clickable(self) -> bool:
        return self.state in ("install", "queued", "installed", "remove")

    def _set_ticking(self, on: bool) -> None:
        if on == self._ticking:
            return
        self._ticking = on
        if on:
            _Ticker.instance().subscribe(self.update)
        else:
            _Ticker.instance().unsubscribe(self.update)

    # ---- public API ----------------------------------------------------------
    def play_appear(self, delay_ms: int) -> None:
        self._appear.set_now(0.0)
        if delay_ms <= 0:
            self._start_appear()
        else:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._start_appear)
            timer.timeout.connect(timer.deleteLater)
            timer.start(delay_ms)

    def _start_appear(self) -> None:
        self._appear.animate_to(1.0)

    def set_icon(self, pixmap: Optional[QPixmap]) -> None:
        self.icon = pixmap
        self.update()

    def set_state(self, state: str, progress: Optional[float] = None) -> None:
        if progress is not None:
            self.progress = progress
            if progress >= 0:
                if state == "installing" and self.state != "installing":
                    self._prog.set_now(0.0)
                self._prog.animate_to(progress)
        if state != self.state:
            if state == "installing" and self.state != "installing":
                self._prog.set_now(max(0.0, self.progress))
            self.state = state
            self.setCursor(Qt.CursorShape.PointingHandCursor if (self._over_btn and self._clickable())
                           else Qt.CursorShape.ArrowCursor)
        self._set_ticking(state == "installing" and self.progress < 0)
        self.update()

    def flash(self) -> None:
        """Brief green glow confirming a successful install."""
        self._flash.set_now(1.0)
        self._flash.animate_to(0.0)

    # ---- events --------------------------------------------------------------
    def enterEvent(self, event) -> None:
        self._hover.animate_to(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover.animate_to(0.0)
        self._btn_hover.animate_to(0.0)
        self._over_btn = False
        self._btn_down = False
        super().leaveEvent(event)

    def mouseMoveEvent(self, event) -> None:
        over = self._btn_rect().contains(event.position())
        if over != self._over_btn:
            self._over_btn = over
            self._btn_hover.animate_to(1.0 if over else 0.0)
            self.setCursor(Qt.CursorShape.PointingHandCursor if (over and self._clickable())
                           else Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:
        if (event.button() == Qt.MouseButton.LeftButton and self._clickable()
                and self._btn_rect().contains(event.position())):
            self._btn_down = True
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._btn_down:
            self._btn_down = False
            self.update()
            if self._btn_rect().contains(event.position()) and self._clickable():
                self.action_clicked.emit(self)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if not self._btn_rect().contains(event.position()) and self.info.get("url"):
            self.open_requested.emit(self)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def hideEvent(self, event) -> None:
        self._set_ticking(False)
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.state == "skeleton" or (self.state == "installing" and self.progress < 0):
            self._set_ticking(True)

    # ---- painting ------------------------------------------------------------
    def _button_style(self):
        """(bg, fg, text) for the current state + hover amount."""
        hv = self._btn_hover.value
        if self.state == "install":
            bg = _lerp_color(QColor("#0A84FF"), QColor("#2F95FF"), hv)
            if self._btn_down:
                bg = QColor("#0067C0")
            return bg, QColor("#FFFFFF"), "Install"
        if self.state == "queued":
            return (_lerp_color(QColor("#3A3A3C"), QColor("#48484A"), hv), QColor("#D1D1D6"),
                    "Cancel" if hv > 0.5 else "Queued")
        if self.state == "installed":
            bg = _lerp_color(QColor(48, 209, 88, 40), QColor(255, 69, 58, 46), hv)
            fg = _lerp_color(QColor("#30D158"), QColor("#FF6961"), hv)
            return bg, fg, ("Remove" if hv > 0.5 else "✓ Installed")
        if self.state == "remove":
            bg = _lerp_color(QColor("#2C2C2E"), QColor("#FF453A"), hv)
            fg = _lerp_color(QColor("#D1D1D6"), QColor("#FFFFFF"), hv)
            return bg, fg, "Remove"
        if self.state == "required":
            return QColor("#2A2A2C"), QColor("#8E8E93"), "Required"
        return QColor(10, 132, 255, 56), QColor("#FFFFFF"), ""

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        appear = self._appear.value
        if appear < 1.0:
            painter.setOpacity(max(0.0, appear))
            painter.translate(0, (1.0 - appear) * 10)

        w, h = self.width(), self.HEIGHT
        card = QRectF(0.5, 0.5, w - 1, h - 1)
        hv = self._hover.value
        bg = _lerp_color(QColor("#242426"), QColor("#2C2C2F"), hv)
        if self._flash.value > 0:
            bg = _lerp_color(bg, QColor(48, 209, 88, 255), self._flash.value * 0.22)
        painter.setPen(QPen(_lerp_color(QColor("#2F2F32"), QColor("#3E3E42"), hv), 1))
        painter.setBrush(bg)
        painter.drawRoundedRect(card, 10, 10)

        icon_rect = QRectF(11, (h - self._ICON) / 2, self._ICON, self._ICON)

        if self.state == "skeleton":
            pulse = 0.5 + 0.5 * math.sin(time.monotonic() * 4.0)
            shade = QColor(255, 255, 255, int(14 + 14 * pulse))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(shade)
            painter.drawRoundedRect(icon_rect, 9, 9)
            painter.drawRoundedRect(QRectF(60, 16, w * 0.35, 9), 4, 4)
            painter.drawRoundedRect(QRectF(60, 33, w * 0.55, 8), 4, 4)
            return

        # Icon (or a coloured monogram while it loads / if it has none).
        painter.save()
        clip = QPainterPath()
        clip.addRoundedRect(icon_rect, 9, 9)
        painter.setClipPath(clip)
        if self.icon is not None and not self.icon.isNull():
            painter.fillRect(icon_rect, QColor("#1C1C1E"))
            painter.drawPixmap(icon_rect.toRect(), self.icon)
        else:
            title = self.info.get("title") or "?"
            color = QColor(self._PALETTE[sum(map(ord, title)) % len(self._PALETTE)])
            grad = QLinearGradient(0, icon_rect.top(), 0, icon_rect.bottom())
            grad.setColorAt(0.0, color.lighter(120))
            grad.setColorAt(1.0, color.darker(135))
            painter.fillRect(icon_rect, QBrush(grad))
            painter.setPen(QColor("#FFFFFF"))
            painter.setFont(self._font("letter"))
            painter.drawText(icon_rect, Qt.AlignmentFlag.AlignCenter, title[:1].upper())
        painter.restore()

        btn = self._btn_rect()
        text_x = icon_rect.right() + 11
        text_w = max(20.0, btn.left() - 10 - text_x)

        # Title + author on line one.
        title_font = self._font("title")
        meta_font = self._font("meta")
        title = self.info.get("title") or ""
        author = self.info.get("author") or ""
        fm_title = QFontMetrics(title_font)
        title_text = fm_title.elidedText(title, Qt.TextElideMode.ElideRight, int(text_w))
        painter.setFont(title_font)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(QRectF(text_x, 11, text_w, 18),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title_text)
        used = fm_title.horizontalAdvance(title_text)
        if author and used + 30 < text_w:
            fm_meta = QFontMetrics(meta_font)
            by = fm_meta.elidedText(f"by {author}", Qt.TextElideMode.ElideRight, int(text_w - used - 6))
            painter.setFont(meta_font)
            painter.setPen(QColor("#8E8E93"))
            painter.drawText(QRectF(text_x + used + 6, 12, text_w - used - 6, 18),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, by)

        # Meta / description on line two.
        subtitle = self.info.get("subtitle") or ""
        painter.setFont(meta_font)
        painter.setPen(QColor("#9A9AA0"))
        sub_text = QFontMetrics(meta_font).elidedText(subtitle, Qt.TextElideMode.ElideRight, int(text_w))
        painter.drawText(QRectF(text_x, 31, text_w, 16),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, sub_text)

        # Action button.
        bg_c, fg_c, label = self._button_style()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg_c)
        painter.drawRoundedRect(btn, 8, 8)
        painter.setFont(self._font("btn"))
        if self.state == "installing":
            if self.progress >= 0:
                fill = QRectF(btn.left(), btn.top(), btn.width() * max(0.0, min(1.0, self._prog.value)), btn.height())
                painter.save()
                clip = QPainterPath()
                clip.addRoundedRect(btn, 8, 8)
                painter.setClipPath(clip)
                painter.fillRect(fill, QColor("#0A84FF"))
                painter.restore()
                painter.setPen(QColor("#FFFFFF"))
                painter.drawText(btn, Qt.AlignmentFlag.AlignCenter, f"{int(self._prog.value * 100)}%")
            else:
                size = 14.0
                arc = QRectF(btn.center().x() - size / 2, btn.center().y() - size / 2, size, size)
                painter.setPen(QPen(QColor("#FFFFFF"), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                start = int(-(time.monotonic() * 360 * 1.4) % 360 * 16)
                painter.drawArc(arc, start, 100 * 16)
        else:
            painter.setPen(fg_c)
            painter.drawText(btn, Qt.AlignmentFlag.AlignCenter, label)


class ModListView(QScrollArea):
    """Scrollable column of ModCards with eased wheel scrolling and empty/loading states."""
    near_bottom = pyqtSignal()
    cleared = pyqtSignal()  # emitted after all cards were scheduled for deletion

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("modList")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._inner = QWidget(objectName="modListInner")
        self._layout = QVBoxLayout(self._inner)
        self._layout.setContentsMargins(0, 0, 6, 0)
        self._layout.setSpacing(6)

        self._message = QLabel(objectName="modListMessage")
        self._message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message.setWordWrap(True)
        self._message.setTextFormat(Qt.TextFormat.RichText)
        self._message.setMinimumHeight(140)
        self._message.hide()
        self._layout.addWidget(self._message)
        self._layout.addStretch(1)
        self.setWidget(self._inner)

        self.cards: List[ModCard] = []
        self._scroll_anim = QVariantAnimation(self)
        self._scroll_anim.setDuration(240)
        self._scroll_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._scroll_anim.valueChanged.connect(lambda v: self.verticalScrollBar().setValue(int(v)))
        self._scroll_target = 0
        self.verticalScrollBar().valueChanged.connect(self._check_bottom)

    def clear(self) -> None:
        self._scroll_anim.stop()
        for card in self.cards:
            card.hide()
            card.deleteLater()
        self.cards = []
        self._message.hide()
        self.verticalScrollBar().setValue(0)
        self.cleared.emit()

    def add_cards(self, cards: List[ModCard], animate: bool = True) -> None:
        self._message.hide()
        for i, card in enumerate(cards):
            self._layout.insertWidget(self._layout.count() - 1, card)
            self.cards.append(card)
            if animate:
                card.play_appear(min(i, 12) * 26)

    def show_skeletons(self, count: int = 5) -> None:
        self.clear()
        self.add_cards([ModCard({"skeleton": True}) for _ in range(count)], animate=False)

    def show_message(self, title: str, subtitle: str = "") -> None:
        self.clear()
        html = f"<div style='color:#E5E5EA; font-size:13px; font-weight:600;'>{title}</div>"
        if subtitle:
            html += f"<div style='color:#8E8E93; font-size:11px; margin-top:4px;'>{subtitle}</div>"
        self._message.setText(html)
        self._message.show()

    def wheelEvent(self, event) -> None:
        # Touchpads already deliver smooth pixel deltas; only ease notched wheels.
        if not event.pixelDelta().isNull() or event.angleDelta().y() == 0:
            super().wheelEvent(event)
            return
        bar = self.verticalScrollBar()
        base = self._scroll_target if self._scroll_anim.state() == QVariantAnimation.State.Running else bar.value()
        target = max(bar.minimum(), min(bar.maximum(), int(base - event.angleDelta().y() * 0.75)))
        self._scroll_target = target
        self._scroll_anim.stop()
        self._scroll_anim.setStartValue(bar.value())
        self._scroll_anim.setEndValue(target)
        self._scroll_anim.start()
        event.accept()

    def _check_bottom(self, value: int) -> None:
        bar = self.verticalScrollBar()
        if bar.maximum() > 0 and bar.maximum() - value < 140:
            self.near_bottom.emit()


class ShadowCanvas(QWidget):
    """Central widget that paints soft drop shadows behind the floating panels.

    Replaces QGraphicsDropShadowEffect on the card: a graphics effect
    re-renders the whole card offscreen on every child repaint (the
    progress-bar shimmer alone triggers that 60x/s). Stacked translucent
    rounded rects cost a handful of fills and only when this widget repaints.
    """

    _LAYERS = 9

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._panels: List[QWidget] = []

    def set_panels(self, panels: List[QWidget]) -> None:
        self._panels = list(panels)
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        for panel in self._panels:
            if not panel.isVisible():
                continue
            r = QRectF(panel.geometry())
            for i in range(self._LAYERS, 0, -1):
                alpha = int(11 * (1.0 - i / (self._LAYERS + 1)) + 1)
                painter.setBrush(QColor(0, 0, 0, alpha))
                painter.drawRoundedRect(r.adjusted(-i, -i + 3, i, i + 3), 16 + i, 16 + i)


class EaseAnimator(QObject):
    """Drives a value from start to end at a precise 60 FPS with ease-out cubic interpolation."""
    valueChanged = pyqtSignal(float)

    def __init__(self, duration_ms: int, callback, parent=None):
        super().__init__(parent)
        self._duration = max(1, duration_ms)
        self._callback = callback
        self._from = 0.0
        self._to = 0.0
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self._clock = QElapsedTimer()
        self._running = False

    def start(self, start_value: float, end_value: float) -> None:
        self._from = float(start_value)
        self._to = float(end_value)
        self._clock.restart()
        self._running = True
        try:
            self._callback(self._from)
        except (RuntimeError, ReferenceError):
            self._running = False
            return
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self._running = False

    def finish(self) -> None:
        """Stop and apply the end value immediately."""
        if not self._running:
            return
        self.stop()
        try:
            self._callback(self._to)
        except (RuntimeError, ReferenceError):
            pass

    def is_running(self) -> bool:
        return self._running

    def _tick(self) -> None:
        elapsed = self._clock.elapsed()
        if elapsed >= self._duration:
            self._timer.stop()
            self._running = False
            try:
                self._callback(self._to)
            except (RuntimeError, ReferenceError):
                pass
            return
        t = elapsed / self._duration
        eased = 1.0 - (1.0 - t) ** 3
        try:
            self._callback(self._from + (self._to - self._from) * eased)
        except (RuntimeError, ReferenceError):
            self.stop()


class VantaDialog(QDialog):
    """Dark, launcher-styled replacement for native QMessageBox.

    Supports long messages by splitting them into a primary summary and a
    collapsible "Details" section. The details pane uses a monospace font so
    log tails and stack traces stay readable. An optional log-folder path can
    be passed to expose an "Open log folder" button for crash triage.
    """

    _STYLE = """
        #dialogCard {{
            background-color: #1C1C1E;
            border: 1px solid #2C2C2E;
            border-radius: 14px;
        }}
        #dialogIcon {{
            background-color: {accent_bg};
            border-radius: 18px;
            color: {accent_fg};
            font-family: 'Segoe UI', sans-serif;
            font-size: 20px;
            font-weight: bold;
        }}
        #dialogTitle {{
            color: #FFFFFF;
            font-family: 'Segoe UI', sans-serif;
            font-size: 14px;
            font-weight: bold;
            background: transparent;
        }}
        #dialogMessage {{
            color: #B0B0B5;
            font-family: 'Segoe UI', sans-serif;
            font-size: 12px;
            background: transparent;
        }}
        #dialogOkBtn, #dialogYesBtn {{
            background-color: {btn_bg};
            border: none;
            border-radius: 8px;
            color: #FFFFFF;
            font-family: 'Segoe UI', sans-serif;
            font-size: 12px;
            font-weight: bold;
            padding: 8px 0;
        }}
        #dialogOkBtn:hover, #dialogYesBtn:hover {{
            background-color: {btn_hover};
        }}
        #dialogNoBtn {{
            background-color: #2C2C2E;
            border: none;
            border-radius: 8px;
            color: #B0B0B5;
            font-family: 'Segoe UI', sans-serif;
            font-size: 12px;
            font-weight: bold;
            padding: 8px 0;
        }}
        #dialogNoBtn:hover {{
            background-color: #3A3A3C;
            color: #FFFFFF;
        }}
        #dialogCopyBtn, #dialogDetailsBtn, #dialogLogBtn {{
            background-color: transparent;
            border: 1px solid #3A3A3C;
            border-radius: 8px;
            color: #98989D;
            font-family: 'Segoe UI', sans-serif;
            font-size: 12px;
            font-weight: bold;
            padding: 8px 0;
        }}
        #dialogCopyBtn:hover, #dialogDetailsBtn:hover, #dialogLogBtn:hover {{
            border-color: #0A84FF;
            color: #FFFFFF;
        }}
        #dialogDetailsPane {{
            background-color: #161618;
            border: 1px solid #2C2C2E;
            border-radius: 8px;
            color: #B0B0B5;
            font-family: 'Consolas', 'Cascadia Mono', 'Courier New', monospace;
            font-size: 11px;
        }}
        #dialogScroll {{
            background: transparent;
            border: none;
        }}
        #dialogScroll QScrollBar:vertical {{
            background: transparent;
            width: 6px;
        }}
        #dialogScroll QScrollBar::handle:vertical {{
            background: #3A3A3C;
            border-radius: 3px;
            min-height: 24px;
        }}
        #dialogScroll QScrollBar::add-line:vertical, #dialogScroll QScrollBar::sub-line:vertical {{
            height: 0;
            border: none;
            background: none;
        }}
        #dialogScroll QScrollBar::add-page:vertical, #dialogScroll QScrollBar::sub-page:vertical {{
            background: none;
        }}
    """

    _KIND = {
        "error": {"glyph": "!", "accent_bg": "rgba(255, 69, 58, 0.18)",
                  "accent_fg": "#FF453A", "btn_bg": "#FF453A", "btn_hover": "#E03B31"},
        "warning": {"glyph": "!", "accent_bg": "rgba(255, 159, 10, 0.18)",
                    "accent_fg": "#FF9F0A", "btn_bg": "#FF9F0A", "btn_hover": "#E08C08"},
        "info": {"glyph": "i", "accent_bg": "rgba(10, 132, 255, 0.18)",
                 "accent_fg": "#0A84FF", "btn_bg": "#0A84FF", "btn_hover": "#0069D9"},
    }

    # When the message is long enough, everything after the first line break
    # block is shown as collapsible "Details" with a monospace font. Tuned so
    # short user-facing errors stay one-card while real crash dumps still fit.
    _DETAILS_TRIGGER_LEN = 200
    _MAX_PRIMARY_LEN = 480

    def __init__(self, parent, kind: str, title: str, message: str, buttons,
                 details: Optional[str] = None,
                 log_dir: Optional[str] = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        self.result_choice = None
        self._log_dir = log_dir
        # Stash the accept button's result so Enter can replay it without
        # scanning the original `buttons` tuple from the closure.
        self._accept_result = next(
            (r for _, is_accept, r in buttons if is_accept), None
        )

        k = self._KIND.get(kind, self._KIND["info"])
        self.setStyleSheet(self._STYLE.format(**k))

        # Split the incoming message into a primary summary (user-friendly
        # prose) and the verbose tail (log lines, stack traces). The split
        # is the first blank line, falling back to the first paragraph break.
        primary, auto_details = self._split_message(message)
        if details is not None:
            full_details = auto_details + ("\n" + details if auto_details else details)
        else:
            full_details = auto_details

        card = QFrame(self, objectName="dialogCard")
        card.setMinimumWidth(360)
        card.setMaximumWidth(560)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(12)
        icon = QLabel(k["glyph"], objectName="dialogIcon")
        icon.setFixedSize(36, 36)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        title_lbl = QLabel(title, objectName="dialogTitle")
        title_lbl.setWordWrap(True)
        header.addWidget(title_lbl, 1)
        layout.addLayout(header)

        msg_lbl = QLabel(primary, objectName="dialogMessage")
        msg_lbl.setWordWrap(True)
        msg_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        if len(primary) > 260:
            scroll = QScrollArea(objectName="dialogScroll")
            scroll.setWidgetResizable(True)
            scroll.setWidget(msg_lbl)
            scroll.setFixedHeight(170)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            layout.addWidget(scroll)
        else:
            layout.addWidget(msg_lbl)

        self._details_pane: Optional[QScrollArea] = None
        self._details_toggle: Optional[QPushButton] = None
        if full_details:
            self._details_toggle = QPushButton("Show details", objectName="dialogDetailsBtn")
            self._details_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
            self._details_toggle.setFixedHeight(28)
            self._details_toggle.clicked.connect(self._toggle_details)
            layout.addWidget(self._details_toggle)

            details_view = QLabel(full_details.rstrip(), objectName="dialogDetailsPane")
            details_view.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            details_view.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
            details_view.setWordWrap(True)
            details_view.setContentsMargins(10, 8, 10, 8)

            self._details_pane = QScrollArea(objectName="dialogScroll")
            self._details_pane.setWidgetResizable(True)
            self._details_pane.setWidget(details_view)
            self._details_pane.setFixedHeight(160)
            self._details_pane.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self._details_pane.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self._details_pane.hide()
            layout.addWidget(self._details_pane)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        if len(message) > 120 or full_details:
            copy_btn = QPushButton("Copy", objectName="dialogCopyBtn")
            copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            copy_btn.setFixedHeight(32)
            copy_btn.setFixedWidth(72)
            copy_btn.setToolTip("Copy full details to clipboard")

            def _copy_details() -> None:
                if full_details:
                    payload = f"{title}\n\n{primary}\n\n--- Details ---\n{full_details}"
                else:
                    payload = f"{title}\n\n{primary}"
                QApplication.clipboard().setText(payload)
                copy_btn.setText("Copied!")
                copy_btn.setStyleSheet("color: #30D158; border-color: #30D158;")
                QTimer.singleShot(1600, lambda: (
                    copy_btn.setText("Copy"),
                    copy_btn.setStyleSheet(""),
                ))

            copy_btn.clicked.connect(_copy_details)
            btn_row.addWidget(copy_btn)

        if log_dir and os.path.isdir(log_dir):
            log_btn = QPushButton("Open logs", objectName="dialogLogBtn")
            log_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            log_btn.setFixedHeight(32)
            log_btn.setFixedWidth(90)
            log_btn.setToolTip(f"Open the logs folder ({log_dir})")
            log_btn.clicked.connect(lambda: self._open_log_dir(log_dir))
            btn_row.addWidget(log_btn)

        btn_row.addStretch(1)
        for text, is_accept, result in buttons:
            name = "dialogYesBtn" if is_accept and len(buttons) > 1 else (
                "dialogNoBtn" if not is_accept and len(buttons) > 1 else "dialogOkBtn"
            )
            btn = QPushButton(text, objectName=name)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(32)
            btn.setFixedWidth(110)
            btn.clicked.connect(lambda _, r=result: self._finish(r))
            btn_row.addWidget(btn)
        layout.addLayout(btn_row)

        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(30)
        shadow.setYOffset(8)
        shadow.setColor(QColor(0, 0, 0, 140))
        card.setGraphicsEffect(shadow)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.addWidget(card)

        self.setMinimumWidth(400)
        self.setMaximumWidth(600)
        self.adjustSize()

        if parent is not None:
            pg = parent.geometry()
            self.move(
                pg.x() + (pg.width() - self.width()) // 2,
                max(pg.y() + 20, pg.y() + (pg.height() - self.height()) // 3),
            )

    @staticmethod
    def _split_message(message: str) -> tuple:
        """Return (primary, details) pair.

        The primary is the user-facing summary. Anything past the first blank
        line, "Last log lines:" marker, or first long block becomes the
        collapsible details. Short messages stay single-section.
        """
        if not message:
            return "", ""
        if len(message) <= VantaDialog._DETAILS_TRIGGER_LEN:
            return message, ""

        markers = ("\n\nLast log lines:", "\nLast log lines:\n",
                   "\n\nTraceback ", "\n\nDetails:\n")
        for marker in markers:
            idx = message.find(marker)
            if idx > 0:
                head = message[:idx].rstrip()
                tail = message[idx + (2 if marker.startswith("\n\n") else 1):]
                if tail.strip():
                    return head, tail
                break

        # Fallback: cut at the longest run of paragraph breaks near 60% length.
        break_idx = message.find("\n\n")
        if break_idx > 0 and break_idx < len(message) * 0.85:
            return message[:break_idx].rstrip(), message[break_idx + 2:].lstrip()

        return message, ""

    def _toggle_details(self) -> None:
        if self._details_pane is None or self._details_toggle is None:
            return
        visible = not self._details_pane.isVisible()
        self._details_pane.setVisible(visible)
        self._details_toggle.setText("Hide details" if visible else "Show details")
        self.adjustSize()

    def _open_log_dir(self, path: str) -> None:
        try:
            if sys.platform == "win32":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except OSError as e:
            sys.stderr.write(f"Could not open log dir {path}: {e}\n")

    def _finish(self, result) -> None:
        self.result_choice = result
        self.accept()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self._finish(None)
            return
        # Enter accepts the dialog (only when focus is not on a text input,
        # so we don't fight with copy/paste in the monospace details pane).
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            focus = self.focusWidget()
            if not isinstance(focus, QLineEdit):
                self._finish(self._accept_result if self._accept_result is not None else True)
                return
        super().keyPressEvent(event)

    @staticmethod
    def _show(parent, kind: str, title: str, message: str, buttons,
              details: Optional[str] = None, log_dir: Optional[str] = None):
        dlg = VantaDialog(parent, kind, title, message, buttons,
                          details=details, log_dir=log_dir)
        dlg.setWindowOpacity(0.0)
        fade = QVariantAnimation(dlg)
        fade.setDuration(170)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        fade.valueChanged.connect(dlg.setWindowOpacity)
        fade.start()
        dlg.exec()
        return dlg.result_choice

    @staticmethod
    def error(parent, title: str, message: str,
              details: Optional[str] = None, log_dir: Optional[str] = None) -> None:
        VantaDialog._show(parent, "error", title, message, [("OK", True, "ok")],
                          details=details, log_dir=log_dir)

    @staticmethod
    def warning(parent, title: str, message: str,
                details: Optional[str] = None, log_dir: Optional[str] = None) -> None:
        VantaDialog._show(parent, "warning", title, message, [("OK", True, "ok")],
                          details=details, log_dir=log_dir)

    @staticmethod
    def info(parent, title: str, message: str,
             details: Optional[str] = None, log_dir: Optional[str] = None) -> None:
        VantaDialog._show(parent, "info", title, message, [("OK", True, "ok")],
                          details=details, log_dir=log_dir)

    @staticmethod
    def question(parent, title: str, message: str, default_yes: bool = False,
                 details: Optional[str] = None, log_dir: Optional[str] = None) -> bool:
        result = VantaDialog._show(
            parent, "info", title, message,
            [("Yes", True, True), ("No", False, False)],
            details=details, log_dir=log_dir,
        )
        return bool(result) if result is not None else default_yes


class ComboPopup(QWidget):
    """Floating dropdown list with dark theme, smooth scrolling, and drop shadow."""
    def __init__(self, combo: "AnchoredComboBox"):
        super().__init__(
            None,
            Qt.WindowType.Popup
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.combo = combo

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(16, 14, 16, 16)

        self.card = QFrame(self, objectName="comboPopupCard")
        self.card.setStyleSheet("""
            #comboPopupCard {
                background-color: #1C1C1E;
                border: 1px solid #2C2C2E;
                border-radius: 10px;
            }
            QListWidget {
                background: transparent;
                border: none;
                outline: 0;
                color: #FFFFFF;
                font-family: 'Segoe UI', -apple-system, sans-serif;
                font-size: 12px;
            }
            QListWidget::item {
                min-height: 26px;
                padding: 4px 10px;
                border-radius: 6px;
                margin: 1px 2px;
                color: #E0E0E0;
            }
            QListWidget::item:hover {
                background-color: #2C2C2E;
                color: #FFFFFF;
            }
            QListWidget::item:selected {
                background-color: #0A84FF;
                color: #FFFFFF;
            }
            QScrollBar:vertical {
                background: transparent;
                width: 6px;
                border: none;
                margin: 4px 2px 4px 0;
            }
            QScrollBar::handle:vertical {
                background: #3A3A3C;
                border-radius: 3px;
                min-height: 24px;
            }
            QScrollBar::handle:vertical:hover {
                background: #505054;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
                border: none;
                background: none;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: none;
            }
        """)

        # Soft, symmetric drop shadow. yOffset is left at 0 so the shadow
        # blooms evenly around the card instead of pooling on one side.
        # Opacity is dialled down so the popup doesn't look like it's
        # floating in tar against the launcher background.
        shadow = QGraphicsDropShadowEffect(self.card)
        shadow.setBlurRadius(16)
        shadow.setOffset(0, 0)
        shadow.setColor(QColor(0, 0, 0, 70))
        self.card.setGraphicsEffect(shadow)

        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(6, 6, 6, 6)

        self.list_widget = QListWidget(self.card)
        self.list_widget.setCursor(Qt.CursorShape.PointingHandCursor)
        self.list_widget.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_widget.itemClicked.connect(self._on_item_selected)
        self.list_widget.itemActivated.connect(self._on_item_selected)
        card_layout.addWidget(self.list_widget)

        outer_layout.addWidget(self.card)

    def repopulate(self) -> None:
        # The version list holds several hundred entries; rebuilding it on
        # every open made the dropdown hitch. Rebuild only when it changed.
        items = tuple(self.combo.itemText(i) for i in range(self.combo.count()))
        if items != getattr(self, "_items", None):
            self.list_widget.setUpdatesEnabled(False)
            self.list_widget.clear()
            self.list_widget.addItems(items)
            self.list_widget.setUpdatesEnabled(True)
            self._items = items
        idx = self.combo.currentIndex()
        if 0 <= idx < self.list_widget.count():
            self.list_widget.setCurrentRow(idx)
            self.list_widget.scrollToItem(self.list_widget.item(idx), QAbstractItemView.ScrollHint.PositionAtCenter)

    def _on_item_selected(self, item: QListWidgetItem) -> None:
        text = item.text()
        # Route through the combobox's animated close so the popup fades
        # out instead of disappearing instantly when a version is picked.
        if text != self.combo.currentText():
            self.combo.setCurrentText(text)
        self.combo.hidePopup()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.combo.hidePopup()
            event.accept()
            return
        super().keyPressEvent(event)

    def hideEvent(self, event) -> None:
        self.combo._last_hide_time = time.monotonic()
        super().hideEvent(event)


class _ComboOutsideClickFilter(QObject):
    """Routes outside clicks on a ComboPopup through the animated close.

    Qt.WindowType.Popup auto-hides the popup the moment the user clicks
    anywhere outside it, which would skip ``AnchoredComboBox.hidePopup()``
    and its close animation. This filter is installed on the QApplication
    while a popup is visible and intercepts the first outside click so
    that the close animation runs regardless of how the popup is closed.
    """

    def __init__(self, combo: "AnchoredComboBox") -> None:
        super().__init__()
        # Hold a weak reference so the filter doesn't keep the combobox
        # alive after the launcher is destroyed.
        self._combo_ref = combo

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # type: ignore[override]
        if event.type() != QEvent.Type.MouseButtonPress:
            return super().eventFilter(obj, event)

        combo = self._combo_ref
        popup = combo._popup if combo is not None else None
        if popup is None or not popup.isVisible():
            return super().eventFilter(obj, event)

        # Resolve the click's global position. PyQt6 exposes globalPosition()
        # on QMouseEvent; older code paths fall back to globalPos().
        try:
            gp = event.globalPosition().toPoint()
        except AttributeError:
            gp = event.globalPos()

        # Clicks inside the popup must pass through to the popup's own
        # handlers (item selection, scrolling, etc.).
        if popup.geometry().contains(gp):
            return super().eventFilter(obj, event)

        # Outside click: trigger the animated close and swallow the event
        # so Qt never gets the chance to hide the popup itself.
        event.accept()
        combo.hidePopup()
        return True


class AnchoredComboBox(QComboBox):
    """Custom combobox with a floating frameless popup list.

    The popup opens with a smooth animation that combines an opacity fade
    and a height expansion anchored at the edge closest to the combobox
    (top for downward growth, bottom for upward growth when there's no
    room below). Closing mirrors the animation in reverse before the
    popup is actually hidden.
    """

    _OPEN_DURATION_MS = 170
    _CLOSE_DURATION_MS = 130

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._popup: Optional[ComboPopup] = None
        self._last_hide_time = 0.0
        self._popup_anim: Optional[QParallelAnimationGroup] = None
        self._grow_upward = False

    def _ensure_popup(self) -> ComboPopup:
        if self._popup is None:
            self._popup = ComboPopup(self)
        return self._popup

    def _stop_popup_animation(self) -> None:
        """Cancel any in-flight open/close animation on the popup."""
        if self._popup_anim is not None:
            try:
                self._popup_anim.stop()
            except RuntimeError:
                # Animation or its target was already destroyed.
                pass
            self._popup_anim = None

    def _install_outside_click_filter(self) -> None:
        """Watch for outside mouse presses while the popup is visible.

        Qt.WindowType.Popup auto-closes the popup the instant the user
        clicks outside it, which would bypass our animated close. This
        filter intercepts those clicks and routes them through
        ``hidePopup()`` instead, so every close path runs the animation.
        """
        if getattr(self, "_outside_filter_installed", False):
            return
        app = QApplication.instance()
        if app is None:
            return
        self._outside_filter = _ComboOutsideClickFilter(self)
        app.installEventFilter(self._outside_filter)
        self._outside_filter_installed = True

    def _remove_outside_click_filter(self) -> None:
        if not getattr(self, "_outside_filter_installed", False):
            return
        app = QApplication.instance()
        if app is not None and getattr(self, "_outside_filter", None) is not None:
            try:
                app.removeEventFilter(self._outside_filter)
            except RuntimeError:
                pass
        self._outside_filter = None
        self._outside_filter_installed = False

    def showPopup(self) -> None:
        if self.count() == 0 or not self.isEnabled():
            return

        popup = self._ensure_popup()
        popup.repopulate()

        row_h = 28
        visible_rows = min(popup.list_widget.count(), 7)
        needed_content_h = max(60, visible_rows * row_h + 16)
        total_h = needed_content_h + 30
        total_w = max(self.width() + 32, 256)

        global_pos = self.mapToGlobal(QPoint(-16, self.height() - 10))

        screen = self.screen() or QApplication.primaryScreen()
        grow_upward = False
        if screen is not None:
            avail = screen.availableGeometry()
            if global_pos.y() + total_h > avail.bottom():
                grow_upward = True
                global_pos.setY(self.mapToGlobal(QPoint(0, 0)).y() - total_h + 16)
            if global_pos.x() + total_w > avail.right():
                global_pos.setX(avail.right() - total_w)
            if global_pos.x() < avail.left():
                global_pos.setX(avail.left())

        target_x = global_pos.x()
        target_y = global_pos.y()
        self._grow_upward = grow_upward

        # Start state: zero-height sliver anchored at the edge near the
        # combobox. Downward growth keeps the top at target_y; upward
        # growth keeps the bottom (= target_y + total_h) fixed.
        start_h = 1
        if grow_upward:
            start_y = target_y + total_h - start_h
        else:
            start_y = target_y

        self._stop_popup_animation()
        popup.setWindowOpacity(0.0)
        popup.setGeometry(target_x, start_y, total_w, start_h)
        popup.show()
        popup.raise_()
        popup.list_widget.setFocus()
        self._install_outside_click_filter()

        # Build the open animation: opacity fade + height expansion in
        # parallel, both using OutCubic so the popup feels snappy.
        self._popup_anim = QParallelAnimationGroup(popup)
        geom = QPropertyAnimation(popup, b"geometry")
        geom.setDuration(self._OPEN_DURATION_MS)
        geom.setStartValue(QRect(target_x, start_y, total_w, start_h))
        geom.setEndValue(QRect(target_x, target_y, total_w, total_h))
        geom.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._popup_anim.addAnimation(geom)

        op = QPropertyAnimation(popup, b"windowOpacity")
        op.setDuration(self._OPEN_DURATION_MS)
        op.setStartValue(0.0)
        op.setEndValue(1.0)
        op.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._popup_anim.addAnimation(op)
        self._popup_anim.start()

    def hidePopup(self) -> None:
        if self._popup is None or not self._popup.isVisible():
            return

        popup = self._popup
        self._stop_popup_animation()

        cur_geo = popup.geometry()
        end_h = 1
        if self._grow_upward:
            end_y = cur_geo.y() + cur_geo.height() - end_h
        else:
            end_y = cur_geo.y()

        self._popup_anim = QParallelAnimationGroup(popup)
        geom = QPropertyAnimation(popup, b"geometry")
        geom.setDuration(self._CLOSE_DURATION_MS)
        geom.setStartValue(cur_geo)
        geom.setEndValue(QRect(cur_geo.x(), end_y, cur_geo.width(), end_h))
        geom.setEasingCurve(QEasingCurve.Type.InCubic)
        self._popup_anim.addAnimation(geom)

        op = QPropertyAnimation(popup, b"windowOpacity")
        op.setDuration(self._CLOSE_DURATION_MS)
        op.setStartValue(popup.windowOpacity())
        op.setEndValue(0.0)
        op.setEasingCurve(QEasingCurve.Type.InCubic)
        self._popup_anim.addAnimation(op)

        def _on_close_finished() -> None:
            # Reset opacity so the next open starts cleanly. The animation
            # group may already have been torn down if hidePopup was called
            # twice in quick succession; guard against that.
            try:
                if popup.isVisible():
                    popup.hide()
                popup.setWindowOpacity(1.0)
            except RuntimeError:
                pass
            self._remove_outside_click_filter()

        self._popup_anim.finished.connect(_on_close_finished)
        self._popup_anim.start()

    def mousePressEvent(self, event) -> None:
        if not self.isEnabled():
            return
        if event.button() == Qt.MouseButton.LeftButton:
            if time.monotonic() - getattr(self, "_last_hide_time", 0.0) < 0.25:
                event.accept()
                return
            if self._popup is not None and self._popup.isVisible():
                self.hidePopup()
            else:
                self.showPopup()
            event.accept()
        else:
            super().mousePressEvent(event)


class ShimmerProgressBar(QProgressBar):
    """Progress bar with a soft highlight sweeping left-to-right across the fill.

    Both determinate and indeterminate states share identical shimmer
    geometry and transparency so the animation looks the same everywhere.
    """

    _RANGE = 1.8
    _SPEED = 0.016
    _BAND_FRAC = 0.3
    _TRAILS = ((0.12, 40), (0.06, 80), (0.0, 150))
    _SHIMMER_RGB = (255, 255, 255)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._shimmer_pos = -0.4
        # Displayed fill fraction; eased toward the real value each frame so
        # coarse progress jumps (e.g. 20% -> 60%) glide instead of snapping.
        self._display_frac = 0.0
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._advance)

    def _real_frac(self) -> float:
        maximum = self.maximum()
        if maximum <= self.minimum():
            return 0.0
        frac = (self.value() - self.minimum()) / (maximum - self.minimum())
        return max(0.0, min(1.0, frac))

    def _advance(self) -> None:
        self._shimmer_pos += self._SPEED
        if self._shimmer_pos > 1.4:
            self._shimmer_pos -= self._RANGE
        target = self._real_frac()
        delta = target - self._display_frac
        if delta < -0.25 or abs(delta) < 0.002:
            # Big backwards jump = a new phase started (reset to 0): snap.
            self._display_frac = target
        else:
            self._display_frac += delta * 0.16
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._timer.start()

    def hideEvent(self, event) -> None:
        self._timer.stop()
        super().hideEvent(event)

    def _paint_sweep(self, painter, clip_rect: QRectF, band_w: int) -> None:
        """Draw the unified shimmer trail clipped to clip_rect."""
        r, g, b = self._SHIMMER_RGB
        painter.save()
        clip = QPainterPath()
        clip.addRoundedRect(clip_rect, 9, 9)
        painter.setClipPath(clip)
        for trail, alpha in self._TRAILS:
            band_x = int(
                self.rect().x() + (self._shimmer_pos - trail) * (self.rect().width() + band_w) - band_w
            )
            band_rect = QRect(band_x, int(clip_rect.y()), band_w, int(clip_rect.height()))
            sweep = QLinearGradient(band_x, 0, band_x + band_w, 0)
            sweep.setColorAt(0.0, QColor(r, g, b, 0))
            sweep.setColorAt(0.5, QColor(r, g, b, alpha))
            sweep.setColorAt(1.0, QColor(r, g, b, 0))
            painter.fillRect(band_rect, QBrush(sweep))
        painter.restore()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#3A3A3C"), 1))
        painter.setBrush(QColor("#2C2C2E"))
        painter.drawRoundedRect(0, 0, self.width() - 1, self.height() - 1, 10, 10)

        rect = self.rect().adjusted(2, 2, -2, -2)
        frac = self._real_frac()
        fill_w = int(rect.width() * self._display_frac)

        band_w = max(60, int(rect.width() * self._BAND_FRAC))

        if fill_w > 0:
            fill_rect = QRect(rect.x(), rect.y(), fill_w, rect.height())
            grad = QLinearGradient(fill_rect.x(), 0, fill_rect.x() + rect.width(), 0)
            grad.setColorAt(0.0, QColor("#0A84FF"))
            grad.setColorAt(1.0, QColor("#2F95FF"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(grad))
            painter.drawRoundedRect(fill_rect, 9, 9)
            self._paint_sweep(painter, QRectF(fill_rect), band_w)
        else:
            painter.save()
            clip = QPainterPath()
            clip.addRoundedRect(QRectF(rect), 9, 9)
            painter.setClipPath(clip)
            # Faint blue base tint so the track matches the filled state.
            painter.fillRect(rect, QColor(10, 132, 255, 12))
            painter.restore()
            self._paint_sweep(painter, QRectF(rect), band_w)

        painter.setPen(QColor("#FFFFFF"))
        label_font = QFont(self.font())
        label_font.setPointSizeF(10.0)
        painter.setFont(label_font)
        text = self.format().replace("%p%", f"{int(frac * 100)}%")
        rect_up = self.rect().adjusted(0, -2, 0, -2)
        painter.drawText(rect_up, Qt.AlignmentFlag.AlignCenter, text)


class SplashScreen(QWidget):
    """Frameless startup splash shown instantly while the launcher UI loads."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.SplashScreen
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(340, 190)

        screen = QApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            self.move(
                geo.x() + (geo.width() - self.width()) // 2,
                geo.y() + (geo.height() - self.height()) // 2,
            )

        self._dot_phase = 0
        self._dot_timer = QTimer(self)
        self._dot_timer.setInterval(450)
        self._dot_timer.timeout.connect(self._advance_dots)

        self._bar_pos = -0.4
        self._bar_timer = QTimer(self)
        self._bar_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._bar_timer.setInterval(16)
        self._bar_timer.timeout.connect(self._advance_bar)

        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._icon = self._load_icon()
        self._player_name = ""
        self._player_pixmap = QPixmap()
        self._status_text = None

    def set_player(self, name: str, pixmap: QPixmap) -> None:
        """Show the player's head and name once their avatar has loaded."""
        self._player_name = name or ""
        self._player_pixmap = pixmap if (pixmap is not None and not pixmap.isNull()) else QPixmap()
        self.update()

    def set_status(self, text: Optional[str]) -> None:
        """Override the sub-line text (e.g. 'Loading versions...')."""
        self._status_text = text
        self.update()

    @staticmethod
    def _load_icon() -> QPixmap:
        base = _resource_base()
        for name in ("icon.png", "icon.ico"):
            path = os.path.join(base, "icons", name)
            if os.path.exists(path):
                return QPixmap(path)
        return QPixmap()

    def _advance_dots(self) -> None:
        self._dot_phase = (self._dot_phase + 1) % 4
        self.update()

    def _advance_bar(self) -> None:
        self._bar_pos += 0.018
        if self._bar_pos > 1.4:
            self._bar_pos -= 1.8
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.setWindowOpacity(0.0)
        self._dot_timer.start()
        self._bar_timer.start()
        self._fade.stop()
        self._fade.setDuration(260)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()

    def finish(self) -> None:
        """Fade out and close; call once the main window is visible."""
        self._dot_timer.stop()
        self._bar_timer.stop()
        self._fade.stop()
        self._fade.setDuration(300)
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        try:
            self._fade.finished.disconnect()
        except TypeError:
            pass
        self._fade.finished.connect(self.close)
        self._fade.start()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        w = self.width()
        painter.setPen(QColor("#2C2C2E"))
        painter.setBrush(QColor("#161618"))
        painter.drawRoundedRect(0, 0, w - 1, self.height() - 1, 18, 18)

        has_player = not self._player_pixmap.isNull()

        if has_player:
            avatar = QRect((w - 64) // 2, 24, 64, 64)
            painter.save()
            clip = QPainterPath()
            clip.addRoundedRect(QRectF(avatar), 14, 14)
            painter.setClipPath(clip)
            painter.drawPixmap(avatar, self._player_pixmap)
            painter.restore()
        elif not self._icon.isNull():
            painter.drawPixmap(QRect((w - 64) // 2, 24, 64, 64), self._icon)

        painter.setPen(QColor("#FFFFFF"))
        painter.setFont(QFont("Segoe UI", 15, QFont.Weight.Bold))
        painter.drawText(
            QRect(0, 94, w, 32),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            f"Vanta v{APP_VERSION_RUNTIME}",
        )

        painter.setPen(QColor("#8E8E93"))
        painter.setFont(QFont("Segoe UI", 10))
        if has_player:
            painter.drawText(
                QRect(0, 126, w, 22),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                f"Welcome back, {self._player_name}",
            )
        elif self._status_text:
            painter.drawText(
                QRect(0, 126, w, 22),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                self._status_text,
            )
        else:
            painter.drawText(
                QRect(0, 126, w, 22),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                "Starting up" + "." * self._dot_phase,
            )

        track = QRect((w - 180) // 2, 156, 180, 5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#2C2C2E"))
        painter.drawRoundedRect(track, 2, 2)

        band_w = 60
        painter.save()
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(track), 2.5, 2.5)
        painter.setClipPath(clip)
        for trail, alpha in ((0.14, 45), (0.07, 100), (0.0, 200)):
            band_x = int(
                track.x() + (self._bar_pos - trail) * (track.width() + band_w) - band_w
            )
            band = QRect(band_x, track.y() - 1, band_w, track.height() + 2)
            grad = QLinearGradient(band_x, 0, band_x + band_w, 0)
            grad.setColorAt(0.0, QColor(10, 132, 255, 0))
            grad.setColorAt(0.5, QColor(10, 132, 255, alpha))
            grad.setColorAt(1.0, QColor(10, 132, 255, 0))
            painter.fillRect(band, QBrush(grad))
        painter.restore()


class MinecraftLauncher(QMainWindow):
    _FADE_DURATION = 220
    _EXPAND_DURATION = 380
    _CARD_H = 192
    _MODS_DRAWER_H = 440
    _LAYOUT_DURATION = 380

    def __init__(self, initial_avatar: Optional[QPixmap] = None,
                 preloaded_versions: Optional[List[str]] = None) -> None:
        super().__init__()
        self.minecraft_dir = minecraft_launcher_lib.utils.get_minecraft_directory()
        self.settings = QSettings("Vanta", "Preferences")
        self._preloaded_versions = preloaded_versions if preloaded_versions else None
        self._drag_position = QPoint()
        self._is_closing = False
        self._drawer_expanded = False
        self._drawer_animator = None
        self._window_animator = None
        self.rpc = None
        self._rpc_lock = threading.Lock()
        self._workers: list = []
        self._launch_in_progress = False
        self._update_in_progress = False
        self._pending_update: Optional[tuple] = None
        self._update_check_worker: Optional[UpdateCheckWorker] = None
        self._update_download_worker: Optional[UpdateDownloadWorker] = None
        self._initial_avatar = initial_avatar if (initial_avatar is not None and not initial_avatar.isNull()) else None

        self.vanta_dir = get_vanta_dir()

        # Mods manager state (see the "Mods manager" section below).
        self._layout_animator: Optional[EaseAnimator] = None
        self._total_ram_cached: Optional[int] = None
        self._mods_activated = False
        self._mods_mode = 0  # 0 = Browse, 1 = Installed
        self._mods_queries = ["", ""]
        self._search_token = 0
        self._search_offset = 0
        self._search_total = 0
        self._search_loading = False
        self._search_key: tuple = ("", "")
        self._search_cache: dict = {}
        self._installed_files: List[tuple] = []
        self._installed_meta: dict = {}
        self._installed_projects: dict = {}
        self._installed_token = 0
        self._mod_queue: List[dict] = []
        self._mod_active: Optional[dict] = None
        self._cards: dict = {}
        self._status_restore_timer: Optional[QTimer] = None
        self.icon_loader = IconLoader(os.path.join(self.vanta_dir, "cache", "icons"), self)
        self.icon_loader.icon_ready.connect(self._on_icon_ready)

        self.setWindowOpacity(0.0)
        self._init_ui()

        try:
            installed = [
                v.get("id", "") for v in minecraft_launcher_lib.utils.get_installed_versions(
                    self.minecraft_dir
                )
            ]
            clean_installed = [v for v in installed if v and not v.startswith(("fabric-", "quilt-", "forge-", "neoforge-"))]
            if clean_installed:
                self.version_combo.clear()
                self.version_combo.addItems(clean_installed)
                self.version_combo.setEnabled(True)
        except Exception:
            pass

        self._init_ram_slider()
        QTimer.singleShot(700, self._init_rpc)
        self._load_settings()
        self._fetch_versions()
        QTimer.singleShot(UPDATE_CHECK_DELAY_MS, self._start_update_check)

    def _init_rpc(self) -> None:
        if Presence is None:
            self.rpc = None
            self._set_rpc_unavailable()
            return

        if not _to_bool(self.settings.value("rpc_enabled", True)):
            self.rpc = None
            return

        def connect_discord():
            try:
                with self._rpc_lock:
                    if self.rpc:
                        try:
                            self.rpc.close()
                        except Exception:
                            pass
                    self.rpc = Presence("1509979983874097404")
                    self.rpc.connect()
                    self.rpc.update(
                        state="Free Non-Premium Launcher",
                        details="Playing Minecraft",
                        start=int(time.time())
                    )
            except Exception:
                with self._rpc_lock:
                    self.rpc = None

        threading.Thread(target=connect_discord, daemon=True).start()

    def _update_rpc(self, state: str, details: str) -> None:
        def update_task():
            with self._rpc_lock:
                if self.rpc:
                    try:
                        self.rpc.update(state=state, details=details, start=int(time.time()))
                    except Exception:
                        self.rpc = None

        threading.Thread(target=update_task, daemon=True).start()

    def _set_rpc_unavailable(self) -> None:
        self.rpc_checkbox.blockSignals(True)
        self.rpc_checkbox.setChecked(False)
        self.rpc_checkbox.setEnabled(False)
        self.rpc_checkbox.setText("Discord Rich Presence (pypresence not installed)")
        self.rpc_checkbox.blockSignals(False)
        self.settings.setValue("rpc_enabled", "false")

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_position = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() == Qt.MouseButton.LeftButton and not self._drag_position.isNull():
            self.move(event.globalPosition().toPoint() - self._drag_position)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:
        self._drag_position = QPoint()
        super().mouseReleaseEvent(event)

    def _stop_animations(self) -> None:
        group = getattr(self, "_anim_group", None)
        if group is not None and group.state() == QParallelAnimationGroup.State.Running:
            group.stop()
        if self._layout_animator is not None:
            self._layout_animator.finish()
        for anim_name in ("_drawer_animator", "_window_animator", "_motion_anim"):
            anim = getattr(self, anim_name, None)
            if anim is not None:
                anim.stop()

    @staticmethod
    def _get_taskbar_geometry() -> Optional[QRect]:
        """
        Derive the taskbar rectangle from Qt screen metrics.

        QScreen.availableGeometry() reports the usable desktop area with
        OS-reserved bars (taskbar/dock) already removed, so the taskbar is
        the strip of the full screen geometry not covered by the available
        area. Replaces the former Win32 FindWindowW("Shell_TrayWnd") lookup,
        which antivirus heuristics flagged (FindShellTrayWindow).
        """
        screen = QApplication.primaryScreen()
        if screen is None:
            return None
        full = screen.geometry()
        available = screen.availableGeometry()
        if full == available:
            return None
        if available.top() > full.top():  # taskbar docked at the top
            return QRect(full.left(), full.top(), full.width(), available.top() - full.top())
        if available.bottom() < full.bottom():  # taskbar docked at the bottom
            return QRect(full.left(), available.bottom() + 1, full.width(), full.bottom() - available.bottom())
        if available.left() > full.left():  # taskbar docked at the left
            return QRect(full.left(), full.top(), available.left() - full.left(), full.height())
        if available.right() < full.right():  # taskbar docked at the right
            return QRect(available.right() + 1, full.top(), full.right() - available.right(), full.height())
        return None

    def _fade_out_with_shrink(self, finish_callback, *,
                              target_geo: Optional[QRect] = None,
                              slide_down: bool = False) -> None:
        self._stop_animations()

        opacity = QPropertyAnimation(self, b"windowOpacity")
        opacity.setDuration(self._FADE_DURATION)
        opacity.setStartValue(self.windowOpacity())
        opacity.setEndValue(0.0)
        opacity.setEasingCurve(QEasingCurve.Type.OutCubic)

        geo = QPropertyAnimation(self, b"geometry")
        geo.setDuration(self._FADE_DURATION)
        geo.setStartValue(self.geometry())

        if target_geo is not None:
            self.setMinimumSize(0, 0)
            self.setMaximumSize(16777215, 16777215)
            geo.setEndValue(target_geo)
        elif slide_down:
            r = self.geometry()
            geo.setEndValue(QRect(r.x(), r.y() + 20, r.width(), r.height()))
        else:
            geo.setEndValue(self.geometry())

        geo.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._anim_group = QParallelAnimationGroup()
        self._anim_group.addAnimation(opacity)
        self._anim_group.addAnimation(geo)
        self._anim_group.finished.connect(finish_callback)
        self._anim_group.start()

    def _fade_in(self) -> None:
        self._stop_animations()
        self.setWindowOpacity(self.windowOpacity() if self.windowOpacity() < 1.0 else 0.0)

        opacity = QPropertyAnimation(self, b"windowOpacity")
        opacity.setDuration(self._EXPAND_DURATION)
        opacity.setStartValue(self.windowOpacity())
        opacity.setEndValue(1.0)
        opacity.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._anim_group = QParallelAnimationGroup()
        self._anim_group.addAnimation(opacity)
        self._anim_group.start()

    def _apply_card_shadow(self) -> None:
        # Shadows are painted by the ShadowCanvas central widget (much
        # cheaper than a QGraphicsDropShadowEffect); just make sure no
        # stale effect is left on the card and repaint the canvas.
        self.card.setGraphicsEffect(None)
        central = getattr(self, "_central", None)
        if central is not None:
            central.update()

    def _fade_in_with_motion_blur(self) -> None:
        """Fade in with a strong motion-blur style transition (blur + vertical glide)."""
        self._stop_animations()
        self.setWindowOpacity(0.0)

        start_y = self.y()
        # Remove nested effects (Qt does not support effects on parent + child)
        self.card.setGraphicsEffect(None)

        blur = QGraphicsBlurEffect(self._central)
        blur.setBlurRadius(24.0)
        blur.setBlurHints(QGraphicsBlurEffect.BlurHint.QualityHint)
        self._central.setGraphicsEffect(blur)

        anim = QVariantAnimation(self)
        anim.setDuration(650)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        def apply(t: float) -> None:
            self.setWindowOpacity(t)
            blur.setBlurRadius(24.0 * (1.0 - t))
            self.move(self.x(), round(start_y + 18 * (1.0 - t)))

        def finish() -> None:
            self._central.setGraphicsEffect(None)
            self._apply_card_shadow()
            self.setWindowOpacity(1.0)
            self.move(self.x(), start_y)

        anim.valueChanged.connect(apply)
        anim.finished.connect(finish)
        self._motion_anim = anim
        anim.start()

    def _fade_in_from_taskbar(self) -> None:
        self._stop_animations()
        target = getattr(self, "_restore_geometry", self.geometry())

        taskbar = self._get_taskbar_geometry()
        if taskbar is None:
            self._fade_in()
            return

        cx = taskbar.x() + taskbar.width() // 2
        cy = taskbar.y() + taskbar.height() // 2
        start = QRect(cx - 10, cy - 10, 20, 20)

        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)
        self.setGeometry(start)
        self.setWindowOpacity(0.0)

        opacity = QPropertyAnimation(self, b"windowOpacity")
        opacity.setDuration(self._EXPAND_DURATION)
        opacity.setStartValue(0.0)
        opacity.setEndValue(1.0)
        opacity.setEasingCurve(QEasingCurve.Type.OutCubic)

        geo = QPropertyAnimation(self, b"geometry")
        geo.setDuration(self._EXPAND_DURATION)
        geo.setStartValue(start)
        geo.setEndValue(target)
        geo.setEasingCurve(QEasingCurve.Type.OutBack)
        if hasattr(geo, "setOvershoot"):
            geo.setOvershoot(0.8)

        def on_restored():
            self.setFixedSize(target.size())

        self._anim_group = QParallelAnimationGroup()
        self._anim_group.addAnimation(opacity)
        self._anim_group.addAnimation(geo)
        self._anim_group.finished.connect(on_restored)
        self._anim_group.start()

    def _fade_out_and_minimize(self) -> None:
        if self._layout_animator is not None:
            self._layout_animator.finish()
        self._restore_geometry = self.geometry()
        taskbar = self._get_taskbar_geometry()

        if taskbar is not None:
            cx = taskbar.x() + taskbar.width() // 2
            cy = taskbar.y() + taskbar.height() // 2
            target = QRect(cx, cy, 1, 1)
            self._fade_out_with_shrink(self._minimize_now, target_geo=target)
        else:
            self._fade_out_with_shrink(self._minimize_now, slide_down=True)

    def _minimize_now(self) -> None:
        if hasattr(self, "_restore_geometry"):
            self.setGeometry(self._restore_geometry)
            self.setFixedSize(self._restore_geometry.size())
        self.setWindowOpacity(0.0)
        self.showMinimized()

    def closeEvent(self, event) -> None:
        if not self._is_closing:
            launch_worker = getattr(self, "_launch_worker", None)
            proc = getattr(launch_worker, "process", None)
            if proc is not None and proc.poll() is None:
                reply = VantaDialog.question(
                    self, "Game Running",
                    "Minecraft is still running.\n\nQuit and close the game?",
                    default_yes=False,
                )
                if not reply:
                    self._is_closing = False
                    event.ignore()
                    return

            # Persist whatever the user picked (version, username, RAM,
            # toggles) before the window tears down. Without this, selecting
            # a version and closing the launcher without launching means
            # the version is lost and the user has to pick again next time.
            try:
                self._save_settings()
            except Exception:
                # QSettings can throw if the platform backend is unavailable
                # mid-shutdown; not worth blocking the close for that.
                pass

            self._is_closing = True
            event.ignore()

            def cleanup_and_close():
                self.icon_loader.shutdown()
                self._shutdown_workers()
                with self._rpc_lock:
                    if self.rpc:
                        try:
                            self.rpc.clear()
                            self.rpc.close()
                        except Exception:
                            pass
                        self.rpc = None
                self.close()

            self._fade_out_with_shrink(cleanup_and_close)
        else:
            event.accept()

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.WindowStateChange:
            if not self.isMinimized() and (event.oldState() & Qt.WindowState.WindowMinimized):
                if hasattr(self, "_restore_geometry"):
                    self._fade_in_from_taskbar()
                else:
                    self._fade_in()
        super().changeEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.windowOpacity() == 0.0:
            self._fade_in()

    def _register_worker(self, worker: QThread) -> None:
        if worker not in self._workers:
            self._workers.append(worker)
            worker.finished.connect(lambda: self._unregister_worker(worker))

    def _unregister_worker(self, worker: QThread) -> None:
        try:
            self._workers.remove(worker)
        except ValueError:
            pass

    def _shutdown_workers(self) -> None:
        worker = getattr(self, "_launch_worker", None)
        if worker is not None:
            worker.abort()
            if worker.isRunning():
                worker.wait(3000)

        for w in list(self._workers):
            if w is not worker and w.isRunning():
                w.wait(2000)

    @staticmethod
    def _get_total_ram_gb() -> int:
        if sys.platform == "win32":
            try:
                if psutil is not None:
                    return max(1, int(psutil.virtual_memory().total / (1024 ** 3)))
            except Exception:
                pass
        elif sys.platform.startswith("linux"):
            try:
                with open("/proc/meminfo", "r") as f:
                    for line in f:
                        if line.startswith("MemTotal:"):
                            kb = int(line.split()[1])
                            return max(1, int(kb / (1024 ** 2)))
            except Exception:
                pass
        elif sys.platform == "darwin":
            try:
                result = subprocess.run(
                    ["sysctl", "-n", "hw.memsize"],
                    capture_output=True, text=True, timeout=2
                )
                if result.returncode == 0:
                    bytes_total = int(result.stdout.strip())
                    return max(1, int(bytes_total / (1024 ** 3)))
            except Exception:
                pass
        return 4

    def _show_progress(self, visible: bool, text: str = "") -> None:
        if visible:
            self.play_stack.setCurrentIndex(1)
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat(f"{text} · v{APP_VERSION_RUNTIME} %p%" if text else f"v{APP_VERSION_RUNTIME} %p%")
        else:
            self.play_stack.setCurrentIndex(0)
            self.play_button.setEnabled(True)
            self.play_button.setText("Play")

    def _init_ui(self) -> None:
        self.setWindowTitle(f"Vanta Launcher v{APP_VERSION_RUNTIME}")

        icon_path = os.path.join(_resource_base(), "icons", "icon.ico")

        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._CLOSED_WIDTH = 364
        self._OPEN_WIDTH = 704
        # Closed = compact "card-only" launcher. Open = drawer slides out
        # next to the card; both panels share the same height so the
        # layout stays visually balanced (mirror look).
        self.setFixedSize(self._CLOSED_WIDTH, self._CARD_H + 24)

        screen = QApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            self.move(
                geo.x() + (geo.width() - self.width()) // 2,
                geo.y() + (geo.height() - self.height()) // 2,
            )

        arrow_path = _get_arrow_image_path()
        self.setStyleSheet(self._stylesheet(arrow_path))

        central = ShadowCanvas(self)
        self.setCentralWidget(central)
        self._central = central

        self.card = QFrame(central, objectName="cardFrame")
        self.card.setGeometry(12, 12, 340, self._CARD_H)
        self._apply_card_shadow()

        card_layout = QVBoxLayout(self.card)
        # Equal gaps above the title row and below the Play button.
        card_layout.setContentsMargins(16, 11, 16, 11)
        card_layout.setSpacing(3)

        title = QHBoxLayout()
        title.setContentsMargins(0, 0, 0, 0)
        title.setSpacing(8)

        brand = QLabel("Vanta")
        brand.setStyleSheet(
            "color: #FFFFFF; font-family: 'Segoe UI', -apple-system, sans-serif;"
            " font-size: 13px; font-weight: bold; background: transparent; padding: 0;"
        )
        title.addWidget(brand)
        title.addStretch(1)

        self._settings_btn = QPushButton(objectName="settingsBtn")
        self._settings_btn.setFixedSize(16, 16)
        settings_icon = _generate_settings_image()
        if not settings_icon.isNull():
            self._settings_btn.setIcon(QIcon(settings_icon))
            self._settings_btn.setIconSize(self._settings_btn.size())
        self._settings_btn.clicked.connect(self._toggle_drawer)

        self._min_btn = QPushButton(objectName="minBtn")
        self._min_btn.setFixedSize(12, 12)
        self._min_btn.clicked.connect(self._fade_out_and_minimize)

        self._close_btn = QPushButton(objectName="closeBtn")
        self._close_btn.setFixedSize(12, 12)
        self._close_btn.clicked.connect(self.close)

        title.addWidget(self._settings_btn)
        title.addWidget(self._min_btn)
        title.addWidget(self._close_btn)
        card_layout.addLayout(title)
        card_layout.addSpacing(6)

        nick_layout = QHBoxLayout()
        nick_layout.setContentsMargins(0, 0, 0, 0)
        nick_layout.setSpacing(8)

        self.avatar_label = QLabel()
        self.avatar_label.setFixedSize(32, 32)
        self.avatar_label.setStyleSheet("border-radius: 4px; background: #2C2C2E;")
        if self._initial_avatar is not None:
            dpr = self.devicePixelRatioF()
            scaled = self._initial_avatar.scaled(
                round(32 * dpr), round(32 * dpr),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation
            )
            scaled.setDevicePixelRatio(dpr)
            self.avatar_label.setPixmap(scaled)
        nick_layout.addWidget(self.avatar_label)

        self.nick_input = QLineEdit()
        self.nick_input.setPlaceholderText("Username")
        self.nick_input.setFixedHeight(36)
        nick_layout.addWidget(self.nick_input)
        card_layout.addLayout(nick_layout)

        self.version_combo = AnchoredComboBox()
        self.version_combo.setFixedHeight(36)
        self.version_combo.addItem("Loading versions...")
        self.version_combo.setEnabled(False)
        self.version_combo.currentTextChanged.connect(self._on_version_changed)
        card_layout.addWidget(self.version_combo)

        # Compact meta-line under the version picker. Shows the expected Java
        # runtime ("Java 21"), the native-Vulkan availability, and total RAM
        # on the machine so the user has the most important context without
        # opening the drawer.
        self.meta_label = QLabel()
        self.meta_label.setStyleSheet(
            "color: #8E8E93; font-family: 'Segoe UI', -apple-system, sans-serif;"
            " font-size: 10px; background: transparent; padding: 0 2px;"
        )
        self.meta_label.setMinimumHeight(14)
        card_layout.addWidget(self.meta_label)

        card_layout.addSpacing(5)
        self.play_stack = QStackedWidget()
        self.play_stack.setFixedHeight(42)

        self.play_button = SmoothButton("Play")
        self.play_button.setObjectName("playBtn")
        self.play_button.setFixedHeight(42)
        self.play_button.clicked.connect(self._launch_game)

        self.progress_bar = ShimmerProgressBar()
        self.progress_bar.setFixedHeight(42)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%p%")

        self.play_stack.addWidget(self.play_button)
        self.play_stack.addWidget(self.progress_bar)
        card_layout.addWidget(self.play_stack)

        self.drawer = QFrame(central, objectName="drawer")
        # Drawer mirrors the card in size so the two panels balance visually
        # when the drawer slides out to the right. On the Mods tab it grows
        # taller (see _layout_target) to give the mod list room.
        self.drawer.setGeometry(12, 12, 340, self._CARD_H)
        self.drawer.stackUnder(self.card)
        central.set_panels([self.drawer, self.card])

        drawer_layout = QVBoxLayout(self.drawer)
        drawer_layout.setContentsMargins(14, 14, 14, 14)
        drawer_layout.setSpacing(10)

        self.drawer_tabs = SegmentedControl(["Settings", "Mods"])
        self.drawer_tabs.currentChanged.connect(self._on_drawer_tab_changed)
        drawer_layout.addWidget(self.drawer_tabs)

        self.drawer_stack = QStackedWidget()
        drawer_layout.addWidget(self.drawer_stack, 1)

        settings_widget = QWidget()
        # Tight spacing so the RAM row + 3 toggles fit inside the drawer's
        # 190 px height without clipping.
        settings_layout = QVBoxLayout(settings_widget)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.setSpacing(4)

        # RAM on a single row: [RAM] [slider.............] [4 GB]
        ram_row = QHBoxLayout()
        ram_row.setContentsMargins(0, 0, 0, 0)
        ram_row.setSpacing(8)
        ram_lbl = QLabel("RAM")
        ram_lbl.setStyleSheet(
            "color: #FFFFFF; font-family: 'Segoe UI', sans-serif; font-size: 11px;"
        )
        self.ram_val_lbl = QLabel("4 GB")
        self.ram_val_lbl.setMinimumWidth(40)
        self.ram_val_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.ram_val_lbl.setStyleSheet(
            "color: #0A84FF; font-family: 'Segoe UI', sans-serif;"
            " font-size: 11px; font-weight: bold;"
        )
        # Swim-tracker state for the RAM label. A timer lerps the
        # displayed value toward the target so the readout glides
        # smoothly instead of snapping between whole-GB positions.
        self._ram_swim_timer = QTimer(self)
        self._ram_swim_timer.setInterval(16)  # ~60 fps
        self._ram_swim_timer.timeout.connect(self._tick_ram_swim)
        self._ram_swim_displayed = 4.0
        self._ram_swim_target = 4.0
        self.ram_slider = QSlider(Qt.Orientation.Horizontal)
        # Whole-GB steps (1..16). 16 positions is enough - the smooth
        # "swim" between values is what makes the drag feel continuous,
        # not adding more discrete positions.
        self.ram_slider.setMinimum(1)
        self.ram_slider.setMaximum(16)
        self.ram_slider.setSingleStep(1)
        self.ram_slider.setPageStep(1)
        self.ram_slider.setValue(4)
        self.ram_slider.valueChanged.connect(self._on_ram_slider_changed)
        ram_row.addWidget(ram_lbl)
        ram_row.addWidget(self.ram_slider, 1)
        ram_row.addWidget(self.ram_val_lbl)
        settings_layout.addLayout(ram_row)

        self.perf_checkbox = ToggleSwitch("Performance Mode")
        self.perf_checkbox.setChecked(True)
        self.perf_checkbox.toggled.connect(self._on_perf_toggled)
        self.perf_checkbox.setToolTip(
            "Performance Mode installs Fabric and downloads Sodium, Lithium,"
            " Ferrite-Core and EntityCulling for better FPS."
        )
        settings_layout.addWidget(self.perf_checkbox)

        self.vulkan_checkbox = ToggleSwitch("Native Vulkan (Mojang)")
        self.vulkan_checkbox.setChecked(False)
        self.vulkan_checkbox.toggled.connect(self._on_vulkan_toggled)
        self.vulkan_checkbox.setEnabled(False)
        self.vulkan_checkbox.setToolTip(
            "Requires Minecraft 26.2 or newer.\n"
            "Uses Mojang's official Vulkan backend (not VulkanMod)."
        )
        settings_layout.addWidget(self.vulkan_checkbox)

        self.rpc_checkbox = ToggleSwitch("Discord Rich Presence")
        self.rpc_checkbox.setChecked(True)
        self.rpc_checkbox.stateChanged.connect(self._on_rpc_state_changed)
        settings_layout.addWidget(self.rpc_checkbox)
        settings_layout.addStretch(1)

        self.drawer_stack.addWidget(settings_widget)
        self.drawer_stack.addWidget(self._build_mods_page())

        self._avatar_timer = QTimer()
        self._avatar_timer.setSingleShot(True)
        self._avatar_timer.timeout.connect(self._fetch_avatar)
        self.nick_input.textChanged.connect(self._on_nick_changed)

        # Enter launches whenever focus is on the username field or the
        # version picker; we deliberately ignore Enter from text inputs
        # elsewhere to avoid hijacking copy/paste. The combo is non-editable
        # so it has no lineEdit; its popup handles Enter for selection.
        self.nick_input.returnPressed.connect(self._launch_game)

        for btn in self.findChildren(QPushButton):
            btn.setCursor(Qt.CursorShape.PointingHandCursor)

    def _init_ram_slider(self) -> None:
        total_ram = self._get_total_ram_gb()
        # Cap the slider at (system RAM - 1 GB) so the OS always keeps at
        # least 1 GB while Minecraft is running.
        max_ram = max(2, total_ram - 1)
        self.ram_slider.setMaximum(max_ram)
        self.ram_slider.setToolTip(
            f"Auto-configured to your system.\n"
            f"System RAM: {total_ram} GB\n"
            f"Maximum allocatable to Minecraft: {max_ram} GB"
        )
        # Apply any saved value now that the slider range is final.
        self._apply_saved_ram()

    def _apply_saved_ram(self) -> None:
        """Restore the saved RAM value (whole-GB units)."""
        try:
            saved = int(self.settings.value("ram_gb", "4"))
        except (ValueError, TypeError):
            saved = 4
        if saved < 1:
            saved = 4
        lo = self.ram_slider.minimum()
        hi = self.ram_slider.maximum()
        saved = max(lo, min(hi, saved))
        self.ram_slider.blockSignals(True)
        self.ram_slider.setValue(saved)
        self.ram_slider.blockSignals(False)
        self.ram_val_lbl.setText(f"{saved} GB")
        # Seed the swim tracker so the very first drag animates from the
        # loaded value (not from a stale 0).
        self._ram_swim_displayed = float(saved)
        self._ram_swim_target = float(saved)

    def _on_drawer_tab_changed(self, index: int) -> None:
        if self.drawer_stack.currentIndex() == index:
            return
        self.drawer_stack.setCurrentIndex(index)
        self._fade_in_page(self.drawer_stack.widget(index))
        if index == 1:
            self._activate_mods_tab()
        # The Mods tab needs more height than the settings page; the drawer
        # (and window) grow/shrink smoothly to match.
        self._animate_layout()

    @staticmethod
    def _fade_in_page(widget: QWidget, duration: int = 240) -> None:
        """Fade a freshly shown page in from transparent.

        The opacity effect is removed once the fade finishes: a live
        QGraphicsEffect forces offscreen rendering of the whole page on
        every repaint, which would tax the scrolling mod list.
        """
        prev = getattr(widget, "_fade_anim", None)
        if prev is not None:
            try:
                prev.stop()
            except RuntimeError:
                pass
        effect = QGraphicsOpacityEffect(widget)
        effect.setOpacity(0.0)
        widget.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", widget)
        anim.setDuration(duration)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(lambda: widget.setGraphicsEffect(None))
        widget._fade_anim = anim
        anim.start()

    def _toggle_drawer(self) -> None:
        if hasattr(self, "version_combo") and hasattr(self.version_combo, "hidePopup"):
            self.version_combo.hidePopup()
        self._drawer_expanded = not self._drawer_expanded
        if self._drawer_expanded and self.drawer_tabs.currentIndex() == 1:
            self._activate_mods_tab()
        self._animate_layout()

    def _layout_target(self) -> tuple:
        """(drawer_x, drawer_h, window_w, window_h) for the current drawer state."""
        expanded = self._drawer_expanded
        on_mods = expanded and self.drawer_tabs.currentIndex() == 1
        drawer_h = self._MODS_DRAWER_H if on_mods else self._CARD_H
        drawer_x = 352 if expanded else 12
        window_w = self._OPEN_WIDTH if expanded else self._CLOSED_WIDTH
        return drawer_x, drawer_h, window_w, drawer_h + 24

    def _animate_layout(self) -> None:
        """Animate drawer slide/height and window size together in one eased pass.

        The window widens symmetrically (shifting left by half the delta so
        the launcher stays centred) and grows downward, clamped so it never
        leaves the screen's available area.
        """
        if self._layout_animator is not None and self._layout_animator.is_running():
            self._layout_animator.stop()

        drawer_x, drawer_h, win_w, win_h = self._layout_target()
        g = self.geometry()
        d = self.drawer.geometry()

        target_x = g.x() - (win_w - g.width()) // 2
        target_y = g.y()
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            target_x = max(avail.left(), min(target_x, avail.right() + 1 - win_w))
            target_y = max(avail.top(), min(target_y, avail.bottom() + 1 - win_h))

        start = (d.x(), d.height(), g.x(), g.y(), g.width(), g.height())
        end = (drawer_x, drawer_h, target_x, target_y, win_w, win_h)
        if start == end:
            return

        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)

        def apply(t: float) -> None:
            dx, dh, wx, wy, ww, wh = (round(a + (b - a) * t) for a, b in zip(start, end))
            self.setGeometry(wx, wy, ww, wh)
            self.drawer.setGeometry(dx, 12, 340, dh)
            self._central.update()
            if t >= 1.0:
                self.setFixedSize(ww, wh)

        self._layout_animator = EaseAnimator(self._LAYOUT_DURATION, apply, parent=self)
        self._layout_animator.start(0.0, 1.0)

    def _on_ram_slider_changed(self, value: int) -> None:
        # The slider snaps between whole-GB positions, but the displayed
        # text smoothly tracks the target via a timer-based, spring-damped
        # animator instead of a QVariantAnimation. That avoids the
        # "rubberband" effect of restarting an animation on every tick
        # (which reads as jumping forward and back when the user wobbles
        # the slider or drags fast across several positions).
        self._ram_swim_target = float(value)
        if not self._ram_swim_timer.isActive():
            self._ram_swim_timer.start()
        self.settings.setValue("ram_gb", value)

    def _render_ram_value(self, gb: float) -> None:
        # Live update of the RAM label during the swim animation.
        if gb == int(gb):
            text = f"{int(gb)} GB"
        else:
            text = f"{gb:.1f} GB"
        self.ram_val_lbl.setText(text)

    def _tick_ram_swim(self) -> None:
        # One frame of the swim tracker. Lerp toward the target by 20%
        # per frame (≈ 60 fps) and snap when close enough. The 20%
        # coefficient gives a critically-damped feel — no overshoot, no
        # abrupt velocity changes when the target jumps.
        delta = self._ram_swim_target - self._ram_swim_displayed
        if abs(delta) < 0.02:
            self._ram_swim_displayed = self._ram_swim_target
            self._render_ram_value(self._ram_swim_displayed)
            self._ram_swim_timer.stop()
            return
        self._ram_swim_displayed += delta * 0.20
        self._render_ram_value(self._ram_swim_displayed)

    def _on_perf_toggled(self, checked: bool) -> None:
        version = self.version_combo.currentText()
        if is_fabric_compatible(version):
            self.settings.setValue("performance_mode", "true" if checked else "false")

    def _on_vulkan_toggled(self, checked: bool) -> None:
        version = self.version_combo.currentText()
        # Only persist when the current version actually supports it; this
        # avoids the preference being clobbered if the user toggles while
        # a compatible version briefly loses focus during selection.
        if is_native_vulkan_compatible(version):
            self.settings.setValue("native_vulkan", "true" if checked else "false")

    def _on_rpc_state_changed(self, state: int) -> None:
        enabled = state == 2
        self.settings.setValue("rpc_enabled", "true" if enabled else "false")
        if enabled:
            self._init_rpc()
        else:
            def close_rpc():
                with self._rpc_lock:
                    if self.rpc:
                        try:
                            self.rpc.close()
                        except Exception:
                            pass
                        self.rpc = None
            threading.Thread(target=close_rpc, daemon=True).start()

    def _on_nick_changed(self) -> None:
        self._avatar_timer.start(500)
        self.settings.setValue("username", self.nick_input.text().strip())

    def _fetch_avatar(self) -> None:
        username = self.nick_input.text().strip()
        if not username:
            self.avatar_label.clear()
            return
        self._avatar_loader = AvatarLoaderWorker(username)
        self._register_worker(self._avatar_loader)
        self._avatar_loader.avatar_loaded.connect(self._on_avatar_loaded)
        self._avatar_loader.start()

    def _on_avatar_loaded(self, username: str, image: QImage) -> None:
        if username != self.nick_input.text().strip():
            return
        if not image.isNull():
            pixmap = QPixmap.fromImage(image)
            dpr = self.devicePixelRatioF()
            scaled = pixmap.scaled(
                round(32 * dpr), round(32 * dpr),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation
            )
            scaled.setDevicePixelRatio(dpr)
            self.avatar_label.setPixmap(scaled)

    def _on_version_changed(self, version: str) -> None:
        compatible = is_fabric_compatible(version)
        self.perf_checkbox.setEnabled(compatible)
        if not compatible:
            self.perf_checkbox.setChecked(False)
            self.perf_checkbox.setToolTip("Performance mode requires Minecraft 1.14 or newer.")
        else:
            self.perf_checkbox.setToolTip("")
            self.perf_checkbox.setChecked(_to_bool(self.settings.value("performance_mode", "true")))

        vulkan_ok = is_native_vulkan_compatible(version)
        self.vulkan_checkbox.blockSignals(True)
        self.vulkan_checkbox.setEnabled(vulkan_ok)
        if not vulkan_ok:
            self.vulkan_checkbox.setChecked(False)
            self.vulkan_checkbox.setToolTip(
                "Requires Minecraft 26.2 or newer.\n"
                "Uses Mojang's official Vulkan backend (not VulkanMod)."
            )
        else:
            self.vulkan_checkbox.setToolTip(
                "Launches Minecraft with Mojang's native Vulkan renderer.\n"
                "Equivalent to picking Vulkan in the video settings menu."
            )
            self.vulkan_checkbox.setChecked(
                _to_bool(self.settings.value("native_vulkan", "false"))
            )
        self.vulkan_checkbox.blockSignals(False)

        self._refresh_meta_label()
        self._refresh_installed_mods()
        if self._mods_activated:
            self._show_mods_view()

    def _refresh_meta_label(self) -> None:
        """Update the small meta-line under the version picker."""
        if not hasattr(self, "meta_label"):
            return
        version = self.version_combo.currentText()
        if not version or version == "Loading versions...":
            self.meta_label.setText("")
            return

        runtime = get_expected_runtime_name(version)
        major = RUNTIME_JAVA_MAJOR.get(runtime, "?")
        bits = [f"Java {major}"]

        if is_native_vulkan_compatible(version):
            bits.append("Vulkan ready")
        else:
            bits.append("OpenGL only")

        try:
            if self._total_ram_cached is None:
                self._total_ram_cached = self._get_total_ram_gb()
            bits.append(f"{self._total_ram_cached} GB system RAM")
        except Exception:
            pass

        self.meta_label.setText("  ·  ".join(bits))

    # ------------------------------------------------------------------
    # Mods manager
    # ------------------------------------------------------------------
    def _build_mods_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        self.mods_view_tabs = SegmentedControl(["Browse", "Installed"], height=28)
        self.mods_view_tabs.currentChanged.connect(self._on_mods_view_changed)
        top.addWidget(self.mods_view_tabs, 1)
        self.mods_folder_btn = QPushButton("Folder", objectName="ghostBtn")
        self.mods_folder_btn.setFixedSize(64, 28)
        self.mods_folder_btn.setToolTip("Open this version's mods folder")
        self.mods_folder_btn.clicked.connect(self._open_mods_folder)
        top.addWidget(self.mods_folder_btn)
        layout.addLayout(top)

        self.mod_search_input = QLineEdit(objectName="modSearch")
        self.mod_search_input.setFixedHeight(32)
        self.mod_search_input.setClearButtonEnabled(True)
        search_icon = _generate_search_icon()
        if not search_icon.isNull():
            self.mod_search_input.addAction(QIcon(search_icon), QLineEdit.ActionPosition.LeadingPosition)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(320)
        self._search_timer.timeout.connect(self._on_mod_search)
        self.mod_search_input.textChanged.connect(self._on_mod_search_text)
        self.mod_search_input.returnPressed.connect(self._on_mod_search)
        layout.addWidget(self.mod_search_input)

        self.mods_status = QLabel(objectName="modsStatus")
        self.mods_status.setFixedHeight(14)
        layout.addWidget(self.mods_status)

        self.mods_view = ModListView()
        self.mods_view.near_bottom.connect(self._on_mods_near_bottom)
        self.mods_view.cleared.connect(self._cards.clear)
        layout.addWidget(self.mods_view, 1)

        self._update_search_placeholder()
        return page

    def _current_mod_version(self) -> Optional[str]:
        version = self.version_combo.currentText() if hasattr(self, "version_combo") else ""
        if not version or version == "Loading versions...":
            return None
        return version

    def _mods_dir(self, version: Optional[str] = None) -> Optional[str]:
        version = version or self._current_mod_version()
        if not version:
            return None
        return os.path.join(self.vanta_dir, "instances", safe_instance_name(version), "mods")

    def _update_search_placeholder(self) -> None:
        version = self._current_mod_version() or ""
        if self._mods_mode == 0:
            text = f"Search Fabric mods for {version}" if version else "Search Fabric mods"
        else:
            text = "Filter installed mods"
        self.mod_search_input.setPlaceholderText(text)

    def _set_mods_status(self, text: str, color: str = "#8E8E93", restore_ms: int = 0) -> None:
        if self._status_restore_timer is not None:
            self._status_restore_timer.stop()
        self.mods_status.setStyleSheet(f"color: {color};")
        self.mods_status.setText(text)
        if restore_ms > 0:
            if self._status_restore_timer is None:
                self._status_restore_timer = QTimer(self)
                self._status_restore_timer.setSingleShot(True)
                self._status_restore_timer.timeout.connect(self._restore_mods_status)
            self._status_restore_timer.start(restore_ms)

    def _restore_mods_status(self) -> None:
        if self._mods_mode == 0:
            self._set_browse_status()
        else:
            self._set_installed_status()

    def _set_browse_status(self) -> None:
        version = self._current_mod_version() or ""
        query = self._mods_queries[0].strip()
        if self._search_total <= 0:
            self._set_mods_status("")
        elif query:
            self._set_mods_status(f"{self._search_total:,} results for {version}")
        else:
            self._set_mods_status(f"Most popular Fabric mods for {version}")

    def _set_installed_status(self) -> None:
        n = len(self._installed_files)
        total = sum(size for _, size in self._installed_files)
        if n:
            self._set_mods_status(f"{n} mod{'s' if n != 1 else ''} installed · {_format_size(total)}"
                                  "  ·  Double-click a mod to open its page")
        else:
            self._set_mods_status("")

    def _activate_mods_tab(self) -> None:
        """Lazy first load: nothing touches Modrinth until the Mods tab is opened."""
        if self._mods_activated:
            return
        self._mods_activated = True
        self._refresh_installed_mods()
        self._show_mods_view()

    def _show_mods_view(self) -> None:
        self._update_search_placeholder()
        if self._mods_mode == 0:
            self._start_mod_search(reset=True)
        else:
            self._render_installed()

    def _on_mods_view_changed(self, index: int) -> None:
        self._mods_queries[self._mods_mode] = self.mod_search_input.text()
        self._mods_mode = index
        self.mod_search_input.blockSignals(True)
        self.mod_search_input.setText(self._mods_queries[index])
        self.mod_search_input.blockSignals(False)
        self._search_timer.stop()
        self._show_mods_view()

    def _on_mod_search_text(self, text: str) -> None:
        self._mods_queries[self._mods_mode] = text
        if self._mods_mode == 1:
            self._render_installed(animate=False)  # local filter: instant
        else:
            self._search_timer.start()

    def _on_mod_search(self) -> None:
        self._search_timer.stop()
        if self._mods_mode == 0:
            self._start_mod_search(reset=True)

    # ---- Browse ------------------------------------------------------------
    def _start_mod_search(self, reset: bool = True) -> None:
        version = self._current_mod_version()
        if not version:
            self.mods_view.show_message("Loading versions…")
            return
        if not is_fabric_compatible(version):
            self._search_total = 0
            self._set_mods_status("")
            self.mods_view.show_message(
                "Mods need Minecraft 1.14 or newer",
                f"Fabric doesn't support {version}. Pick a newer version to browse mods.",
            )
            return

        query = self._mods_queries[0].strip()
        offset = 0 if reset else self._search_offset
        self._search_token += 1
        token = self._search_token
        self._search_key = (query, version)
        self._search_loading = True

        cached = self._search_cache.get((query, version, offset))
        if cached is not None and time.monotonic() - cached[2] < 300:
            self._on_search_results(token, offset, cached[0], cached[1])
            return

        if reset:
            self._search_offset = 0
            self._search_total = 0
            self.mods_view.show_skeletons(5)
            self._set_mods_status("Searching Modrinth…")
        else:
            self._set_mods_status("Loading more…")

        worker = ModSearchWorker(token, query, version, offset)
        self._register_worker(worker)
        worker.results_ready.connect(self._on_search_results)
        worker.failed.connect(self._on_search_failed)
        worker.start()

    def _on_search_results(self, token: int, offset: int, hits: list, total: int) -> None:
        if token != self._search_token:
            return  # a newer search superseded this one
        query, version = self._search_key
        self._search_cache[(query, version, offset)] = (hits, total, time.monotonic())
        self._search_loading = False
        if self._mods_mode != 0:
            return

        if offset == 0:
            self.mods_view.clear()
            self._cards.clear()
        self._search_offset = offset + len(hits)
        self._search_total = total

        if offset == 0 and not hits:
            self.mods_view.show_message(
                "No mods found",
                f"Nothing matches “{query}” for {version}." if query else "Try searching for a mod by name.",
            )
            self._set_mods_status("")
            return

        cards = []
        for hit in hits:
            project_id = hit.get("project_id", "")
            if not project_id or project_id in self._cards:
                continue
            desc = (hit.get("description") or "").strip()
            info = {
                "key": project_id,
                "project_id": project_id,
                "slug": hit.get("slug", "") or "",
                "title": hit.get("title", "") or "Unknown",
                "author": hit.get("author", "") or "",
                "subtitle": f"↓ {_format_count(hit.get('downloads', 0))}  ·  {desc}",
                "tooltip": desc,
                "icon_url": hit.get("icon_url", "") or "",
                "url": f"https://modrinth.com/mod/{hit.get('slug') or project_id}",
            }
            card = self._make_card(info)
            card.set_state(*self._browse_state(info))
            cards.append(card)
        self.mods_view.add_cards(cards)
        self._set_browse_status()

    def _on_search_failed(self, token: int, error: str) -> None:
        if token != self._search_token:
            return
        self._search_loading = False
        if self._mods_mode != 0:
            return
        if self._search_offset == 0:
            self.mods_view.show_message(
                "Couldn't reach Modrinth",
                "Check your internet connection, then press Enter to retry.",
            )
            self._set_mods_status("")
        else:
            self._set_mods_status("Couldn't load more results.", "#FF9F0A", restore_ms=3000)

    def _on_mods_near_bottom(self) -> None:
        if (self._mods_mode == 0 and not self._search_loading
                and 0 < self._search_offset < self._search_total):
            self._start_mod_search(reset=False)

    def _browse_state(self, info: dict) -> tuple:
        project_id = info.get("project_id", "")
        if self._mod_active is not None and self._mod_active["key"] == project_id:
            return "installing", self._mod_active.get("progress", -1.0)
        if any(q["key"] == project_id for q in self._mod_queue):
            return "queued", None
        if project_id in self._installed_projects:
            return "installed", None
        slug = info.get("slug", "")
        if slug and not self._installed_meta and any(
            matches_mod(fn, slug) for fn, _ in self._installed_files
        ):
            # Hash identification unavailable (offline): filename heuristic.
            return "installed", None
        return "install", None

    # ---- Installed -------------------------------------------------------------
    def _refresh_installed_mods(self) -> None:
        """Rescan the current version's mods folder and refresh every view of it."""
        mods_dir = self._mods_dir()
        files: List[tuple] = []
        if mods_dir and os.path.isdir(mods_dir):
            try:
                with os.scandir(mods_dir) as it:
                    for entry in it:
                        if entry.is_file() and entry.name.lower().endswith(".jar"):
                            files.append((entry.name, entry.stat().st_size))
            except OSError:
                pass
        files.sort(key=lambda f: f[0].lower())
        self._installed_files = files
        names = {fn for fn, _ in files}
        self._installed_meta = {fn: m for fn, m in self._installed_meta.items() if fn in names}
        self._rebuild_installed_projects()
        if hasattr(self, "mods_view_tabs"):
            self.mods_view_tabs.setLabel(1, f"Installed · {len(files)}" if files else "Installed")

        if not self._mods_activated:
            return
        if self._mods_mode == 1:
            self._render_installed(animate=False)
        else:
            self._refresh_card_states()
        if files and mods_dir:
            self._installed_token += 1
            worker = InstalledModsWorker(mods_dir)
            token = self._installed_token
            worker.identified.connect(lambda d, m, t=token: self._on_installed_identified(t, d, m))
            self._register_worker(worker)
            worker.start()

    def _rebuild_installed_projects(self) -> None:
        self._installed_projects = {
            m["project_id"]: fn for fn, m in self._installed_meta.items() if m.get("project_id")
        }

    def _on_installed_identified(self, token: int, mods_dir: str, mapping: dict) -> None:
        if token != self._installed_token or mods_dir != self._mods_dir():
            return
        self._installed_meta = mapping
        self._rebuild_installed_projects()
        if self._mods_mode == 1:
            # Upgrade existing cards in place (names/icons) - no re-animation.
            for card in self.mods_view.cards:
                meta = mapping.get(card.info.get("filename", ""))
                if meta:
                    self._apply_installed_meta(card, meta)
        else:
            self._refresh_card_states()

    def _installed_info(self, filename: str, size: int) -> dict:
        meta = self._installed_meta.get(filename)
        info = {
            "key": f"file:{filename}",
            "filename": filename,
            "size": size,
            "title": _pretty_jar_name(filename),
            "subtitle": f"{filename}  ·  {_format_size(size)}",
            "tooltip": filename,
            "locked": matches_mod(filename, "fabric-api"),
        }
        if meta:
            self._merge_meta(info, meta)
        return info

    @staticmethod
    def _merge_meta(info: dict, meta: dict) -> None:
        info["title"] = meta.get("title") or info["title"]
        version = meta.get("version", "")
        info["subtitle"] = (f"v{version}  ·  " if version else "") + _format_size(info.get("size", 0))
        info["icon_url"] = meta.get("icon_url", "")
        info["project_id"] = meta.get("project_id", "")
        if meta.get("slug"):
            info["url"] = f"https://modrinth.com/mod/{meta['slug']}"
        if meta.get("project_id") == FABRIC_API_PROJECT_ID:
            info["locked"] = True

    def _apply_installed_meta(self, card: "ModCard", meta: dict) -> None:
        self._merge_meta(card.info, meta)
        if card.info.get("locked"):
            card.set_state("required")
        url = card.info.get("icon_url", "")
        if url:
            pixmap = self.icon_loader.get(url)
            if pixmap is not None:
                card.set_icon(pixmap)
            else:
                self.icon_loader.request(url)
        card.update()

    def _render_installed(self, animate: bool = True) -> None:
        if self._mods_mode != 1:
            return
        self._search_token += 1  # drop any in-flight browse results
        needle = self._mods_queries[1].strip().lower()
        self.mods_view.clear()
        self._cards.clear()
        if not self._current_mod_version():
            self.mods_view.show_message("Loading versions…")
            return
        if not self._installed_files:
            self.mods_view.show_message(
                "No mods installed yet",
                "Switch to Browse and hit Install on anything you like.",
            )
            self._set_mods_status("")
            return

        cards = []
        for filename, size in self._installed_files:
            info = self._installed_info(filename, size)
            if needle and needle not in info["title"].lower() and needle not in filename.lower():
                continue
            card = self._make_card(info)
            card.set_state("required" if info.get("locked") else "remove")
            cards.append(card)
        if not cards:
            self.mods_view.show_message("No matches", f"No installed mod matches “{needle}”.")
        else:
            self.mods_view.add_cards(cards, animate=animate)
        self._set_installed_status()

    # ---- Cards & actions ---------------------------------------------------
    def _make_card(self, info: dict) -> "ModCard":
        card = ModCard(info)
        card.action_clicked.connect(self._on_card_action)
        card.open_requested.connect(self._on_card_open)
        url = info.get("icon_url", "")
        if url:
            pixmap = self.icon_loader.get(url)
            if pixmap is not None:
                card.icon = pixmap
            else:
                self.icon_loader.request(url)
        self._cards[info["key"]] = card
        return card

    def _on_icon_ready(self, url: str, pixmap: QPixmap) -> None:
        for card in self.mods_view.cards:
            if card.info.get("icon_url") == url:
                card.set_icon(pixmap)

    def _refresh_card_states(self) -> None:
        if self._mods_mode != 0:
            return
        for card in self.mods_view.cards:
            if card.state != "skeleton":
                card.set_state(*self._browse_state(card.info))

    def _on_card_open(self, card: "ModCard") -> None:
        url = card.info.get("url")
        if url and url.startswith("https://modrinth.com/"):
            webbrowser.open(url)

    def _on_card_action(self, card: "ModCard") -> None:
        info = card.info
        if card.state == "install":
            self._enqueue_mod(info)
        elif card.state == "queued":
            self._mod_queue = [q for q in self._mod_queue if q["key"] != info["key"]]
            card.set_state("install")
            self._set_mods_status(f"Removed {info['title']} from the queue.", restore_ms=2200)
        elif card.state == "installed":
            filename = self._installed_projects.get(info.get("project_id", ""))
            if not filename:
                slug = info.get("slug", "")
                filename = next((fn for fn, _ in self._installed_files if slug and matches_mod(fn, slug)), None)
            if filename:
                self._remove_mod_file(filename, info.get("title", filename))
        elif card.state == "remove":
            self._remove_mod_file(info["filename"], info.get("title", info["filename"]))

    def _enqueue_mod(self, info: dict) -> None:
        version = self._current_mod_version()
        if not version:
            return
        job = {
            "key": info["project_id"],
            "project_id": info["project_id"],
            "slug": info.get("slug", ""),
            "title": info.get("title", ""),
            "version": version,
            "progress": -1.0,
        }
        self._mod_queue.append(job)
        card = self._cards.get(job["key"])
        if card is not None:
            card.set_state("queued")
        self._pump_mod_queue()

    def _pump_mod_queue(self) -> None:
        if self._mod_active is not None or not self._mod_queue or self._is_closing:
            return
        job = self._mod_queue.pop(0)
        self._mod_active = job
        card = self._cards.get(job["key"])
        if card is not None:
            card.set_state("installing", -1.0)
        pending = len(self._mod_queue)
        self._set_mods_status(
            f"Installing {job['title']}…" + (f"  ({pending} queued)" if pending else ""),
            "#0A84FF",
        )
        instance_dir = os.path.join(self.vanta_dir, "instances", safe_instance_name(job["version"]))
        worker = ModInstallWorker(job["project_id"], job["slug"], job["version"], instance_dir)
        self._register_worker(worker)
        worker.progress.connect(self._on_mod_progress)
        worker.completed.connect(self._on_mod_installed)
        worker.error.connect(self._on_mod_install_failed)
        worker.start()

    def _on_mod_progress(self, key: str, fraction: float) -> None:
        if self._mod_active is not None and self._mod_active["key"] == key:
            self._mod_active["progress"] = fraction
        card = self._cards.get(key)
        if card is not None:
            card.set_state("installing", fraction)

    def _on_mod_installed(self, key: str, filenames: list) -> None:
        job = self._mod_active or {}
        self._mod_active = None
        title = job.get("title", "Mod")
        extra = len(filenames) - 1
        self._refresh_installed_mods()
        # Mark it installed right away; the hash lookup in the refresh above
        # confirms it moments later.
        if job.get("version") == self._current_mod_version() and filenames:
            self._installed_projects.setdefault(key, filenames[0])
        card = self._cards.get(key)
        if card is not None:
            card.set_state("installed")
            card.flash()
        suffix = f" + {extra} dependenc{'ies' if extra != 1 else 'y'}" if extra > 0 else ""
        self._set_mods_status(f"✓ Installed {title}{suffix}", "#30D158", restore_ms=3000)
        self._pump_mod_queue()

    def _on_mod_install_failed(self, key: str, error: str) -> None:
        job = self._mod_active or {}
        self._mod_active = None
        card = self._cards.get(key)
        if card is not None:
            card.set_state("install")
        self._set_mods_status(f"Couldn't install {job.get('title', 'mod')}", "#FF9F0A", restore_ms=3500)
        self._pump_mod_queue()
        VantaDialog.warning(self, "Mod Install Error",
                            f"Failed to install {job.get('title', 'the mod')}:\n\n{error}")

    def _remove_mod_file(self, filename: str, title: str) -> None:
        mods_dir = self._mods_dir()
        if not mods_dir or not filename:
            return
        if matches_mod(filename, "fabric-api"):
            VantaDialog.warning(
                self, "Cannot Remove",
                "Fabric API is required for mod support and cannot be removed from here."
            )
            return
        if not VantaDialog.question(self, "Remove Mod", f"Remove {title}?\n\n{filename}", default_yes=False):
            return
        mods_dir = os.path.abspath(mods_dir)
        filepath = os.path.abspath(os.path.join(mods_dir, filename))
        if os.path.normcase(os.path.dirname(filepath)) != os.path.normcase(mods_dir):
            return
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except OSError as e:
            VantaDialog.warning(self, "Remove Error", f"Could not delete mod file:\n\n{e}")
            return
        self._installed_meta.pop(filename, None)
        self._refresh_installed_mods()
        self._set_mods_status(f"Removed {title}", restore_ms=2500)

    def _open_mods_folder(self) -> None:
        mods_dir = self._mods_dir()
        if not mods_dir:
            return
        try:
            os.makedirs(mods_dir, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(mods_dir)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", mods_dir])
            else:
                subprocess.Popen(["xdg-open", mods_dir])
        except OSError as e:
            VantaDialog.warning(self, "Open Folder", f"Could not open the mods folder:\n\n{e}")

    @staticmethod
    def _stylesheet(arrow_path: str) -> str:
        arrow_qss = f'image: url("{arrow_path}");' if arrow_path else ""
        return f"""
            QMainWindow {{
                background: transparent;
            }}
            #cardFrame {{
                background-color: #1C1C1E;
                border: 1px solid #2C2C2E;
                border-radius: 16px;
            }}
            #drawer {{
                background-color: #1C1C1E;
                border: 1px solid #2C2C2E;
                border-radius: 16px;
            }}
            QLineEdit {{
                background-color: #2C2C2E;
                border: 2px solid #38383A;
                border-radius: 10px;
                padding: 0px 12px;
                height: 36px;
                font-family: 'Segoe UI', -apple-system, sans-serif;
                font-size: 13px;
                color: #FFFFFF;
                selection-background-color: #0A84FF;
            }}
            QLineEdit:focus {{
                border: 2px solid #0A84FF;
                background-color: #2C2C2E;
            }}
            QLineEdit::placeholder {{
                color: #8E8E93;
            }}
            QComboBox {{
                background-color: #2C2C2E;
                border: 2px solid #38383A;
                border-radius: 10px;
                padding: 0px 12px;
                height: 36px;
                font-family: 'Segoe UI', -apple-system, sans-serif;
                font-size: 13px;
                color: #FFFFFF;
                selection-background-color: #0A84FF;
            }}
            QComboBox:focus {{
                border: 2px solid #0A84FF;
                background-color: #2C2C2E;
            }}
            QComboBox:disabled, QLineEdit:disabled {{
                color: #6A6A6E;
                border-color: #2C2C2E;
            }}
            QComboBox::drop-down {{
                border: none;
                background: transparent;
                width: 30px;
                subcontrol-origin: padding;
                subcontrol-position: top right;
            }}
            QComboBox::down-arrow {{
                {arrow_qss}
                width: 10px;
                height: 6px;
            }}
            QPushButton {{
                background-color: #0A84FF;
                color: #FFFFFF;
                border: none;
                border-radius: 8px;
                padding: 0px 16px;
                height: 42px;
                font-family: 'Segoe UI', -apple-system, sans-serif;
                font-size: 14px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: #007AFF;
            }}
            QPushButton:pressed {{
                background-color: #0056B3;
            }}
            QPushButton:disabled {{
                background-color: #3A3A3C;
                color: #8E8E93;
            }}
            #playBtn {{
                border-radius: 10px;
            }}
            QProgressBar {{
                background-color: #2C2C2E;
                border: 1px solid #3A3A3C;
                border-radius: 10px;
                color: #FFFFFF;
                font-family: 'Segoe UI', -apple-system, sans-serif;
                font-size: 9px;
                text-align: center;
            }}
            QProgressBar::chunk {{
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0A84FF, stop:1 #2F95FF);
                border-radius: 9px;
            }}
            #closeBtn {{
                background-color: #FF5F56;
                border: none;
                border-radius: 6px;
            }}
            #closeBtn:hover {{
                background-color: #E0443E;
            }}
            #minBtn {{
                background-color: #27C93F;
                border: none;
                border-radius: 6px;
            }}
            #minBtn:hover {{
                background-color: #1AAB33;
            }}
            #settingsBtn {{
                background-color: transparent;
                border: none;
                padding: 0;
            }}
            #settingsBtn:hover {{
                background-color: rgba(255, 255, 255, 0.1);
                border-radius: 4px;
            }}
            #drawer QLabel {{
                color: #FFFFFF;
                font-family: 'Segoe UI', sans-serif;
            }}
            #ghostBtn {{
                background-color: #232325;
                border: 1px solid #38383A;
                border-radius: 8px;
                padding: 0px 8px;
                height: 28px;
                font-size: 11px;
                font-weight: 600;
                color: #D1D1D6;
            }}
            #ghostBtn:hover {{
                background-color: #2C2C2E;
                border-color: #0A84FF;
                color: #FFFFFF;
            }}
            #ghostBtn:pressed {{
                background-color: #1C1C1E;
            }}
            QLineEdit#modSearch {{
                background-color: #232325;
                border: 1px solid #38383A;
                border-radius: 9px;
                padding: 0px 6px;
                height: 32px;
                font-size: 12px;
            }}
            QLineEdit#modSearch:focus {{
                border: 1px solid #0A84FF;
                background-color: #262628;
            }}
            #modsStatus {{
                font-family: 'Segoe UI', sans-serif;
                font-size: 10px;
                padding: 0 2px;
                background: transparent;
            }}
            #modList, #modListInner {{
                background: transparent;
                border: none;
            }}
            #modList QScrollBar:vertical {{
                background: transparent;
                width: 5px;
                margin: 2px 0 2px 0;
                border: none;
            }}
            #modList QScrollBar::handle:vertical {{
                background: #3A3A3C;
                border-radius: 2px;
                min-height: 30px;
            }}
            #modList QScrollBar::handle:vertical:hover {{
                background: #55555A;
            }}
            #modList QScrollBar::add-line:vertical, #modList QScrollBar::sub-line:vertical {{
                height: 0;
                border: none;
                background: none;
            }}
            #modList QScrollBar::add-page:vertical, #modList QScrollBar::sub-page:vertical {{
                background: none;
            }}
            QSlider::groove:horizontal {{
                height: 4px;
                background: #3A3A3C;
                border-radius: 2px;
            }}
            QSlider::sub-page:horizontal {{
                background: #0A84FF;
                border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                background: #FFFFFF;
                width: 14px;
                margin-top: -5px;
                margin-bottom: -5px;
                border-radius: 7px;
            }}
            QSlider::handle:horizontal:hover {{
                background: #E5F1FF;
            }}
            QCheckBox {{
                color: #FFFFFF;
                font-family: 'Segoe UI', sans-serif;
                font-size: 11px;
            }}
            QCheckBox::indicator {{
                width: 14px;
                height: 14px;
                border: 2px solid #3A3A3C;
                border-radius: 4px;
                background: #2C2C2E;
            }}
            QCheckBox::indicator:checked {{
                background-color: #0A84FF;
                border-color: #0A84FF;
            }}
            /* Visibly dimmed state for checkboxes that aren't applicable
               to the selected version (e.g. Native Vulkan on <26.2). The
               text fades to a soft grey and the indicator loses its border
               so it reads as "off / unavailable" at a glance. */
            QCheckBox:disabled {{
                color: #5A5A5E;
            }}
            QCheckBox::indicator:disabled {{
                border: 2px solid #2A2A2C;
                background: #1C1C1E;
            }}
            QCheckBox::indicator:checked:disabled {{
                background-color: #2C4D7A;
                border-color: #2C4D7A;
            }}
        """

    def _load_settings(self) -> None:
        if self._initial_avatar is not None:
            self.nick_input.blockSignals(True)
        self.nick_input.setText(self.settings.value("username", ""))
        self.nick_input.blockSignals(False)

        try:
            saved_ram = int(self.settings.value("ram_gb", "4"))
        except (ValueError, TypeError):
            saved_ram = 4
        if saved_ram < 1:
            saved_ram = 4
        lo = self.ram_slider.minimum()
        hi = self.ram_slider.maximum()
        saved_ram = max(lo, min(hi, saved_ram))
        self.ram_slider.blockSignals(True)
        self.ram_slider.setValue(saved_ram)
        self.ram_slider.blockSignals(False)
        self.ram_val_lbl.setText(f"{saved_ram} GB")

        self.perf_checkbox.blockSignals(True)
        self.perf_checkbox.setChecked(_to_bool(self.settings.value("performance_mode", "true")))
        self.perf_checkbox.blockSignals(False)

        rpc_on = _to_bool(self.settings.value("rpc_enabled", "true"))
        self.rpc_checkbox.blockSignals(True)
        self.rpc_checkbox.setChecked(rpc_on)
        self.rpc_checkbox.blockSignals(False)

    def _save_settings(self) -> None:
        self.settings.setValue("username", self.nick_input.text().strip())
        self.settings.setValue("version", self.version_combo.currentText())
        self.settings.setValue("performance_mode", "true" if self.perf_checkbox.isChecked() else "false")
        self.settings.setValue("native_vulkan", "true" if self.vulkan_checkbox.isChecked() else "false")
        self.settings.setValue("ram_gb", self.ram_slider.value())

    def _fetch_versions(self) -> None:
        if self._preloaded_versions:
            QTimer.singleShot(0, lambda: self._on_versions_fetched(list(self._preloaded_versions)))
            return
        self._fetch_worker = VersionFetchWorker()
        self._register_worker(self._fetch_worker)
        self._fetch_worker.versions_fetched.connect(self._on_versions_fetched)
        self._fetch_worker.error_occurred.connect(self._on_versions_fetch_failed)
        self._fetch_worker.start()

    def _on_versions_fetched(self, versions: List[str]) -> None:
        saved_version = self.settings.value("version", "")
        current = self.version_combo.currentText()

        self.version_combo.blockSignals(True)
        self.version_combo.clear()
        self.version_combo.addItems(versions)
        self.version_combo.setEnabled(True)

        # Selection priority: saved > default (26.2) > whatever was already
        # selected > first item in the list. A previously-saved version is
        # always honoured so existing users don't get bumped on upgrade.
        if saved_version in versions:
            self.version_combo.setCurrentText(saved_version)
        elif _DEFAULT_VERSION in versions:
            self.version_combo.setCurrentText(_DEFAULT_VERSION)
        elif current in versions:
            self.version_combo.setCurrentText(current)
        self.version_combo.blockSignals(False)

        self._on_version_changed(self.version_combo.currentText())

    def _on_versions_fetch_failed(self, _error_message: str) -> None:
        # 26.2 leads the offline fallback list so the default-version
        # logic below can still find it when the manifest is unreachable.
        fallback = ["26.2", "1.21.4", "1.21.1", "1.20.4", "1.19.4", "1.16.5", "1.8.9"]
        try:
            installed = [
                v.get("id", "") for v in minecraft_launcher_lib.utils.get_installed_versions(
                    self.minecraft_dir
                )
            ]
            clean_installed = [v for v in installed if v and not v.startswith(("fabric-", "quilt-", "forge-", "neoforge-"))]
            combined = list(dict.fromkeys(clean_installed + fallback))
        except Exception:
            combined = fallback

        saved_version = self.settings.value("version", "")
        self.version_combo.blockSignals(True)
        self.version_combo.clear()
        self.version_combo.addItems(combined)
        self.version_combo.setEnabled(True)

        if saved_version in combined:
            self.version_combo.setCurrentText(saved_version)
        elif _DEFAULT_VERSION in combined:
            self.version_combo.setCurrentText(_DEFAULT_VERSION)
        self.version_combo.blockSignals(False)

        self._on_version_changed(self.version_combo.currentText())
        # Tell the user the list came from cache + fallback so they don't
        # think the launcher just doesn't have the latest versions.
        VantaDialog.warning(
            self,
            "Offline Mode",
            "Could not reach the Mojang version manifest.\n\n"
            "Showing your installed versions plus a recent fallback list. "
            "Reconnect to the internet and the launcher will refresh the list.",
        )

    def _start_update_check(self) -> None:
        if self._is_closing or self._update_in_progress or self._pending_update:
            return
        if self._update_check_worker is not None and self._update_check_worker.isRunning():
            return
        self._update_check_worker = UpdateCheckWorker()
        self._register_worker(self._update_check_worker)
        self._update_check_worker.update_available.connect(self._on_update_available)
        self._update_check_worker.start()

    def _game_process_running(self) -> bool:
        worker = getattr(self, "_launch_worker", None)
        proc = getattr(worker, "process", None)
        return proc is not None and proc.poll() is None

    def _on_update_available(self, latest_tag: str, download_url: str) -> None:
        if self._is_closing or self._update_in_progress or self._pending_update:
            return
        if self._launch_in_progress or self._game_process_running():
            self._pending_update = (latest_tag, download_url)
            QTimer.singleShot(UPDATE_CHECK_RETRY_MS, self._prompt_pending_update)
            return
        self._offer_update(latest_tag, download_url)

    def _prompt_pending_update(self) -> None:
        if self._pending_update and not self._is_closing and not self._update_in_progress:
            latest_tag, download_url = self._pending_update
            self._offer_update(latest_tag, download_url)

    def _offer_update(self, latest_tag: str, download_url: str) -> None:
        self._pending_update = None
        if self._launch_in_progress or self._game_process_running():
            self._pending_update = (latest_tag, download_url)
            QTimer.singleShot(UPDATE_CHECK_RETRY_MS, self._prompt_pending_update)
            return
        reply = VantaDialog.question(
            self,
            "Update Available",
            f"Vanta {latest_tag} is available (you are running v{APP_VERSION_RUNTIME}).\n\n"
            "Download and install it now?",
            default_yes=True,
        )
        if self._is_closing or self._update_in_progress:
            return
        if not reply:
            return
        if not getattr(sys, "frozen", False):
            webbrowser.open(download_url)
            VantaDialog.info(
                self,
                "Download Started",
                "Your browser is opening the latest release.\n"
                "Run the downloaded launcher to update.",
            )
            return
        self._begin_update_download(download_url)

    def _begin_update_download(self, download_url: str) -> None:
        self._update_in_progress = True
        self._set_ui_enabled(False)
        self._show_progress(True, "Updating")
        self._update_download_worker = UpdateDownloadWorker(download_url)
        self._register_worker(self._update_download_worker)
        self._update_download_worker.progress.connect(self._on_update_progress)
        self._update_download_worker.ready_to_install.connect(self._on_update_ready)
        self._update_download_worker.error.connect(self._on_update_error)
        self._update_download_worker.start()

    def _on_update_progress(self, percent: int) -> None:
        self.progress_bar.setValue(percent)
        self.progress_bar.setFormat(f"Downloading update: {percent}%")

    def _on_update_ready(self, new_exe_path: str) -> None:
        self._show_progress(False)
        self.play_button.setText("Restarting...")
        try:
            _apply_update(new_exe_path)
        except OSError as e:
            self._update_in_progress = False
            self._set_ui_enabled(True)
            self.play_button.setText("Play")
            VantaDialog.error(
                self,
                "Update Failed",
                f"Could not install the update:\n\n{e}\n\n"
                "The launcher will keep running the current version.",
            )
            return
        self._is_closing = True

        def _finish_update() -> None:
            self.icon_loader.shutdown()
            self._shutdown_workers()
            with self._rpc_lock:
                if self.rpc:
                    try:
                        self.rpc.clear()
                        self.rpc.close()
                    except Exception:
                        pass
                    self.rpc = None
            QApplication.quit()

        self._fade_out_with_shrink(_finish_update)

    def _on_update_error(self, error_message: str) -> None:
        self._update_in_progress = False
        self._set_ui_enabled(True)
        self._show_progress(False)
        self.play_button.setText("Play")
        VantaDialog.error(
            self,
            "Update Failed",
            f"The update could not be downloaded:\n\n{error_message}",
        )

    def _set_ui_enabled(self, enabled: bool) -> None:
        self.nick_input.setEnabled(enabled)
        self.version_combo.setEnabled(enabled)
        self.play_button.setEnabled(enabled)

    def _prompt_java_download(self, runtime_name: str, on_download: callable) -> bool:
        reply = VantaDialog.question(
            self,
            "Java Runtime Missing",
            f"Minecraft {self.version_combo.currentText()} requires the '{runtime_name}' "
            "Java runtime, which is not installed.\n\n"
            "Would you like the launcher to download and install it automatically?\n"
            "No administrator privileges are required.",
            default_yes=True,
        )
        if reply:
            on_download()
            return True
        VantaDialog.warning(
            self,
            "Cannot Launch",
            "A compatible Java runtime is required to start Minecraft.\n"
            "Please install one manually or allow the launcher to download it."
        )
        return False

    def _launch_game(self) -> None:
        if self._launch_in_progress or self._update_in_progress:
            return

        username = self.nick_input.text().strip()
        version = self.version_combo.currentText()

        if not username:
            VantaDialog.warning(self, "Invalid Username", "Please enter a username.")
            return

        if not version or version == "Loading versions..." or not self.version_combo.isEnabled():
            VantaDialog.warning(self, "Launcher Busy", "Please wait for the version list to load.")
            return

        self._save_settings()
        self._launch_in_progress = True

        required_runtime = None
        required_major = None
        try:
            runtime_info = minecraft_launcher_lib.runtime.get_version_runtime_information(
                version, self.minecraft_dir
            )
            required_runtime = runtime_info.get("name") if runtime_info else None
            required_major = runtime_info.get("javaMajorVersion") if runtime_info else None
        except Exception:
            pass

        if not required_runtime:
            required_runtime = get_expected_runtime_name(version)

        def start_launch(java_exec: Optional[str] = None):
            self._set_ui_enabled(False)
            self._show_progress(True, "Preparing")
            ram_gb = self.ram_slider.value()
            perf = self.perf_checkbox.isChecked() and is_fabric_compatible(version)
            use_vulkan = self.vulkan_checkbox.isChecked() and is_native_vulkan_compatible(version)

            self._update_rpc(state="In-Game", details=f"Playing Minecraft {version}")

            self._launch_worker = LaunchWorker(username, version, self.minecraft_dir,
                                               ram_gb, perf, java_path=java_exec,
                                               use_native_vulkan=use_vulkan)
            self._register_worker(self._launch_worker)
            self._launch_worker.progress_updated.connect(self._on_launch_progress)
            self._launch_worker.launch_success.connect(self._on_launch_success)
            self._launch_worker.game_exited.connect(self._on_game_exited)
            self._launch_worker.game_confirmed.connect(self._on_game_confirmed)
            self._launch_worker.launch_failed.connect(self._on_launch_failed)
            self._launch_worker.error_occurred.connect(self._on_launch_error)
            self._launch_worker.mods_missing.connect(self._on_mods_missing)
            self._launch_worker.performance_mods_installed.connect(self._refresh_installed_mods)
            self._launch_worker.start()

        java_exec = None
        if required_runtime:
            java_exec = minecraft_launcher_lib.runtime.get_executable_path(
                required_runtime, self.minecraft_dir
            )

        if java_exec and not os.path.exists(java_exec):
            java_exec = None

        def on_download():
            self._set_ui_enabled(False)
            self._show_progress(True, "Downloading Java")
            self._java_worker = JavaDownloadWorker(required_runtime, self.minecraft_dir)
            self._register_worker(self._java_worker)
            self._java_worker.progress.connect(self._on_java_progress)
            self._java_worker.completed.connect(
                lambda: start_launch(
                    minecraft_launcher_lib.runtime.get_executable_path(
                        required_runtime, self.minecraft_dir
                    )
                )
            )
            self._java_worker.error.connect(self._on_java_error)
            self._java_worker.start()

        def on_java_located(path) -> None:
            if self._is_closing:
                return
            if path:
                start_launch(path)
                return
            self._show_progress(False)
            self._set_ui_enabled(True)
            if not self._prompt_java_download(required_runtime, on_download):
                self._launch_in_progress = False

        if java_exec:
            start_launch(java_exec)
            return

        # Probing system Java spawns `java -version` for every candidate
        # (up to several seconds); run it on a worker so the UI stays live.
        self._set_ui_enabled(False)
        self._show_progress(True, "Locating Java")
        self.progress_bar.setFormat("Locating Java...")
        self._java_locate_worker = JavaLocateWorker(
            RUNTIME_JAVA_MAJOR.get(required_runtime) or required_major or 17
        )
        self._register_worker(self._java_locate_worker)
        self._java_locate_worker.located.connect(on_java_located)
        self._java_locate_worker.start()

    def _on_java_progress(self, status: str, percent: int) -> None:
        if percent >= 0:
            self.progress_bar.setValue(percent)
            self.progress_bar.setFormat(f"Downloading Java: {percent}%")
        else:
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat("Downloading Java...")

    def _on_java_error(self, error_message: str) -> None:
        self._set_ui_enabled(True)
        self._show_progress(False)
        self._launch_in_progress = False
        VantaDialog.error(
            self,
            "Java Install Error",
            f"Failed to install portable Java runtime:\n\n{error_message}",
        )

    def _on_mods_missing(self, details: str) -> None:
        key = f"mods_skipped/{safe_instance_name(self.version_combo.currentText())}"
        if self.settings.value(key, "") == details:
            return
        self.settings.setValue(key, details)
        VantaDialog.warning(
            self,
            "Some Mods Skipped",
            details + "\n\nThe game will still launch without them.",
        )

    def _on_launch_progress(self, status: str, percent: int) -> None:
        if percent >= 0:
            self.progress_bar.setValue(percent)
            self.progress_bar.setFormat(f"Installing: {percent}%")
        else:
            self.progress_bar.setValue(0)
            label = status[:20] + "..." if len(status) > 20 else status
            self.progress_bar.setFormat(f"{label}...")

    def _on_launch_success(self) -> None:
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Starting Minecraft...")

    def _on_game_confirmed(self) -> None:
        self._fade_out_with_shrink(self.hide)

    def _on_launch_failed(self, message: str) -> None:
        if self._is_closing:
            return
        self._launch_in_progress = False
        self._set_ui_enabled(True)
        self._show_progress(False)
        self.play_button.setText("Play")
        self.show()
        self.setWindowOpacity(1.0)
        # Split message: human-readable summary up top, log-tail into the
        # collapsible "Details" section so the dialog stays compact while
        # still preserving every line of the Minecraft log.
        version = self.version_combo.currentText()
        instance_dir = os.path.join(self.vanta_dir, "instances", safe_instance_name(version))
        VantaDialog.error(
            self, "Launch Failed", message,
            details=_read_log_tail(os.path.join(instance_dir, "latest.log"), max_lines=80, max_chars=8000),
            log_dir=instance_dir,
        )
        self._update_rpc(state="Free Non-Premium Launcher", details="Playing Minecraft")

    def _on_game_exited(self) -> None:
        if self._is_closing:
            return
        self._launch_in_progress = False
        self.setWindowOpacity(0.0)
        self.show()
        self._set_ui_enabled(True)
        self._show_progress(False)
        self.play_button.setText("Play")
        self._update_rpc(state="Free Non-Premium Launcher", details="Playing Minecraft")

    def _on_launch_error(self, error_message: str) -> None:
        if self._is_closing:
            return
        self._launch_in_progress = False
        self._set_ui_enabled(True)
        self._show_progress(False)
        self.play_button.setText("Play")
        version = self.version_combo.currentText()
        instance_dir = os.path.join(self.vanta_dir, "instances", safe_instance_name(version))
        VantaDialog.error(
            self,
            "Launch Error",
            f"An error occurred while launching Minecraft:\n\n{error_message}",
            details=_read_log_tail(os.path.join(instance_dir, "latest.log"), max_lines=80, max_chars=8000),
            log_dir=instance_dir,
        )
        self._update_rpc(state="Free Non-Premium Launcher", details="Playing Minecraft")


if __name__ == "__main__":
    silence_asyncio_windows_bugs()
    _cleanup_stale_update()

    # Application identity is provided through Qt metadata (no raw Win32
    # calls). Qt surfaces the app name/version to the OS for taskbar
    # grouping and jump lists; the former ctypes shell32
    # SetCurrentProcessExplicitAppUserModelID call was removed as part of
    # the antivirus-heuristic cleanup.
    QApplication.setApplicationName("Vanta Launcher")
    QApplication.setApplicationVersion(APP_VERSION_RUNTIME)
    QApplication.setOrganizationName("Vanta")
    QApplication.setOrganizationDomain("getvanta.xyz")

    import traceback

    def _excepthook(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        try:
            log_dir = get_vanta_dir()
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, "crash.log"), "a", encoding="utf-8") as f:
                f.write("\n" + "=" * 60 + "\n")
                traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _excepthook

    app = QApplication(sys.argv)
    app.setStyleSheet(
        "QToolTip { background-color: #2C2C2E; color: #F2F2F7; border: 1px solid #3A3A3C;"
        " border-radius: 6px; padding: 5px 8px; font-family: 'Segoe UI'; font-size: 11px; }"
    )

    splash = SplashScreen()
    splash.show()

    MIN_SPLASH_MS = 900
    boot_clock = QElapsedTimer()
    boot_clock.start()

    avatar_state = {"done": False, "data": None}
    refs = {"launcher": None, "avatar": QPixmap()}
    version_state = {"done": False, "versions": None}

    settings = QSettings("Vanta", "Preferences")
    saved_username = (settings.value("username", "") or "").strip()

    def _fetch_player_avatar() -> None:
        data = None
        if saved_username:
            try:
                r = HTTP.get(
                    f"https://minotar.net/helm/{saved_username}/128.png",
                    headers=API_HEADERS,
                    timeout=(3, 5),
                )
                if r.status_code == 200:
                    data = r.content
            except Exception:
                pass
        avatar_state["data"] = data
        avatar_state["done"] = True

    def _prefetch_versions() -> None:
        try:
            version_list = minecraft_launcher_lib.utils.get_version_list()
            releases = [v["id"] for v in version_list if v["type"] == "release"]
            version_state["versions"] = releases or None
        except Exception:
            version_state["versions"] = None
        version_state["done"] = True

    if saved_username:
        threading.Thread(target=_fetch_player_avatar, daemon=True).start()
    else:
        avatar_state["done"] = True

    threading.Thread(target=_prefetch_versions, daemon=True).start()
    splash.set_status("Loading versions...")

    def _build_avatar_pixmap() -> QPixmap:
        data = avatar_state["data"]
        if not data:
            return QPixmap()
        image = QImage()
        if image.loadFromData(data):
            return QPixmap.fromImage(image)
        return QPixmap()

    def _boot_launcher() -> None:
        elapsed = boot_clock.elapsed()
        if (not avatar_state["done"] or not version_state["done"]) and elapsed < 4000:
            QTimer.singleShot(100, _boot_launcher)
            return
        if not avatar_state["done"]:
            avatar_state["data"] = None
            avatar_state["done"] = True
        if not version_state["done"]:
            version_state["done"] = True

        remaining = max(0, MIN_SPLASH_MS - elapsed)
        if remaining > 0:
            QTimer.singleShot(min(remaining, 100), _boot_launcher)
            return

        pixmap = _build_avatar_pixmap()
        splash.set_player(saved_username, pixmap)
        refs["avatar"] = pixmap

        def _open_window() -> None:
            try:
                refs["launcher"] = MinecraftLauncher(
                    initial_avatar=refs["avatar"],
                    preloaded_versions=version_state["versions"],
                )
                refs["launcher"].show()
                refs["launcher"]._fade_in_with_motion_blur()
                QTimer.singleShot(160, splash.finish)
            except Exception:
                splash.finish()
                raise

        QTimer.singleShot(350, _open_window)

    QTimer.singleShot(150, _boot_launcher)
    sys.exit(app.exec())