"""Progress and thumbnail buttons on a Windows task bar button.

``ITaskbarList3`` by hand, through ctypes: Qt 6 dropped QtWinExtras, and
comtypes or pywin32 would be a dependency the plugin installer cannot pull in.
Only the handful of methods used here are bound, each by its vtable slot.

Everything is a no-op off Windows and every failure is swallowed: a task bar
that cannot show progress loses nothing the status bar does not already say.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import uuid
from ctypes import wintypes
from typing import Callable, Dict, List, Optional, Sequence, Tuple

AVAILABLE = sys.platform == "win32"

#: ``TBPFLAG`` values, keyed by the names :mod:`job_manager.presence` uses.
PROGRESS_FLAGS = {"none": 0, "indeterminate": 1, "normal": 2, "error": 4, "paused": 8}

WM_COMMAND = 0x0111
THBN_CLICKED = 0x1800
_THB_ICON = 0x2
_THB_TOOLTIP = 0x4
_THB_FLAGS = 0x8
_THBF_ENABLED = 0x0

_CLSID_TASKBAR_LIST = "56FDF344-FD6D-11d0-958A-006097C9A090"
_IID_ITASKBAR_LIST3 = "ea1afb91-9e28-4b86-90e9-9e9f8a5eefaf"

# vtable slots: IUnknown (0-2), ITaskbarList (3-7), ITaskbarList2 (8), then
# ITaskbarList3 in declaration order.
_HR_INIT = 3
_SET_PROGRESS_VALUE = 9
_SET_PROGRESS_STATE = 10
_THUMB_BAR_ADD_BUTTONS = 15
_THUMB_BAR_UPDATE_BUTTONS = 16


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> _GUID:
    return _GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


class THUMBBUTTON(ctypes.Structure):
    # Fixed widths rather than wintypes: off Windows a DWORD there is 8 bytes
    # and a WCHAR 4, and the layout test would be checking a struct no Windows
    # ever reads.
    _fields_ = [
        ("dwMask", ctypes.c_uint32),
        ("iId", ctypes.c_uint32),
        ("iBitmap", ctypes.c_uint32),
        ("hIcon", ctypes.c_void_p),
        ("szTip", ctypes.c_uint16 * 260),
        ("dwFlags", ctypes.c_uint32),
    ]


def _set_tip(button: THUMBBUTTON, tip: str) -> None:
    encoded = tip.encode("utf-16-le")[: 259 * 2]
    units = [int.from_bytes(encoded[i : i + 2], "little") for i in range(0, len(encoded), 2)]
    for index, unit in enumerate(units):
        button.szTip[index] = unit
    button.szTip[len(units)] = 0


#: None until first asked, False once creating it has failed.
_taskbar = None


def _method(obj: ctypes.c_void_p, slot: int, *argtypes):
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0]
    address = ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[slot]
    # c_long, not HRESULT: ctypes raises on a failing HRESULT, and a refused
    # call here is an ordinary outcome (no task bar button yet, Explorer gone).
    prototype = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
    return prototype(address)


def _instance() -> Optional[ctypes.c_void_p]:
    global _taskbar
    if _taskbar is None:
        _taskbar = False
        if not AVAILABLE:
            return None
        try:
            ole32 = ctypes.windll.ole32
            # Qt has already initialised COM on the GUI thread; this is a
            # harmless S_FALSE then, and the one thing that makes it work if not.
            ole32.CoInitializeEx(None, 0x2)
            obj = ctypes.c_void_p()
            clsid, iid = _guid(_CLSID_TASKBAR_LIST), _guid(_IID_ITASKBAR_LIST3)
            hr = ole32.CoCreateInstance(
                ctypes.byref(clsid), None, 0x1, ctypes.byref(iid), ctypes.byref(obj)
            )
            if hr != 0 or not obj.value:
                logging.debug("Job Manager: no ITaskbarList3 (0x%08x)", hr & 0xFFFFFFFF)
                return None
            if _method(obj, _HR_INIT)(obj) != 0:
                return None
            _taskbar = obj
        except Exception:
            logging.debug("Job Manager: the task bar could not be reached", exc_info=True)
    return _taskbar or None


def set_progress(hwnd: int, state: str, value: int = 0, total: int = 0) -> bool:
    """Show ``state`` (a :data:`PROGRESS_FLAGS` key) on ``hwnd``'s button."""
    obj = _instance()
    if obj is None or not hwnd:
        return False
    flag = PROGRESS_FLAGS.get(state, 0)
    try:
        if flag in (2, 4, 8):
            # Before the state: setting a value switches a bar that has no
            # progress, or an indeterminate one, back to NORMAL.
            set_value = _method(
                obj, _SET_PROGRESS_VALUE, wintypes.HWND, ctypes.c_ulonglong, ctypes.c_ulonglong
            )
            set_value(obj, hwnd, max(0, int(value)), max(1, int(total)))
        set_state = _method(obj, _SET_PROGRESS_STATE, wintypes.HWND, ctypes.c_int)
        return set_state(obj, hwnd, flag) == 0
    except Exception:
        logging.debug("Job Manager: task bar progress was refused", exc_info=True)
        return False


