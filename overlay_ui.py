import os
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import List, Dict, Any, Optional, Tuple

import cv2
from PIL import Image, ImageTk

import win_utils as wu
from ai_client import AIError
from ai_settings import AISettings, DEFAULT_SECRETS_PATH
from automation import AutomationWorker, make_clients
from config import AppConfig, DEFAULT_CONFIG_PATH
from projects import ProjectDetector, ProjectWindow, contains_assistant
from driver import build_docs_question, build_goal_question, doc_candidates
from sessions import now_str, phase_label, phase_of, read_kiro_specs, read_plans
from uia_driver import name_in as ud_name_in
from clicker import ClickController
from scanner import ScreenScanner, read_image_unicode, write_image_unicode
from region_selector import MultiBoxSelector, RegionBorderOverlayManager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

BG = "#1E293B"
BG2 = "#0F172A"
FG = "#E2E8F0"
FG_DIM = "#94A3B8"
FONT = ("Microsoft JhengHei UI", 9)
FONT_B = ("Microsoft JhengHei UI", 9, "bold")
FONT_S = ("Microsoft JhengHei UI", 8)

APP_NAME = "AI 工程助理"

MODE_TEXT = {"off": "關閉", "suggest": "只建議", "auto": "自動駕駛"}
MODE_COLOR = {"off": "#64748B", "suggest": "#38BDF8", "auto": "#34D399"}

MODE_LABELS = {
    "image": "基本按鈕精靈・圖片比對",
    "uia": "基本按鈕精靈・UIA 按鈕",
    "ai": APP_NAME,
}


def _label(parent, text, **kw) -> tk.Label:
    opts = dict(bg=BG, fg=FG, font=FONT)
    opts.update(kw)
    return tk.Label(parent, text=text, **opts)


def _entry(parent, var, width=24, show=None) -> tk.Entry:
    return tk.Entry(parent, textvariable=var, width=width, bg=BG2, fg="#FFFFFF", insertbackground="#FFFFFF",
                    bd=1, relief="flat", font=("Consolas", 9), show=show or "")


def _button(parent, text, cmd, bg="#334155", **kw) -> tk.Button:
    opts = dict(bg=bg, fg="#FFFFFF", bd=0, padx=6, pady=2, font=FONT_S, cursor="hand2",
                activebackground="#475569", activeforeground="#FFFFFF")
    opts.update(kw)
    return tk.Button(parent, text=text, command=cmd, **opts)


def _check(parent, text, var, cmd) -> tk.Checkbutton:
    return tk.Checkbutton(parent, text=text, variable=var, command=cmd, bg=BG, fg="#CBD5E1", selectcolor=BG2,
                          activebackground=BG, activeforeground="#FFFFFF", font=FONT_S)


