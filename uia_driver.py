"""UI Automation 操作：找按鈕並 Invoke（不動滑鼠）、找聊天輸入框、讀取聊天面板文字。

Chromium / Electron（Kiro、Antigravity、VS Code）預設不開放無障礙樹；第一次被 UIA 查詢後
才會開啟，所以剛啟動時可能只看得到視窗外框按鈕，下一輪輪巡就會正常。
"""
import difflib
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

import uiautomation as auto

TREE_SCOPE_DESCENDANTS = 4
_INPUT_CLASS_HINTS = ("prosemirror", "chat-input", "contenteditable", "interactive-input", "monaco-inputbox")
_INPUT_CLASS_EXCLUDE = ("native-edit-context", "xterm-helper-textarea", "inputarea")

Rect = Tuple[int, int, int, int]


@dataclass
class ButtonInfo:
    name: str
    rect: Rect
    control: object


def _iuia():
    return auto.uiautomation._AutomationClient.instance().IUIAutomation


def _find_all(root_control, property_id: int, value) -> List[object]:
    cond = _iuia().CreatePropertyCondition(property_id, value)
    arr = root_control.Element.FindAll(TREE_SCOPE_DESCENDANTS, cond)
    out = []
    for i in range(arr.Length):
        c = auto.Control.CreateControlFromElement(arr.GetElement(i))
        if c is not None:
            out.append(c)
    return out


def _rect(c) -> Rect:
    r = c.BoundingRectangle
    return (r.left, r.top, r.right, r.bottom)


def _visible(rect: Rect) -> bool:
    return rect[2] - rect[0] > 0 and rect[3] - rect[1] > 0


_TRAILING_HINT = re.compile(r"\s*[\(\[（][^\)\]）]*(ctrl|alt|shift|enter|⌘|↵)[^\)\]）]*[\)\]）]\s*$", re.I)
# 含 codicon 等圖示字型使用的私用區字元 (U+E000–U+F8FF)
_SYMBOLS = re.compile(r"[↵⏎⌘⇧⌃⌥➤▶→\ue000-\uf8ff]+")


def normalize_name(name: str) -> str:
    """'Submit ↵' / 'Run (Ctrl+Enter)' → 'submit' / 'run'。"""
    s = (name or "").strip()
    s = _TRAILING_HINT.sub("", s)
    s = _SYMBOLS.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s.lower()


def name_in(name: str, names: Iterable[str]) -> bool:
    n = normalize_name(name)
    return bool(n) and any(n == normalize_name(x) for x in names)


class UiaWindow:
    """包裝單一頂層視窗的 UIA 操作。"""

    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd
        self.control = auto.ControlFromHandle(hwnd)

    def window_rect(self) -> Rect:
        return _rect(self.control)

    def buttons(self) -> List[ButtonInfo]:
        out: List[ButtonInfo] = []
        for c in _find_all(self.control, auto.PropertyId.ControlTypeProperty, auto.ControlType.ButtonControl):
            try:
                name = c.Name or ""
                rect = _rect(c)
                if name and _visible(rect) and not c.IsOffscreen:
                    out.append(ButtonInfo(name, rect, c))
            except Exception:
                continue
        return out

    def chat_input(self) -> Optional[object]:
        best = None
        best_bottom = -1
        for c in _find_all(self.control, auto.PropertyId.IsKeyboardFocusableProperty, True):
            try:
                cls = (c.ClassName or "").lower()
                if any(x in cls for x in _INPUT_CLASS_EXCLUDE):
                    continue
                is_hint = any(h in cls for h in _INPUT_CLASS_HINTS)
                is_edit = c.ControlTypeName == "EditControl"
                if not (is_hint or is_edit):
                    continue
                rect = _rect(c)
                if not _visible(rect) or c.IsOffscreen:
                    continue
                score_bottom = rect[3] + (100000 if is_hint else 0)
                if score_bottom > best_bottom:
                    best, best_bottom = c, score_bottom
            except Exception:
                continue
        return best

    def panel_of(self, inner) -> Optional[object]:
        """從聊天輸入框往上找，第一個高度超過視窗 45% 的祖先當作聊天面板。"""
        win_h = max(1, self.window_rect()[3] - self.window_rect()[1])
        c = inner
        for _ in range(25):
            if c is None:
                return None
            r = _rect(c)
            if r[3] - r[1] >= 0.45 * win_h:
                return c
            c = c.GetParentControl()
        return None

    def panel_text(self, panel, max_chars: int = 4000) -> str:
        if panel is None:
            return ""
        parts: List[str] = []
        for c in _find_all(panel, auto.PropertyId.ControlTypeProperty, auto.ControlType.TextControl):
            try:
                t = (c.Name or "").strip()
                if t and _visible(_rect(c)):
                    parts.append(t)
            except Exception:
                continue
        text = "\n".join(parts)
        return text[-max_chars:]


