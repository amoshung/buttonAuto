import ctypes
import ctypes.wintypes
import time
from typing import Tuple

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
VK_F8 = 0x77


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def get_cursor_pos() -> Tuple[int, int]:
    pt = ctypes.wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def set_cursor_pos(x: int, y: int) -> None:
    ctypes.windll.user32.SetCursorPos(int(x), int(y))


def _send_mouse_event(flags: int, dx: int = 0, dy: int = 0, data: int = 0, extra: int = 0) -> None:
    ctypes.windll.user32.mouse_event(flags, dx, dy, data, extra)


class ClickController:
    def __init__(self) -> None:
        self._last_click_time: float = 0.0
        self._last_click_pos: Tuple[int, int] = (-9999, -9999)
        self._f8_was_down: bool = False

    def click_and_restore(self, x: int, y: int, cooldown_sec: float = 0.8) -> bool:
        now = time.monotonic()
        lx, ly = self._last_click_pos
        if (now - self._last_click_time) < cooldown_sec and abs(x - lx) < 25 and abs(y - ly) < 25:
            return False

        orig_x, orig_y = get_cursor_pos()
        set_cursor_pos(x, y)
        time.sleep(0.006)
        _send_mouse_event(MOUSEEVENTF_LEFTDOWN)
        time.sleep(0.006)
        _send_mouse_event(MOUSEEVENTF_LEFTUP)
        time.sleep(0.006)
        set_cursor_pos(orig_x, orig_y)

        self._last_click_time = time.monotonic()
        self._last_click_pos = (x, y)
        return True

    def poll_f8_toggled(self) -> bool:
        try:
            state = ctypes.windll.user32.GetAsyncKeyState(VK_F8)
            is_down = bool(state & 0x8000)
            toggled = is_down and not self._f8_was_down
            self._f8_was_down = is_down
            return toggled
        except Exception:
            return False

