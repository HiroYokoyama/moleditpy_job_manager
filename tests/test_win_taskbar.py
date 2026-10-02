"""The Windows task bar wrapper: what can be checked without Explorer.

The COM calls themselves only mean anything on a Windows desktop; what is
held here is that the struct matches the C layout, that a click is told apart
from every other WM_COMMAND, and that nothing raises where there is no task
bar at all -- which is every other platform, and Windows CI's offscreen Qt.
"""

from __future__ import annotations

import ctypes
import sys
import unittest
from unittest.mock import MagicMock, patch

from job_manager import win_taskbar


class TestClicks(unittest.TestCase):
    def test_a_thumbnail_click_names_its_button(self):
        wparam = (win_taskbar.THBN_CLICKED << 16) | 2
        self.assertEqual(win_taskbar.thumb_click_id(win_taskbar.WM_COMMAND, wparam), 2)

    def test_a_menu_command_is_not_a_click(self):
        # WM_COMMAND also carries menu and accelerator ids, with 0 or 1 above.
        self.assertIsNone(win_taskbar.thumb_click_id(win_taskbar.WM_COMMAND, 2))
        self.assertIsNone(win_taskbar.thumb_click_id(win_taskbar.WM_COMMAND, (1 << 16) | 2))

    def test_another_message_is_not_a_click(self):
        wparam = (win_taskbar.THBN_CLICKED << 16) | 2
        self.assertIsNone(win_taskbar.thumb_click_id(0x0112, wparam))


class TestLayout(unittest.TestCase):
    def test_thumbbutton_matches_the_c_struct(self):
        # 4 + 4 + 4 (+4 padding on 64-bit) + HICON + 260 WCHARs + 4, padded to
        # the pointer size. A wrong size makes Windows read the tooltip of the
        # next button out of the middle of this one.
        pointer = ctypes.sizeof(ctypes.c_void_p)
        expected = 12 + (pointer - 12 % pointer) % pointer + pointer + 520 + 4
        expected += (pointer - expected % pointer) % pointer
        self.assertEqual(ctypes.sizeof(win_taskbar.THUMBBUTTON), expected)

    def test_buttons_carry_id_icon_and_tooltip(self):
        array = win_taskbar._buttons([(1, 0x1234, "Refresh now"), (2, 0, "x" * 400)])
        self.assertEqual((array[0].iId, array[0].hIcon), (1, 0x1234))
        tip = bytes(array[0].szTip).decode("utf-16-le").split("\x00")[0]
        self.assertEqual(tip, "Refresh now")
        # Cut to fit, and still terminated: 260 units including the NUL.
        long_tip = bytes(array[1].szTip).decode("utf-16-le")
        self.assertEqual(long_tip.index("\x00"), 259)

    def test_a_tooltip_outside_the_bmp_survives(self):
        array = win_taskbar._buttons([(1, 0, "job \U0001f9ea")])
        tip = bytes(array[0].szTip).decode("utf-16-le").split("\x00")[0]
        self.assertEqual(tip, "job \U0001f9ea")

    def test_guids_round_trip(self):
        guid = win_taskbar._guid(win_taskbar._IID_ITASKBAR_LIST3)
        self.assertEqual(guid.Data1, 0xEA1AFB91)
        self.assertEqual(guid.Data2, 0x9E28)

    def test_every_progress_state_has_a_flag(self):
        for state in ("none", "indeterminate", "normal", "error", "paused"):
            self.assertIn(state, win_taskbar.PROGRESS_FLAGS)


@unittest.skipIf(sys.platform == "win32", "checks the no-op off Windows")
class TestOffWindows(unittest.TestCase):
    def test_nothing_is_attempted(self):
        self.assertFalse(win_taskbar.AVAILABLE)
        self.assertFalse(win_taskbar.set_progress(1234, "normal", 1, 2))
        self.assertFalse(win_taskbar.add_thumb_buttons(1234, [(1, 0, "x")]))
        self.assertEqual(win_taskbar.button_created_message(), 0)
        self.assertEqual(win_taskbar.hicon_from_png(b"png", 16), 0)

    def test_a_window_wrapper_is_inert(self):
        widget = MagicMock()
        widget.winId.return_value = 1234
        callback = MagicMock()
        bar = win_taskbar.WindowTaskbar(widget, [(1, None, "Refresh", callback)])
        bar.set_progress("normal", 1, 2)
        bar.clear()
        self.assertFalse(bar.handle(0))
        callback.assert_not_called()


