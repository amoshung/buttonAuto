"""UIA 偵察工具：把 Kiro / Antigravity 所有視窗的 UI Automation 結構與截圖存成檔案（唯讀）。

用途：設計「agent 模式視窗」的偵測、對話清單讀取、切換對話之前，先看清楚實際的 UIA 結構。

保證唯讀：只讀屬性與 Pattern 的「狀態」，絕不呼叫 Invoke／Select／SetValue／SetFocus／Toggle，
不送鍵盤滑鼠、不改視窗大小或位置；最小化的視窗只記錄、不還原。結束時核對前景視窗沒有被改變。

執行：tools\\recon.bat（或 python tools\\uia_probe.py）。結果在 recon\\<時間>\\，summary.md 是摘要。
"""
import datetime
import json
import os
import re
import sys
import time
import traceback

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import uiautomation as auto  # noqa: E402

import win_utils as wu  # noqa: E402

MAX_NODES = 9000          # 每個視窗最多記錄幾個節點
MAX_SECONDS = 150         # 每個視窗最多花幾秒
MAX_DEPTH = 60
APP_HINTS = ("kiro", "antigravity")

PATTERNS = [
    ("Invoke", "InvokePattern"), ("SelectionItem", "SelectionItemPattern"), ("Value", "ValuePattern"),
    ("Toggle", "TogglePattern"), ("ExpandCollapse", "ExpandCollapsePattern"), ("Scroll", "ScrollPattern"),
    ("Text", "TextPattern"), ("LegacyIAccessible", "LegacyIAccessiblePattern"),
]


def _rect(c):
    try:
        r = c.BoundingRectangle
        return [int(r.left), int(r.top), int(r.right), int(r.bottom)]
    except Exception:
        return [0, 0, 0, 0]