def pick_button(buttons: List[ButtonInfo], wanted: str, panel_rect: Optional[Rect] = None,
                fuzzy: bool = False) -> Optional[ButtonInfo]:
    """依名稱挑按鈕；有多個同名時，優先在聊天面板內、位置最下方（通常是最新）的那個。

    fuzzy=True 時，找不到完全相同的名稱就退而求其次，接受相似度 ≥ 0.85 的按鈕
    （OCR 抄寫長選項按鈕時偶爾會差一兩個字）。
    """
    hits = [b for b in buttons if name_in(b.name, [wanted])]
    if not hits and fuzzy:
        target = normalize_name(wanted)
        scored = [(difflib.SequenceMatcher(None, normalize_name(b.name), target).ratio(), b) for b in buttons]
        best = max((s for s, _ in scored), default=0.0)
        if best >= 0.85:
            hits = [b for s, b in scored if s == best]
    if not hits:
        return None

    def inside(b: ButtonInfo) -> bool:
        if panel_rect is None:
            return False
        l, t, r, btm = panel_rect
        return l <= b.rect[0] and b.rect[2] <= r and t <= b.rect[1] and b.rect[3] <= btm

    hits.sort(key=lambda b: (inside(b), b.rect[3]), reverse=True)
    return hits[0]


APPROVAL_BUTTONS = ("Allow", "Always allow", "Deny", "Always deny", "Reject", "Run", "Accept", "Skip",
                    "允許", "一律允許", "拒絕", "執行")


def _inside(rect: Rect, panel_rect: Optional[Rect]) -> bool:
    if panel_rect is None:
        return True
    l, t, r, b = panel_rect
    return l <= rect[0] and rect[2] <= r and t <= rect[1] and rect[3] <= b


def approval_buttons(buttons: List[ButtonInfo], panel_rect: Optional[Rect] = None) -> List[str]:
    """聊天面板內「等你允許」類的按鈕名稱（依畫面由上到下）。"""
    hits = [b for b in buttons if name_in(b.name, APPROVAL_BUTTONS) and _inside(b.rect, panel_rect)]
    hits.sort(key=lambda b: (b.rect[1], b.rect[0]))
    seen, out = set(), []
    for b in hits:
        n = normalize_name(b.name)
        if n not in seen:
            seen.add(n)
            out.append(b.name)
    return out


def agent_busy_reason(buttons: List[ButtonInfo], panel_rect: Optional[Rect] = None) -> str:
    """代理現在不適合接新訊息的原因；空字串代表可以送。"""
    if approval_buttons(buttons, panel_rect):
        return "代理正在等你按允許（Allow）"
    if any(name_in(b.name, ["Cancel", "Stop", "取消", "停止"]) and _inside(b.rect, panel_rect) for b in buttons):
        return "代理還在工作中"
    return ""


def invoke(control) -> bool:
    try:
        pat = control.GetPattern(auto.PatternId.InvokePattern)
        if pat:
            pat.Invoke()
            return True
    except Exception:
        pass
    try:
        legacy = control.GetPattern(auto.PatternId.LegacyIAccessiblePattern)
        if legacy:
            legacy.DoDefaultAction()
            return True
    except Exception:
        pass
    return False


