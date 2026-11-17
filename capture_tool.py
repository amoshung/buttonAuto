"""框選工具（給網頁主控台的「基本按鈕精靈」用）：凍結全螢幕，拉框選出目標按鈕或掃描區域。

由 server.py 以子行程啟動，結果寫成 JSON：
  python capture_tool.py --kind targets --out result.json   → {"targets": [{"path": "targets/custom_…png"}]}
  python capture_tool.py --kind regions --out result.json   → {"boxes": [{"left":…, "top":…, …}]}
"""
import argparse
import json
import os
import time
import tkinter as tk

from clicker import enable_dpi_awareness
from region_selector import MultiBoxSelector
from scanner import write_image_unicode

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=("targets", "regions"), required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    enable_dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    result = {"kind": args.kind, "boxes": [], "targets": []}

    def done(boxes, crops) -> None:
        if args.kind == "regions":
            result["boxes"] = list(boxes or [])
        else:
            ts = int(time.time())
            for i, crop in enumerate(crops or []):
                rel = f"targets/custom_{ts}_{i + 1}.png"
                if write_image_unicode(os.path.join(BASE_DIR, rel), crop):
                    result["targets"].append({"path": rel})
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        root.after(50, root.destroy)

    if args.kind == "regions":
        MultiBoxSelector(root, mode_title="新增掃描區域框（可連續拉多個框；Enter 確認、Esc 取消）",
                         box_color="#10B981", on_complete=done)
    else:
        MultiBoxSelector(root, mode_title="框選要自動點擊的目標按鈕（可一次框多個；Enter 確認、Esc 取消）",
                         box_color="#F59E0B", on_complete=done)
    root.mainloop()


if __name__ == "__main__":
    main()