def _buttons(buttons: Sequence[Tuple[int, int, str]]):
    array = (THUMBBUTTON * len(buttons))()
    for slot, (button_id, hicon, tip) in zip(array, buttons):
        slot.dwMask = _THB_ICON | _THB_TOOLTIP | _THB_FLAGS
        slot.iId = button_id
        slot.hIcon = hicon or None
        _set_tip(slot, tip)
        slot.dwFlags = _THBF_ENABLED
    return array


def add_thumb_buttons(hwnd: int, buttons: Sequence[Tuple[int, int, str]], update=False) -> bool:
    """Put ``(id, hicon, tooltip)`` buttons under the window's thumbnail.

    Windows accepts the add once per window; later changes go through
    ``update``. Both need the task bar button to exist already, which is what
    the ``TaskbarButtonCreated`` message announces.
    """
    obj = _instance()
    if obj is None or not hwnd or not buttons:
        return False
    slot = _THUMB_BAR_UPDATE_BUTTONS if update else _THUMB_BAR_ADD_BUTTONS
    try:
        array = _buttons(buttons)
        call = _method(obj, slot, wintypes.HWND, wintypes.UINT, ctypes.POINTER(THUMBBUTTON))
        return call(obj, hwnd, len(buttons), array) == 0
    except Exception:
        logging.debug("Job Manager: thumbnail buttons were refused", exc_info=True)
        return False


_button_created: Optional[int] = None


def button_created_message() -> int:
    """The id of the registered ``TaskbarButtonCreated`` message, 0 off Windows."""
    global _button_created
    if _button_created is None:
        _button_created = 0
        if AVAILABLE:
            try:
                _button_created = int(
                    ctypes.windll.user32.RegisterWindowMessageW("TaskbarButtonCreated")
                )
            except Exception:
                logging.debug("Job Manager: no TaskbarButtonCreated", exc_info=True)
    return _button_created


def thumb_click_id(message: int, wparam: int) -> Optional[int]:
    """The thumbnail button a ``WM_COMMAND`` reports as clicked, if it is one."""
    if message != WM_COMMAND or (wparam >> 16) & 0xFFFF != THBN_CLICKED:
        return None
    return wparam & 0xFFFF


