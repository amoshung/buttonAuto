import ctypes
import tkinter as tk
from typing import List, Dict, Any, Optional, Callable, Tuple
import cv2
import mss
import numpy as np
from PIL import Image, ImageTk

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008


def normalize_box(x1: int, y1: int, x2: int, y2: int, min_size: int = 12) -> Optional[Dict[str, Any]]:
    left = min(int(x1), int(x2))
    top = min(int(y1), int(y2))
    w = abs(int(x2) - int(x1))
    h = abs(int(y2) - int(y1))
    if w < min_size or h < min_size:
        return None
    return {"x": left, "y": top, "w": w, "h": h, "enabled": True}


class MultiBoxSelector:
    """
    Fullscreen frozen-screen overlay that allows dragging multiple bounding boxes.
    - Left mouse drag: draw a box (can draw multiple boxes in a row)
    - Backspace / Delete: remove the last drawn box
    - Enter / Space / Right-click: confirm and return all drawn boxes + cropped BGR images
    - Escape: cancel
    """

    def __init__(
        self,
        parent: tk.Misc,
        mode_title: str,
        box_color: str = "#00FF66",
        on_complete: Optional[Callable[[List[Dict[str, Any]], List[np.ndarray]], None]] = None,
    ) -> None:
        self.parent = parent
        self.mode_title = mode_title
        self.box_color = box_color
        self.on_complete = on_complete

        self.boxes: List[Dict[str, Any]] = []
        self.rect_ids: List[Tuple[int, int]] = []
        self._start_x = 0
        self._start_y = 0
        self._cur_rect_id: Optional[int] = None

        with mss.mss() as sct:
            self.mon = sct.monitors[0]
            raw = np.array(sct.grab(self.mon))
            self.screen_bgr = cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)
            screen_rgb = cv2.cvtColor(raw, cv2.COLOR_BGRA2RGB)
            darkened = (screen_rgb.astype(np.float32) * 0.65).astype(np.uint8)
            self.pil_bg = Image.fromarray(darkened)

        self.top = tk.Toplevel(parent)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        geometry_str = f"{self.mon['width']}x{self.mon['height']}+{self.mon['left']}+{self.mon['top']}"
        self.top.geometry(geometry_str)
        self.top.configure(cursor="crosshair")

        self.tk_bg = ImageTk.PhotoImage(self.pil_bg)
        self.canvas = tk.Canvas(
            self.top,
            width=self.mon["width"],
            height=self.mon["height"],
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_image(0, 0, anchor="nw", image=self.tk_bg)

        banner_text = (
            f"【{self.mode_title}】左鍵拖曳可連續框選多個區域  |  "
            f"Enter 或 滑鼠右鍵：完成確認  |  Backspace：復原上一個框  |  Esc：取消"
        )
        self.banner_bg = self.canvas.create_rectangle(
            20, 20, 920, 64, fill="#111827", outline=self.box_color, width=2
        )
        self.banner_label = self.canvas.create_text(
            36,
            42,
            anchor="w",
            text=banner_text,
            fill="#FFFFFF",
            font=("Microsoft JhengHei UI", 11, "bold"),
        )

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<ButtonPress-3>", self._on_confirm)
        self.top.bind("<Return>", self._on_confirm)
        self.top.bind("<space>", self._on_confirm)
        self.top.bind("<BackSpace>", self._on_undo)
        self.top.bind("<Delete>", self._on_undo)
        self.top.bind("<Escape>", self._on_cancel)
        self.top.focus_force()

    def _on_press(self, event: tk.Event) -> None:
        self._start_x = event.x
        self._start_y = event.y
        self._cur_rect_id = self.canvas.create_rectangle(
            self._start_x,
            self._start_y,
            self._start_x,
            self._start_y,
            outline=self.box_color,
            width=2,
            dash=(4, 2),
        )

    def _on_drag(self, event: tk.Event) -> None:
        if self._cur_rect_id is not None:
            self.canvas.coords(self._cur_rect_id, self._start_x, self._start_y, event.x, event.y)

    def _on_release(self, event: tk.Event) -> None:
        if self._cur_rect_id is not None:
            self.canvas.delete(self._cur_rect_id)
            self._cur_rect_id = None

        box = normalize_box(self._start_x, self._start_y, event.x, event.y, min_size=12)
        if box is None:
            return

        abs_box = {
            "x": box["x"] + self.mon["left"],
            "y": box["y"] + self.mon["top"],
            "w": box["w"],
            "h": box["h"],
            "_canvas_x": box["x"],
            "_canvas_y": box["y"],
            "enabled": True,
        }
        self.boxes.append(abs_box)
        idx = len(self.boxes)
        rid = self.canvas.create_rectangle(
            box["x"],
            box["y"],
            box["x"] + box["w"],
            box["y"] + box["h"],
            outline=self.box_color,
            width=3,
        )
        tid = self.canvas.create_text(
            box["x"] + 6,
            max(12, box["y"] - 10),
            anchor="w",
            text=f"#{idx} ({box['w']}x{box['h']})",
            fill=self.box_color,
            font=("Microsoft JhengHei UI", 10, "bold"),
        )
        self.rect_ids.append((rid, tid))

    def _on_undo(self, _event: Optional[tk.Event] = None) -> None:
        if self.boxes and self.rect_ids:
            self.boxes.pop()
            rid, tid = self.rect_ids.pop()
            self.canvas.delete(rid)
            self.canvas.delete(tid)

    def _on_confirm(self, _event: Optional[tk.Event] = None) -> None:
        clean_boxes: List[Dict[str, Any]] = []
        crops: List[np.ndarray] = []
        for b in self.boxes:
            cx, cy, cw, ch = b["_canvas_x"], b["_canvas_y"], b["w"], b["h"]
            crop = self.screen_bgr[cy : cy + ch, cx : cx + cw].copy()
            crops.append(crop)
            clean_boxes.append({"x": b["x"], "y": b["y"], "w": cw, "h": ch, "enabled": True})

        self.top.destroy()
        if self.on_complete is not None:
            self.on_complete(clean_boxes, crops)

    def _on_cancel(self, _event: Optional[tk.Event] = None) -> None:
        self.top.destroy()
        if self.on_complete is not None:
            self.on_complete([], [])


class RegionBorderOverlayManager:
    """
    Renders click-through green outline rectangles around active scan regions on Windows.
    """

    def __init__(self, root: tk.Misc) -> None:
        self.root = root
        self._windows: List[tk.Toplevel] = []

    def clear(self) -> None:
        for w in self._windows:
            try:
                w.destroy()
            except Exception:
                pass
        self._windows.clear()

    def update_borders(self, regions: List[Dict[str, Any]], visible: bool) -> None:
        self.clear()
        if not visible:
            return

        active = [r for r in regions if r.get("enabled", True) and r.get("w", 0) > 10 and r.get("h", 0) > 10]
        transparent_key = "#010203"

        for idx, reg in enumerate(active):
            x, y, w, h = int(reg["x"]), int(reg["y"]), int(reg["w"]), int(reg["h"])
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.configure(bg=transparent_key)
            win.attributes("-transparentcolor", transparent_key)
            win.geometry(f"{w}x{h}+{x}+{y}")

            canvas = tk.Canvas(win, width=w, height=h, bg=transparent_key, highlightthickness=0, bd=0)
            canvas.pack(fill="both", expand=True)
            canvas.create_rectangle(1, 1, w - 2, h - 2, outline="#10B981", width=2)
            canvas.create_rectangle(2, 2, 76, 20, fill="#10B981", outline="#10B981")
            canvas.create_text(
                8, 11, anchor="w", text=f"掃描區 #{idx + 1}", fill="#000000", font=("Microsoft JhengHei UI", 8, "bold")
            )

            win.update_idletasks()
            self._make_click_through(win)
            self._windows.append(win)

    @staticmethod
    def _make_click_through(win: tk.Toplevel) -> None:
        try:
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
            if not hwnd:
                hwnd = win.winfo_id()
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            new_style = style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_TOPMOST
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new_style)
        except Exception:
            pass

