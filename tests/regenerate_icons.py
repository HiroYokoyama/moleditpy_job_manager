"""Rewrite the PNG icon constants in web_monitor.py from the SVG.

Run after changing ``FAVICON_SVG``::

    python -m tests.regenerate_icons

The PNGs exist because Safari does not render SVG favicons, so the drawing has
to be shipped twice. Twice means they can drift, which is why this is a script
rather than a thing to do by hand, and why ``test_icon.py`` re-renders the SVG
and compares -- a change to the SVG without a run of this fails there.

Not part of the package: it needs Qt's SVG renderer, which the plugin does not
require at runtime, and it writes to source.
"""

from __future__ import annotations

import base64
import pathlib
import re
import sys
import textwrap

SIZES = {"FAVICON_PNG_B64": 32, "TOUCH_ICON_PNG_B64": 180}
SOURCE = pathlib.Path(__file__).resolve().parents[1] / "job_manager" / "web_monitor.py"

#: Held here, not in a local. `QApplication.instance() or QApplication([])`
#: leaves the new one unreferenced, Python collects it, and the next QPixmap
#: dies with "Must construct a QGuiApplication before a QPixmap".
_app = None


def render_png_base64(svg: str, size: int) -> str:
    from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, Qt
    from PyQt6.QtGui import QPainter, QPixmap
    from PyQt6.QtSvg import QSvgRenderer
    from PyQt6.QtWidgets import QApplication

    global _app
    _app = QApplication.instance() or QApplication([])
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(QByteArray(svg.encode("utf-8"))).render(painter)
    painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buffer, "PNG")
    return base64.b64encode(bytes(buffer.data())).decode("ascii")


def main() -> int:
    sys.path.insert(0, str(SOURCE.parents[1]))
    from job_manager.web_monitor import FAVICON_SVG

    text = SOURCE.read_text(encoding="utf-8")
    for name, size in SIZES.items():
        wrapped = "\n".join(
            '    "%s"' % chunk for chunk in textwrap.wrap(render_png_base64(FAVICON_SVG, size), 88)
        )
        pattern = re.compile(rf"^{name} = \(\n(?:    \".*\"\n)+\)$", re.MULTILINE)
        if not pattern.search(text):
            print(f"could not find {name} to replace", file=sys.stderr)
            return 1
        text = pattern.sub(f"{name} = (\n{wrapped}\n)", text)
    SOURCE.write_text(text, encoding="utf-8")
    print(f"rewrote {', '.join(SIZES)} in {SOURCE.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