class FloatingOverlayApp:
    def __init__(self, root: tk.Tk, config_path: str = DEFAULT_CONFIG_PATH,
                 secrets_path: str = DEFAULT_SECRETS_PATH) -> None:
        self.root = root
        self.config_path = config_path
        self.secrets_path = secrets_path
        self.cfg = AppConfig.load(self.config_path)
        self.ai_settings = AISettings.load(self.secrets_path)

        self.scanner = ScreenScanner()
        self.clicker = ClickController()
        self.border_mgr = RegionBorderOverlayManager(self.root)
        self.detector = ProjectDetector()

        self.events: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.worker = AutomationWorker(self.cfg, lambda: self.ai_settings, self.events)
        self.worker.start()

        self.is_running = False
        self.click_count = 0
        self.last_scan_time = 0.0
        self.is_collapsed = False
        self._expanded_geometry = ""
        self._drag_offset = (0, 0)
        self._thumb_refs: List[ImageTk.PhotoImage] = []

        self._migrate_session_modes()  # 要在建立畫面前完成，選單才會顯示正確的模式
        self._setup_window()
        self._build_ui()
        self._reload_scanner_and_borders()
        if self.cfg.mode in ("ai", "uia"):
            self.toggle_running()  # 開啟就開始運作；每個視窗的模式決定它會做什麼
        self._schedule_loops()

    # ------------------------------------------------------------------ 視窗與版面
    def _setup_window(self) -> None:
        self.root.title(f"🤖 {APP_NAME}")
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.96)
        self.root.configure(bg=BG)

        cx, cy = self.root.winfo_pointerxy()
        if cx <= 0 and cy <= 0:
            x_pos, y_pos = 120, 120
        else:
            x_pos = max(40, cx - 150)
            y_pos = max(40, cy - 60)
        self.root.geometry(f"410x680+{x_pos}+{y_pos}")
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background="#334155", foreground=FG, padding=(7, 3), font=FONT_S)
        style.map("TNotebook.Tab", background=[("selected", "#0284C7")], foreground=[("selected", "#FFFFFF")])
        style.configure("TCombobox", fieldbackground=BG2, background="#334155", foreground="#FFFFFF")

    def _build_ui(self) -> None:
        self.title_bar = tk.Frame(self.root, bg=BG2, height=34, cursor="fleur")
        self.title_bar.pack(fill="x", side="top")
        self.title_lbl = tk.Label(self.title_bar, text=f"🤖 {APP_NAME} (F8 啟停)", bg=BG2, fg="#F8FAFC",
                                  font=("Microsoft JhengHei UI", 10, "bold"))
        self.title_lbl.pack(side="left", padx=10, pady=6)
        tk.Button(self.title_bar, text="✕", bg=BG2, fg=FG_DIM, activebackground="#EF4444", activeforeground="#FFFFFF",
                  bd=0, font=("Segoe UI", 10, "bold"), width=3, command=self._on_close).pack(side="right", padx=2)
        self.collapse_btn = tk.Button(self.title_bar, text="—", bg=BG2, fg=FG_DIM, activebackground="#334155",
                                      activeforeground="#FFFFFF", bd=0, font=("Segoe UI", 10, "bold"), width=3,
                                      command=self._toggle_collapse)
        self.collapse_btn.pack(side="right", padx=2)
        for widget in (self.title_bar, self.title_lbl):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._on_drag)

        self.body = tk.Frame(self.root, bg=BG, padx=8, pady=6)
        self.body.pack(fill="both", expand=True)

        self.toggle_btn = tk.Button(self.body, text="▶ 啟動 (F8)", bg="#10B981", fg="#FFFFFF",
                                    activebackground="#059669", activeforeground="#FFFFFF",
                                    font=("Microsoft JhengHei UI", 11, "bold"), bd=0, pady=6, cursor="hand2",
                                    command=self.toggle_running)
        self.toggle_btn.pack(fill="x", pady=(0, 4))

        self.status_lbl = _label(self.body, "狀態：待機中 | 累計動作：0 次", fg="#CBD5E1")
        self.status_lbl.pack(fill="x", pady=(0, 4))

        mode_frame = tk.Frame(self.body, bg=BG)
        mode_frame.pack(fill="x", pady=(0, 6))
        self.mode_var = tk.StringVar(value="ai" if self.cfg.mode == "ai" else "basic")
        for key, text in (("ai", f"🤖 {APP_NAME}"), ("basic", "↩ 退回基本按鈕精靈")):
            tk.Radiobutton(mode_frame, text=text, value=key, variable=self.mode_var, command=self._on_mode_changed,
                           bg=BG, fg=FG, selectcolor=BG2, activebackground=BG, activeforeground="#FFFFFF",
                           font=FONT_B).pack(side="left", padx=(0, 10))

        self.nb = ttk.Notebook(self.body)
        self.nb.pack(fill="both", expand=True)
        self.tab_sessions = tk.Frame(self.nb, bg=BG, padx=6, pady=6)
        self.tab_ai = tk.Frame(self.nb, bg=BG, padx=6, pady=6)
        self.tab_api = tk.Frame(self.nb, bg=BG, padx=6, pady=6)
        self.tab_log = tk.Frame(self.nb, bg=BG, padx=4, pady=4)
        self.tab_basic = tk.Frame(self.nb, bg=BG, padx=4, pady=4)
        self.nb.add(self.tab_sessions, text="工作階段")
        self.nb.add(self.tab_ai, text="AI 設定")
        self.nb.add(self.tab_api, text="API 設定")
        self.nb.add(self.tab_log, text="日誌")
        self.nb.add(self.tab_basic, text="基本按鈕精靈")

        self._build_sessions_tab(self.tab_sessions)
        self._build_ai_tab(self.tab_ai)
        self._build_api_tab(self.tab_api)
        self._build_log_tab(self.tab_log)
        self._build_basic_tab(self.tab_basic)
        self._select_tab_for_mode()
        self._update_status_idle()

    # ------------------------------------------------------------------ 分頁：圖片比對（原有功能）
    def _build_image_tab(self, parent: tk.Frame) -> None:
        speed_frame = tk.LabelFrame(parent, text=" ⏱ 掃描速度與精準度 ", bg=BG, fg="#38BDF8", font=FONT_B, padx=8, pady=6)
        speed_frame.pack(fill="x", pady=(0, 8))

        row1 = tk.Frame(speed_frame, bg=BG)
        row1.pack(fill="x", pady=2)
        _label(row1, "掃描間隔(秒):").pack(side="left")
        self.interval_var = tk.StringVar(value=str(self.cfg.scan_interval))
        self.interval_entry = tk.Spinbox(row1, from_=0.1, to=10.0, increment=0.1, textvariable=self.interval_var,
                                         width=6, bg=BG2, fg="#FFFFFF", insertbackground="#FFFFFF", bd=1,
                                         font=("Consolas", 10), command=self._on_params_changed)
        self.interval_entry.pack(side="left", padx=6)
        self.interval_entry.bind("<Return>", lambda _e: self._on_params_changed())
        self.interval_entry.bind("<FocusOut>", lambda _e: self._on_params_changed())
        for sec in (0.2, 0.5, 1.0, 2.0):
            tk.Button(row1, text=f"{sec}s", bg="#334155", fg=FG, bd=0, padx=4, font=("Consolas", 8),
                      command=lambda s=sec: self._set_interval(s)).pack(side="left", padx=2)

        row2 = tk.Frame(speed_frame, bg=BG)
        row2.pack(fill="x", pady=(4, 0))
        self.thresh_lbl = _label(row2, f"相似度門檻: {int(self.cfg.threshold * 100)}%")
        self.thresh_lbl.pack(side="left")
        self.thresh_scale = tk.Scale(row2, from_=70, to=98, orient="horizontal", bg=BG, fg=FG, highlightthickness=0,
                                     troughcolor=BG2, showvalue=False, length=110, command=self._on_thresh_changed)
        self.thresh_scale.set(int(self.cfg.threshold * 100))
        self.thresh_scale.pack(side="right")

        reg_frame = tk.LabelFrame(parent, text=" 📐 掃描螢幕區域 (可多框) ", bg=BG, fg="#34D399", font=FONT_B, padx=8, pady=6)
        reg_frame.pack(fill="x", pady=(0, 8))
        reg_btn_row = tk.Frame(reg_frame, bg=BG)
        reg_btn_row.pack(fill="x", pady=(0, 4))
        _button(reg_btn_row, "＋ 拉框新增區域 (可多框)", self._start_select_regions, bg="#0284C7",
                font=FONT_B, pady=3).pack(side="left")
        _button(reg_btn_row, "清空(全螢幕)", self._clear_regions, bg="#475569", pady=3).pack(side="right")
        self.show_border_var = tk.BooleanVar(value=self.cfg.show_region_borders)
        _check(reg_frame, "在螢幕上顯示綠色掃描框 (滑鼠可穿透)", self.show_border_var, self._on_border_toggle).pack(anchor="w")
        self.regions_list_frame = tk.Frame(reg_frame, bg=BG2, pady=2)
        self.regions_list_frame.pack(fill="x", pady=(4, 0))

        tgt_frame = tk.LabelFrame(parent, text=" 🎯 目標按鈕清單 (看到即點) ", bg=BG, fg="#FBBF24", font=FONT_B, padx=8, pady=6)
        tgt_frame.pack(fill="both", expand=True)
        tgt_top_row = tk.Frame(tgt_frame, bg=BG)
        tgt_top_row.pack(fill="x", pady=(0, 4))
        _button(tgt_top_row, "＋ 截圖新增目標按鈕 (可多框)", self._start_capture_targets, bg="#D97706",
                font=FONT_B, pady=3).pack(side="left", fill="x", expand=True)
        self.targets_list_frame = tk.Frame(tgt_frame, bg=BG2, pady=4)
        self.targets_list_frame.pack(fill="both", expand=True)

        self._refresh_regions_ui()
        self._refresh_targets_ui()

    # ------------------------------------------------------------------ 分頁：工作階段（勾選才會被操作）
    def _build_sessions_tab(self, parent: tk.Frame) -> None:
        self.hum_box = tk.LabelFrame(parent, text=" 🙋 需要你決定 ", bg=BG, fg="#FBBF24", font=FONT_B, padx=6, pady=4)
        self.human_frame = tk.Frame(self.hum_box, bg=BG2)
        self.human_frame.pack(fill="both", expand=True)
        head = tk.Frame(parent, bg=BG)
        head.pack(fill="x")
        self._sessions_head = head
        _label(head, "每個視窗選一個模式：關閉／只建議／自動駕駛", fg=FG_DIM, font=FONT_S).pack(side="left")
        _button(head, "↻", self._refresh_sessions).pack(side="right")
        self.sessions_frame = tk.Frame(parent, bg=BG2, pady=2)
        self.sessions_frame.pack(fill="x", pady=(4, 6))
        self.session_status: Dict[str, str] = {}
        self._session_sig: Tuple = ()
        self._session_status_lbls: Dict[str, tk.Label] = {}
        self._last_session_refresh = 0.0
        self._dialogs: Dict[str, "SessionDialog"] = {}
        self._mode_boxes: Dict[str, ttk.Combobox] = {}

        self._refresh_sessions()

    def _refresh_sessions(self) -> None:
        self._last_session_refresh = time.monotonic()
        try:
            windows = self.detector.detect()
        except Exception as e:
            self._append_log(f"⚠ 偵測視窗失敗：{e!r}")
            return
        groups: Dict[str, List[ProjectWindow]] = {}
        no_project: List[ProjectWindow] = []
        for w in windows:
            if w.project:
                groups.setdefault(w.key, []).append(w)
            else:
                no_project.append(w)
        self._session_iconic = {k: all(x.iconic for x in g) for k, g in groups.items()}
        sig = tuple(sorted((k, g[0].project, g[0].location, g[0].ambiguous, len(g)) for k, g in groups.items())) + (len(no_project),)
        if sig == self._session_sig:
            self._update_session_status_labels()
            return
        self._session_sig = sig
        for c in self.sessions_frame.winfo_children():
            c.destroy()
        self._session_status_lbls.clear()
        self._mode_boxes = {}
        if not groups:
            tk.Label(self.sessions_frame, text="沒有偵測到開著專案的 Kiro / Antigravity 視窗", bg=BG2, fg=FG_DIM,
                     font=FONT_S, pady=6).pack(fill="x")
        for key, g in sorted(groups.items(), key=lambda kv: (kv[1][0].app, kv[1][0].project.lower())):
            w = g[0]
            row = tk.Frame(self.sessions_frame, bg=BG2, pady=2)
            row.pack(fill="x", padx=4, pady=1)
            top = tk.Frame(row, bg=BG2)
            top.pack(fill="x")
            more = f"（{len(g)} 個視窗）" if len(g) > 1 else ""
            self_mark = "　🛡 助理本身" if contains_assistant(w.path, w.remote) else ""
            tk.Label(top, text=f"{w.app_label}｜{w.project}{more}{self_mark}", bg=BG2, fg="#F8FAFC",
                     font=FONT_B).pack(side="left")
            _button(top, "前往", lambda h=w.hwnd: wu.activate_window(h)).pack(side="right")
            _button(top, "詳情", lambda pw=w: self._open_session_dialog(pw), bg="#7C3AED").pack(side="right", padx=4)
            mode_cb = ttk.Combobox(top, values=list(MODE_TEXT.values()), state="readonly", width=8, font=FONT_S)
            mode_cb.set(MODE_TEXT[self.cfg.mode_of(key)])
            mode_cb.pack(side="right", padx=2)
            mode_cb.bind("<<ComboboxSelected>>", lambda _e, k=key, cb=mode_cb, pw=w: self._on_session_mode(k, cb, pw))
            self._mode_boxes[key] = mode_cb
            loc = w.location + ("　⚠ 同名專案有多個路徑，已取最近開啟的" if w.ambiguous else "")
            if w.remote:
                loc += "　⚠ 遠端，無法在本機查核證據"
            tk.Label(row, text=loc, bg=BG2, fg=FG_DIM, font=("Consolas", 8), anchor="w", justify="left",
                     wraplength=330).pack(fill="x", padx=22)
            st = tk.Label(row, text="", bg=BG2, fg="#38BDF8", font=FONT_S, anchor="w", wraplength=340, justify="left")
            st.pack(fill="x", padx=22)
            self._session_status_lbls[key] = st
        if no_project:
            tk.Label(self.sessions_frame, text=f"另有 {len(no_project)} 個視窗沒開專案（例如歡迎頁），不會被操作",
                     bg=BG2, fg=FG_DIM, font=FONT_S, anchor="w").pack(fill="x", padx=6, pady=(2, 0))
        self._update_session_status_labels()

    def _update_session_status_labels(self) -> None:
        for key, lbl in self._session_status_lbls.items():
            mode = self.cfg.mode_of(key)
            if mode == "off":
                lbl.configure(text="關閉：助理不會看也不會操作這個視窗", fg=MODE_COLOR["off"])
            elif not self.is_running:
                lbl.configure(text="⏸ 已全部暫停（按上方按鈕或 F8 繼續）", fg="#FBBF24")
            elif getattr(self, "_session_iconic", {}).get(key):
                lbl.configure(text="🗕 視窗最小化：無法觀察，還原視窗後才會繼續", fg="#FBBF24")
            else:
                default = "自動駕駛：下一輪觀察後就會動作" if mode == "auto" else "只建議：會寫出建議，送出要你按"
                lbl.configure(text=self.session_status.get(key, default), fg=MODE_COLOR[mode])

    def _open_session_dialog(self, pw: ProjectWindow) -> None:
        old = self._dialogs.get(pw.key)
        if old is not None and old.win.winfo_exists():
            old.win.lift()
            old.reload()
            return
        self._dialogs[pw.key] = SessionDialog(self, pw)

    def _on_session_mode(self, key: str, cb: ttk.Combobox, pw: ProjectWindow) -> None:
        mode = {v: k for k, v in MODE_TEXT.items()}.get(cb.get(), "off")
        if mode == "auto" and contains_assistant(pw.path, pw.remote):
            ok = messagebox.askyesno(
                "確認", "這個視窗開的是 AI 工程助理自己的專案。\n\n如果你在這個視窗的對話裡調整助理，"
                "自動駕駛會替你代按確認、打字進同一個對話，造成干擾。\n\n確定要開自動駕駛嗎？", parent=self.root)
            if not ok:
                cb.set(MODE_TEXT[self.cfg.mode_of(key)])
                return
        self.set_session_mode(key, mode)
        if mode == "auto":
            st = self.worker.store.get(pw.key, pw.app_label, pw.project, pw.path, pw.remote).load()
            if not st.goal:
                self._open_session_dialog(pw)  # 還沒有目標：打開詳情讓你直接輸入

    def set_session_mode(self, key: str, mode: str) -> None:
        self.cfg.set_mode(key, mode)
        self.cfg.save(self.config_path)
        if mode == "auto":
            for s in self.worker.store.all():
                st = s.load()
                if st.key == key and st.paused_reason:
                    s.update(lambda x: setattr(x, "paused_reason", ""))
        cb = self._mode_boxes.get(key)
        if cb is not None and cb.winfo_exists():
            cb.set(MODE_TEXT[mode])
        self._append_log(f"模式 → {MODE_TEXT[mode]}：{key}")
        self._update_session_status_labels()
        self._update_status_idle()
        dlg = self._dialogs.get(key)
        if dlg is not None and dlg.win.winfo_exists():
            dlg.reload()

    # ------------------------------------------------------------------ 分頁：退回基本按鈕精靈
    def _build_basic_tab(self, parent: tk.Frame) -> None:
        _label(parent, "選「↩ 退回基本按鈕精靈」後使用以下方式（不經 AI 判斷，看到按鈕就點）", fg=FG_DIM, font=FONT_S,
               wraplength=370, justify="left").pack(anchor="w")
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=(2, 4))
        self.basic_var = tk.StringVar(value=self.cfg.basic_mode)
        for val, text in (("uia", "UIA 按鈕（不動滑鼠，只點勾選的視窗）"), ("image", "圖片比對（會移動滑鼠）")):
            tk.Radiobutton(row, text=text, value=val, variable=self.basic_var, command=self._on_basic_changed,
                           bg=BG, fg=FG, selectcolor=BG2, activebackground=BG, font=FONT_S).pack(anchor="w")
        inner = ttk.Notebook(parent)
        inner.pack(fill="both", expand=True)
        tab_uia = tk.Frame(inner, bg=BG, padx=6, pady=6)
        tab_image = tk.Frame(inner, bg=BG, padx=6, pady=6)
        inner.add(tab_uia, text="UIA 按鈕")
        inner.add(tab_image, text="圖片比對")
        self._build_uia_settings(tab_uia)
        self._build_image_tab(tab_image)

    def _build_uia_settings(self, ui: tk.Frame) -> None:
        _label(ui, "自動點擊的按鈕名稱（逗號分隔，忽略大小寫與 ↵ 等符號）", fg=FG_DIM, font=FONT_S).pack(anchor="w")
        self.uia_names_var = tk.StringVar(value=", ".join(self.cfg.uia_button_names))
        e = _entry(ui, self.uia_names_var, width=48)
        e.pack(fill="x", pady=2)
        e.bind("<FocusOut>", lambda _e: self._save_uia_settings())
        e.bind("<Return>", lambda _e: self._save_uia_settings())
        _label(ui, "永不點擊（任何模式）", fg=FG_DIM, font=FONT_S).pack(anchor="w")
        self.never_var = tk.StringVar(value=", ".join(self.cfg.never_click))
        e2 = _entry(ui, self.never_var, width=48)
        e2.pack(fill="x", pady=2)
        e2.bind("<FocusOut>", lambda _e: self._save_uia_settings())
        e2.bind("<Return>", lambda _e: self._save_uia_settings())
        r = tk.Frame(ui, bg=BG)
        r.pack(fill="x")
        _label(r, "輪巡間隔(秒)").pack(side="left")
        self.uia_interval_var = tk.StringVar(value=str(self.cfg.uia_interval))
        e3 = _entry(r, self.uia_interval_var, width=6)
        e3.pack(side="left", padx=4)
        e3.bind("<FocusOut>", lambda _e: self._save_uia_settings())
        e3.bind("<Return>", lambda _e: self._save_uia_settings())

    def _save_uia_settings(self) -> None:
        self.cfg.uia_button_names = [s.strip() for s in self.uia_names_var.get().split(",") if s.strip()]
        self.cfg.never_click = [s.strip() for s in self.never_var.get().split(",") if s.strip()]
        self.cfg.uia_interval = self._float(self.uia_interval_var, self.cfg.uia_interval, 0.5, 60.0)
        self.cfg.save(self.config_path)

    # ------------------------------------------------------------------ 分頁：AI 代理
    def _build_ai_tab(self, parent: tk.Frame) -> None:
        _label(parent, "要不要自動動作，在「工作階段」分頁每個視窗的模式選單設定（關閉／只建議／自動駕駛）。", fg=FG_DIM,
               font=FONT_S, wraplength=370, justify="left").pack(anchor="w")
        self.allow_reply_var = tk.BooleanVar(value=self.cfg.ai_allow_reply)
        _check(parent, "允許 AI 理解問題後自行回覆（不只是「繼續」）", self.allow_reply_var, self._save_ai_settings).pack(anchor="w")
        self.auto_choose_var = tk.BooleanVar(value=self.cfg.ai_auto_choose)
        _check(parent, "選項題由決策模型選出最佳選項", self.auto_choose_var, self._save_ai_settings).pack(anchor="w")
        self.driver_var = tk.BooleanVar(value=self.cfg.driver_enabled)
        _check(parent, "驅動器：代理停下來時寫出建議的下一步（只建議，送出要你按）", self.driver_var,
               self._save_ai_settings).pack(anchor="w")
        srow = tk.Frame(parent, bg=BG)
        srow.pack(fill="x", pady=(2, 0))
        _label(srow, "每日摘要時間", font=FONT_S).pack(side="left")
        self.summary_time_var = tk.StringVar(value=self.cfg.daily_summary_time)
        se = _entry(srow, self.summary_time_var, width=6)
        se.pack(side="left", padx=4)
        se.bind("<FocusOut>", lambda _e: self._save_ai_settings())
        se.bind("<Return>", lambda _e: self._save_ai_settings())
        _button(srow, "立即產生", self.worker.request_summary).pack(side="left", padx=2)
        _button(srow, "開啟資料夾", lambda: self._open_path(os.path.join(BASE_DIR, "reports"))).pack(side="left", padx=2)
        self.summary_lbl = _label(parent, "", fg=FG_DIM, font=FONT_S)
        self.summary_lbl.pack(anchor="w")

        grid = tk.Frame(parent, bg=BG)
        grid.pack(fill="x", pady=(6, 4))
        self.ai_vars: Dict[str, tk.StringVar] = {}
        fields_def = [
            ("ai_interval", "輪巡間隔(秒)"),
            ("ai_min_confidence", "最低信心度 (0~1)"),
            ("ai_choice_min_confidence", "選項題最低信心度"),
            ("ai_max_risk", "最高可接受風險 (0~1)"),
            ("ai_idle_sec", "你閒置幾秒後才打字"),
            ("ai_recheck_sec", "畫面不變時重新判讀(秒)"),
            ("ai_continue_text", "「繼續」要打的字"),
        ]
        for r, (key, text) in enumerate(fields_def):
            _label(grid, text, font=FONT_S).grid(row=r, column=0, sticky="w", pady=1)
            var = tk.StringVar(value=str(getattr(self.cfg, key)))
            self.ai_vars[key] = var
            e = _entry(grid, var, width=14)
            e.grid(row=r, column=1, sticky="w", padx=6, pady=1)
            e.bind("<FocusOut>", lambda _e: self._save_ai_settings())
            e.bind("<Return>", lambda _e: self._save_ai_settings())
        r = len(fields_def)
        _label(grid, "送出鍵", font=FONT_S).grid(row=r, column=0, sticky="w", pady=1)
        self.send_key_var = tk.StringVar(value=self.cfg.ai_send_key)
        cb = ttk.Combobox(grid, textvariable=self.send_key_var, values=["enter", "ctrl+enter"], state="readonly", width=12)
        cb.grid(row=r, column=1, sticky="w", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda _e: self._save_ai_settings())

        self._render_humans([])

    def _save_ai_settings(self) -> None:
        c = self.cfg
        c.ai_allow_reply = bool(self.allow_reply_var.get())
        c.ai_auto_choose = bool(self.auto_choose_var.get())
        c.driver_enabled = bool(self.driver_var.get())
        t = self.summary_time_var.get().strip()
        if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
            c.daily_summary_time = t
        else:
            self.summary_time_var.set(c.daily_summary_time)
        c.ai_choice_min_confidence = self._float(self.ai_vars["ai_choice_min_confidence"], c.ai_choice_min_confidence, 0.0, 1.0)
        c.ai_interval = self._float(self.ai_vars["ai_interval"], c.ai_interval, 2.0, 600.0)
        c.ai_min_confidence = self._float(self.ai_vars["ai_min_confidence"], c.ai_min_confidence, 0.0, 1.0)
        c.ai_max_risk = self._float(self.ai_vars["ai_max_risk"], c.ai_max_risk, 0.0, 1.0)
        c.ai_idle_sec = self._float(self.ai_vars["ai_idle_sec"], c.ai_idle_sec, 0.0, 120.0)
        c.ai_recheck_sec = self._float(self.ai_vars["ai_recheck_sec"], c.ai_recheck_sec, 5.0, 3600.0)
        c.ai_continue_text = self.ai_vars["ai_continue_text"].get().strip() or "繼續"
        c.ai_send_key = self.send_key_var.get() or "enter"
        c.save(self.config_path)

    def _render_humans(self, items: List[Dict[str, Any]]) -> None:
        for w in self.human_frame.winfo_children():
            w.destroy()
        self.nb.tab(self.tab_sessions, text=f"工作階段 🙋{len(items)}" if items else "工作階段")
        if not items:
            self.hum_box.pack_forget()
            return
        self.hum_box.pack(fill="x", pady=(0, 6), before=self._sessions_head)
        for it in items:
            box = tk.Frame(self.human_frame, bg=BG2, pady=2)
            box.pack(fill="x", padx=2, pady=2)
            tk.Label(box, text=it["title"][:46], bg=BG2, fg="#FBBF24", font=FONT_B, anchor="w").pack(fill="x")
            tk.Label(box, text=f"{it['reason']}\n{it['summary']}", bg=BG2, fg=FG, font=FONT_S, anchor="w",
                     justify="left", wraplength=340).pack(fill="x")
            row = tk.Frame(box, bg=BG2)
            row.pack(fill="x")
            _button(row, "前往視窗", lambda h=it["hwnd"]: wu.activate_window(h), bg="#0284C7").pack(side="left")
            _button(row, "我處理好了，恢復輪巡", lambda h=it["hwnd"]: self.worker.resume(h)).pack(side="left", padx=4)

    # ------------------------------------------------------------------ 分頁：API 設定
    def _build_api_tab(self, parent: tk.Frame) -> None:
        s = self.ai_settings
        _label(parent, "設定存在 secrets.local.json（不會提交到 git）", fg=FG_DIM, font=FONT_S).pack(anchor="w")
        src = tk.Frame(parent, bg=BG)
        src.pack(fill="x", pady=(2, 2))
        _label(src, "金鑰來源：", font=FONT_S).pack(side="left")
        self.key_source_var = tk.StringVar(value=s.key_source)
        for val, text in (("file", "從檔案讀取"), ("manual", "手動輸入")):
            tk.Radiobutton(src, text=text, value=val, variable=self.key_source_var, command=self._update_key_rows,
                           bg=BG, fg=FG, selectcolor=BG2, activebackground=BG, font=FONT_S).pack(side="left")
        be = tk.Frame(parent, bg=BG)
        be.pack(fill="x", pady=(0, 6))
        _label(be, "決策方式：", font=FONT_S).pack(side="left")
        self.backend_var = tk.StringVar(value=s.decision_backend)
        for val, text in (("llm", "主權 AI 判斷模型（免費）"), ("decision1", "Decision-1（付費）")):
            tk.Radiobutton(be, text=text, value=val, variable=self.backend_var, command=self._update_key_rows,
                           bg=BG, fg=FG, selectcolor=BG2, activebackground=BG, font=FONT_S).pack(side="left")

        self.api_vars: Dict[str, tk.StringVar] = {}
        self.key_rows: Dict[str, Tuple[tk.Frame, tk.Frame]] = {}
        self.api_boxes: Dict[str, tk.LabelFrame] = {}
        sections = (
            ("vision", "① 中鋼主權 AI", "#34D399",
             (("base_url", "Base URL"), ("model", "OCR 判讀模型"), ("judge_model", "判斷/回覆模型"))),
            ("decision", "② Decision-1（LiteLLM，選用）", "#A78BFA", (("base_url", "Base URL"), ("model", "模型"))),
        )
        for section, title, color, rows in sections:
            ep = getattr(s, section)
            box = tk.LabelFrame(parent, text=f" {title} ", bg=BG, fg=color, font=FONT_B, padx=6, pady=4)
            box.pack(fill="x", pady=(0, 6))
            self.api_boxes[section] = box
            for key, text in rows:
                row = tk.Frame(box, bg=BG)
                row.pack(fill="x", pady=1)
                _label(row, text, width=12, anchor="w", font=FONT_S).pack(side="left")
                value = s.judge_model if key == "judge_model" else getattr(ep, key)
                var = tk.StringVar(value=value)
                self.api_vars[f"{section}.{key}"] = var
                _entry(row, var, width=30).pack(side="left", fill="x", expand=True)
            file_row = tk.Frame(box, bg=BG)
            _label(file_row, "金鑰檔案", width=12, anchor="w", font=FONT_S).pack(side="left")
            fvar = tk.StringVar(value=ep.key_file)
            self.api_vars[f"{section}.key_file"] = fvar
            _entry(file_row, fvar, width=30).pack(side="left", fill="x", expand=True)
            key_row = tk.Frame(box, bg=BG)
            _label(key_row, "金鑰", width=12, anchor="w", font=FONT_S).pack(side="left")
            kvar = tk.StringVar(value=ep.key)
            self.api_vars[f"{section}.key"] = kvar
            _entry(key_row, kvar, width=30, show="•").pack(side="left", fill="x", expand=True)
            self.key_rows[section] = (file_row, key_row)
        _label(parent, "OCR 判讀模型負責看畫面，判斷/回覆模型負責快速決策與擬回覆，兩者都建議 gemma-4-31b（實測抄指令最準、最快）。"
                       "選 Decision-1 時，決策改由 Decision-1 負責，擬回覆仍用判斷/回覆模型。",
               fg=FG_DIM, font=FONT_S, wraplength=370, justify="left").pack(anchor="w")

        btns = tk.Frame(parent, bg=BG)
        btns.pack(fill="x", pady=(4, 4))
        _button(btns, "💾 儲存", self._save_api_settings, bg="#0284C7").pack(side="left")
        _button(btns, "🔌 測試連線", self._test_api).pack(side="left", padx=6)
        self.api_result = _label(parent, "", fg=FG_DIM, font=FONT_S, wraplength=370, justify="left", anchor="w")
        self.api_result.pack(fill="x")
        self._update_key_rows()

    def _update_key_rows(self) -> None:
        manual = self.key_source_var.get() == "manual"
        for file_row, key_row in self.key_rows.values():
            file_row.pack_forget()
            key_row.pack_forget()
            (key_row if manual else file_row).pack(fill="x", pady=1)
        use_d1 = self.backend_var.get() == "decision1"
        self.api_boxes["decision"].configure(fg="#A78BFA" if use_d1 else "#475569")

    def _collect_api_settings(self) -> AISettings:
        s = AISettings()
        s.key_source = self.key_source_var.get()
        s.decision_backend = self.backend_var.get()
        s.judge_model = self.api_vars["vision.judge_model"].get().strip() or "gemma-4-31b"
        for section in ("vision", "decision"):
            ep = getattr(s, section)
            for key in ("base_url", "model", "key", "key_file"):
                setattr(ep, key, self.api_vars[f"{section}.{key}"].get().strip())
        return s

    def _save_api_settings(self) -> None:
        self.ai_settings = self._collect_api_settings()
        self.ai_settings.save(self.secrets_path)
        self.api_result.configure(text="已儲存。", fg="#34D399")

    def _test_api(self) -> None:
        s = self._collect_api_settings()
        self.api_result.configure(text="測試中…", fg=FG_DIM)

        def work() -> None:
            lines = []
            vision, judge, decision = make_clients(s)
            for role, client in (("OCR 判讀", vision), ("判斷/回覆", judge)):
                t = time.time()
                try:
                    out = client.chat("只回覆 OK 兩個字母", max_tokens=20)
                    lines.append(f"✅ {role} {client.model}：{out.strip()[:20]!r}（{time.time() - t:.1f}s）")
                except AIError as e:
                    lines.append(f"❌ {role} {client.model}：{e}")
            t = time.time()
            label = "Decision-1" if s.decision_backend == "decision1" else f"決策（{s.judge_model}）"
            try:
                ans = decision.systemone("使用者按下了連線測試按鈕。",
                                         {"ok": {"type": "noul", "instructions": "這是一則連線測試嗎？"}})
                lines.append(f"✅ {label}：noul={ans['ok']['noul']:.2f}（{time.time() - t:.1f}s）")
            except (AIError, KeyError, TypeError) as e:
                lines.append(f"❌ {label}：{e}")
            self.events.put({"type": "api_test", "text": "\n".join(lines)})

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ 分頁：日誌
    def _build_log_tab(self, parent: tk.Frame) -> None:
        self.log_text = tk.Text(parent, bg=BG2, fg=FG, font=("Consolas", 8), wrap="word", bd=0, height=10)
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")
        _label(parent, "完整紀錄：automation.log", fg=FG_DIM, font=FONT_S).pack(anchor="w")

    def _append_log(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 400:
            self.log_text.delete("1.0", f"{lines - 400}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _migrate_session_modes(self) -> None:
        """舊設定（勾選＋詳情裡的自動駕駛開關）轉成新的單一模式；助理自己的專案一律從「關閉」開始。"""
        changed = False
        for key in list(self.cfg.authorized_sessions):
            if key in self.cfg.session_modes:
                continue
            mode = "suggest"
            for s in self.worker.store.all():
                st = s.load()
                if st.key == key:
                    if contains_assistant(st.path, st.remote):
                        mode = "off"
                    elif st.autopilot:
                        mode = "auto"
            self.cfg.set_mode(key, mode)
            changed = True
        if changed:
            self.cfg.save(self.config_path)

    # ------------------------------------------------------------------ 共用小工具
    @staticmethod
    def _open_path(path: str) -> None:
        if not os.path.splitext(path)[1]:
            os.makedirs(path, exist_ok=True)
        try:
            os.startfile(path)
        except OSError:
            pass

    @staticmethod
    def _float(var: tk.StringVar, fallback: float, lo: float, hi: float) -> float:
        try:
            val = max(lo, min(hi, float(var.get())))
        except ValueError:
            val = fallback
        var.set(str(val))
        return val

    def _on_mode_changed(self) -> None:
        self.cfg.mode = "ai" if self.mode_var.get() == "ai" else self.cfg.basic_mode
        self.cfg.save(self.config_path)
        self._select_tab_for_mode()
        self._sync_worker()
        self._update_status_idle()

    def _on_basic_changed(self) -> None:
        self.cfg.basic_mode = self.basic_var.get()
        if self.mode_var.get() == "basic":
            self.cfg.mode = self.cfg.basic_mode
            self._sync_worker()
            self._update_status_idle()
        self.cfg.save(self.config_path)

    def _select_tab_for_mode(self) -> None:
        self.nb.select(self.tab_sessions if self.cfg.mode == "ai" else self.tab_basic)

    def _sync_worker(self) -> None:
        if self.is_running and self.cfg.mode in ("uia", "ai"):
            self.worker.running.set()
        else:
            self.worker.running.clear()

    def _update_status_idle(self) -> None:
        if self.cfg.mode == "ai":
            modes = [self.cfg.mode_of(k) for k in self.cfg.authorized_sessions]
            scope = f"自動駕駛 {modes.count('auto')}、只建議 {modes.count('suggest')}"
        else:
            scope = "基本按鈕精靈・" + ("UIA 按鈕" if self.cfg.mode == "uia" else "圖片比對")
        if self.is_running:
            self.status_lbl.configure(text=f"🟢 運作中｜{scope}｜累計 {self.click_count} 次", fg="#34D399")
        else:
            self.status_lbl.configure(text=f"⏸ 已暫停｜{scope}｜累計 {self.click_count} 次", fg="#FBBF24")

    # ------------------------------------------------------------------ 圖片比對模式（原有）
    def _refresh_regions_ui(self) -> None:
        for w in self.regions_list_frame.winfo_children():
            w.destroy()
        if not self.cfg.regions:
            tk.Label(self.regions_list_frame, text="🌐 目前為【全螢幕掃描】模式", bg=BG2, fg=FG_DIM, font=FONT_S,
                     pady=4).pack(fill="x")
            return
        for idx, reg in enumerate(self.cfg.regions):
            row = tk.Frame(self.regions_list_frame, bg=BG2)
            row.pack(fill="x", padx=4, pady=1)
            var = tk.BooleanVar(value=reg.get("enabled", True))
            tk.Checkbutton(row, text=f"區域 #{idx + 1}: ({reg['x']},{reg['y']}) {reg['w']}x{reg['h']}", variable=var,
                           bg=BG2, fg=FG, selectcolor=BG, activebackground=BG2, font=("Consolas", 8),
                           command=lambda i=idx, v=var: self._toggle_region_item(i, v.get())).pack(side="left")
            tk.Button(row, text="✕", bg=BG2, fg="#EF4444", bd=0, font=("Segoe UI", 8, "bold"),
                      command=lambda i=idx: self._delete_region_item(i)).pack(side="right")

    def _refresh_targets_ui(self) -> None:
        for w in self.targets_list_frame.winfo_children():
            w.destroy()
        self._thumb_refs.clear()
        if not self.cfg.targets:
            tk.Label(self.targets_list_frame, text="尚無目標按鈕，請點擊上方按鈕截圖新增", bg=BG2, fg=FG_DIM,
                     font=FONT_S, pady=6).pack(fill="x")
            return
        for idx, tgt in enumerate(self.cfg.targets):
            row = tk.Frame(self.targets_list_frame, bg=BG2)
            row.pack(fill="x", padx=4, pady=2)
            var = tk.BooleanVar(value=tgt.get("enabled", True))
            tk.Checkbutton(row, variable=var, bg=BG2, selectcolor=BG, activebackground=BG2,
                           command=lambda i=idx, v=var: self._toggle_target_item(i, v.get())).pack(side="left")
            full_path = tgt["path"] if os.path.isabs(tgt["path"]) else os.path.join(BASE_DIR, tgt["path"])
            img_bgr = read_image_unicode(full_path)
            if img_bgr is not None:
                pil_img = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
                pil_img.thumbnail((68, 24))
                tk_img = ImageTk.PhotoImage(pil_img)
                self._thumb_refs.append(tk_img)
                tk.Label(row, image=tk_img, bg=BG2).pack(side="left", padx=4)
            tk.Label(row, text=tgt.get("name", f"目標 #{idx + 1}"), bg=BG2, fg=FG, font=FONT_S).pack(side="left", padx=2)
            tk.Button(row, text="✕", bg=BG2, fg="#EF4444", bd=0, font=("Segoe UI", 8, "bold"),
                      command=lambda i=idx: self._delete_target_item(i)).pack(side="right")

    def _set_interval(self, sec: float) -> None:
        self.interval_var.set(str(sec))
        self._on_params_changed()

    def _on_params_changed(self) -> None:
        try:
            val = float(self.interval_var.get())
            self.cfg.scan_interval = max(0.1, min(10.0, val))
        except ValueError:
            self.cfg.scan_interval = 1.0
        self.interval_var.set(f"{self.cfg.scan_interval:.1f}")
        self.cfg.save(self.config_path)

    def _on_thresh_changed(self, val_str: str) -> None:
        val = int(float(val_str))
        self.cfg.threshold = round(val / 100.0, 2)
        self.thresh_lbl.configure(text=f"相似度門檻: {val}%")
        self.cfg.save(self.config_path)

    def _on_border_toggle(self) -> None:
        self.cfg.show_region_borders = bool(self.show_border_var.get())
        self.cfg.save(self.config_path)
        self.border_mgr.update_borders(self.cfg.regions, self.cfg.show_region_borders)

    def _toggle_region_item(self, idx: int, enabled: bool) -> None:
        if 0 <= idx < len(self.cfg.regions):
            self.cfg.regions[idx]["enabled"] = enabled
            self.cfg.save(self.config_path)
            self.border_mgr.update_borders(self.cfg.regions, self.cfg.show_region_borders)

    def _delete_region_item(self, idx: int) -> None:
        if 0 <= idx < len(self.cfg.regions):
            self.cfg.regions.pop(idx)
            self.cfg.save(self.config_path)
            self._refresh_regions_ui()
            self.border_mgr.update_borders(self.cfg.regions, self.cfg.show_region_borders)

    def _clear_regions(self) -> None:
        self.cfg.regions.clear()
        self.cfg.save(self.config_path)
        self._refresh_regions_ui()
        self.border_mgr.update_borders(self.cfg.regions, self.cfg.show_region_borders)

    def _toggle_target_item(self, idx: int, enabled: bool) -> None:
        if 0 <= idx < len(self.cfg.targets):
            self.cfg.targets[idx]["enabled"] = enabled
            self.cfg.save(self.config_path)
            self.scanner.load_templates(self.cfg.targets, BASE_DIR)

    def _delete_target_item(self, idx: int) -> None:
        if 0 <= idx < len(self.cfg.targets):
            self.cfg.targets.pop(idx)
            self.cfg.save(self.config_path)
            self._refresh_targets_ui()
            self.scanner.load_templates(self.cfg.targets, BASE_DIR)

    def _start_select_regions(self) -> None:
        was_running = self.is_running
        if was_running:
            self.toggle_running()
        self.border_mgr.clear()
        self.root.withdraw()
        self.root.after(180, lambda: self._open_region_selector(was_running))

    def _open_region_selector(self, resume_after: bool) -> None:
        def on_done(boxes: List[Dict[str, Any]], _crops: List[Any]) -> None:
            self.root.deiconify()
            if boxes:
                self.cfg.regions.extend(boxes)
                self.cfg.save(self.config_path)
                self._refresh_regions_ui()
            self.border_mgr.update_borders(self.cfg.regions, self.cfg.show_region_borders)
            if resume_after:
                self.toggle_running()

        MultiBoxSelector(self.root, mode_title="新增掃描區域框 (可連續拉多個框)", box_color="#10B981", on_complete=on_done)

    def _start_capture_targets(self) -> None:
        was_running = self.is_running
        if was_running:
            self.toggle_running()
        self.border_mgr.clear()
        self.root.withdraw()
        self.root.after(180, lambda: self._open_target_selector(was_running))

    def _open_target_selector(self, resume_after: bool) -> None:
        def on_done(boxes: List[Dict[str, Any]], crops: List[Any]) -> None:
            self.root.deiconify()
            if crops:
                ts = int(time.time())
                for i, crop in enumerate(crops):
                    rel_path = f"targets/custom_{ts}_{i + 1}.png"
                    if write_image_unicode(os.path.join(BASE_DIR, rel_path), crop):
                        self.cfg.targets.append({"name": f"自訂按鈕 #{len(self.cfg.targets) + 1}", "path": rel_path,
                                                 "enabled": True})
                self.cfg.save(self.config_path)
                self._refresh_targets_ui()
                self.scanner.load_templates(self.cfg.targets, BASE_DIR)
            self.border_mgr.update_borders(self.cfg.regions, self.cfg.show_region_borders)
            if resume_after:
                self.toggle_running()

        MultiBoxSelector(self.root, mode_title="框選要自動點擊的目標按鈕 (可一次框多個按鈕)", box_color="#F59E0B",
                         on_complete=on_done)

    def _reload_scanner_and_borders(self) -> None:
        self.scanner.load_templates(self.cfg.targets, BASE_DIR)
        self.border_mgr.update_borders(self.cfg.regions, self.cfg.show_region_borders)

    # ------------------------------------------------------------------ 啟停與主迴圈
    def toggle_running(self) -> None:
        self.is_running = not self.is_running
        if self.is_running:
            self.toggle_btn.configure(text="⏸ 全部暫停 (F8)", bg="#475569", activebackground="#334155")
            self.last_scan_time = 0.0
        else:
            self.toggle_btn.configure(text="▶ 繼續 (F8)", bg="#10B981", activebackground="#059669")
        self._sync_worker()
        self._update_status_idle()
        if hasattr(self, "_session_status_lbls"):
            self._update_session_status_labels()

    def _get_self_rect(self) -> Tuple[int, int, int, int]:
        return (self.root.winfo_rootx(), self.root.winfo_rooty(), self.root.winfo_width(), self.root.winfo_height())

    def _schedule_loops(self) -> None:
        self._tick()

    def _drain_events(self) -> None:
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                return
            kind = ev.get("type")
            if kind == "log":
                self._append_log(ev["text"])
            elif kind == "count":
                self.click_count = ev["value"]
                self._update_status_idle()
            elif kind == "status":
                if self.is_running:
                    self.status_lbl.configure(text=f"🟢 {ev['text']} | 累計：{self.click_count}", fg="#34D399")
            elif kind == "session":
                self.session_status[ev["key"]] = ev["text"]
                self._update_session_status_labels()
                dlg = self._dialogs.get(ev["key"])
                if dlg is not None and dlg.win.winfo_exists():
                    dlg.reload(full=False)
            elif kind == "modes_changed":
                for key, cb in self._mode_boxes.items():
                    if cb.winfo_exists():
                        cb.set(MODE_TEXT[self.cfg.mode_of(key)])
                self._update_session_status_labels()
                self._update_status_idle()
            elif kind == "send_result":
                dlg = self._dialogs.get(ev["key"])
                if dlg is not None and dlg.win.winfo_exists():
                    dlg.on_send_result(ev["ok"], ev["text"])
                self._append_log(("✅ " if ev["ok"] else "⛔ ") + ev["text"])
            elif kind == "summary":
                self.summary_lbl.configure(text=f"最近一次：{os.path.basename(ev['path'])}")
                if ev.get("alert"):
                    self.root.bell()
            elif kind == "humans":
                self._render_humans(ev.get("items", []))
                if ev.get("alert"):
                    self.root.bell()
            elif kind == "api_test":
                ok = "❌" not in ev["text"]
                self.api_result.configure(text=ev["text"], fg="#34D399" if ok else "#F87171")

    def _tick(self) -> None:
        if self.clicker.poll_f8_toggled():
            self.toggle_running()
        self._drain_events()
        if time.monotonic() - self._last_session_refresh > 3.0:
            self._refresh_sessions()

        now = time.monotonic()
        if self.is_running and self.cfg.mode == "image" and (now - self.last_scan_time) >= self.cfg.scan_interval:
            self.last_scan_time = now
            matches = self.scanner.scan_screen(regions=self.cfg.regions, threshold=self.cfg.threshold,
                                               multi_scale=self.cfg.multi_scale, exclude_rect=self._get_self_rect())
            if matches:
                best = matches[0]
                if self.clicker.click_and_restore(best.x, best.y, cooldown_sec=self.cfg.cooldown_sec):
                    self.click_count += 1
                    self.status_lbl.configure(
                        text=f"⚡ 已點擊 [{best.target_name}] ({best.x},{best.y}) | 累計：{self.click_count}")

        self.root.after(40, self._tick)

    def _toggle_collapse(self) -> None:
        self.is_collapsed = not self.is_collapsed
        x, y = self.root.winfo_x(), self.root.winfo_y()
        if self.is_collapsed:
            self._expanded_geometry = f"{self.root.winfo_width()}x{self.root.winfo_height()}"
            self.body.pack_forget()
            self.collapse_btn.configure(text="▢")
            self.root.geometry(f"310x34+{x}+{y}")
        else:
            self.body.pack(fill="both", expand=True)
            self.collapse_btn.configure(text="—")
            self.root.geometry(f"{self._expanded_geometry or '410x680'}+{x}+{y}")

    def _start_drag(self, event: tk.Event) -> None:
        self._drag_offset = (event.x, event.y)

    def _on_drag(self, event: tk.Event) -> None:
        nx = self.root.winfo_x() + event.x - self._drag_offset[0]
        ny = self.root.winfo_y() + event.y - self._drag_offset[1]
        self.root.geometry(f"+{nx}+{ny}")

    def _on_close(self) -> None:
        self.worker.stop()
        self.border_mgr.clear()
        self.cfg.save(self.config_path)
        self.root.destroy()


class SessionDialog:
    """單一工作階段的目標與建議：設定目標、採用代理說明的目標、檢視並送出建議的 prompt。"""

    def __init__(self, app: "FloatingOverlayApp", pw: ProjectWindow) -> None:
        self.app = app
        self.pw = pw
        self.sess = app.worker.store.get(pw.key, pw.app_label, pw.project, pw.path, pw.remote)
        self.win = tk.Toplevel(app.root)
        self.win.title(f"{pw.app_label}｜{pw.project}")
        self.win.configure(bg=BG)
        self.win.attributes("-topmost", True)
        sw, sh = app.root.winfo_screenwidth(), app.root.winfo_screenheight()
        x = app.root.winfo_rootx() - 470
        x = x if x >= 0 else min(app.root.winfo_rootx() + app.root.winfo_width() + 10, sw - 470)
        y = max(0, min(app.root.winfo_rooty(), sh - 900))
        self.win.geometry(f"460x{min(860, sh - 60)}+{max(0, x)}+{y}")
        body = tk.Frame(self.win, bg=BG, padx=10, pady=8)
        body.pack(fill="both", expand=True)

        # 操作列固定在最下方，內容再多也不會被擠出視窗
        bar = tk.Frame(body, bg=BG2, padx=6, pady=6)
        bar.pack(side="bottom", fill="x", pady=(6, 0))
        brow = tk.Frame(bar, bg=BG2)
        brow.pack(fill="x")
        self.send_btn = _button(brow, "⌨ 送出這則訊息到 IDE", self.send, bg="#10B981", font=FONT_B, pady=4)
        self.send_btn.pack(side="left")
        _button(brow, "↻ 重新整理", self.reload).pack(side="left", padx=6)
        _button(brow, "開啟 progress.md", lambda: FloatingOverlayApp._open_path(self.sess.progress_path)).pack(side="right")
        self.result_lbl = tk.Label(bar, text="", bg=BG2, fg=FG_DIM, font=FONT_S, wraplength=420, justify="left", anchor="w")
        self.result_lbl.pack(fill="x", pady=(4, 0))

        _label(body, f"{pw.app_label}｜{pw.project}", font=("Microsoft JhengHei UI", 11, "bold")).pack(anchor="w")
        _label(body, pw.location, fg=FG_DIM, font=("Consolas", 8), wraplength=430, justify="left").pack(anchor="w")


        gbox = tk.LabelFrame(body, text=" 🎯 目標（由你訂定） ", bg=BG, fg="#34D399", font=FONT_B, padx=6, pady=4)
        gbox.pack(fill="x", pady=(6, 4))
        self.goal_text = tk.Text(gbox, height=4, bg=BG2, fg="#FFFFFF", insertbackground="#FFFFFF", wrap="word",
                                 font=FONT, bd=0)
        self.goal_text.pack(fill="x")
        self.goal_info = _label(gbox, "", fg=FG_DIM, font=FONT_S, wraplength=420, justify="left")
        self.goal_info.pack(anchor="w")
        grow = tk.Frame(gbox, bg=BG)
        grow.pack(fill="x", pady=(2, 0))
        _button(grow, "💾 儲存目標", self.save_goal, bg="#0284C7").pack(side="left")
        self.adopt_btn = _button(grow, "採用代理說明的目標", self.adopt_agent_goal)
        self.adopt_btn.pack(side="left", padx=6)

        self.p2_box = tk.LabelFrame(body, text=" 🚦 階段與自動駕駛 ", bg=BG, fg="#F472B6", font=FONT_B, padx=6, pady=4)
        self.p2_box.pack(fill="x", pady=(0, 4))
        prow = tk.Frame(self.p2_box, bg=BG)
        prow.pack(fill="x")
        self.phase_lbl = _label(prow, "", font=FONT_B)
        self.phase_lbl.pack(side="left")
        self.mode_cb = ttk.Combobox(prow, values=list(MODE_TEXT.values()), state="readonly", width=8, font=FONT_S)
        self.mode_cb.pack(side="right")
        self.mode_cb.bind("<<ComboboxSelected>>", lambda _e: self.app._on_session_mode(self.pw.key, self.mode_cb, self.pw))
        _label(prow, "模式", font=FONT_S).pack(side="right", padx=4)
        self.ap_info = _label(self.p2_box, "", fg=FG_DIM, font=FONT_S, wraplength=420, justify="left")
        self.ap_info.pack(anchor="w")
        self.pause_row = tk.Frame(self.p2_box, bg=BG)
        self.pause_lbl = _label(self.pause_row, "", fg="#F87171", font=FONT_S, wraplength=330, justify="left")
        self.pause_lbl.pack(side="left")
        _button(self.pause_row, "恢復自動駕駛", self.resume_autopilot).pack(side="right")
        self.acc_list = tk.Listbox(self.p2_box, height=4, bg=BG2, fg="#FFFFFF", font=("Microsoft JhengHei UI", 8),
                                   activestyle="none", bd=0, highlightthickness=0, selectbackground=BG2)
        self.acc_list.pack(fill="x", pady=(2, 2))
        arow = tk.Frame(self.p2_box, bg=BG)
        arow.pack(fill="x")
        self.confirm_btn = _button(arow, "✅ 確認驗收 spec", self.confirm_spec, bg="#0284C7")
        self.confirm_btn.pack(side="left")
        self.rollover_btn = _button(arow, "🔄 開新對話並送出", self.rollover)
        self.rollover_btn.pack(side="left", padx=6)
        _button(arow, "報告資料夾", lambda: FloatingOverlayApp._open_path(self.sess.reports_dir)).pack(side="right")

        self.proposal_box = tk.LabelFrame(body, text=" 🤖 代理說明的最終目標（待你採用） ", bg=BG, fg="#A78BFA",
                                          font=FONT_B, padx=6, pady=4)
        self.proposal_lbl = _label(self.proposal_box, "", font=FONT_S, wraplength=420, justify="left")
        self.proposal_lbl.pack(anchor="w")

        self.docs_box = tk.LabelFrame(body, text=" 📄 正在進行的計畫（可多選；由你勾選或請代理判斷） ", bg=BG,
                                      fg="#38BDF8", font=FONT_B, padx=6, pady=4)
        self._docs_open = False
        self.docs_toggle = _button(body, "▸ 進階：正在進行的計畫文件", self._toggle_docs)
        self.docs_toggle.pack(anchor="w", pady=(2, 2))
        lb_row = tk.Frame(self.docs_box, bg=BG)
        lb_row.pack(fill="x")
        self.docs_list = tk.Listbox(lb_row, selectmode="multiple", height=4, bg=BG2, fg="#FFFFFF",
                                    selectbackground="#0284C7", selectforeground="#FFFFFF", font=("Consolas", 8),
                                    activestyle="none", bd=0, highlightthickness=0, exportselection=False)
        self.docs_list.pack(side="left", fill="x", expand=True)
        sb = tk.Scrollbar(lb_row, command=self.docs_list.yview)
        sb.pack(side="right", fill="y")
        self.docs_list.configure(yscrollcommand=sb.set)
        hsb = tk.Scrollbar(self.docs_box, orient="horizontal", command=self.docs_list.xview)
        hsb.pack(fill="x")
        self.docs_list.configure(xscrollcommand=hsb.set)
        self.docs_info = _label(self.docs_box, "", fg=FG_DIM, font=FONT_S, wraplength=420, justify="left")
        self.docs_info.pack(anchor="w")
        drow = tk.Frame(self.docs_box, bg=BG)
        drow.pack(fill="x", pady=(2, 0))
        _button(drow, "💾 儲存勾選", self.save_docs, bg="#0284C7").pack(side="left")
        _button(drow, "🤖 讓代理判斷", self.ask_agent_docs).pack(side="left", padx=6)
        self.adopt_docs_btn = _button(drow, "套用代理的判斷", self.adopt_agent_docs)
        self.adopt_docs_btn.pack(side="left")
        self.candidates: List[Dict[str, Any]] = []

        self.approval_box = tk.LabelFrame(body, text=" ⏸ 代理正在等你允許（處理完它才會繼續） ", bg=BG, fg="#F87171",
                                          font=FONT_B, padx=6, pady=4)
        self.approval_lbl = tk.Label(self.approval_box, text="", bg=BG2, fg="#FFFFFF", font=("Consolas", 8),
                                     wraplength=420, justify="left", anchor="w", padx=4, pady=3)
        self.approval_lbl.pack(fill="x")
        self.approval_btns = tk.Frame(self.approval_box, bg=BG)
        self.approval_btns.pack(fill="x", pady=(4, 0))

        sbox = tk.LabelFrame(body, text=" 💡 建議的下一步（只建議，按「送出」才會輸入到 IDE） ", bg=BG, fg="#FBBF24",
                             font=FONT_B, padx=6, pady=4)
        sbox.pack(fill="both", expand=True)
        self.sug_info = _label(sbox, "", font=FONT_S, wraplength=420, justify="left")
        self.sug_info.pack(anchor="w")
        self.sug_text = tk.Text(sbox, height=5, bg=BG2, fg="#FFFFFF", insertbackground="#FFFFFF", wrap="word",
                                font=FONT, bd=0)
        self.sug_text.pack(fill="both", expand=True, pady=(2, 2))
        _label(sbox, "可以先修改上面的文字再送出。送出時會短暫切到 IDE 視窗輸入，再切回來。", fg=FG_DIM, font=FONT_S,
               wraplength=420, justify="left").pack(anchor="w")
        self.reload()

    def reload(self, full: bool = True) -> None:
        """full=False 是背景自動更新：不覆蓋你正在修改的目標或訊息。"""
        st = self.sess.load()
        if full or not self.goal_text.edit_modified():
            self.goal_text.delete("1.0", "end")
            self.goal_text.insert("1.0", st.goal)
            self.goal_text.edit_modified(False)
        self.goal_info.configure(text=f"{st.goal_confirmed_at} 確認" if st.goal_confirmed_at else
                                 "尚未設定。進行中的專案可先送出下方的「詢問最終目標」，代理回答後再採用或修改。")
        self._fill_p2(st)
        if st.agent_goal_proposal:
            self.proposal_lbl.configure(text=st.agent_goal_proposal)
            self.proposal_box.pack(fill="x", pady=(0, 4), before=self.docs_toggle)
            self.adopt_btn.configure(state="normal")
        else:
            self.proposal_box.pack_forget()
            self.adopt_btn.configure(state="disabled")
        for c in self.approval_btns.winfo_children():
            c.destroy()
        if st.pending_approval:
            self.approval_lbl.configure(text=st.pending_approval)
            for name in st.approval_buttons:
                deny = ud_name_in(name, ["Deny", "Always deny", "Reject", "Skip", "拒絕"])
                _button(self.approval_btns, f"代按「{name}」", lambda n=name: self.click(n),
                        bg="#B91C1C" if deny else "#10B981").pack(side="left", padx=(0, 6))
            self.approval_box.pack(fill="x", pady=(0, 4), before=self.docs_toggle)
        else:
            self.approval_box.pack_forget()
        local = bool(self.pw.path) and not self.pw.remote
        specs = read_kiro_specs(self.pw.path) if (self.pw.app == "kiro" and local) else []
        plans = read_plans(self.pw.path) if local else []
        if full or not self.docs_list.curselection() or not self.candidates:
            self._fill_docs(st, doc_candidates(specs, plans))
        prompt = st.suggested_prompt
        if not prompt and not st.goal and not st.goal_question_sent_at:
            prompt = build_goal_question(st, self.candidates)
        action = st.suggested_action or ("ask_goal" if not st.goal else "—")
        self.sug_info.configure(text=f"動作：{action}｜{st.suggested_reason or '尚未產生建議（代理停下來時會自動產生）'}"
                                     + (f"｜{st.suggested_at}" if st.suggested_at else ""))
        if full or not self.sug_text.edit_modified():
            self.sug_text.delete("1.0", "end")
            self.sug_text.insert("1.0", prompt)
            self.sug_text.edit_modified(False)

    def _fill_p2(self, st) -> None:
        phase = phase_of(st)
        self.phase_lbl.configure(text=f"階段：{phase_label(st)}")
        mode = self.app.cfg.mode_of(self.pw.key)
        self.mode_cb.set(MODE_TEXT[mode])
        notes = []
        if mode == "off":
            notes.append("模式是「關閉」：助理不會看也不會操作這個視窗")
        if not self.app.is_running:
            notes.append("⏸ 已全部暫停：按主畫面上方的按鈕或 F8 繼續")
        if mode == "auto":
            notes.append(f"自動駕駛開著：代理停下來且你閒置 {self.app.cfg.ai_idle_sec:.0f} 秒後自動送出建議，"
                         f"每小時最多 {int(self.app.cfg.autopilot_max_per_hour)} 次；Allow 依危險規則與風險門檻代按")
        elif mode == "suggest":
            notes.append("只建議：助理寫出建議的訊息，送出要你按下方按鈕")
        if phase == "spec" and st.acceptance and not st.acceptance_confirmed_at:
            notes.append("驗收 spec 草稿已出來：確認後才會開始實作")
        self.ap_info.configure(text="\n".join(notes))
        if st.paused_reason:
            self.pause_lbl.configure(text=f"⏸ 已暫停：{st.paused_reason}")
            self.pause_row.pack(fill="x", after=self.ap_info)
        else:
            self.pause_row.pack_forget()
        self.acc_list.delete(0, "end")
        icon = {"verified": "✅", "claimed": "🟡", "todo": "⬜"}
        for a in st.acceptance:
            self.acc_list.insert("end", f"{icon.get(a.get('status', 'todo'), '⬜')} {a.get('text', '')}")
        if not st.acceptance:
            self.acc_list.insert("end", "（驗收條件尚未提出）")
        can_confirm = bool(st.acceptance) and not st.acceptance_confirmed_at
        self.confirm_btn.configure(state="normal" if can_confirm else "disabled")
        self.rollover_btn.configure(state="normal" if st.goal else "disabled")

    def resume_autopilot(self) -> None:
        def mutate(st) -> None:
            reason = st.paused_reason
            st.paused_reason = ""
            st.evidence_attempts = 0
            st.pending_human = [p for p in st.pending_human if p != reason]
            st.add_interaction("resume", "使用者恢復自動駕駛")

        self.sess.update(mutate)
        self.sess.history("resume", by="user")
        self.reload()

    def confirm_spec(self) -> None:
        def mutate(st) -> None:
            st.acceptance_confirmed_at = now_str()
            st.pending_human = [p for p in st.pending_human if "驗收 spec" not in p]
            st.spec_issues = []
            st.suggested_action, st.suggested_prompt = "next_task", (
                "驗收 spec 已確認。請建立功能分支，依驗收條件逐條實作；每完成一條就 commit 並回報 commit hash（不要 push）。")
            st.suggested_reason = "使用者已確認驗收 spec，開始實作"
            st.suggested_at = now_str()
            st.add_interaction("spec_confirmed", f"{len(st.acceptance)} 條")

        self.sess.update(mutate)
        self.sess.history("spec_confirmed", by="user")
        self.result_lbl.configure(text="已確認驗收 spec。下方是開始實作的訊息，可以修改後送出（自動駕駛開著會自動送出）。",
                                  fg="#34D399")
        self.reload()

    def rollover(self) -> None:
        self.result_lbl.configure(text="開新對話並送出接續摘要中…", fg=FG_DIM)
        self.app.worker.request_rollover(self.pw.key)

    def _fill_docs(self, st, candidates: List[Dict[str, Any]]) -> None:
        self.candidates = candidates
        self.docs_list.delete(0, "end")
        agent = set(st.agent_active_docs_proposal)
        for i, c in enumerate(candidates):
            tag = "計畫" if c["kind"] == "plan" else "spec"
            mark = " 🤖" if c["id"] in agent else ""
            self.docs_list.insert("end", f"[{tag}] {c['label']}{mark}")
            if c["id"] in st.active_docs:
                self.docs_list.selection_set(i)
        if not candidates:
            self.docs_info.configure(text="這個專案找不到 plan / progress 文件或 Kiro spec。")
        elif st.active_docs:
            who = "你勾選" if st.active_docs_by == "user" else "採用代理的判斷"
            self.docs_info.configure(text=f"已確認（{who}）：" + "、".join(st.active_docs))
        else:
            self.docs_info.configure(text="尚未確認。選取正在進行的項目後按「儲存勾選」，或按「讓代理判斷」。")
        if agent and set(st.active_docs) != agent and not self._docs_open:
            self._toggle_docs(True)
        if agent and set(st.active_docs) != agent:
            self.docs_info.configure(text=self.docs_info.cget("text") + "\n🤖 = 代理認為正在進行，可按「套用代理的判斷」")
            self.adopt_docs_btn.configure(state="normal")
        else:
            self.adopt_docs_btn.configure(state="disabled")

    def _set_docs(self, docs: List[str], by: str) -> None:
        def mutate(st) -> None:
            st.active_docs, st.active_docs_by = docs, (by if docs else "")
            st.pending_human = [p for p in st.pending_human if "正在進行的計畫" not in p]
            st.add_interaction("docs", f"{by}：" + ("、".join(docs) or "（清空）"))
            st.last_panel_hash = ""  # 讓驅動器依新的計畫重新產生建議

        self.sess.update(mutate)
        self.sess.history("docs", active_docs=docs, by=by)

    def save_docs(self) -> None:
        docs = [self.candidates[i]["id"] for i in self.docs_list.curselection() if i < len(self.candidates)]
        self._set_docs(docs, "user")
        self.result_lbl.configure(text=f"已儲存正在進行的計畫：{len(docs)} 份。", fg="#34D399")
        self.reload()

    def adopt_agent_docs(self) -> None:
        st = self.sess.load()
        ids = [c["id"] for c in self.candidates]
        for i in range(len(ids)):
            self.docs_list.selection_clear(i)
        for d in st.agent_active_docs_proposal:
            if d in ids:
                self.docs_list.selection_set(ids.index(d))
        self.result_lbl.configure(text="已帶入代理的判斷，確認無誤後按「儲存勾選」。", fg=FG_DIM)

    def ask_agent_docs(self) -> None:
        if not self.candidates:
            self.result_lbl.configure(text="沒有可判斷的文件。", fg="#F87171")
            return
        self.sug_text.delete("1.0", "end")
        self.sug_text.insert("1.0", build_docs_question(self.candidates))
        self.sug_text.edit_modified(True)
        self.result_lbl.configure(text="已把「請代理判斷哪些計畫正在進行」放進下方訊息，按「送出」即可。", fg=FG_DIM)

    def save_goal(self) -> None:
        goal = self.goal_text.get("1.0", "end").strip()

        def mutate(st) -> None:
            st.goal = goal
            st.goal_confirmed_at = now_str() if goal else ""
            if goal:
                st.pending_human = [p for p in st.pending_human if "最終目標" not in p]
                st.add_interaction("goal", goal)
                st.last_panel_hash = ""  # 讓驅動器依新目標重新產生建議

        self.sess.update(mutate)
        self.sess.history("goal", goal=goal, by="user")
        self.result_lbl.configure(text="目標已儲存。" if goal else "已清除目標。", fg="#34D399")
        self.reload()

    def adopt_agent_goal(self) -> None:
        st = self.sess.load()
        if st.agent_goal_proposal:
            self.goal_text.delete("1.0", "end")
            self.goal_text.insert("1.0", st.agent_goal_proposal)
            self.result_lbl.configure(text="已帶入代理說明的目標，可修改後按「儲存目標」。", fg=FG_DIM)

    def _toggle_docs(self, force: Optional[bool] = None) -> None:
        self._docs_open = (not self._docs_open) if force is None else force
        if self._docs_open:
            self.docs_box.pack(fill="x", pady=(0, 4), after=self.docs_toggle)
            self.docs_toggle.configure(text="▾ 進階：正在進行的計畫文件")
        else:
            self.docs_box.pack_forget()
            self.docs_toggle.configure(text="▸ 進階：正在進行的計畫文件")

    def click(self, name: str) -> None:
        self.result_lbl.configure(text=f"代按「{name}」中…（UIA，不動滑鼠）", fg=FG_DIM)
        self.app.worker.request_click(self.pw.key, name)

    def send(self) -> None:
        text = self.sug_text.get("1.0", "end").strip()
        if not text:
            self.result_lbl.configure(text="沒有要送出的內容。", fg="#F87171")
            return
        self.send_btn.configure(state="disabled")
        self.result_lbl.configure(text="送出中…（會短暫切換到 IDE 視窗輸入，再切回來）", fg=FG_DIM)
        self.app.worker.request_send(self.pw.key, text)

    def on_send_result(self, ok: bool, text: str) -> None:
        self.send_btn.configure(state="normal")
        self.result_lbl.configure(text=("✅ " if ok else "⛔ ") + text, fg="#34D399" if ok else "#F87171")
        if ok:
            self.reload()
