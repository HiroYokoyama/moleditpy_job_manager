"""The plugin's own icon, for the Qt windows and for the web page alike.

One definition, in :mod:`job_manager.web_monitor`, rather than a .ico beside a
.png beside an inline copy: the plugin is installed by unzipping a package, and
an icon that lives in a file is one more thing that can arrive missing.

A server rack in MoleditPy's blue: this plugin is about the machines the work
runs on, and the application's own icon already carries the molecule.
"""

from __future__ import annotations

import logging
from typing import Optional

from PyQt6.QtCore import QByteArray, Qt
from PyQt6.QtGui import QIcon, QPixmap

from .web_monitor import FAVICON_SVG

_cached: Optional[QIcon] = None

#: The sizes Qt is asked to bake. A QIcon built from a single pixmap is scaled
#: by the window manager wherever it needs another size, and a 16 px title bar
#: scaling down a 256 px bitmap is where thin bonds turn to mush.
_SIZES = (16, 24, 32, 48, 64, 128, 256)


def plugin_icon() -> QIcon:
    """The plugin icon, or an empty one where SVG cannot be rendered.

    Empty rather than raising: an icon is decoration, and a Qt build without
    the SVG image plugin must not be a reason a window fails to open.
    """
    global _cached
    if _cached is not None:
        return _cached

    icon = QIcon()
    data = QByteArray(FAVICON_SVG.encode("utf-8"))
    try:
        from PyQt6.QtGui import QPainter
        from PyQt6.QtSvg import QSvgRenderer
    except ImportError:  # pragma: no cover - depends on the Qt build
        logging.debug("Job Manager: QtSvg is absent; the plugin icon is skipped")
        _cached = icon
        return icon

    renderer = QSvgRenderer(data)
    if not renderer.isValid():  # pragma: no cover - would mean a broken constant
        logging.debug("Job Manager: the plugin icon did not parse")
        _cached = icon
        return icon

    for size in _SIZES:
        # Rendered *at* each size, not rasterised once and scaled: the drawing
        # is vector, and scaling a 32 px bitmap up to 256 is what made every
        # size but one look soft -- visibly so on a Retina title bar.
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter)
        painter.end()
        icon.addPixmap(pixmap)
    _cached = icon
    return icon


def apply_icon(widget) -> None:
    """Give a window the plugin's icon, if there is one to give."""
    icon = plugin_icon()
    if not icon.isNull():
        widget.setWindowIcon(icon)


__all__ = ["apply_icon", "plugin_icon"]
