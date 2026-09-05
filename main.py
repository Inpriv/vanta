import sys
import os
import uuid
import json
import subprocess
import shutil
import base64
import time
import hashlib
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
    QStackedWidget, QSlider, QCheckBox, QListWidget, QListWidgetItem, QProgressBar,
    QGraphicsDropShadowEffect, QDialog, QAbstractItemView, QGraphicsBlurEffect
)
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QBrush, QPolygon, QIcon, QPixmap, QLinearGradient, QPainterPath, QPen

import minecraft_launcher_lib
import minecraft_launcher_lib.runtime
import minecraft_launcher_lib.fabric

APP_VERSION = "1.7"

# Seconds the launch watchdog waits before considering the game confirmed running.
STARTUP_CONFIRM_SECONDS = 90

API_HEADERS = {
    "User-Agent": f"VantaLauncher/{APP_VERSION} (+https://github.com/inpriv/vanta; support@getvanta.xyz)"
}

UPDATE_MANIFEST_URL = "https://raw.githubusercontent.com/inpriv/vanta/refs/heads/main/version.json"
UPDATE_CHECK_DELAY_MS = 3000
UPDATE_CHECK_RETRY_MS = 30000


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
            if major > 1 or (major == 1 and (minor > 20 or (minor == 20 and patch >= 5))):
                return "java-runtime-delta"  # Java 21
            if major == 1 and minor >= 18:
                return "java-runtime-gamma"  # Java 17
            if major == 1 and minor == 17:
                return "java-runtime-alpha"  # Java 16
    except Exception:
        pass
    return "jre-legacy"  # Java 8


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


