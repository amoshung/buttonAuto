import os
import traceback
import tkinter as tk
from tkinter import messagebox
from clicker import enable_dpi_awareness
from overlay_ui import FloatingOverlayApp

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "error.log")


def main() -> None:
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
            messagebox.showerror("按鈕精靈啟動錯誤", f"發生未預期的錯誤：\n{err}")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