_SEND_NAMES = ("send", "submit", "送出", "傳送", "send message")
_NOT_SEND = ("add context", "attach", "model", "default", "agent", "context", "image", "mic", "voice")


def find_send_button(inp) -> Optional[ButtonInfo]:
    """聊天輸入框旁的送出按鈕。Kiro 的送出鈕只有圖示、沒有名稱，位在輸入框外框右上角。"""
    wrapper = inp
    for _ in range(4):
        try:
            wrapper = wrapper.GetParentControl()
        except Exception:
            return None
        if wrapper is None:
            return None
        cls = (wrapper.ClassName or "").lower()
        if "chat-input-wrapper" in cls or "input-wrapper" in cls or "session-view-input" in cls:
            break
    in_rect = _rect(inp)
    cands = []
    for c in _find_all(wrapper, auto.PropertyId.ControlTypeProperty, auto.ControlType.ButtonControl):
        try:
            if not c.IsEnabled or not c.GetPattern(auto.PatternId.InvokePattern):
                continue
            n = normalize_name(c.Name or "")
            r = _rect(c)
        except Exception:
            continue
        if not _visible(r) or any(x in n for x in _NOT_SEND):
            continue
        if n in _SEND_NAMES or (n == "" and r[0] >= in_rect[2] - 10):
            cands.append(ButtonInfo(c.Name or "", r, c))
    if not cands:
        return None
    cands.sort(key=lambda b: (normalize_name(b.name) not in _SEND_NAMES, -b.rect[2], b.rect[1]))
    return cands[0]


def same_element(a, b) -> bool:
    try:
        return bool(_iuia().CompareElements(a.Element, b.Element))
    except Exception:
        return False


def focus_is_within(control, max_up: int = 8) -> bool:
    """鍵盤焦點是否在 control 本身或它的子元件上（用來確認字會打進聊天框，而不是終端機或編輯器）。"""
    try:
        c = auto.GetFocusedControl()
    except Exception:
        return False
    for _ in range(max_up):
        if c is None:
            return False
        if same_element(c, control):
            return True
        try:
            c = c.GetParentControl()
        except Exception:
            return False
    return False


def set_focus(control) -> bool:
    try:
        return bool(control.SetFocus())
    except Exception:
        return False


def input_text(control) -> str:
    """輸入框目前的文字（空的輸入框回傳空字串，不回傳提示文字）。"""
    try:
        for c, _depth in auto.WalkControl(control, maxDepth=3):
            if "is-empty" in (c.ClassName or "") or "is-editor-empty" in (c.ClassName or ""):
                return ""
        vp = control.GetPattern(auto.PatternId.ValuePattern)
        if vp:
            return vp.Value or ""
        tp = control.GetPattern(auto.PatternId.TextPattern)
        if tp:
            return tp.DocumentRange.GetText(-1) or ""
    except Exception:
        pass
    return ""


def same_text(a: str, b: str) -> bool:
    """忽略空白與換行的逐字比對（ProseMirror 會把換行轉成段落）。"""
    return re.sub(r"\s+", "", a or "") == re.sub(r"\s+", "", b or "")


def input_has_draft(control) -> bool:
    """輸入框裡是否已有使用者打到一半的文字（有就不自動輸入，避免把草稿一起送出）。"""
    try:
        for c, _depth in auto.WalkControl(control, maxDepth=3):
            if "is-empty" in (c.ClassName or "") or "is-editor-empty" in (c.ClassName or ""):
                return False
        vp = control.GetPattern(auto.PatternId.ValuePattern)
        if vp:
            return bool((vp.Value or "").strip())
        tp = control.GetPattern(auto.PatternId.TextPattern)
        if tp:
            return bool((tp.DocumentRange.GetText(-1) or "").strip())
    except Exception:
        return True
    return False


def control_rect(control) -> Optional[Rect]:
    try:
        return _rect(control)
    except Exception:
        return None
