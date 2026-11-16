"""Win32 輔助函式：視窗列舉、背景截圖、閒置偵測、鍵盤輸入、焦點切換。"""
import ctypes
import ctypes.wintypes as W
import io
import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

VP = ctypes.c_void_p
user32.GetForegroundWindow.restype = VP
user32.GetWindowDC.restype = VP
user32.GetWindowDC.argtypes = [VP]
user32.ReleaseDC.argtypes = [VP, VP]
user32.PrintWindow.argtypes = [VP, VP, ctypes.c_uint]
user32.GetWindowRect.argtypes = [VP, ctypes.POINTER(W.RECT)]
user32.IsIconic.argtypes = [VP]
user32.IsWindowVisible.argtypes = [VP]
user32.IsWindow.argtypes = [VP]
user32.GetWindowTextW.argtypes = [VP, ctypes.c_wchar_p, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [VP, ctypes.POINTER(W.DWORD)]
user32.SetForegroundWindow.argtypes = [VP]
user32.BringWindowToTop.argtypes = [VP]
user32.FlashWindowEx.argtypes = [VP]
gdi32.CreateCompatibleDC.restype = VP
gdi32.CreateCompatibleDC.argtypes = [VP]
gdi32.CreateCompatibleBitmap.restype = VP
gdi32.CreateCompatibleBitmap.argtypes = [VP, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.restype = VP
gdi32.SelectObject.argtypes = [VP, VP]
gdi32.GetDIBits.argtypes = [VP, VP, ctypes.c_uint, ctypes.c_uint, VP, VP, ctypes.c_uint]
gdi32.DeleteObject.argtypes = [VP]
gdi32.DeleteDC.argtypes = [VP]
kernel32.OpenProcess.restype = VP
kernel32.CloseHandle.argtypes = [VP]
kernel32.QueryFullProcessImageNameW.argtypes = [VP, W.DWORD, ctypes.c_wchar_p, ctypes.POINTER(W.DWORD)]

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PW_RENDERFULLCONTENT = 0x2


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    exe: str
    iconic: bool
    pid: int = 0


def window_pid(hwnd: int) -> int:
    pid = W.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def window_title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def process_name(hwnd: int) -> str:
    pid = W.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(520)
        n = W.DWORD(520)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return buf.value.replace("/", "\\").split("\\")[-1]
        return ""
    finally:
        kernel32.CloseHandle(h)


def list_top_windows() -> List[WindowInfo]:
    result: List[WindowInfo] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, VP, VP)
    def _cb(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            title = window_title(hwnd)
            if title:
                result.append(WindowInfo(int(hwnd), title, process_name(hwnd), bool(user32.IsIconic(hwnd)), window_pid(hwnd)))
        return True

    user32.EnumWindows(_cb, 0)
    return result


def window_matches(match: str, title: str, exe: str) -> bool:
    m = (match or "").strip().lower()
    if not m:
        return False
    return m == exe.lower() or m in title.lower()


def is_window(hwnd: int) -> bool:
    return bool(user32.IsWindow(hwnd))


def is_iconic(hwnd: int) -> bool:
    return bool(user32.IsIconic(hwnd))


SW_SHOWNOACTIVATE = 4
HWND_BOTTOM = 1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
user32.ShowWindow.argtypes = [VP, ctypes.c_int]
user32.SetWindowPos.argtypes = [VP, VP, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]


def restore_behind(hwnd: int) -> bool:
    """把最小化的視窗還原到所有視窗的最後面，不啟用、不搶焦點。"""
    if not is_iconic(hwnd):
        return True
    fg = get_foreground()
    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    user32.SetWindowPos(hwnd, HWND_BOTTOM, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
    if fg and get_foreground() != fg and is_window(fg):
        activate_window(fg)  # 萬一系統還是把焦點給了它，立刻還回去
    return not is_iconic(hwnd)


def get_window_rect(hwnd: int) -> Tuple[int, int, int, int]:
    r = W.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG), ("biPlanes", W.WORD),
        ("biBitCount", W.WORD), ("biCompression", W.DWORD), ("biSizeImage", W.DWORD),
        ("biXPelsPerMeter", W.LONG), ("biYPelsPerMeter", W.LONG), ("biClrUsed", W.DWORD),
        ("biClrImportant", W.DWORD),
    ]


def capture_window(hwnd: int) -> Optional[np.ndarray]:
    """用 PrintWindow 擷取視窗畫面（被其他視窗蓋住也可以；最小化時回傳 None）。回傳 RGB 陣列。"""
    if is_iconic(hwnd):
        return None
    left, top, right, bottom = get_window_rect(hwnd)
    w, h = right - left, bottom - top
    if w <= 0 or h <= 0:
        return None
    hdc = user32.GetWindowDC(hwnd)
    if not hdc:
        return None
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    try:
        gdi32.SelectObject(mdc, bmp)
        if not user32.PrintWindow(hwnd, mdc, PW_RENDERFULLCONTENT):
            return None
        bi = _BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bi.biWidth, bi.biHeight, bi.biPlanes, bi.biBitCount = w, -h, 1, 32
        buf = ctypes.create_string_buffer(w * h * 4)
        if not gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0):
            return None
        bgra = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)
        return bgra[..., 2::-1].copy()
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(hwnd, hdc)


