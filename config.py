import json
import os
from dataclasses import dataclass, field, asdict, fields
from typing import List, Dict, Any

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# 運作模式：image = 截圖比對點擊（會動滑鼠）；uia = UIA 按鈕點擊（不動滑鼠）；ai = AI 代理（視覺模型 + Decision-1）
MODES = ("image", "uia", "ai")
SESSION_MODES = ("off", "suggest", "auto")


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


_RETIRED_DENY_PATTERNS = (r"git\s+push\b", r"git\s+push\b.*(--force|-f\b)")


def _default_deny_patterns() -> List[str]:
    # 命中任一規則（不分大小寫的正規表示式）時，AI 模式一律轉交人工決定
    return [
        r"\brm\s+-[a-z]*r[a-z]*f",
        r"\brm\s+-[a-z]*f[a-z]*r",
        r"remove-item\b.*-recurse",
        r"\bdel\s+/[sq]",
        r"\brmdir\s+/s",
        r"\bformat\s+[a-z]:",
        # git：commit／push／merge 可以自動；刪除或覆寫歷史的才交給使用者
        r"git\s+push\b.*(--force|--force-with-lease|\s-f\b|--delete|\s-d\b|\s:\S)",
        r"git\s+reset\s+--hard",
        r"git\s+clean\s+-[a-z]*f",
        r"git\s+rm\b",
        r"git\s+branch\s+.*-[a-z]*[dD]\b",
        r"git\s+tag\s+.*-d\b",
        r"git\s+(checkout|restore)\s+.*--\s+\.",
        r"git\s+stash\s+(drop|clear)\b",
        r"git\s+filter-(branch|repo)\b",
        r"git\s+rebase\b",
        r"\bdrop\s+(table|database|schema)\b",
        r"\btruncate\s+table\b",
        r"\bdelete\s+from\b",
        r"\bshutdown\b",
        r"\breg\s+delete\b",
        r"\bmkfs\b",
        r"\bdd\s+if=",
        # 安裝或移除套件、改系統環境：一律交給使用者
        r"\b(pip|pip3|conda|mamba|uv)\s+(install|uninstall|remove)\b",
        r"\b(apt|apt-get|yum|dnf|zypper|brew|choco|winget|scoop)\s+(install|remove|uninstall|upgrade)\b",
        r"\bnpm\s+(i|install)\b.*(-g|--global)\b",
        r"\bInstall-(Module|Package)\b",
    ]


def _default_never_click() -> List[str]:
    # 視窗外框與常見的破壞性按鈕，任何模式都不會點
    return [
        "Close", "Minimize", "Maximize", "Restore", "Cancel", "Reject", "Deny", "Delete",
        "Stop", "Discard", "關閉", "取消", "拒絕", "刪除", "停止", "捨棄",
    ]


# 自主程度：同一組門檻一次調好。危險指令規則（deny_patterns）與「刪除／覆寫歷史一律交給你」在任何程度都不變。
AUTONOMY_PRESETS: Dict[str, Dict[str, Any]] = {
    "careful": {
        "label": "保守", "desc": "風險稍高就交給你；適合剛開始觀察助理的判斷",
        "values": {"ai_max_risk": 0.2, "ai_risk_review_max": 0.5, "ai_min_confidence": 0.7,
                   "ai_choice_min_confidence": 0.6, "autopilot_max_per_hour": 8.0, "evidence_max_attempts": 3.0},
    },
    "balanced": {
        "label": "平衡", "desc": "一般指令自動處理，灰色地帶找第二意見；預設值",
        "values": {"ai_max_risk": 0.3, "ai_risk_review_max": 0.7, "ai_min_confidence": 0.6,
                   "ai_choice_min_confidence": 0.5, "autopilot_max_per_hour": 12.0, "evidence_max_attempts": 3.0},
    },
    "bold": {
        "label": "積極", "desc": "盡量不停下來問你，一路推進；只有危險規則與高風險才交給你",
        "values": {"ai_max_risk": 0.45, "ai_risk_review_max": 0.85, "ai_min_confidence": 0.5,
                   "ai_choice_min_confidence": 0.4, "autopilot_max_per_hour": 20.0, "evidence_max_attempts": 4.0},
    },
}