def _s(v, n=300):
    s = re.sub(r"\s+", " ", str(v or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def describe(c, depth):
    """一個節點的所有唯讀資訊。"""
    d = {"depth": depth}
    for key, attr in (("type", "ControlTypeName"), ("name", "Name"), ("class", "ClassName"),
                      ("aid", "AutomationId"), ("help", "HelpText"), ("status", "ItemStatus"),
                      ("ltype", "LocalizedControlType"), ("akey", "AccessKey")):
        try:
            v = getattr(c, attr)
            if v:
                d[key] = _s(v, 500 if key == "name" else 200)
        except Exception:
            pass
    d["rect"] = _rect(c)
    for key, attr in (("enabled", "IsEnabled"), ("offscreen", "IsOffscreen"), ("focusable", "IsKeyboardFocusable"),
                      ("focused", "HasKeyboardFocus")):
        try:
            d[key] = bool(getattr(c, attr))
        except Exception:
            pass
    pats = {}
    for short, pname in PATTERNS:
        try:
            p = c.GetPattern(getattr(auto.PatternId, pname))
        except Exception:
            p = None
        if not p:
            continue
        info = True
        try:
            if short == "SelectionItem":
                info = {"selected": bool(p.IsSelected)}
            elif short == "Value":
                info = {"readonly": bool(p.IsReadOnly), "value": _s(p.Value, 200)}
            elif short == "Toggle":
                info = {"state": int(p.ToggleState)}
            elif short == "ExpandCollapse":
                info = {"state": int(p.ExpandCollapseState)}
            elif short == "LegacyIAccessible":
                desc = _s(getattr(p, "Description", ""), 200)
                info = {"role": int(getattr(p, "Role", 0) or 0), "state": int(getattr(p, "State", 0) or 0)}
                if desc:
                    info["desc"] = desc
                val = _s(getattr(p, "Value", ""), 120)
                if val:
                    info["value"] = val
        except Exception:
            pass
        pats[short] = info
    if pats:
        d["patterns"] = pats
    return d


def walk(root):
    """深度優先走訪整棵 UIA 樹（有節點與時間上限）。"""
    out, stack, t0 = [], [(root, 0)], time.monotonic()
    truncated = ""
    while stack:
        if len(out) >= MAX_NODES:
            truncated = f"超過 {MAX_NODES} 個節點，後面沒有記錄"
            break
        if time.monotonic() - t0 > MAX_SECONDS:
            truncated = f"超過 {MAX_SECONDS} 秒，後面沒有記錄"
            break
        c, depth = stack.pop()
        try:
            out.append(describe(c, depth))
        except Exception as e:
            out.append({"depth": depth, "error": repr(e)})
            continue
        if depth >= MAX_DEPTH:
            continue
        try:
            kids = c.GetChildren()
        except Exception:
            kids = []
        for k in reversed(kids):
            stack.append((k, depth + 1))
    return out, truncated, time.monotonic() - t0


def tree_line(n):
    pats = n.get("patterns") or {}
    ptxt = ",".join(f"{k}{'' if v is True else json.dumps(v, ensure_ascii=False)}" for k, v in pats.items())
    flags = "".join(f for f, k in (("F", "focused"), ("k", "focusable"), ("x", "offscreen")) if n.get(k))
    if n.get("enabled") is False:
        flags += "d"
    bits = [n.get("type", "?")]
    if n.get("name"):
        bits.append(f'"{n["name"]}"')
    for k in ("class", "aid", "help", "status"):
        if n.get(k):
            bits.append(f"{k}={n[k]}")
    bits.append("rect=" + ",".join(map(str, n.get("rect", []))))
    if ptxt:
        bits.append("[" + ptxt + "]")
    if flags:
        bits.append("{" + flags + "}")
    return "  " * n.get("depth", 0) + " ".join(bits)


def wake_chromium(hwnd):
    """Chromium 只有在有人用 MSAA／UIA 查詢「網頁內容視窗」時才打開無障礙樹。
    對每個 Chrome_RenderWidgetHostHWND 子視窗做一次唯讀查詢（AccessibleObjectFromWindow），不送任何輸入。"""
    import ctypes
    from ctypes import wintypes as W
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, W.HWND, W.LPARAM)
    def cb(h, _l):
        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(h, buf, 256)
        if buf.value == "Chrome_RenderWidgetHostHWND":
            found.append(int(h))
        return True
    ctypes.windll.user32.EnumChildWindows(W.HWND(hwnd), cb, 0)

    class GUID(ctypes.Structure):
        _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]
    iid = GUID(0x618736E0, 0x3C3D, 0x11CF, (ctypes.c_ubyte * 8)(0x81, 0x0C, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71))
    for h in found:
        try:
            ptr = ctypes.c_void_p()
            ctypes.oledll.oleacc.AccessibleObjectFromWindow(W.HWND(h), ctypes.c_long(-4), ctypes.byref(iid), ctypes.byref(ptr))
        except Exception:
            pass
        try:
            auto.ControlFromHandle(h).GetChildren()
        except Exception:
            pass
    return found


def driver_view(hwnd, app):
    """現有 uia_driver 在這個視窗會看到什麼（判斷現有邏輯在 agent 視窗上能不能用）。"""
    import uia_driver as ud
    v = {}
    try:
        uw = ud.UiaWindow(hwnd)
        bs = uw.buttons()
        v["buttons"] = [{"name": b.name, "rect": list(b.rect), "virtual": b.submit is not None} for b in bs][:200]
        v["chat_title"] = uw.chat_title(app)
        inp = uw.chat_input()
        if inp is not None:
            v["chat_input"] = describe(inp, 0)
            v["input_text"] = _s(ud.input_text(inp), 200)
            panel = uw.panel_of(inp)
            if panel is not None:
                pr = ud.control_rect(panel)
                v["panel_rect"] = list(pr) if pr else None
                v["panel_text_tail"] = uw.panel_text(panel, 3000)[-3000:]
                v["approval_buttons"] = ud.approval_buttons(bs, pr)
                v["busy_reason"] = ud.agent_busy_reason(bs, pr)
            sb = ud.find_send_button(inp)
            v["send_button"] = {"name": sb.name, "rect": list(sb.rect)} if sb else None
    except Exception:
        v["error"] = traceback.format_exc()
    return v