class TestWindowWrapper(unittest.TestCase):
    """The dispatch, with the platform check and the MSG read stood in for."""

    def test_release_destroys_every_icon_it_made(self):
        # Three per monitor window, every time one opened: a GDI handle leak.
        bar = win_taskbar.WindowTaskbar(MagicMock())
        bar._icons = {1: 11, 2: 22, 3: 33}
        with (
            patch.object(win_taskbar, "destroy_icon") as destroy,
            patch.object(win_taskbar, "set_progress"),
        ):
            bar.release()
        self.assertEqual(sorted(c.args[0] for c in destroy.call_args_list), [11, 22, 33])
        self.assertEqual(bar._icons, {})

    def test_destroying_nothing_is_nothing(self):
        win_taskbar.destroy_icon(0)

    def test_a_null_message_is_never_read(self):
        # Reading a MSG at address 0 is an access violation, not an exception.
        bar = win_taskbar.WindowTaskbar(MagicMock())
        with (
            patch.object(win_taskbar, "AVAILABLE", True),
            patch.object(win_taskbar.wintypes.MSG, "from_address") as read,
        ):
            self.assertFalse(bar.handle(0))
            self.assertFalse(bar.handle(None))
        read.assert_not_called()

    def _msg(self, message, wparam):
        msg = MagicMock()
        msg.message = message
        msg.wParam = wparam
        return msg

    def test_a_click_runs_the_buttons_callback(self):
        callback = MagicMock()
        bar = win_taskbar.WindowTaskbar(MagicMock(), [(3, None, "Hosts", callback)])
        msg = self._msg(win_taskbar.WM_COMMAND, (win_taskbar.THBN_CLICKED << 16) | 3)
        with (
            patch.object(win_taskbar, "AVAILABLE", True),
            patch.object(win_taskbar.wintypes.MSG, "from_address", return_value=msg),
            patch.object(win_taskbar, "button_created_message", return_value=0xC123),
        ):
            self.assertTrue(bar.handle(1))
        callback.assert_called_once_with()

    def test_the_button_appearing_reapplies_progress_and_buttons(self):
        # The window is told its progress before it has a button; without the
        # replay the first state it ever shows would be lost.
        widget = MagicMock()
        widget.winId.return_value = 77
        bar = win_taskbar.WindowTaskbar(widget, [(1, None, "Refresh", MagicMock())])
        bar.set_progress("normal", 2, 5)
        msg = self._msg(0xC123, 0)
        with (
            patch.object(win_taskbar, "AVAILABLE", True),
            patch.object(win_taskbar.wintypes.MSG, "from_address", return_value=msg),
            patch.object(win_taskbar, "button_created_message", return_value=0xC123),
            patch.object(win_taskbar, "_hicon_for", return_value=0),
            patch.object(win_taskbar, "add_thumb_buttons") as add,
            patch.object(win_taskbar, "set_progress") as progress,
        ):
            self.assertFalse(bar.handle(1))
        add.assert_called_once()
        progress.assert_called_once_with(77, "normal", 2, 5)


@unittest.skipUnless(sys.platform == "win32", "needs the real ITaskbarList3")
class TestOnWindows(unittest.TestCase):
    def test_calls_on_a_window_that_is_not_there_fail_quietly(self):
        # Proves the vtable slots by calling through them: a wrong slot would
        # crash the process here rather than return an HRESULT.
        self.assertFalse(win_taskbar.set_progress(0, "normal", 1, 2))
        win_taskbar.set_progress(0x7FFF0, "indeterminate")
        win_taskbar.set_progress(0x7FFF0, "none")
        self.assertNotEqual(win_taskbar.button_created_message(), 0)


if __name__ == "__main__":
    unittest.main()
