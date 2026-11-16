import json
import os
from dataclasses import dataclass, field, asdict, fields
from typing import List, Dict, Any

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# 運作模式：image = 截圖比對點擊（會動滑鼠）；uia = UIA 按鈕點擊（不動滑鼠）；ai = AI 代理（視覺模型 + Decision-1）
MODES = ("image", "uia", "ai")


def _default_targets() -> List[Dict[str, Any]]:
    return [
        {"name": "Submit (深色)", "path": "targets/submit_dark.png", "enabled": True},
        {"name": "Submit (淺色)", "path": "targets/submit_light.png", "enabled": True},
    ]


def _default_watch_apps() -> List[Dict[str, Any]]:
    # match 會比對「程式名稱 (exe)」或「視窗標題」，不分大小寫，包含即算符合
    return [
        {"name": "Kiro", "match": "Kiro.exe", "enabled": True},
        {"name": "Antigravity", "match": "Antigravity IDE.exe", "enabled": True},
    ]


def _default_uia_button_names() -> List[str]:
    # 與 targets/ 內既有截圖相同的按鈕；名稱比對時會忽略大小寫與 ↵ 等符號
    return ["Allow", "Always allow", "Submit", "允許", "一律允許", "提交"]


def _default_deny_patterns() -> List[str]:
    # 命中任一規則（不分大小寫的正規表示式）時，AI 模式一律轉交人工決定
    return [
        r"\brm\s+-[a-z]*r[a-z]*f",
        r"\brm\s+-[a-z]*f[a-z]*r",
        r"remove-item\b.*-recurse",
        r"\bdel\s+/[sq]",
        r"\brmdir\s+/s",
        r"\bformat\s+[a-z]:",
        r"git\s+push\b.*(--force|-f\b)",
        r"git\s+reset\s+--hard",
        r"git\s+clean\s+-[a-z]*f",
        r"\bdrop\s+(table|database|schema)\b",
        r"\btruncate\s+table\b",
        r"\bdelete\s+from\b",
        r"\bshutdown\b",
        r"\breg\s+delete\b",
        r"\bmkfs\b",
        r"\bdd\s+if=",
    ]


def _default_never_click() -> List[str]:
    # 視窗外框與常見的破壞性按鈕，任何模式都不會點
    return [
        "Close", "Minimize", "Maximize", "Restore", "Cancel", "Reject", "Deny", "Delete",
        "Stop", "Discard", "關閉", "取消", "拒絕", "刪除", "停止", "捨棄",
    ]


@dataclass
class AppConfig:
    # 共用：ai = AI 工程助理；image / uia = 退回基本按鈕精靈
    mode: str = "ai"
    basic_mode: str = "uia"
    # 使用者勾選、允許助理操作的工作階段（projects.session_key 的值）。新偵測到的視窗預設不勾選
    authorized_sessions: List[str] = field(default_factory=list)
    # 截圖比對模式
    scan_interval: float = 1.0
    threshold: float = 0.85
    multi_scale: bool = False
    show_region_borders: bool = True
    cooldown_sec: float = 0.8
    regions: List[Dict[str, Any]] = field(default_factory=list)
    targets: List[Dict[str, Any]] = field(default_factory=_default_targets)
    # 監看的 App（UIA / AI 模式共用）
    watch_apps: List[Dict[str, Any]] = field(default_factory=_default_watch_apps)
    never_click: List[str] = field(default_factory=_default_never_click)
    # UIA 按鈕模式
    uia_interval: float = 2.0
    uia_button_names: List[str] = field(default_factory=_default_uia_button_names)
    # AI 代理模式
    ai_interval: float = 10.0
    ai_dry_run: bool = True
    ai_min_confidence: float = 0.6
    ai_max_risk: float = 0.3
    ai_idle_sec: float = 3.0
    ai_recheck_sec: float = 60.0
    ai_continue_text: str = "繼續"
    ai_send_key: str = "enter"
    ai_allow_reply: bool = True
    # 選項題：由決策模型依 OCR 讀到的選項選出最佳的一個
    ai_auto_choose: bool = True
    # 多選一時機率會分散在各選項，門檻比一般動作低
    ai_choice_min_confidence: float = 0.5
    deny_patterns: List[str] = field(default_factory=_default_deny_patterns)
    # 驅動器（P1）：代理停下來時寫出建議的下一步，只建議、不自動送出
    driver_enabled: bool = True
    # 每日摘要產生時間（HH:MM）
    daily_summary_time: str = "18:00"

    @classmethod
    def load(cls, path: str = DEFAULT_CONFIG_PATH) -> "AppConfig":
        if not os.path.exists(path):
            cfg = cls()
            cfg.save(path)
            return cfg
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return cls()

        cfg = cls()
        for fdef in fields(cls):
            if fdef.name not in data:
                continue
            default_val = getattr(cfg, fdef.name)
            raw = data[fdef.name]
            try:
                if isinstance(default_val, bool):
                    val: Any = bool(raw)
                elif isinstance(default_val, float):
                    val = float(raw)
                elif isinstance(default_val, list):
                    val = list(raw)
                else:
                    val = str(raw)
            except (TypeError, ValueError):
                continue
            setattr(cfg, fdef.name, val)
        if cfg.mode not in MODES:
            cfg.mode = "ai"
        if cfg.basic_mode not in ("image", "uia"):
            cfg.basic_mode = "uia"
        return cfg

    def is_authorized(self, key: str) -> bool:
        return key in self.authorized_sessions

    def set_authorized(self, key: str, allowed: bool) -> None:
        if allowed and key not in self.authorized_sessions:
            self.authorized_sessions.append(key)
        elif not allowed and key in self.authorized_sessions:
            self.authorized_sessions.remove(key)

    def save(self, path: str = DEFAULT_CONFIG_PATH) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)
