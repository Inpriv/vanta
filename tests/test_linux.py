"""Linux launch regressions; no network, real Minecraft or desktop required."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
_test_home = tempfile.TemporaryDirectory()
os.environ['XDG_CONFIG_HOME'] = _test_home.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from PyQt6.QtWidgets import QApplication
from PyQt6.QtTest import QTest


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux-specific regressions')
class LinuxLauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.patches = [
            patch.object(main, 'get_vanta_dir', return_value=_test_home.name + '/data'),
            patch.object(main, 'get_minecraft_dir', return_value=_test_home.name + '/minecraft'),
            patch.object(main.MinecraftLauncher, '_fetch_versions'),
            patch.object(main.MinecraftLauncher, '_init_rpc'),
        ]
        for mock in self.patches:
            mock.start()
        self.window = main.MinecraftLauncher(preloaded_versions=['1.21.1'])
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window._is_closing = True
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        for mock in reversed(self.patches):
            mock.stop()

    def test_no_windows_update_on_linux(self):
        with patch.object(main, 'UpdateCheckWorker') as worker:
            self.window._start_update_check()
            worker.assert_not_called()

    def test_start_and_return_from_game_stay_visible(self):
        self.window._fade_in_with_motion_blur()
        self.assertIsNone(self.window._central.graphicsEffect())
        self.window._on_game_confirmed()
        self.assertFalse(self.window.isVisible())
        self.window._on_game_exited()
        self.app.processEvents()
        self.assertTrue(self.window.isVisible())
        self.assertEqual(self.window.windowOpacity(), 1.0)

    def test_drawer_reaches_target_size(self):
        self.window._toggle_drawer()
        QTest.qWait(500)
        self.assertEqual(self.window.width(), self.window._OPEN_WIDTH)
        self.assertEqual(self.window.drawer.x(), 352)
        self.window._toggle_drawer()
        QTest.qWait(500)
        self.assertEqual(self.window.width(), self.window._CLOSED_WIDTH)

    def test_linux_discovers_jvms_outside_path(self):
        with patch.dict(os.environ, {'PATH': '', 'JAVA_HOME': ''}), \
             patch.object(main.glob, 'glob', return_value=['/usr/lib/jvm/test/bin/java']), \
             patch.object(main.os.path, 'exists', return_value=True), \
             patch.object(main, 'probe_java_executable', return_value=21):
            self.assertEqual(main.find_system_java(21), '/usr/lib/jvm/test/bin/java')
            self.assertIsNone(main.find_system_java(25))

    def test_native_popup_is_dark_compact_and_keyboard_selects(self):
        combo = self.window.version_combo
        combo.clear()
        combo.addItems([f"1.{i}" for i in range(200)])
        combo.setCurrentIndex(0)
        combo.setEnabled(True)
        combo.showPopup()
        self.app.processEvents()
        view = combo.view()
        self.assertTrue(view.isVisible())
        self.assertLessEqual(view.window().height(), 260)
        self.assertGreater(view.verticalScrollBar().maximum(), 0)
        self.assertLess(view.palette().color(view.backgroundRole()).lightness(), 70)
        self.assertGreater(view.palette().color(view.foregroundRole()).lightness(), 180)
        QTest.keyClick(view, main.Qt.Key.Key_End)
        QTest.keyClick(view, main.Qt.Key.Key_Return)
        self.app.processEvents()
        self.assertEqual(combo.currentIndex(), 199)
        self.assertFalse(view.isVisible())


if __name__ == '__main__':
    unittest.main()