def download_mod_file(url: str, expected_sha1: Optional[str], dest_path: str) -> None:
    """Stream a mod jar to disk atomically, verifying its SHA-1 hash."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"Refusing non-HTTPS download URL: {url}")
    tmp_path = dest_path + ".part"
    sha1 = hashlib.sha1()
    try:
        with requests.get(url, headers=API_HEADERS, stream=True, timeout=(5, 60)) as r:
            r.raise_for_status()
            with open(tmp_path, "wb") as out:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        sha1.update(chunk)
                        out.write(chunk)
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
            r = requests.get(UPDATE_MANIFEST_URL, headers=API_HEADERS, timeout=(5, 10))
            if r.status_code != 200:
                raise ValueError(f"Update check failed (HTTP {r.status_code}).")
            data = r.json()
            latest = str(data.get("latest", "")).strip()
            if not latest:
                raise ValueError("version.json is missing the 'latest' field.")
            if _parse_version_tag(latest) > _parse_version_tag(APP_VERSION):
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
            with requests.get(self.url, headers=API_HEADERS, stream=True, timeout=(5, 60)) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length", 0) or 0)
                with open(tmp_path, "wb") as out:
                    for chunk in r.iter_content(chunk_size=65536):
                        if not chunk:
                            continue
                        out.write(chunk)
                        done += len(chunk)
                        if total > 0:
                            self.progress.emit(min(100, int(done * 100 / total)))

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

            def set_progress(val: int) -> None:
                if self._max_val > 0:
                    percent = max(0, min(100, int((val / self._max_val) * 100)))
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
                 ram_gb: int, performance_mode: bool, java_path: Optional[str] = None):
        super().__init__()
        self.username = username
        self.version = version
        self.minecraft_dir = minecraft_dir
        self.ram_gb = ram_gb
        self.performance_mode = performance_mode
        self.java_path = java_path
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

    def _fabric_version_valid(self, fabric_id: str) -> bool:
        """A fabric version is usable only if its version json exists."""
        json_path = os.path.join(
            self.minecraft_dir, "versions", fabric_id, fabric_id + ".json"
        )
        return os.path.exists(json_path) and os.path.getsize(json_path) > 2

    def run(self) -> None:
        try:
            instance_subname = safe_instance_name(self.version)

            def set_status(text: str) -> None:
                self.progress_updated.emit(text, -1)

            def set_max(val: int) -> None:
                self._max_val = val

            def set_progress(val: int) -> None:
                if self._max_val > 0:
                    percent = max(0, min(100, int((val / self._max_val) * 100)))
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
                found = next(
                    (v for v in installed if v.startswith("fabric-loader-") and v.endswith(self.version)),
                    None
                )
                if found is not None and not self._fabric_version_valid(found):
                    sys.stderr.write(f"Removing broken Fabric installation: {found}\n")
                    try:
                        shutil.rmtree(os.path.join(self.minecraft_dir, "versions", found))
                    except OSError as rm_err:
                        sys.stderr.write(f"Could not remove broken Fabric dir: {rm_err}\n")
                    found = None

                if found is not None:
                    target_version = found
                    self.progress_updated.emit("Fabric loader (cached)...", 10)
                else:
                    install_error = None
                    try:
                        self.progress_updated.emit("Installing Fabric...", 10)
                        minecraft_launcher_lib.fabric.install_fabric(self.version, self.minecraft_dir, callback=callbacks)
                    except Exception as e:
                        install_error = str(e)
                        sys.stderr.write(f"Fabric installation failed: {e}\n")

                    target_version = None
                    try:
                        installed_after = [
                            v.get("id", "") for v in minecraft_launcher_lib.utils.get_installed_versions(self.minecraft_dir)
                        ]
                        matching = [
                            v for v in installed_after
                            if v.startswith("fabric-loader-") and v.endswith(self.version)
                            and self._fabric_version_valid(v)
                        ]
                        if matching:
                            target_version = matching[-1]
                    except Exception as e:
                        sys.stderr.write(f"Cannot list installed versions ({e})\n")

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
                "launcherVersion": APP_VERSION,
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

            if self.java_path and os.path.exists(self.java_path):
                options["executablePath"] = self.java_path
            else:
                try:
                    runtime_info = minecraft_launcher_lib.runtime.get_version_runtime_information(
                        self.version, self.minecraft_dir
                    )
                    if runtime_info and runtime_info.get("name"):
                        java_exec = minecraft_launcher_lib.runtime.get_executable_path(
                            runtime_info["name"], self.minecraft_dir
                        )
                        if java_exec and os.path.exists(java_exec):
                            options["executablePath"] = java_exec
                except Exception:
                    pass

                if "executablePath" not in options:
                    expected = get_expected_runtime_name(self.version)
                    expected_exec = minecraft_launcher_lib.runtime.get_executable_path(
                        expected, self.minecraft_dir
                    )
                    if expected_exec and os.path.exists(expected_exec):
                        options["executablePath"] = expected_exec
                    else:
                        legacy_exec = minecraft_launcher_lib.runtime.get_executable_path(
                            "jre-legacy", self.minecraft_dir
                        )
                        if legacy_exec and os.path.exists(legacy_exec):
                            options["executablePath"] = legacy_exec

            command = minecraft_launcher_lib.command.get_minecraft_command(
                target_version,
                self.minecraft_dir,
                options
            )

            self.progress_updated.emit("Launching...", 100)

            log_path = os.path.join(instance_dir, "latest.log")
            log_file = open(log_path, "w", encoding="utf-8")
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
                        except Exception:
                            pass
                        tail = _read_log_tail(log_path)
                        code = self.process.returncode
                        if code != 0:
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
                self.process.wait()
                if not self._aborted:
                    self.game_exited.emit()
            finally:
                watcher.close()
                log_file.close()

        except FileNotFoundError:
            self.error_occurred.emit(
                "Java environment not found.\n\n"
                "Please ensure Java (OpenJDK 17 or 21 recommended) "
                "is installed and present in your system's PATH."
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
            r = requests.get(url, headers=API_HEADERS, params=params, timeout=(5, 15))
            if r.status_code == 200:
                info = _pick(r.json())
                if info:
                    return info, None

            # Fallback: query without the game-version filter and match client-side.
            r = requests.get(
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
            r = requests.get(url, headers=API_HEADERS, timeout=(3, 5))
            if r.status_code == 200:
                image = QImage()
                image.loadFromData(r.content)
                if not image.isNull():
                    self.avatar_loaded.emit(self.username, image)
        except Exception:
            pass


class ModSearchWorker(QThread):
    results_ready = pyqtSignal(list)

    def __init__(self, query: str):
        super().__init__()
        self.query = query

    def run(self) -> None:
        try:
            params = {
                "query": self.query,
                "facets": json.dumps([["categories:fabric"], ["project_type:mod"]])
            }
            r = requests.get("https://api.modrinth.com/v2/search",
                             headers=API_HEADERS, params=params, timeout=(5, 10))
            if r.status_code == 200:
                hits = r.json().get("hits", [])
                self.results_ready.emit(hits)
        except Exception:
            self.results_ready.emit([])


class ModInstallWorker(QThread):
    progress = pyqtSignal(str)
    completed = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, project_id: str, slug: str, mc_version: str, instance_dir: str):
        super().__init__()
        self.project_id = project_id
        self.slug = slug or project_id
        self.mc_version = mc_version
        self.instance_dir = instance_dir

    def run(self) -> None:
        try:
            self.progress.emit("Locating version...")
            params = {
                "loaders": json.dumps(["fabric"]),
                "game_versions": json.dumps([self.mc_version])
            }
            url = f"https://api.modrinth.com/v2/project/{self.project_id}/version"
            r = requests.get(url, headers=API_HEADERS, params=params, timeout=(5, 15))
            if r.status_code != 200:
                raise ValueError(f"Modrinth API error (HTTP {r.status_code}).")
            data = r.json()
            if not data or not isinstance(data, list) or len(data) == 0:
                raise ValueError(f"No compatible Fabric versions found for Minecraft {self.mc_version}.")

            files_list = data[0].get("files", [])
            if not files_list:
                raise ValueError("No compatible files found in this project version.")

            file_info = files_list[0]
            for f in files_list:
                if f.get("primary"):
                    file_info = f
                    break

            target_filename = sanitize_mod_filename(file_info.get("filename", ""))
            if target_filename is None:
                raise ValueError("Unsafe or missing filename returned by Modrinth API.")
            self.progress.emit(f"Downloading {target_filename}...")
            mods_dir = os.path.join(self.instance_dir, "mods")
            dest = os.path.join(mods_dir, target_filename)
            os.makedirs(mods_dir, exist_ok=True)

            if os.path.exists(mods_dir):
                for f in os.listdir(mods_dir):
                    if f.endswith(".jar") and (matches_mod(f, self.slug) or matches_mod(f, self.project_id)) and f != target_filename:
                        try:
                            os.remove(os.path.join(mods_dir, f))
                        except Exception:
                            pass

            if not os.path.exists(dest) or os.path.getsize(dest) == 0:
                expected_sha1 = (file_info.get("hashes") or {}).get("sha1")
                download_mod_file(file_info["url"], expected_sha1, dest)

            if is_fabric_compatible(self.mc_version):
                self._ensure_fabric_api()
            self.completed.emit()
        except Exception as e:
            self.error.emit(str(e))

    def _ensure_fabric_api(self) -> None:
        mods_dir = os.path.join(self.instance_dir, "mods")
        os.makedirs(mods_dir, exist_ok=True)

        try:
            params = {
                "loaders": json.dumps(["fabric"]),
                "game_versions": json.dumps([self.mc_version])
            }
            r = requests.get("https://api.modrinth.com/v2/project/fabric-api/version",
                             headers=API_HEADERS, params=params, timeout=(5, 15))

            target_filename = None
            file_info = None
            if r.status_code == 200:
                data = r.json()
                if data and isinstance(data, list) and len(data) > 0:
                    files_list = data[0].get("files", [])
                    if files_list:
                        file_info = files_list[0]
                        for f in files_list:
                            if f.get("primary"):
                                file_info = f
                                break
                        target_filename = sanitize_mod_filename(file_info.get("filename", ""))

            if target_filename and file_info and "url" in file_info:
                if os.path.exists(mods_dir):
                    for f in os.listdir(mods_dir):
                        if f.endswith(".jar") and matches_mod(f, "fabric-api") and f != target_filename:
                            try:
                                os.remove(os.path.join(mods_dir, f))
                            except Exception:
                                pass

                dest_path = os.path.join(mods_dir, target_filename)
                if not os.path.exists(dest_path) or os.path.getsize(dest_path) == 0:
                    self.progress.emit("Downloading Fabric API...")
                    expected_sha1 = (file_info.get("hashes") or {}).get("sha1")
                    download_mod_file(file_info["url"], expected_sha1, dest_path)
            else:
                local_match = False
                if os.path.exists(mods_dir):
                    for f in os.listdir(mods_dir):
                        f_path = os.path.join(mods_dir, f)
                        if f.endswith(".jar") and os.path.getsize(f_path) > 0 and matches_mod(f, "fabric-api"):
                            local_match = True
                            break
                if not local_match:
                    self.progress.emit("Fabric API could not be located.")
        except Exception as e:
            sys.stderr.write(f"Failed to auto-download Fabric API: {e}\n")


class SmoothButton(QPushButton):
    """QPushButton with an animated background-color transition on hover/press."""
    def __init__(self, text="", parent=None, base="#0A84FF", hover="#2F95FF", pressed="#0067C0"):
        super().__init__(text, parent)
        self._base, self._hover, self._pressed = QColor(base), QColor(hover), QColor(pressed)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(140)
        self._anim.valueChanged.connect(self._apply_color)
        self._current = QColor(base)

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
        self.setStyleSheet(
            f"QPushButton {{ background-color: {self._current.name()}; color: #FFFFFF;"
            f" border: none; border-radius: 10px; font-weight: bold; }}"
            f"QPushButton:disabled {{ background-color: #3A3A3C; color: #8E8E93; }}"
        )

    def enterEvent(self, e):
        self._update_target()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._update_target()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        self._update_target()
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        self._update_target()
        super().mouseReleaseEvent(e)

    def changeEvent(self, e):
        if e.type() == QEvent.Type.EnabledChange:
            self._update_target()
        super().changeEvent(e)


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
    """Dark, launcher-styled replacement for native QMessageBox."""

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
        #dialogCopyBtn {{
            background-color: transparent;
            border: 1px solid #3A3A3C;
            border-radius: 8px;
            color: #98989D;
            font-family: 'Segoe UI', sans-serif;
            font-size: 12px;
            font-weight: bold;
            padding: 8px 0;
        }}
        #dialogCopyBtn:hover {{
            border-color: #0A84FF;
            color: #FFFFFF;
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

    def __init__(self, parent, kind: str, title: str, message: str, buttons) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        self.result_choice = None

        k = self._KIND.get(kind, self._KIND["info"])
        self.setStyleSheet(self._STYLE.format(**k))

        card = QFrame(self, objectName="dialogCard")
        card.setFixedWidth(360)
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

        msg_lbl = QLabel(message, objectName="dialogMessage")
        msg_lbl.setWordWrap(True)
        if len(message) > 260:
            scroll = QScrollArea(objectName="dialogScroll")
            scroll.setWidgetResizable(True)
            scroll.setWidget(msg_lbl)
            scroll.setFixedHeight(170)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            layout.addWidget(scroll)
        else:
            layout.addWidget(msg_lbl)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        if len(message) > 120:
            copy_btn = QPushButton("Copy", objectName="dialogCopyBtn")
            copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            copy_btn.setFixedHeight(32)
            copy_btn.setFixedWidth(72)
            copy_btn.setToolTip("Copy error details to clipboard")

            def _copy_details() -> None:
                QApplication.clipboard().setText(f"{title}\n\n{message}")
                copy_btn.setText("Copied!")
                copy_btn.setStyleSheet("color: #30D158; border-color: #30D158;")
                QTimer.singleShot(1600, lambda: (
                    copy_btn.setText("Copy"),
                    copy_btn.setStyleSheet(""),
                ))

            copy_btn.clicked.connect(_copy_details)
            btn_row.addWidget(copy_btn)
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

        self.setFixedWidth(400)
        self.adjustSize()

        if parent is not None:
            pg = parent.geometry()
            self.move(
                pg.x() + (pg.width() - self.width()) // 2,
                max(pg.y() + 20, pg.y() + (pg.height() - self.height()) // 3),
            )

    def _finish(self, result) -> None:
        self.result_choice = result
        self.accept()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self._finish(None)
            return
        super().keyPressEvent(event)

    @staticmethod
    def _show(parent, kind: str, title: str, message: str, buttons):
        dlg = VantaDialog(parent, kind, title, message, buttons)
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
    def error(parent, title: str, message: str) -> None:
        VantaDialog._show(parent, "error", title, message, [("OK", True, "ok")])

    @staticmethod
    def warning(parent, title: str, message: str) -> None:
        VantaDialog._show(parent, "warning", title, message, [("OK", True, "ok")])

    @staticmethod
    def info(parent, title: str, message: str) -> None:
        VantaDialog._show(parent, "info", title, message, [("OK", True, "ok")])

    @staticmethod
    def question(parent, title: str, message: str, default_yes: bool = False) -> bool:
        result = VantaDialog._show(
            parent, "info", title, message,
            [("Yes", True, True), ("No", False, False)],
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

        shadow = QGraphicsDropShadowEffect(self.card)
        shadow.setBlurRadius(14)
        shadow.setYOffset(3)
        shadow.setColor(QColor(0, 0, 0, 160))
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
        self.list_widget.clear()
        for i in range(self.combo.count()):
            text = self.combo.itemText(i)
            item = QListWidgetItem(text)
            self.list_widget.addItem(item)
        idx = self.combo.currentIndex()
        if 0 <= idx < self.list_widget.count():
            self.list_widget.setCurrentRow(idx)
            self.list_widget.scrollToItem(self.list_widget.item(idx), QAbstractItemView.ScrollHint.PositionAtCenter)

    def _on_item_selected(self, item: QListWidgetItem) -> None:
        text = item.text()
        self.hide()
        if text != self.combo.currentText():
            self.combo.setCurrentText(text)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            event.accept()
            return
        super().keyPressEvent(event)

    def hideEvent(self, event) -> None:
        self.combo._last_hide_time = time.monotonic()
        super().hideEvent(event)


class AnchoredComboBox(QComboBox):
    """Custom combobox with a floating frameless popup list."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._popup: Optional[ComboPopup] = None
        self._last_hide_time = 0.0

    def _ensure_popup(self) -> ComboPopup:
        if self._popup is None:
            self._popup = ComboPopup(self)
        return self._popup

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
        if screen is not None:
            avail = screen.availableGeometry()
            if global_pos.y() + total_h > avail.bottom():
                global_pos.setY(self.mapToGlobal(QPoint(0, 0)).y() - total_h + 16)
            if global_pos.x() + total_w > avail.right():
                global_pos.setX(avail.right() - total_w)
            if global_pos.x() < avail.left():
                global_pos.setX(avail.left())

        popup.setGeometry(global_pos.x(), global_pos.y(), total_w, total_h)
        popup.show()
        popup.raise_()
        popup.list_widget.setFocus()

    def hidePopup(self) -> None:
        if self._popup is not None and self._popup.isVisible():
            self._popup.hide()

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
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._advance)

    def _advance(self) -> None:
        self._shimmer_pos += self._SPEED
        if self._shimmer_pos > 1.4:
            self._shimmer_pos -= self._RANGE
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
        maximum = self.maximum()
        frac = 0.0 if maximum <= self.minimum() else (self.value() - self.minimum()) / (maximum - self.minimum())
        frac = max(0.0, min(1.0, frac))
        fill_w = int(rect.width() * frac)

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
            "Vanta",
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
        shadow = QGraphicsDropShadowEffect(self.card)
        shadow.setBlurRadius(18)
        shadow.setXOffset(0)
        shadow.setYOffset(4)
        shadow.setColor(QColor(0, 0, 0, 100))
        self.card.setGraphicsEffect(shadow)

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

            self._is_closing = True
            event.ignore()

            def cleanup_and_close():
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
            self.progress_bar.setFormat(f"{text} %p%" if text else "%p%")
        else:
            self.play_stack.setCurrentIndex(0)
            self.play_button.setEnabled(True)
            self.play_button.setText("Play")

    def _init_ui(self) -> None:
        self.setWindowTitle("Vanta Launcher")

        icon_path = os.path.join(_resource_base(), "icons", "icon.ico")

        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._CLOSED_WIDTH = 364
        self._OPEN_WIDTH = 704
        self.setFixedSize(self._CLOSED_WIDTH, 214)

        screen = QApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            self.move(
                geo.x() + (geo.width() - self.width()) // 2,
                geo.y() + (geo.height() - self.height()) // 2,
            )

        arrow_path = _get_arrow_image_path()
        self.setStyleSheet(self._stylesheet(arrow_path))

        central = QWidget(self)
        self.setCentralWidget(central)
        self._central = central

        self.card = QFrame(central, objectName="cardFrame")
        self.card.setGeometry(12, 12, 340, 190)
        self._apply_card_shadow()

        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(16, 16, 16, 16)
        card_layout.setSpacing(8)

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
        self.drawer.setGeometry(12, 12, 340, 190)
        self.drawer.stackUnder(self.card)

        drawer_layout = QVBoxLayout(self.drawer)
        drawer_layout.setContentsMargins(16, 16, 16, 16)
        drawer_layout.setSpacing(10)

        nav_layout = QHBoxLayout()
        nav_layout.setContentsMargins(0, 0, 0, 0)
        self.settings_tab_btn = QPushButton("Settings", objectName="tabBtn")
        self.mods_tab_btn = QPushButton("Mods Manager", objectName="tabBtn")
        self.settings_tab_btn.setCheckable(True)
        self.mods_tab_btn.setCheckable(True)
        self.settings_tab_btn.clicked.connect(self._on_tab_clicked)
        self.mods_tab_btn.clicked.connect(self._on_tab_clicked)
        nav_layout.addWidget(self.settings_tab_btn)
        nav_layout.addWidget(self.mods_tab_btn)
        drawer_layout.addLayout(nav_layout)

        self.drawer_stack = QStackedWidget()
        drawer_layout.addWidget(self.drawer_stack)

        settings_widget = QWidget()
        settings_layout = QVBoxLayout(settings_widget)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.setSpacing(10)

        ram_header_layout = QHBoxLayout()
        ram_lbl = QLabel("Allocated RAM:")
        ram_lbl.setStyleSheet("color: #FFFFFF; font-family: 'Segoe UI', sans-serif; font-size: 12px;")
        self.ram_val_lbl = QLabel("4 GB")
        self.ram_val_lbl.setStyleSheet(
            "color: #0A84FF; font-family: 'Segoe UI', sans-serif; font-size: 12px; font-weight: bold;"
        )
        ram_header_layout.addWidget(ram_lbl)
        ram_header_layout.addStretch(1)
        ram_header_layout.addWidget(self.ram_val_lbl)
        settings_layout.addLayout(ram_header_layout)

        self.ram_slider = QSlider(Qt.Orientation.Horizontal)
        self.ram_slider.setMinimum(1)
        self.ram_slider.setMaximum(16)
        self.ram_slider.setValue(4)
        self.ram_slider.valueChanged.connect(self._on_ram_slider_changed)
        settings_layout.addWidget(self.ram_slider)

        self.perf_checkbox = QCheckBox("Performance Mode (Fabric + Optimization Mods)")
        self.perf_checkbox.setChecked(True)
        self.perf_checkbox.toggled.connect(self._on_perf_toggled)
        settings_layout.addWidget(self.perf_checkbox)

        self.rpc_checkbox = QCheckBox("Discord Rich Presence")
        self.rpc_checkbox.setChecked(True)
        self.rpc_checkbox.stateChanged.connect(self._on_rpc_state_changed)
        settings_layout.addWidget(self.rpc_checkbox)
        settings_layout.addStretch(1)

        self.drawer_stack.addWidget(settings_widget)

        mods_widget = QWidget()
        mods_layout = QVBoxLayout(mods_widget)
        mods_layout.setContentsMargins(0, 0, 0, 0)
        mods_layout.setSpacing(6)

        search_layout = QHBoxLayout()
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.setSpacing(4)

        self.mod_search_input = QLineEdit()
        self.mod_search_input.setPlaceholderText("Search Modrinth...")
        self.mod_search_input.setFixedHeight(26)
        self.mod_search_input.setStyleSheet("padding: 2px 8px; font-size: 11px;")

        self._search_timer = QTimer()
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._on_mod_search)
        self.mod_search_input.textChanged.connect(self._search_timer.start)

        search_layout.addWidget(self.mod_search_input)
        mods_layout.addLayout(search_layout)

        self.mods_list = QListWidget()
        mods_layout.addWidget(self.mods_list)

        mod_action_layout = QHBoxLayout()
        self.mod_action_btn = QPushButton("Install", objectName="modActionBtn")
        self.mod_action_btn.clicked.connect(self._on_mod_action)
        self.mod_action_btn.setFixedHeight(24)
        self.mod_delete_btn = QPushButton("Delete Selected", objectName="modDeleteBtn")
        self.mod_delete_btn.clicked.connect(self._on_mod_delete)
        self.mod_delete_btn.setFixedHeight(24)
        mod_action_layout.addWidget(self.mod_action_btn)
        mod_action_layout.addWidget(self.mod_delete_btn)
        mods_layout.addLayout(mod_action_layout)

        self.drawer_stack.addWidget(mods_widget)

        self._avatar_timer = QTimer()
        self._avatar_timer.setSingleShot(True)
        self._avatar_timer.timeout.connect(self._fetch_avatar)
        self.nick_input.textChanged.connect(self._on_nick_changed)

        for btn in self.findChildren(QPushButton):
            btn.setCursor(Qt.CursorShape.PointingHandCursor)

    def _init_ram_slider(self) -> None:
        total_ram = self._get_total_ram_gb()
        max_ram = max(2, total_ram - 1)
        self.ram_slider.setMaximum(max_ram)

    def _on_tab_clicked(self) -> None:
        is_settings = self.sender() is self.settings_tab_btn
        self.drawer_stack.setCurrentIndex(0 if is_settings else 1)
        self.settings_tab_btn.setChecked(is_settings)
        self.mods_tab_btn.setChecked(not is_settings)

    def _toggle_drawer(self) -> None:
        if hasattr(self, "version_combo") and hasattr(self.version_combo, "hidePopup"):
            self.version_combo.hidePopup()

        if getattr(self, "_drawer_animator", None) is not None and self._drawer_animator.is_running():
            self._drawer_animator.stop()
        if getattr(self, "_window_animator", None) is not None and self._window_animator.is_running():
            self._window_animator.stop()

        self._drawer_expanded = not self._drawer_expanded
        end_x = 352 if self._drawer_expanded else 12
        start_x = self.drawer.x()

        def apply_drawer(x: float) -> None:
            self.drawer.move(round(x), self.drawer.y())

        self._drawer_animator = EaseAnimator(340, apply_drawer, parent=self)
        self._drawer_animator.start(start_x, end_x)

        self._animate_window_width(self._OPEN_WIDTH if self._drawer_expanded else self._CLOSED_WIDTH)

    def _animate_window_width(self, target_width: int) -> None:
        """
        Expand/collapse window width while shifting the window horizontally.
        Moves left by half the expansion delta when opening so the launcher stays
        visually centered, and moves back right when closing.
        """
        if getattr(self, "_window_animator", None) is not None and self._window_animator.is_running():
            self._window_animator.stop()

        start_geo = self.geometry()
        start_w = start_geo.width()
        delta_w = target_width - start_w
        if delta_w == 0:
            return

        start_x = start_geo.x()
        target_x = start_x - (delta_w // 2)

        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            if target_x + target_width > available.right():
                target_x = available.right() - target_width
            if target_x < available.left():
                target_x = available.left()

        delta_x = target_x - start_x

        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)

        def apply(t: float) -> None:
            w = round(start_w + delta_w * t)
            x = round(start_x + delta_x * t)
            self.setGeometry(x, start_geo.y(), w, start_geo.height())
            if t >= 1.0:
                self.setFixedSize(w, self.height())

        self._window_animator = EaseAnimator(340, apply, parent=self)
        self._window_animator.start(0.0, 1.0)

    def _on_ram_slider_changed(self, value: int) -> None:
        self.ram_val_lbl.setText(f"{value} GB")
        self.settings.setValue("ram_gb", value)

    def _on_perf_toggled(self, checked: bool) -> None:
        version = self.version_combo.currentText()
        if is_fabric_compatible(version):
            self.settings.setValue("performance_mode", "true" if checked else "false")

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
        self._refresh_installed_mods()

    def _on_mod_search(self) -> None:
        query = self.mod_search_input.text().strip()
        if not query:
            self._refresh_installed_mods()
            return

        if hasattr(self, "_search_worker") and self._search_worker.isRunning():
            try:
                self._search_worker.results_ready.disconnect()
            except Exception:
                pass

        self._search_worker = ModSearchWorker(query)
        self._register_worker(self._search_worker)
        self._search_worker.results_ready.connect(self._on_search_results)
        self._search_worker.start()

    def _on_search_results(self, hits: list) -> None:
        self.mods_list.clear()
        self.mod_action_btn.setText("Install")
        self.mod_action_btn.setProperty("mode", "install")
        for hit in hits:
            item = QListWidgetItem(f"{hit.get('title', 'Unknown')} ({hit.get('slug', '')})")
            item.setData(Qt.ItemDataRole.UserRole, (hit.get('project_id', ''), hit.get('slug', '')))
            self.mods_list.addItem(item)

    def _refresh_installed_mods(self) -> None:
        self.mods_list.clear()
        self.mod_action_btn.setText("Refresh")
        self.mod_action_btn.setProperty("mode", "refresh")

        version = self.version_combo.currentText()
        if not version or version == "Loading versions...":
            return

        mods_dir = os.path.join(self.vanta_dir, "instances", safe_instance_name(version), "mods")
        if os.path.exists(mods_dir):
            try:
                for file in os.listdir(mods_dir):
                    if file.endswith(".jar"):
                        item = QListWidgetItem(file)
                        item.setData(Qt.ItemDataRole.UserRole, file)
                        self.mods_list.addItem(item)
            except OSError:
                pass

    def _on_mod_action(self) -> None:
        mode = self.mod_action_btn.property("mode")
        if mode == "refresh":
            self._refresh_installed_mods()
            return

        selected_item = self.mods_list.currentItem()
        if not selected_item:
            return

        data = selected_item.data(Qt.ItemDataRole.UserRole)
        if isinstance(data, (tuple, list)):
            project_id, slug = data[0], data[1]
        else:
            project_id, slug = data, data

        version = self.version_combo.currentText()
        if not project_id or not version or version == "Loading versions...":
            return

        self.mod_action_btn.setEnabled(False)
        self.mod_action_btn.setText("Preparing...")

        instance_dir = os.path.join(self.vanta_dir, "instances", safe_instance_name(version))
        self._install_worker = ModInstallWorker(project_id, slug, version, instance_dir)
        self._register_worker(self._install_worker)
        self._install_worker.progress.connect(self.mod_action_btn.setText)
        self._install_worker.completed.connect(self._on_mod_installed)
        self._install_worker.error.connect(self._on_mod_install_failed)
        self._install_worker.start()

    def _on_mod_installed(self) -> None:
        self.mod_action_btn.setEnabled(True)
        self.mod_action_btn.setText("Install")
        self.mod_search_input.clear()
        self._refresh_installed_mods()

    def _on_mod_install_failed(self, error: str) -> None:
        self.mod_action_btn.setEnabled(True)
        self.mod_action_btn.setText("Install")
        VantaDialog.warning(self, "Mod Install Error", f"Failed to install mod:\n\n{error}")

    def _on_mod_delete(self) -> None:
        selected_item = self.mods_list.currentItem()
        if not selected_item:
            return

        mode = self.mod_action_btn.property("mode")
        if mode != "refresh":
            VantaDialog.warning(
                self, "Cannot Delete",
                "You can only delete installed mods from the list, not search results."
            )
            return

        filename = selected_item.data(Qt.ItemDataRole.UserRole)
        if not filename or not isinstance(filename, str):
            return
        version = self.version_combo.currentText()
        if not version or version == "Loading versions...":
            return

        if "fabric-api" in filename.lower() or "fabric_api" in filename.lower():
            VantaDialog.warning(
                self, "Cannot Delete",
                "Fabric API is required for mod support and cannot be removed from here."
            )
            return

        reply = VantaDialog.question(
            self, "Delete Mod",
            f"Are you sure you want to delete '{filename}'?",
            default_yes=False
        )
        if not reply:
            return

        mods_dir = os.path.abspath(os.path.join(self.vanta_dir, "instances", safe_instance_name(version), "mods"))
        filepath = os.path.abspath(os.path.join(mods_dir, filename))
        if os.path.normcase(os.path.dirname(filepath)) != os.path.normcase(mods_dir):
            return

        if os.path.exists(filepath):
            try:
                os.remove(filepath)
                self._refresh_installed_mods()
            except Exception as e:
                VantaDialog.warning(self, "Delete Error", f"Could not delete mod file:\n\n{e}")

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
            #tabBtn {{
                background-color: #2C2C2E;
                border: 1px solid #3A3A3C;
                border-radius: 6px;
                padding: 0px 12px;
                height: 26px;
                font-size: 11px;
                font-weight: normal;
                color: #FFFFFF;
            }}
            #tabBtn:hover {{
                background-color: #3A3A3C;
            }}
            #tabBtn:checked {{
                background-color: #0A84FF;
                border-color: #0A84FF;
                color: #FFFFFF;
            }}
            #drawer QLabel {{
                color: #FFFFFF;
                font-family: 'Segoe UI', sans-serif;
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
            QListWidget {{
                background-color: #2C2C2E;
                border: 1px solid #3A3A3C;
                border-radius: 8px;
                color: #FFFFFF;
                font-family: 'Segoe UI', sans-serif;
                font-size: 11px;
            }}
            QListWidget::item {{
                padding: 4px;
                border: none;
                border-radius: 6px;
                margin: 1px 3px;
            }}
            QListWidget::item:hover {{
                background-color: #323234;
            }}
            QListWidget::item:selected {{
                background-color: #0A84FF;
                color: #FFFFFF;
            }}
            #modActionBtn, #modDeleteBtn {{
                font-size: 11px;
                padding: 0px 8px;
                height: 22px;
                border-radius: 6px;
            }}
            #modDeleteBtn {{
                background-color: #FF5F56;
            }}
            #modDeleteBtn:hover {{
                background-color: #E0443E;
            }}
        """

    def _load_settings(self) -> None:
        if self._initial_avatar is not None:
            self.nick_input.blockSignals(True)
        self.nick_input.setText(self.settings.value("username", ""))
        self.nick_input.blockSignals(False)

        try:
            saved_ram = int(self.settings.value("ram_gb", 4))
        except (ValueError, TypeError):
            saved_ram = 4
        self.ram_slider.setValue(max(self.ram_slider.minimum(), min(saved_ram, self.ram_slider.maximum())))

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

        if saved_version in versions:
            self.version_combo.setCurrentText(saved_version)
        elif current in versions:
            self.version_combo.setCurrentText(current)
        self.version_combo.blockSignals(False)

        self._on_version_changed(self.version_combo.currentText())

    def _on_versions_fetch_failed(self, _error_message: str) -> None:
        fallback = ["1.21.4", "1.21.1", "1.20.4", "1.19.4", "1.16.5", "1.8.9"]
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
        self.version_combo.blockSignals(False)

        self._on_version_changed(self.version_combo.currentText())

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
            f"Vanta {latest_tag} is available (you are running v{APP_VERSION}).\n\n"
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
        try:
            runtime_info = minecraft_launcher_lib.runtime.get_version_runtime_information(
                version, self.minecraft_dir
            )
            required_runtime = runtime_info.get("name") if runtime_info else None
        except Exception:
            pass

        if not required_runtime:
            required_runtime = get_expected_runtime_name(version)

        def start_launch(java_exec: Optional[str] = None):
            self._set_ui_enabled(False)
            self._show_progress(True, "Preparing")
            ram = self.ram_slider.value()
            perf = self.perf_checkbox.isChecked() and is_fabric_compatible(version)

            self._update_rpc(state="In-Game", details=f"Playing Minecraft {version}")

            self._launch_worker = LaunchWorker(username, version, self.minecraft_dir,
                                               ram, perf, java_path=java_exec)
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

        if not java_exec or not os.path.exists(java_exec):
            if not shutil.which("java"):
                def on_download():
                    self._set_ui_enabled(False)
                    self._show_progress(True, "Downloading Java")
                    self._java_worker = JavaDownloadWorker(required_runtime, self.minecraft_dir)
                    self._register_worker(self._java_worker)
                    self._java_worker.progress.connect(self._on_java_progress)
                    self._java_worker.completed.connect(
                        lambda: start_launch(
                            minecraft_launcher_lib.runtime.get_executable_path(required_runtime, self.minecraft_dir)
                        )
                    )
                    self._java_worker.error.connect(self._on_java_error)
                    self._java_worker.start()

                if not self._prompt_java_download(required_runtime, on_download):
                    self._launch_in_progress = False
                return
            else:
                java_exec = None

        start_launch(java_exec)

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
        VantaDialog.error(self, "Launch Failed", message)
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
        VantaDialog.error(
            self,
            "Launch Error",
            f"An error occurred while launching Minecraft:\n\n{error_message}",
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
    QApplication.setApplicationVersion(APP_VERSION)
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

    splash = SplashScreen()
    splash.show()

    MIN_SPLASH_MS = 1700
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
                r = requests.get(
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
        if (not avatar_state["done"] or not version_state["done"]) and elapsed < 8000:
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

        QTimer.singleShot(800, _open_window)

    QTimer.singleShot(150, _boot_launcher)
    sys.exit(app.exec())