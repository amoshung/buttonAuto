import os
import time
import tkinter as tk
from typing import List, Dict, Any, Tuple
import cv2
from PIL import Image, ImageTk

from config import AppConfig, DEFAULT_CONFIG_PATH
from clicker import ClickController
from scanner import ScreenScanner, read_image_unicode, write_image_unicode
from region_selector import MultiBoxSelector, RegionBorderOverlayManager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class FloatingOverlayApp:
    def __init__(self, root: tk.Tk, config_path: str = DEFAULT_CONFIG_PATH) -> None:
        self.root = root
        self.config_path = config_path
        self.cfg = AppConfig.load(self.config_path)

        self.scanner = ScreenScanner()
        self.clicker = ClickController()
        self.border_mgr = RegionBorderOverlayManager(self.root)

        self.is_running = False
        self.click_count = 0
        self.last_scan_time = 0.0
        self.is_collapsed = False
        self._drag_offset = (0, 0)
        self._thumb_refs: List[ImageTk.PhotoImage] = []

        self._setup_window()
        self._build_ui()
        self._reload_scanner_and_borders()
        self._schedule_loops()

    def _setup_window(self) -> None:
        self.root.title("⚡ 智慧按鈕精靈 (ButtonAuto)")
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.96)
        self.root.configure(bg="#1E293B")
        self.root.resizable(False, False)

        # Position near the user's current cursor / monitor so it's immediately visible on multi-monitor setups
        cx, cy = self.root.winfo_pointerxy()
        if cx <= 0 and cy <= 0:
            x_pos, y_pos = 120, 120
        else:
            x_pos = max(40, cx - 150)
            y_pos = max(40, cy - 60)
        self.root.geometry(f"320x540+{x_pos}+{y_pos}")
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _build_ui(self) -> None:
        # Custom Draggable Title Bar
        self.title_bar = tk.Frame(self.root, bg="#0F172A", height=34, cursor="fleur")
        self.title_bar.pack(fill="x", side="top")

        self.title_lbl = tk.Label(
            self.title_bar,
            text="⚡ 智慧按鈕精靈 (F8 啟停)",
            bg="#0F172A",
            fg="#F8FAFC",
            font=("Microsoft JhengHei UI", 10, "bold"),
        )
        self.title_lbl.pack(side="left", padx=10, pady=6)

        close_btn = tk.Button(
            self.title_bar,
            text="✕",
            bg="#0F172A",
            fg="#94A3B8",
            activebackground="#EF4444",
            activeforeground="#FFFFFF",
            bd=0,
            font=("Segoe UI", 10, "bold"),
            width=3,
            command=self._on_close,
        )
        close_btn.pack(side="right", padx=2)

        self.collapse_btn = tk.Button(
            self.title_bar,
            text="—",
            bg="#0F172A",
            fg="#94A3B8",
            activebackground="#334155",
            activeforeground="#FFFFFF",
            bd=0,
            font=("Segoe UI", 10, "bold"),
            width=3,
            command=self._toggle_collapse,
        )
        self.collapse_btn.pack(side="right", padx=2)

        for widget in (self.title_bar, self.title_lbl):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._on_drag)

        # Main Body Frame
        self.body = tk.Frame(self.root, bg="#1E293B", padx=10, pady=8)
        self.body.pack(fill="both", expand=True)

        # 1. Start / Stop Button & Status
        self.toggle_btn = tk.Button(
            self.body,
            text="▶ 啟動自動掃描 (F8)",
            bg="#10B981",
            fg="#FFFFFF",
            activebackground="#059669",
            activeforeground="#FFFFFF",
            font=("Microsoft JhengHei UI", 11, "bold"),
            bd=0,
            pady=8,
            cursor="hand2",
            command=self.toggle_running,
        )
        self.toggle_btn.pack(fill="x", pady=(0, 6))

        self.status_lbl = tk.Label(
            self.body,
            text="狀態：待機中 | 累計點擊：0 次",
            bg="#1E293B",
            fg="#CBD5E1",
            font=("Microsoft JhengHei UI", 9),
        )
        self.status_lbl.pack(fill="x", pady=(0, 8))

        # 2. Scan Speed & Threshold Controls
        speed_frame = tk.LabelFrame(
            self.body,
            text=" ⏱ 掃描速度與精準度 ",
            bg="#1E293B",
            fg="#38BDF8",
            font=("Microsoft JhengHei UI", 9, "bold"),
            padx=8,
            pady=6,
        )
        speed_frame.pack(fill="x", pady=(0, 8))

        row1 = tk.Frame(speed_frame, bg="#1E293B")
        row1.pack(fill="x", pady=2)
        tk.Label(row1, text="掃描間隔(秒):", bg="#1E293B", fg="#E2E8F0", font=("Microsoft JhengHei UI", 9)).pack(
            side="left"
        )

        self.interval_var = tk.StringVar(value=str(self.cfg.scan_interval))
        self.interval_entry = tk.Spinbox(
            row1,
            from_=0.1,
            to=10.0,
            increment=0.1,
            textvariable=self.interval_var,
            width=6,
            bg="#0F172A",
            fg="#FFFFFF",
            insertbackground="#FFFFFF",
            bd=1,
            font=("Consolas", 10),
            command=self._on_params_changed,
        )
        self.interval_entry.pack(side="left", padx=6)
        self.interval_entry.bind("<Return>", lambda _e: self._on_params_changed())
        self.interval_entry.bind("<FocusOut>", lambda _e: self._on_params_changed())

        for sec in (0.2, 0.5, 1.0, 2.0):
            b = tk.Button(
                row1,
                text=f"{sec}s",
                bg="#334155",
                fg="#E2E8F0",
                bd=0,
                padx=4,
                font=("Consolas", 8),
                command=lambda s=sec: self._set_interval(s),
            )
            b.pack(side="left", padx=2)

        row2 = tk.Frame(speed_frame, bg="#1E293B")
        row2.pack(fill="x", pady=(4, 0))
        self.thresh_lbl = tk.Label(
            row2,
            text=f"相似度門檻: {int(self.cfg.threshold * 100)}%",
            bg="#1E293B",
            fg="#E2E8F0",
            font=("Microsoft JhengHei UI", 9),
        )
        self.thresh_lbl.pack(side="left")

        self.thresh_scale = tk.Scale(
            row2,
            from_=70,
            to=98,
            orient="horizontal",
            bg="#1E293B",
            fg="#E2E8F0",
            highlightthickness=0,
            troughcolor="#0F172A",
            showvalue=False,
            length=110,
            command=self._on_thresh_changed,
        )
        self.thresh_scale.set(int(self.cfg.threshold * 100))
        self.thresh_scale.pack(side="right")

        # 3. Multi-Region Controls
        reg_frame = tk.LabelFrame(
            self.body,
            text=" 📐 掃描螢幕區域 (可多框) ",
            bg="#1E293B",
            fg="#34D399",
            font=("Microsoft JhengHei UI", 9, "bold"),
            padx=8,
            pady=6,
        )
        reg_frame.pack(fill="x", pady=(0, 8))

        reg_btn_row = tk.Frame(reg_frame, bg="#1E293B")
        reg_btn_row.pack(fill="x", pady=(0, 4))

        tk.Button(
            reg_btn_row,
            text="＋ 拉框新增區域 (可多框)",
            bg="#0284C7",
            fg="#FFFFFF",
            bd=0,
            padx=6,
            pady=3,
            font=("Microsoft JhengHei UI", 9, "bold"),
            cursor="hand2",
            command=self._start_select_regions,
        ).pack(side="left")

        tk.Button(
            reg_btn_row,
            text="清空(全螢幕)",
            bg="#475569",
            fg="#FFFFFF",
            bd=0,
            padx=6,
            pady=3,
            font=("Microsoft JhengHei UI", 8),
            cursor="hand2",
            command=self._clear_regions,
        ).pack(side="right")

        self.show_border_var = tk.BooleanVar(value=self.cfg.show_region_borders)
        tk.Checkbutton(
            reg_frame,
            text="在螢幕上顯示綠色掃描框 (滑鼠可穿透)",
            variable=self.show_border_var,
            bg="#1E293B",
            fg="#CBD5E1",
            selectcolor="#0F172A",
            activebackground="#1E293B",
            activeforeground="#FFFFFF",
            font=("Microsoft JhengHei UI", 8),
            command=self._on_border_toggle,
        ).pack(anchor="w")

        self.regions_list_frame = tk.Frame(reg_frame, bg="#0F172A", pady=2)
        self.regions_list_frame.pack(fill="x", pady=(4, 0))

        # 4. Multi-Target Buttons Controls
        tgt_frame = tk.LabelFrame(
            self.body,
            text=" 🎯 目標按鈕清單 (看到即點) ",
            bg="#1E293B",
            fg="#FBBF24",
            font=("Microsoft JhengHei UI", 9, "bold"),
            padx=8,
            pady=6,
        )
        tgt_frame.pack(fill="both", expand=True)

        tgt_top_row = tk.Frame(tgt_frame, bg="#1E293B")
        tgt_top_row.pack(fill="x", pady=(0, 4))

        tk.Button(
            tgt_top_row,
            text="＋ 截圖新增目標按鈕 (可多框)",
            bg="#D97706",
            fg="#FFFFFF",
            bd=0,
            padx=8,
            pady=3,
            font=("Microsoft JhengHei UI", 9, "bold"),
            cursor="hand2",
            command=self._start_capture_targets,
        ).pack(side="left", fill="x", expand=True)

        self.targets_list_frame = tk.Frame(tgt_frame, bg="#0F172A", pady=4)
        self.targets_list_frame.pack(fill="both", expand=True)

        self._refresh_regions_ui()
        self._refresh_targets_ui()

    def _refresh_regions_ui(self) -> None:
        for w in self.regions_list_frame.winfo_children():
            w.destroy()

        if not self.cfg.regions:
            tk.Label(
                self.regions_list_frame,
                text="🌐 目前為【全螢幕掃描】模式",
                bg="#0F172A",
                fg="#94A3B8",
                font=("Microsoft JhengHei UI", 8),
                pady=4,
            ).pack(fill="x")
            return

        for idx, reg in enumerate(self.cfg.regions):
            row = tk.Frame(self.regions_list_frame, bg="#0F172A")
            row.pack(fill="x", padx=4, pady=1)

            var = tk.BooleanVar(value=reg.get("enabled", True))
            cb = tk.Checkbutton(
                row,
                text=f"區域 #{idx + 1}: ({reg['x']},{reg['y']}) {reg['w']}x{reg['h']}",
                variable=var,
                bg="#0F172A",
                fg="#E2E8F0",
                selectcolor="#1E293B",
                activebackground="#0F172A",
                font=("Consolas", 8),
                command=lambda i=idx, v=var: self._toggle_region_item(i, v.get()),
            )
            cb.pack(side="left")

            del_btn = tk.Button(
                row,
                text="✕",
                bg="#0F172A",
                fg="#EF4444",
                bd=0,
                font=("Segoe UI", 8, "bold"),
                command=lambda i=idx: self._delete_region_item(i),
            )
            del_btn.pack(side="right")

    def _refresh_targets_ui(self) -> None:
        for w in self.targets_list_frame.winfo_children():
            w.destroy()
        self._thumb_refs.clear()

        if not self.cfg.targets:
            tk.Label(
                self.targets_list_frame,
                text="尚無目標按鈕，請點擊上方按鈕截圖新增",
                bg="#0F172A",
                fg="#94A3B8",
                font=("Microsoft JhengHei UI", 8),
                pady=6,
            ).pack(fill="x")
            return

        for idx, tgt in enumerate(self.cfg.targets):
            row = tk.Frame(self.targets_list_frame, bg="#0F172A")
            row.pack(fill="x", padx=4, pady=2)

            var = tk.BooleanVar(value=tgt.get("enabled", True))
            cb = tk.Checkbutton(
                row,
                variable=var,
                bg="#0F172A",
                selectcolor="#1E293B",
                activebackground="#0F172A",
                command=lambda i=idx, v=var: self._toggle_target_item(i, v.get()),
            )
            cb.pack(side="left")

            full_path = (
                tgt["path"] if os.path.isabs(tgt["path"]) else os.path.join(BASE_DIR, tgt["path"])
            )
            img_bgr = read_image_unicode(full_path)
            if img_bgr is not None:
                img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
                pil_img = Image.fromarray(img_rgb)
                pil_img.thumbnail((68, 24))
                tk_img = ImageTk.PhotoImage(pil_img)
                self._thumb_refs.append(tk_img)
                img_lbl = tk.Label(row, image=tk_img, bg="#0F172A")
                img_lbl.pack(side="left", padx=4)

            name_lbl = tk.Label(
                row,
                text=tgt.get("name", f"目標 #{idx + 1}"),
                bg="#0F172A",
                fg="#E2E8F0",
                font=("Microsoft JhengHei UI", 8),
            )
            name_lbl.pack(side="left", padx=2)

            del_btn = tk.Button(
                row,
                text="✕",
                bg="#0F172A",
                fg="#EF4444",
                bd=0,
                font=("Segoe UI", 8, "bold"),
                command=lambda i=idx: self._delete_target_item(i),
            )
            del_btn.pack(side="right")

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

        MultiBoxSelector(
            self.root,
            mode_title="新增掃描區域框 (可連續拉多個框)",
            box_color="#10B981",
            on_complete=on_done,
        )

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
                    abs_path = os.path.join(BASE_DIR, rel_path)
                    if write_image_unicode(abs_path, crop):
                        self.cfg.targets.append(
                            {
                                "name": f"自訂按鈕 #{len(self.cfg.targets) + 1}",
                                "path": rel_path,
                                "enabled": True,
                            }
                        )
                self.cfg.save(self.config_path)
                self._refresh_targets_ui()
                self.scanner.load_templates(self.cfg.targets, BASE_DIR)
            self.border_mgr.update_borders(self.cfg.regions, self.cfg.show_region_borders)
            if resume_after:
                self.toggle_running()

        MultiBoxSelector(
            self.root,
            mode_title="框選要自動點擊的目標按鈕 (可一次框多個按鈕)",
            box_color="#F59E0B",
            on_complete=on_done,
        )

    def _reload_scanner_and_borders(self) -> None:
        self.scanner.load_templates(self.cfg.targets, BASE_DIR)
        self.border_mgr.update_borders(self.cfg.regions, self.cfg.show_region_borders)

    def toggle_running(self) -> None:
        self.is_running = not self.is_running
        if self.is_running:
            self.toggle_btn.configure(
                text="⏸ 停止自動掃描 (F8)", bg="#EF4444", activebackground="#DC2626"
            )
            self.status_lbl.configure(
                text=f"狀態：🟢 掃描中... | 累計點擊：{self.click_count} 次", fg="#34D399"
            )
            self.last_scan_time = 0.0
        else:
            self.toggle_btn.configure(
                text="▶ 啟動自動掃描 (F8)", bg="#10B981", activebackground="#059669"
            )
            self.status_lbl.configure(
                text=f"狀態：待機中 | 累計點擊：{self.click_count} 次", fg="#CBD5E1"
            )

    def _get_self_rect(self) -> Tuple[int, int, int, int]:
        return (
            self.root.winfo_rootx(),
            self.root.winfo_rooty(),
            self.root.winfo_width(),
            self.root.winfo_height(),
        )

    def _schedule_loops(self) -> None:
        self._tick()

    def _tick(self) -> None:
        if self.clicker.poll_f8_toggled():
            self.toggle_running()

        now = time.monotonic()
        if self.is_running and (now - self.last_scan_time) >= self.cfg.scan_interval:
            self.last_scan_time = now
            matches = self.scanner.scan_screen(
                regions=self.cfg.regions,
                threshold=self.cfg.threshold,
                multi_scale=self.cfg.multi_scale,
                exclude_rect=self._get_self_rect(),
            )
            if matches:
                best = matches[0]
                if self.clicker.click_and_restore(best.x, best.y, cooldown_sec=self.cfg.cooldown_sec):
                    self.click_count += 1
                    self.status_lbl.configure(
                        text=f"⚡ 已點擊 [{best.target_name}] ({best.x},{best.y}) | 累計：{self.click_count}"
                    )

        self.root.after(40, self._tick)

    def _toggle_collapse(self) -> None:
        self.is_collapsed = not self.is_collapsed
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        if self.is_collapsed:
            self.body.pack_forget()
            self.collapse_btn.configure(text="▢")
            self.root.geometry(f"310x34+{x}+{y}")
        else:
            self.body.pack(fill="both", expand=True)
            self.collapse_btn.configure(text="—")
            self.root.geometry(f"310x520+{x}+{y}")

    def _start_drag(self, event: tk.Event) -> None:
        self._drag_offset = (event.x, event.y)

    def _on_drag(self, event: tk.Event) -> None:
        nx = self.root.winfo_x() + event.x - self._drag_offset[0]
        ny = self.root.winfo_y() + event.y - self._drag_offset[1]
        self.root.geometry(f"+{nx}+{ny}")

    def _on_close(self) -> None:
        self.border_mgr.clear()
        self.cfg.save(self.config_path)
        self.root.destroy()