def hicon_from_png(data: bytes, size: int) -> int:
    """An HICON from PNG bytes, as Vista+ reads PNG-compressed icon images."""
    if not AVAILABLE or not data:
        return 0
    try:
        user32 = ctypes.windll.user32
        user32.CreateIconFromResourceEx.restype = wintypes.HICON
        user32.CreateIconFromResourceEx.argtypes = [
            ctypes.c_char_p,
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        return int(
            user32.CreateIconFromResourceEx(data, len(data), True, 0x00030000, size, size, 0) or 0
        )
    except Exception:
        logging.debug("Job Manager: the thumbnail icon was not made", exc_info=True)
        return 0


def destroy_icon(hicon: int) -> None:
    """Give an HICON back. Each is a GDI handle, and a process has 10,000."""
    if not AVAILABLE or not hicon:
        return
    try:
        ctypes.windll.user32.DestroyIcon(ctypes.c_void_p(int(hicon)))
    except Exception:
        logging.debug("Job Manager: an icon was not destroyed", exc_info=True)


class WindowTaskbar:
    """One window's task bar button: its progress and its thumbnail buttons.

    Remembers what it was last asked to show, because the button may not exist
    yet when asked (a window is told its progress before it is first shown)
    and Explorer restarting throws every button away. Both are announced with
    ``TaskbarButtonCreated``, and :meth:`handle` re-applies on it.
    """

    def __init__(self, widget, buttons: Sequence[Tuple[int, object, str, Callable]] = ()) -> None:
        self.widget = widget
        #: (id, QIcon, tooltip, callback)
        self.buttons = list(buttons)
        self._progress: Tuple[str, int, int] = ("none", 0, 0)
        self._icons: Dict[int, int] = {}

    def hwnd(self) -> int:
        try:
            return int(self.widget.winId())
        except Exception:
            return 0

    def set_progress(self, state: str, value: int = 0, total: int = 0) -> None:
        self._progress = (state, value, total)
        set_progress(self.hwnd(), state, value, total)

    def clear(self) -> None:
        self.set_progress("none")

    def release(self) -> None:
        """Clear the button and destroy the thumbnail icons made for it.

        Every monitor window made three, and nothing gave them back: a
        session that opened and closed the monitor all day leaked GDI handles
        until it hit the per-process limit.
        """
        self.clear()
        for hicon in self._icons.values():
            destroy_icon(hicon)
        self._icons.clear()

    def _native_buttons(self) -> List[Tuple[int, int, str]]:
        out = []
        for button_id, icon, tip, _callback in self.buttons:
            if button_id not in self._icons:
                self._icons[button_id] = _hicon_for(icon)
            out.append((button_id, self._icons[button_id], tip))
        return out

    def handle(self, message_ptr) -> bool:
        """Feed a native ``MSG``; True when it was a thumbnail click handled here."""
        if not AVAILABLE or not message_ptr:
            return False
        try:
            msg = wintypes.MSG.from_address(int(message_ptr))
        except Exception:
            return False
        created = button_created_message()
        if created and msg.message == created:
            add_thumb_buttons(self.hwnd(), self._native_buttons())
            set_progress(self.hwnd(), *self._progress)
            return False
        button_id = thumb_click_id(msg.message, int(msg.wParam or 0))
        if button_id is None:
            return False
        for candidate, _icon, _tip, callback in self.buttons:
            if candidate == button_id:
                callback()
                return True
        return False


def _small_icon_size() -> int:
    """SM_CXSMICON: what a thumbnail button is drawn at, already DPI-scaled."""
    try:
        return int(ctypes.windll.user32.GetSystemMetrics(49)) or 16
    except Exception:
        return 16


def _hicon_for(icon, size: int = 0) -> int:
    from PyQt6.QtCore import QBuffer, QIODevice

    size = size or _small_icon_size()

    pixmap = icon.pixmap(size, size)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buffer, "PNG")
    return hicon_from_png(bytes(buffer.data()), size)


__all__ = [
    "AVAILABLE",
    "PROGRESS_FLAGS",
    "WindowTaskbar",
    "add_thumb_buttons",
    "button_created_message",
    "destroy_icon",
    "hicon_from_png",
    "set_progress",
    "thumb_click_id",
]
