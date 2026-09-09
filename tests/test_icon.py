"""The plugin's Qt icon: one definition, shared with the page."""

from __future__ import annotations

import unittest
import unittest.mock

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

from PyQt6.QtWidgets import QApplication, QDialog  # noqa: E402

from job_manager import icon as plugin_icon_module  # noqa: E402
from job_manager.web_monitor import FAVICON_SVG  # noqa: E402
from job_manager.window_utils import make_independent  # noqa: E402


class IconTestCase(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        # The module caches; a stale cache from another test would make these
        # assert about whatever ran first.
        plugin_icon_module._cached = None
        self.addCleanup(setattr, plugin_icon_module, "_cached", None)


class TestTheIcon(IconTestCase):
    def test_it_renders_at_every_size_qt_was_given(self):
        # A single pixmap scaled by the window manager turns thin bonds to
        # mush at 16 px, which is the size a title bar actually uses.
        sizes = {s.width() for s in plugin_icon_module.plugin_icon().availableSizes()}
        self.assertTrue({16, 32, 256}.issubset(sizes), sizes)

    def test_it_is_the_same_drawing_the_page_uses(self):
        # One definition. Two would drift, and the tab and the task bar would
        # stop being recognisably the same plugin.
        self.assertIn("#1e5fd0", FAVICON_SVG)
        self.assertIn("<rect", FAVICON_SVG)
        self.assertIs(plugin_icon_module.FAVICON_SVG, FAVICON_SVG)

    def test_it_is_built_once(self):
        self.assertIs(plugin_icon_module.plugin_icon(), plugin_icon_module.plugin_icon())

    def test_a_qt_that_cannot_parse_it_is_not_fatal(self):
        # An icon is decoration; a build that cannot draw it must still open
        # its windows rather than failing on the way up.
        from PyQt6.QtSvg import QSvgRenderer

        with unittest.mock.patch.object(QSvgRenderer, "isValid", return_value=False):
            plugin_icon_module._cached = None
            self.assertTrue(plugin_icon_module.plugin_icon().isNull())

        dialog = QDialog()
        self.addCleanup(dialog.deleteLater)
        plugin_icon_module.apply_icon(dialog)  # must not raise

    def test_a_qt_without_qtsvg_at_all_is_not_fatal(self):
        # QtSvg is a separate module and some minimal builds omit it.
        import builtins

        real_import = builtins.__import__

        def refuse(name, *args, **kwargs):
            if name == "PyQt6.QtSvg":
                raise ImportError("no QtSvg in this build")
            return real_import(name, *args, **kwargs)

        with unittest.mock.patch.object(builtins, "__import__", refuse):
            plugin_icon_module._cached = None
            self.assertTrue(plugin_icon_module.plugin_icon().isNull())

    def test_it_is_rendered_at_each_size_not_scaled_from_one(self):
        # Rasterising once at 32 and scaling up is what made the large sizes
        # soft; each pixmap must be its own render.
        icon = plugin_icon_module.plugin_icon()
        big = icon.pixmap(256, 256)
        self.assertEqual((big.width(), big.height()), (256, 256))
        self.assertFalse(big.isNull())


class TestEveryIndependentWindowGetsIt(IconTestCase):
    def test_make_independent_applies_it(self):
        # Each of these windows has its own task bar entry, so each needs its
        # own icon or it shows the generic one beside MoleditPy's.
        dialog = QDialog()
        self.addCleanup(dialog.deleteLater)
        self.assertTrue(dialog.windowIcon().isNull())
        make_independent(dialog)
        self.assertFalse(dialog.windowIcon().isNull())


if __name__ == "__main__":
    unittest.main()


class TestThePngFallbacksMatchTheSvg(IconTestCase):
    """Safari renders no SVG favicon, so the drawing ships twice -- and two
    copies of anything drift. Regenerate with `python -m tests.regenerate_icons`.
    """

    def decode(self, b64: str):
        import base64

        from PyQt6.QtGui import QImage

        image = QImage()
        self.assertTrue(image.loadFromData(base64.b64decode(b64), "PNG"), "not a PNG")
        return image

    def render(self, size: int):
        from PyQt6.QtCore import QByteArray, Qt
        from PyQt6.QtGui import QPainter, QPixmap
        from PyQt6.QtSvg import QSvgRenderer

        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        QSvgRenderer(QByteArray(FAVICON_SVG.encode())).render(painter)
        painter.end()
        return pixmap.toImage()

    def signature(self, image):
        """Mean colour of each quadrant. Compared instead of raw bytes: two
        PNG encoders, or two Qt builds, produce different files from identical
        pixels, and a byte comparison would fail for no reason anyone can act
        on."""
        w, h = image.width(), image.height()
        out = []
        for qx in (0, 1):
            for qy in (0, 1):
                total = [0, 0, 0]
                count = 0
                for x in range(qx * w // 2, (qx + 1) * w // 2, 2):
                    for y in range(qy * h // 2, (qy + 1) * h // 2, 2):
                        c = image.pixelColor(x, y)
                        total[0] += c.red()
                        total[1] += c.green()
                        total[2] += c.blue()
                        count += 1
                out.append(tuple(v // max(1, count) for v in total))
        return out

    def test_the_small_one_is_the_svg(self):
        from job_manager.web_monitor import FAVICON_PNG_B64

        stored = self.decode(FAVICON_PNG_B64)
        self.assertEqual((stored.width(), stored.height()), (32, 32))
        for a, b in zip(self.signature(stored), self.signature(self.render(32))):
            for x, y in zip(a, b):
                self.assertLess(abs(x - y), 12, "the PNG no longer matches the SVG")

    def test_the_touch_icon_is_the_svg_too(self):
        from job_manager.web_monitor import TOUCH_ICON_PNG_B64

        stored = self.decode(TOUCH_ICON_PNG_B64)
        self.assertEqual((stored.width(), stored.height()), (180, 180))
        for a, b in zip(self.signature(stored), self.signature(self.render(180))):
            for x, y in zip(a, b):
                self.assertLess(abs(x - y), 12, "the touch icon no longer matches the SVG")