@dataclass
class AppConfig:
    # 共用：ai = AI 工程助理；image / uia = 退回基本按鈕精靈
    mode: str = "ai"
    basic_mode: str = "uia"
    # 使用者勾選、允許助理操作的工作階段（projects.session_key 的值）。新偵測到的視窗預設不勾選
    authorized_sessions: List[str] = field(default_factory=list)
    # 每個工作階段的模式：off = 關閉、suggest = 只建議、auto = 自動駕駛。這是唯一的開關，authorized_sessions 跟著同步
    session_modes: Dict[str, str] = field(default_factory=dict)
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
    # 風險落在 ai_max_risk ~ ai_risk_review_max 之間時，先請判斷模型逐行審查指令（第二意見），兩者都說安全才代按
    ai_risk_review_max: float = 0.7
    # 鍵盤與滑鼠都停止這麼多秒後才會點擊或打字（讀畫面不受限）
    ai_idle_sec: float = 5.0
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
    # 「需要你決定」與每日摘要跳 Windows 通知
    notify_enabled: bool = True
    # UIA 讀得到足夠的面板文字時先不送截圖，看不出狀態才用視覺模型
    ai_text_first: bool = True
    # 網頁儀表板：只綁 127.0.0.1；連接埠 0 代表每次啟動隨機挑一個空的
    dashboard_enabled: bool = True
    dashboard_port: float = 0.0
    # P2 自動駕駛（各工作階段另外要打開「自動駕駛」才會生效）
    autopilot_max_per_hour: float = 12.0
    evidence_max_attempts: float = 3.0
    rollover_after_sends: float = 30.0
    # 背景輸入：先用 UIA ValuePattern 把訊息放進聊天框、再用 UIA 按送出鈕，完全不碰鍵盤與焦點，
    # 所以你正在打字也不用等。IDE 不接受時自動改用「等你停手 → 切到前景貼上」的舊方式。
    ai_background_input: bool = True
    # 目標綁定的對話不在畫面上時（你在 IDE 切到別的對話），自動駕駛改綁目前顯示的對話繼續推進，而不是停下來等
    follow_visible_chat: bool = False
    # 自主程度：careful／balanced／bold，一次調整風險門檻、信心度與每小時上限（之後仍可個別微調）
    autonomy_level: str = "balanced"
    # 網頁主控台（server.py）：固定連接埠，被占用時往後找空的；啟動後自動在預設瀏覽器打開
    web_port: float = 8765.0
    open_browser: bool = True
    # 多 CLI 辦公室：同時「運作中」的房間上限（其餘排隊）
    team_max_running: float = 4.0
    # 成本護欄：每日 Token 上限（0 表示不限制；超過上限自動暫停全部房間）
    team_daily_token_limit: int = 0
    # Claude／GLM 座位載入的外掛資料夾或 .zip（以分號分隔；找不到就略過）
    team_plugin_dirs: str = ""
    # Decision-1 派工：分數 = 適合度 − 成本權重 × 相對成本
    team_cost_weight: float = 0.3
    # 對話用量達到這個比例 → 存檔、開新對話
    team_context_rollover: float = 0.70
    # 舊助理工作文件相對路徑（分號分隔；發現就從 worktree 移除）
    team_legacy_doc_dirs: str = "docs/ai-team"
    # 額外要連結進 worktree 的 git 外資料目錄（分號分隔）
    team_env_dirs: str = ""

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
                elif isinstance(default_val, dict):
                    val = dict(raw)
                else:
                    val = str(raw)
            except (TypeError, ValueError):
                continue
            setattr(cfg, fdef.name, val)
        if cfg.mode not in MODES:
            cfg.mode = "ai"
        if cfg.basic_mode not in ("image", "uia"):
            cfg.basic_mode = "uia"
        if cfg.autonomy_level not in AUTONOMY_PRESETS and cfg.autonomy_level != "custom":
            cfg.autonomy_level = "balanced"
        # 多 CLI 辦公室：超出範圍（或 NaN）回到預設
        if not 1 <= cfg.team_max_running <= 16:
            cfg.team_max_running = 4.0
        cfg.team_max_running = float(int(cfg.team_max_running))
        if not 0.0 <= cfg.team_cost_weight <= 1.0:
            cfg.team_cost_weight = 0.3
        if not 0.3 <= cfg.team_context_rollover <= 0.95:
            cfg.team_context_rollover = 0.70
        # 改版拿掉的舊預設規則：一般 git push 改為可以自動（使用者 2026-11-16 決定）
        cfg.deny_patterns = [p for p in cfg.deny_patterns if p not in _RETIRED_DENY_PATTERNS]
        # 舊設定檔沒有的新規則補上（使用者自己加的規則不動）
        for p in _default_deny_patterns():
            if p not in cfg.deny_patterns:
                cfg.deny_patterns.append(p)
        return cfg

    def team_plugin_list(self) -> List[str]:
        return [p.strip() for p in (self.team_plugin_dirs or "").split(";") if p.strip()]

    def team_legacy_doc_list(self) -> List[str]:
        vals = [p.strip() for p in (self.team_legacy_doc_dirs or "").split(";") if p.strip()]
        return vals or ["docs/ai-team"]

    def team_env_dir_list(self) -> List[str]:
        return [p.strip() for p in (self.team_env_dirs or "").split(";") if p.strip()]

    def apply_autonomy(self, level: str) -> bool:
        """套用自主程度預設值；不認得的程度回傳 False、不改任何設定。"""
        preset = AUTONOMY_PRESETS.get(level)
        if preset is None:
            return False
        for k, v in preset["values"].items():
            setattr(self, k, v)
        self.autonomy_level = level
        return True

    def is_authorized(self, key: str) -> bool:
        return key in self.authorized_sessions

    def mode_of(self, key: str) -> str:
        m = self.session_modes.get(key)
        if m in SESSION_MODES:
            return m
        return "suggest" if key in self.authorized_sessions else "off"

    def set_mode(self, key: str, mode: str) -> None:
        if mode not in SESSION_MODES:
            mode = "off"
        self.session_modes[key] = mode
        self.set_authorized(key, mode != "off")

    def set_authorized(self, key: str, allowed: bool) -> None:
        if allowed and key not in self.authorized_sessions:
            self.authorized_sessions.append(key)
        elif not allowed and key in self.authorized_sessions:
            self.authorized_sessions.remove(key)

    def save(self, path: str = DEFAULT_CONFIG_PATH) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)