def save_png(hwnd, path):
    try:
        rgb = wu.capture_window(hwnd)
        if rgb is None:
            return False
        from PIL import Image
        Image.fromarray(rgb).save(path)
        return True
    except Exception:
        return False


def summarize(win, nodes):
    """摘要：可能的對話清單、標題、輸入框、按鈕。"""
    lines = []
    by_type = {}
    for n in nodes:
        by_type[n.get("type", "?")] = by_type.get(n.get("type", "?"), 0) + 1
    lines.append("節點種類：" + "、".join(f"{k} {v}" for k, v in sorted(by_type.items(), key=lambda kv: -kv[1])[:14]))
    items = [n for n in nodes if n.get("type") in ("ListItemControl", "TreeItemControl", "TabItemControl", "HyperlinkControl")
             and n.get("name") and not n.get("offscreen")]
    lines.append(f"\n清單項目／分頁／連結（{len(items)}，前 80 個；可能是專案與對話清單）：")
    for n in items[:80]:
        pats = ",".join((n.get("patterns") or {}).keys())
        sel = (n.get("patterns") or {}).get("SelectionItem")
        lines.append(f"- {n['type'][:-7]}｜{n['name'][:90]}｜class={n.get('class', '')}｜aid={n.get('aid', '')}"
                     f"｜{pats}{'｜已選取' if isinstance(sel, dict) and sel.get('selected') else ''}"
                     f"{'｜status=' + n['status'] if n.get('status') else ''}｜rect={n['rect']}")
    edits = [n for n in nodes if n.get("type") in ("EditControl", "DocumentControl") or "Value" in (n.get("patterns") or {})]
    lines.append(f"\n可輸入的元件（{len(edits)}）：")
    for n in edits[:30]:
        lines.append(f"- {n.get('type')}｜{n.get('name', '')[:60]}｜class={n.get('class', '')}｜"
                     f"{json.dumps(n.get('patterns', {}), ensure_ascii=False)[:160]}｜rect={n['rect']}")
    btns = [n for n in nodes if n.get("type") == "ButtonControl" and n.get("name") and not n.get("offscreen")]
    lines.append(f"\n按鈕（{len(btns)}，前 120 個）：")
    lines.append("、".join(n["name"][:40] for n in btns[:120]))
    texts = [n for n in nodes if n.get("type") == "TextControl" and n.get("name") and not n.get("offscreen")]
    top = sorted(texts, key=lambda n: (n["rect"][1], n["rect"][0]))[:40]
    lines.append("\n最上方的文字（可能含對話標題）：")
    lines.append("｜".join(n["name"][:50] for n in top))
    return "\n".join(lines)


class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            try:
                st.write(s)
                st.flush()
            except Exception:
                pass

    def flush(self):
        pass