def to_png(rgb: np.ndarray, max_side: int = 1400) -> bytes:
    im = Image.fromarray(rgb)
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def thumb_fingerprint(rgb: np.ndarray) -> np.ndarray:
    return np.asarray(Image.fromarray(rgb).convert("L").resize((64, 64)), dtype=np.float32)


def thumb_changed(a: Optional[np.ndarray], b: Optional[np.ndarray], tol: float = 3.0) -> bool:
    if a is None or b is None:
        return True
    return float(np.abs(a - b).mean()) > tol


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", W.UINT), ("dwTime", W.DWORD)]


def idle_seconds() -> float:
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return ((kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


def get_foreground() -> int:
    return int(user32.GetForegroundWindow() or 0)


def activate_window(hwnd: int) -> bool:
    """把視窗切到前景（不動滑鼠）。用 AttachThreadInput 取得切換前景的權限。"""
    if get_foreground() == hwnd:
        return True
    fg = get_foreground()
    cur_tid = kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = False
    if fg_tid and fg_tid != cur_tid:
        attached = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(cur_tid, fg_tid, False)
    for _ in range(20):
        if get_foreground() == hwnd:
            return True
        time.sleep(0.02)
    return False


class _FLASHWINFO(ctypes.Structure):
    _fields_ = [("cbSize", W.UINT), ("hwnd", VP), ("dwFlags", W.DWORD), ("uCount", W.UINT), ("dwTimeout", W.DWORD)]


def flash_window(hwnd: int, count: int = 5) -> None:
    info = _FLASHWINFO(ctypes.sizeof(_FLASHWINFO), hwnd, 0x3 | 0xC, count, 0)  # FLASHW_ALL | FLASHW_TIMERNOFG
    user32.FlashWindowEx(ctypes.byref(info))


# ---- 使用者真實輸入偵測（低階鍵盤／滑鼠掛鉤，能分辨使用者輸入與程式模擬的輸入） ----
WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
WM_MOUSEMOVE = 0x0200
LLKHF_INJECTED, LLMHF_INJECTED = 0x10, 0x01
_LRESULT = ctypes.c_ssize_t
_HOOKPROC = ctypes.WINFUNCTYPE(_LRESULT, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t)
user32.SetWindowsHookExW.restype = VP
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC, VP, W.DWORD]
user32.CallNextHookEx.restype = _LRESULT
user32.CallNextHookEx.argtypes = [VP, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t]
user32.UnhookWindowsHookEx.argtypes = [VP]
kernel32.GetModuleHandleW.restype = VP
kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", W.DWORD), ("scanCode", W.DWORD), ("flags", W.DWORD), ("time", W.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", W.POINT), ("mouseData", W.DWORD), ("flags", W.DWORD), ("time", W.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class InputMonitor:
    """在獨立執行緒掛上低階鍵盤與滑鼠掛鉤，只記錄「使用者真實輸入」的時間（略過 SendInput 模擬的輸入）。

    掛鉤只記時間、立刻放行，不攔截、不修改任何輸入。掛不上時退回 GetLastInputInfo（無法分辨模擬輸入）。
    """

    _instance: Optional["InputMonitor"] = None

    def __init__(self) -> None:
        self.last_real = time.monotonic()
        self.ok = False
        self._ready = threading.Event()
        self._kb = _HOOKPROC(self._on_kb)
        self._ms = _HOOKPROC(self._on_ms)
        self._hooks: List[int] = []
        threading.Thread(target=self._run, daemon=True, name="InputMonitor").start()
        self._ready.wait(2.0)

    @classmethod
    def get(cls) -> "InputMonitor":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _on_kb(self, code, wparam, lparam):
        if code >= 0:
            info = ctypes.cast(lparam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
            if not info.flags & LLKHF_INJECTED:
                self.last_real = time.monotonic()
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _on_ms(self, code, wparam, lparam):
        if code >= 0 and wparam != WM_MOUSEMOVE:
            info = ctypes.cast(lparam, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
            if not info.flags & LLMHF_INJECTED:
                self.last_real = time.monotonic()
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _run(self) -> None:
        mod = kernel32.GetModuleHandleW(None)
        for kind, proc in ((WH_KEYBOARD_LL, self._kb), (WH_MOUSE_LL, self._ms)):
            h = user32.SetWindowsHookExW(kind, proc, mod, 0)
            if h:
                self._hooks.append(h)
        self.ok = len(self._hooks) == 2
        self._ready.set()
        msg = W.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def real_idle_seconds(self) -> float:
        if self.ok:
            return time.monotonic() - self.last_real
        return idle_seconds()

    def real_input_since(self, t: float) -> bool:
        """t（time.monotonic）之後，使用者是否動過鍵盤或滑鼠。"""
        if self.ok:
            return self.last_real > t
        return idle_seconds() < (time.monotonic() - t) - 0.05


def real_idle_seconds() -> float:
    return InputMonitor.get().real_idle_seconds()


# ---- 全螢幕偵測（簡報、影片、遊戲時暫停所有動作） ----
class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", W.DWORD), ("rcMonitor", W.RECT), ("rcWork", W.RECT), ("dwFlags", W.DWORD)]


user32.MonitorFromWindow.restype = VP
user32.MonitorFromWindow.argtypes = [VP, W.DWORD]
user32.GetMonitorInfoW.argtypes = [VP, ctypes.POINTER(_MONITORINFO)]
user32.GetClassNameW.argtypes = [VP, ctypes.c_wchar_p, ctypes.c_int]
user32.GetWindowLongW.argtypes = [VP, ctypes.c_int]
user32.GetShellWindow.restype = VP
user32.GetDesktopWindow.restype = VP


user32.MonitorFromPoint.restype = VP
user32.MonitorFromPoint.argtypes = [W.POINT, W.DWORD]


def work_area_at(x: int, y: int) -> Tuple[int, int, int, int]:
    """(x, y) 所在螢幕的工作區（扣掉工作列）。"""
    mon = user32.MonitorFromPoint(W.POINT(int(x), int(y)), 2)  # MONITOR_DEFAULTTONEAREST
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    if mon and user32.GetMonitorInfoW(mon, ctypes.byref(info)):
        r = info.rcWork
        return r.left, r.top, r.right, r.bottom
    return 0, 0, 1280, 720


def foreground_is_fullscreen() -> bool:
    fg = get_foreground()
    if not fg or fg in (int(user32.GetShellWindow() or 0), int(user32.GetDesktopWindow() or 0)):
        return False
    buf = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(fg, buf, 64)
    if buf.value in ("Progman", "WorkerW", "Shell_TrayWnd"):
        return False
    mon = user32.MonitorFromWindow(fg, 2)  # MONITOR_DEFAULTTONEAREST
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    if not mon or not user32.GetMonitorInfoW(mon, ctypes.byref(info)):
        return False
    # 最大化的一般視窗也會蓋滿螢幕，但有標題列；真正的全螢幕（簡報、影片、遊戲）沒有標題列
    style = user32.GetWindowLongW(fg, -16)  # GWL_STYLE
    if style & 0x00C00000 == 0x00C00000:    # WS_CAPTION
        return False
    l, t, r, b = get_window_rect(fg)
    m = info.rcMonitor
    return l <= m.left and t <= m.top and r >= m.right and b >= m.bottom


# ---- 剪貼簿（貼上文字可避開中文輸入法打亂標點） ----
CF_UNICODETEXT = 13
_TEXT_FORMATS = {1, 7, 13, 16}  # CF_TEXT、CF_OEMTEXT、CF_UNICODETEXT、CF_LOCALE
GMEM_MOVEABLE = 0x0002
user32.OpenClipboard.argtypes = [VP]
user32.GetClipboardData.restype = VP
user32.GetClipboardData.argtypes = [ctypes.c_uint]
user32.SetClipboardData.restype = VP
user32.SetClipboardData.argtypes = [ctypes.c_uint, VP]
user32.EnumClipboardFormats.argtypes = [ctypes.c_uint]
user32.EnumClipboardFormats.restype = ctypes.c_uint
kernel32.GlobalAlloc.restype = VP
kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
kernel32.GlobalLock.restype = VP
kernel32.GlobalLock.argtypes = [VP]
kernel32.GlobalUnlock.argtypes = [VP]


def _open_clipboard() -> bool:
    for _ in range(20):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.03)
    return False


# GDI 物件格式不是 HGLOBAL，不能直接複製；Windows 會從 CF_DIB / CF_DIBV5 自動重建 CF_BITMAP
_GDI_FORMATS = {2, 3, 9, 14, 0x80, 0x82, 0x83, 0x8E}
_SYNTH_FROM_DIB = {2}
CF_DIB, CF_DIBV5 = 8, 17
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalSize.argtypes = [VP]


class ClipboardSnapshot:
    """完整暫存剪貼簿（文字、HTML、圖片 DIB 等所有 HGLOBAL 格式），貼上後原樣還原。

    有無法保存的格式（例如只有 metafile）時 ok=False，呼叫端不應覆蓋剪貼簿。
    """

    def __init__(self) -> None:
        self.ok = False
        self.items: List[Tuple[int, bytes]] = []
        self.text: Optional[str] = None
        if not _open_clipboard():
            return
        try:
            fmts, f = [], 0
            while True:
                f = user32.EnumClipboardFormats(f)
                if not f:
                    break
                fmts.append(f)
            has_dib = CF_DIB in fmts or CF_DIBV5 in fmts
            for f in fmts:
                if f in _GDI_FORMATS:
                    if f in _SYNTH_FROM_DIB and has_dib:
                        continue
                    return  # 無法保存，ok 維持 False
                h = user32.GetClipboardData(f)
                if not h:
                    continue
                size = kernel32.GlobalSize(h)
                p = kernel32.GlobalLock(h) if size else None
                if not p:
                    continue
                try:
                    data = ctypes.string_at(p, size)
                finally:
                    kernel32.GlobalUnlock(h)
                self.items.append((f, data))
                if f == CF_UNICODETEXT:
                    self.text = data.decode("utf-16-le", errors="replace").split("\x00", 1)[0]
            self.ok = True
        finally:
            user32.CloseClipboard()

    def restore(self) -> bool:
        if not self.ok or not _open_clipboard():
            return False
        try:
            user32.EmptyClipboard()
            for f, data in self.items:
                h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
                p = kernel32.GlobalLock(h) if h else None
                if not p:
                    continue
                ctypes.memmove(p, data, len(data))
                kernel32.GlobalUnlock(h)
                user32.SetClipboardData(f, h)
            return True
        finally:
            user32.CloseClipboard()


# ---- 輸入法：逐字輸入時暫時關閉，避免全形標點被輸入法打亂順序 ----
WM_IME_CONTROL, IMC_GETOPENSTATUS, IMC_SETOPENSTATUS = 0x0283, 0x0005, 0x0006
_imm32 = ctypes.windll.imm32
_imm32.ImmGetDefaultIMEWnd.restype = VP
_imm32.ImmGetDefaultIMEWnd.argtypes = [VP]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.SendMessageW.argtypes = [VP, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]


class ImeOff:
    """with ImeOff(hwnd): 期間把目標視窗的輸入法切成關閉（英數），結束後還原原本狀態。"""

    def __init__(self, hwnd: int) -> None:
        self.ime = _imm32.ImmGetDefaultIMEWnd(hwnd)
        self.was_open = None

    def __enter__(self) -> "ImeOff":
        if self.ime:
            self.was_open = user32.SendMessageW(self.ime, WM_IME_CONTROL, IMC_GETOPENSTATUS, 0)
            if self.was_open:
                user32.SendMessageW(self.ime, WM_IME_CONTROL, IMC_SETOPENSTATUS, 0)
        return self

    def __exit__(self, *exc) -> None:
        if self.ime and self.was_open:
            user32.SendMessageW(self.ime, WM_IME_CONTROL, IMC_SETOPENSTATUS, 1)


def set_clipboard_text(text: str) -> bool:
    data = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(data)
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
    if not h:
        return False
    p = kernel32.GlobalLock(h)
    if not p:
        return False
    ctypes.memmove(p, data, size)
    kernel32.GlobalUnlock(h)
    if not _open_clipboard():
        return False
    try:
        user32.EmptyClipboard()
        return bool(user32.SetClipboardData(CF_UNICODETEXT, h))
    finally:
        user32.CloseClipboard()


# ---- 鍵盤輸入 (SendInput) ----
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_SHIFT, VK_CONTROL, VK_MENU, VK_RETURN = 0x10, 0x11, 0x12, 0x0D


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", W.WORD), ("wScan", W.WORD), ("dwFlags", W.DWORD), ("time", W.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", W.LONG), ("dy", W.LONG), ("mouseData", W.DWORD), ("dwFlags", W.DWORD), ("time", W.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", W.DWORD), ("u", _INPUTUNION)]


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> _INPUT:
    return _INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=_KEYBDINPUT(vk, scan, flags, 0, 0)))


