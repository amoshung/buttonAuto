import os
import traceback
import tkinter as tk
from tkinter import messagebox
from clicker import enable_dpi_awareness
from overlay_ui import FloatingOverlayApp

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "error.log")


def _single_instance() -> object:
    """系統互斥鎖：已經有一個 AI 工程助理在執行時回傳 None。"""
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.restype = ctypes.c_void_p
    handle = k32.CreateMutexW(None, False, "Local\\AIEngineeringAssistant")
    if not handle or k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


def main() -> None:
    lock = _single_instance()
    if lock is None:
        r = tk.Tk()
        r.withdraw()
        messagebox.showinfo("AI 工程助理", "AI 工程助理已經在執行中（只能開一個，避免同一個視窗被操作兩次）。")
        r.destroy()
        return
    try:
        enable_dpi_awareness()
        root = tk.Tk()
        FloatingOverlayApp(root)
        root.mainloop()
    except Exception:
        err = traceback.format_exc()
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(err + "\n")
        except Exception:
            pass
        try:
            messagebox.showerror("AI 工程助理啟動錯誤", f"發生未預期的錯誤：\n{err}")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