def main():
    os.makedirs(os.path.join(BASE_DIR, "recon"), exist_ok=True)
    log = open(os.path.join(BASE_DIR, "recon", "run.log"), "w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, log)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(BASE_DIR, "recon", stamp)
    os.makedirs(out_dir, exist_ok=True)
    try:
        from clicker import enable_dpi_awareness
        enable_dpi_awareness()
    except Exception:
        pass
    fg_before = wu.get_foreground()
    wins = wu.list_top_windows()
    allw = [{"hwnd": w.hwnd, "title": w.title, "exe": w.exe, "pid": w.pid, "iconic": w.iconic,
             "rect": list(wu.get_window_rect(w.hwnd)), "foreground": w.hwnd == fg_before} for w in wins]
    targets = [w for w in wins if any(h in (w.exe or "").lower() for h in APP_HINTS)]
    if "--all" not in sys.argv:
        targets = [w for w in targets if not w.iconic]  # 最小化的編輯器視窗上次已經分析過
    print(f"找到 {len(wins)} 個視窗，其中 Kiro／Antigravity {len(targets)} 個", flush=True)
    summary = [f"# UIA 偵察 {stamp}", "", f"前景視窗（開始時）：{fg_before}", ""]
    summary.append("## 所有 Kiro／Antigravity 視窗")
    for w in targets:
        summary.append(f"- hwnd={w.hwnd}｜{w.exe}｜pid={w.pid}｜{'最小化｜' if w.iconic else ''}標題：{w.title}")
    with auto.UIAutomationInitializerInThread():
        # Electron／Chromium 第一次被查詢才開啟無障礙樹：先碰一下每個視窗（含網頁內容子視窗），等它建好
        for w in targets:
            try:
                auto.ControlFromHandle(w.hwnd).GetChildren()
            except Exception:
                pass
            n = wake_chromium(w.hwnd)
            print(f"  喚醒無障礙樹：{w.exe}｜{w.title[:40]}（網頁內容視窗 {len(n)} 個）", flush=True)
        print("  等 6 秒讓它建好…", flush=True)
        time.sleep(6.0)
        for i, w in enumerate(targets, 1):
            app = "kiro" if "kiro" in w.exe.lower() else "antigravity"
            base = os.path.join(out_dir, f"{i:02d}_{app}_{w.hwnd}")
            print(f"[{i}/{len(targets)}] {w.exe}｜{w.title[:60]} …（大的視窗要 30 秒左右）", flush=True)
            summary.append(f"\n## {i:02d} {w.exe}｜{w.title}")
            if w.iconic:
                summary.append("（最小化：PrintWindow 截不到畫面，UIA 樹可能不完整；沒有還原它）")
            shot = save_png(w.hwnd, base + ".png")
            summary.append(f"截圖：{'已存 ' + os.path.basename(base) + '.png' if shot else '失敗'}")
            try:
                root = auto.ControlFromHandle(w.hwnd)
                nodes, truncated, secs = walk(root)
            except Exception:
                nodes, truncated, secs = [], traceback.format_exc(), 0.0
            summary.append(f"節點 {len(nodes)} 個，耗時 {secs:.1f} 秒" + (f"；{truncated}" if truncated else ""))
            print(f"      節點 {len(nodes)} 個，{secs:.0f} 秒", flush=True)
            with open(base + "_tree.txt", "w", encoding="utf-8") as f:
                f.write("\n".join(tree_line(n) for n in nodes))
            with open(base + "_tree.json", "w", encoding="utf-8") as f:
                json.dump(nodes, f, ensure_ascii=False)
            dv = driver_view(w.hwnd, app)
            with open(base + "_driver.json", "w", encoding="utf-8") as f:
                json.dump(dv, f, ensure_ascii=False, indent=1)
            summary.append(f"現有驅動看到：對話標題「{dv.get('chat_title', '')}」｜輸入框 "
                           f"{'有' if dv.get('chat_input') else '沒有'}｜送出鈕 {'有' if dv.get('send_button') else '沒有'}"
                           f"｜允許按鈕 {dv.get('approval_buttons')}｜忙碌 {dv.get('busy_reason', '')!r}")
            summary.append(summarize(w, nodes))
    fg_after = wu.get_foreground()
    summary.insert(3, f"前景視窗（結束時）：{fg_after}　{'✅ 沒有改變' if fg_after == fg_before else '⚠ 改變了（可能是你自己切換的）'}")
    with open(os.path.join(out_dir, "windows.json"), "w", encoding="utf-8") as f:
        json.dump(allw, f, ensure_ascii=False, indent=1)
    with open(os.path.join(out_dir, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(summary))
    with open(os.path.join(BASE_DIR, "recon", "latest.txt"), "w", encoding="utf-8") as f:
        f.write(stamp)
    print(f"完成：{out_dir}", flush=True)


if __name__ == "__main__":
    main()