def build_text_inputs(text: str, send_key: str = "enter") -> List[_INPUT]:
    """把文字轉成 SendInput 序列。換行用 Shift+Enter（不送出），最後按送出鍵。"""
    seq: List[_INPUT] = []
    for line_idx, line in enumerate(text.replace("\r\n", "\n").split("\n")):
        if line_idx > 0:
            seq += [_key(VK_SHIFT), _key(VK_RETURN), _key(VK_RETURN, flags=KEYEVENTF_KEYUP), _key(VK_SHIFT, flags=KEYEVENTF_KEYUP)]
        units = line.encode("utf-16-le")
        for i in range(0, len(units), 2):
            code = int.from_bytes(units[i:i + 2], "little")
            seq += [_key(scan=code, flags=KEYEVENTF_UNICODE), _key(scan=code, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
    if send_key == "ctrl+enter":
        seq += [_key(VK_CONTROL), _key(VK_RETURN), _key(VK_RETURN, flags=KEYEVENTF_KEYUP), _key(VK_CONTROL, flags=KEYEVENTF_KEYUP)]
    elif send_key == "enter":
        seq += [_key(VK_RETURN), _key(VK_RETURN, flags=KEYEVENTF_KEYUP)]
    return seq


VK_V, VK_A, VK_BACK = 0x56, 0x41, 0x08


def chord(vk_mod: int, vk: int) -> List[_INPUT]:
    return [_key(vk_mod), _key(vk), _key(vk, flags=KEYEVENTF_KEYUP), _key(vk_mod, flags=KEYEVENTF_KEYUP)]


def paste_inputs() -> List[_INPUT]:
    return chord(VK_CONTROL, VK_V)


VK_DELETE = 0x2E


def clear_focused_input() -> None:
    """Ctrl+A 後停一下再按 Delete（一次送出整串時，Chromium 有時只收到全選、沒刪除）。"""
    send_inputs(chord(VK_CONTROL, VK_A))
    time.sleep(0.15)
    send_inputs([_key(VK_DELETE), _key(VK_DELETE, flags=KEYEVENTF_KEYUP)])
    time.sleep(0.3)


def modifier_keys_down() -> bool:
    return any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in (VK_SHIFT, VK_CONTROL, VK_MENU, 0x5B, 0x5C))


def send_inputs(seq: List[_INPUT]) -> int:
    if not seq:
        return 0
    arr = (_INPUT * len(seq))(*seq)
    return int(user32.SendInput(len(seq), arr, ctypes.sizeof(_INPUT)))
