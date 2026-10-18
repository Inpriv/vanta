"""Exercise the installer's desktop integration without touching the real desktop."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class DesktopIntegrationTests(unittest.TestCase):
    def run_integration(self, directory, disabled=False, with_gio=True):
        root = Path(directory)
        home = root / 'home'
        home.mkdir()
        desktop = home / 'Localized Desktop'
        desktop.mkdir()
        data = home / 'Data with spaces'
        apps = data / 'applications'
        apps.mkdir(parents=True)
        icon = data / 'icons' / 'vanta.png'
        icon.parent.mkdir()
        shutil.copyfile(Path(__file__).resolve().parents[1] / 'icons/icon.png', icon)
        launcher = data / 'vanta/current/start.sh'
        destination = apps / 'xyz.getvanta.Vanta.desktop'
        tools = root / 'bin'
        tools.mkdir()
        (tools / 'xdg-user-dir').write_text('#!/bin/sh\nprintf "%s\\n" "$TEST_DESKTOP"\n')
        if with_gio:
            (tools / 'gio').write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$TEST_GIO_LOG"\n')
        for tool in tools.iterdir():
            tool.chmod(0o755)
        source = (Path(__file__).resolve().parents[1] / 'static/install.sh').read_text()
        integration = source.split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
        result = subprocess.run(
            [sys.executable, '-', str(launcher), str(destination), str(icon)],
            input=integration, text=True, capture_output=True, timeout=10,
            env=dict(os.environ, HOME=str(home), PATH=str(tools),
                     TEST_DESKTOP=str(home if disabled else desktop),
                     TEST_GIO_LOG=str(root / 'gio.log')),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Icon=' + str(icon) + '\n', destination.read_text())
        validator = shutil.which('desktop-file-validate')
        if validator:
            subprocess.run([validator, str(destination)], check=True, capture_output=True)
        return root, destination, desktop / destination.name, result.stdout

    def test_absolute_icon_and_trusted_localized_desktop_shortcut(self):
        with tempfile.TemporaryDirectory() as directory:
            root, menu, shortcut, output = self.run_integration(directory)
            self.assertEqual(shortcut.read_bytes(), menu.read_bytes())
            self.assertTrue(shortcut.stat().st_mode & 0o111)
            self.assertEqual((root / 'gio.log').read_text().splitlines(),
                             ['set', '-t', 'string', str(shortcut), 'metadata::trusted', 'true'])

    def test_disabled_desktop_does_not_create_shortcut_in_home(self):
        with tempfile.TemporaryDirectory() as directory:
            root, menu, shortcut, output = self.run_integration(directory, disabled=True)
            self.assertFalse(shortcut.exists())
            self.assertFalse((root / 'home' / menu.name).exists())

    def test_missing_gio_gives_allow_launching_instruction(self):
        with tempfile.TemporaryDirectory() as directory:
            root, menu, shortcut, output = self.run_integration(directory, with_gio=False)
            self.assertTrue(shortcut.exists())
            self.assertIn('Allow Launching', output)


if __name__ == '__main__':
    unittest.main()
