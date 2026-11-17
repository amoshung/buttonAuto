"""UI Automation 操作：找按鈕並 Invoke（不動滑鼠）、找聊天輸入框、讀取聊天面板文字。

Chromium / Electron（Kiro、Antigravity、VS Code）預設不開放無障礙樹；第一次被 UIA 查詢後
才會開啟，所以剛啟動時可能只看得到視窗外框按鈕，下一輪輪巡就會正常。
"""
import difflib
import re
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

import uiautomation as auto

TREE_SCOPE_DESCENDANTS = 4
_INPUT_CLASS_HINTS = ("prosemirror", "chat-input", "contenteditable", "interactive-input", "monaco-inputbox")
_INPUT_CLASS_EXCLUDE = ("native-edit-context", "xterm-helper-textarea", "inputarea")
# 已知的聊天輸入框名稱（Antigravity 為「Message input」）
_INPUT_NAMES = ("message input", "chat input", "ask anything")
# 這些名稱的輸入框是 IDE 內建的搜尋／篩選框，不是聊天框
_INPUT_NAME_EXCLUDE = ("filter", "search", "find", "replace", "篩選", "搜尋")

Rect = Tuple[int, int, int, int]


@dataclass
class ButtonInfo:
    name: str
    rect: Rect
    control: object
    # Antigravity 的提問卡片：選項是單選鈕，要先選取再按 Submit；submit 不是 None 時代表這是那種虛擬按鈕
    submit: Optional[object] = None


def ask_option_name(raw: str) -> str:
    """Antigravity 提問卡片的選項原文（去掉前面的序號與語音備忘按鈕的字）。

    選項代表什麼意思（只允許一次、永久允許、拒絕…）交給主權 AI 看畫面判斷，這裡不做關鍵字對應。
    """
    t = re.sub(r"^\s*\d+\s+", "", raw or "").strip()
    return re.sub(r"\s*Record voice memo$", "", t).strip()


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
        r = _rect(self.control)
        if r[2] - r[0] <= 0 or r[3] - r[1] <= 0:
            # 有些視窗（例如 Antigravity）UIA 回報的大小是 0，改用 Win32
            import win_utils as _wu
            r = _wu.get_window_rect(self.hwnd)
        return r

    def chat_title(self, app: str) -> str:
        """目前顯示的那個聊天對話的標題；讀不到時回傳空字串。

        Kiro：聊天分頁（class 為 tab-bar-item）中被選取的那個。
        Antigravity：聊天區頂端、「New Conversation」按鈕同一列的標題文字。
        """
        try:
            if app == "kiro":
                for c in _find_all(self.control, auto.PropertyId.ControlTypeProperty, auto.ControlType.TabItemControl):
                    if "tab-bar-item" not in (c.ClassName or ""):
                        continue
                    try:
                        if c.GetPattern(auto.PatternId.SelectionItemPattern).IsSelected:
                            name = re.split(r"\s+Close\s+", c.Name or "", maxsplit=1)[0].strip()
                            return _TAB_STATUS.sub("", name).strip()
                    except Exception:
                        continue
                return ""
            if app == "antigravity":
                bs = self.buttons()
                nc = pick_button(bs, "New Conversation")
                if nc is None:
                    return ""
                left = max([b.rect[2] for b in bs if abs(b.rect[1] - nc.rect[1]) < 12 and b.rect[2] <= nc.rect[0]
                            and name_in(b.name, ["Toggle Sidebar", "Search Conversations"])] or [nc.rect[0] - 500])
                best = ""
                for c in _find_all(self.control, auto.PropertyId.ControlTypeProperty, auto.ControlType.TextControl):
                    r = _rect(c)
                    t = (c.Name or "").strip()
                    if t and abs(r[1] - nc.rect[1]) < 15 and left <= r[0] < nc.rect[0] and not _SYMBOLS.fullmatch(t):
                        if len(t) > len(best):
                            best = t
                return best
        except Exception:
            return ""
        return ""

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
        return out + self._ask_options(out)

    def _ask_options(self, buttons: List[ButtonInfo]) -> List[ButtonInfo]:
        """Antigravity 的提問／允許卡片：每個單選選項包成一個虛擬按鈕（按下＝選取該選項再按 Submit）。"""
        submits = [b for b in buttons if name_in(b.name, ["Submit"])]
        if not submits:
            return []
        out: List[ButtonInfo] = []
        seen = set()
        for c in _find_all(self.control, auto.PropertyId.ControlTypeProperty, auto.ControlType.RadioButtonControl):
            try:
                if not (c.AutomationId or "").startswith("ask-opt-"):
                    continue
                row = c.GetParentControl()
                rect = _rect(row) if row is not None else _rect(c)
                if not _visible(rect):
                    continue
                name = ask_option_name(c.Name or "")
                if not name or name in seen:
                    continue
                # 同一張卡片的 Submit：在選項下方、最近的那個
                below = [s for s in submits if s.rect[1] >= rect[1]]
                if not below:
                    continue
                sub = min(below, key=lambda s: s.rect[1] - rect[1])
                seen.add(name)
                out.append(ButtonInfo(name, rect, c, submit=sub.control))
            except Exception:
                continue
        return out

    def chat_input(self) -> Optional[object]:
        best = None
        best_bottom = -1
        for c in _find_all(self.control, auto.PropertyId.IsKeyboardFocusableProperty, True):
            try:
                cls = (c.ClassName or "").lower()
                name = (c.Name or "").strip().lower()
                if any(x in cls for x in _INPUT_CLASS_EXCLUDE) or any(name.startswith(x) for x in _INPUT_NAME_EXCLUDE):
                    continue
                is_hint = any(h in cls for h in _INPUT_CLASS_HINTS) or any(name.startswith(x) for x in _INPUT_NAMES)
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
        """聊天面板的文字，依畫面順序。

        Antigravity 會把一個段落拆成很多文字片段（每段行內程式碼各一個元件），所以同一個段落或列表項目內的
        片段接在同一行，遇到新的區塊元件才換行。
        """
        if panel is None:
            return ""
        lines: List[str] = []
        cur: List[str] = []
        text_depth = None  # 正在處理的文字元件深度；比它深的元件都屬於同一段文字

        def flush() -> None:
            line = re.sub(r"[ \t]+", " ", "".join(cur)).strip()
            if line:
                lines.append(line)
            cur.clear()

        try:
            for c, depth in auto.WalkControl(panel, maxDepth=80):
                ct = c.ControlTypeName
                if ct == "TextControl":
                    if text_depth is None or depth <= text_depth:
                        text_depth = depth
                    name = c.Name or ""
                    if name:
                        cur.append(name)
                    continue
                if text_depth is not None and depth > text_depth:
                    continue
                text_depth = None
                if ct in ("ButtonControl", "ImageControl", "HyperlinkControl"):
                    continue
                flush()
        except Exception:
            pass
        flush()
        return "\n".join(lines)[-max_chars:]


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


APPROVAL_BUTTONS = ("Allow", "Always allow", "Always allow in this conversation", "Deny", "Always deny", "Reject", "Run", "Accept", "Skip",
                    "允許", "一律允許", "拒絕", "執行")


def _inside(rect: Rect, panel_rect: Optional[Rect]) -> bool:
    if panel_rect is None:
        return True
    l, t, r, b = panel_rect
    return l <= rect[0] and rect[2] <= r and t <= rect[1] and rect[3] <= b


def approval_buttons(buttons: List[ButtonInfo], panel_rect: Optional[Rect] = None) -> List[str]:
    """聊天面板內「等你允許」類的按鈕名稱（依畫面由上到下）。"""
    # 提問卡片的選項（submit 不是 None）一律列入：代理停在卡片上等你回答，選哪個由主權 AI 判斷
    hits = [b for b in buttons if (b.submit is not None or name_in(b.name, APPROVAL_BUTTONS))
            and _inside(b.rect, panel_rect)]
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


def press(b: ButtonInfo) -> bool:
    """按下按鈕；Antigravity 提問卡片的選項是先選取單選鈕、確認選到了，再按同一張卡片的 Submit。"""
    if b.submit is None:
        return invoke(b.control)
    try:
        sel = b.control.GetPattern(auto.PatternId.SelectionItemPattern)
        if sel is None:
            return False
        sel.Select()
        time.sleep(0.2)
        if not sel.IsSelected:
            return False
    except Exception:
        return False
    return invoke(b.submit)


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
_SEND_CLASS_HINTS = ("send-button", "send_button", "sendbutton")
_NOT_SEND = ("add context", "attach", "model", "default", "agent", "context", "image", "mic", "voice")


def find_send_button(inp) -> Optional[ButtonInfo]:
    """聊天輸入框旁的送出按鈕。Kiro 的送出鈕只有圖示、沒有名稱，位在輸入框外框右上角。"""
    wrapper = inp
    for _ in range(6):
        try:
            wrapper = wrapper.GetParentControl()
        except Exception:
            return None
        if wrapper is None:
            return None
        cls = (wrapper.ClassName or "").lower()
        if any(x in cls for x in ("chat-input-wrapper", "input-wrapper", "session-view-input", "composer-card")):
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
        cls = (c.ClassName or "").lower()
        if n in _SEND_NAMES or any(x in cls for x in _SEND_CLASS_HINTS) or (n == "" and r[0] >= in_rect[2] - 10):
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


# Kiro 會在對話分頁名稱前加上狀態字，比對對話時要去掉
_TAB_STATUS = re.compile(r"^(Working|Needs your input|Needs input|Done|Error|Failed|Paused|Waiting)\s+", re.I)

_PLACEHOLDERS = ("ask a question or describe a task", "ask anything, @ to mention")


def input_text(control) -> str:
    """輸入框目前的文字（空的輸入框回傳空字串，不回傳提示文字）。"""
    try:
        for c, _depth in auto.WalkControl(control, maxDepth=3):
            if "is-empty" in (c.ClassName or "") or "is-editor-empty" in (c.ClassName or ""):
                return ""
        vp = control.GetPattern(auto.PatternId.ValuePattern)
        if vp:
            v = vp.Value or ""
            return "" if v.strip().lower().startswith(_PLACEHOLDERS) else v
        tp = control.GetPattern(auto.PatternId.TextPattern)
        if tp:
            return tp.DocumentRange.GetText(-1) or ""
    except Exception:
        pass
    return ""


_FORMAT_CHARS = re.compile(r"[\s`*_#>：:]+")


def same_text(a: str, b: str) -> bool:
    """逐字比對，但忽略空白、換行與 Markdown 格式符號。

    Kiro 的輸入框會把 `程式碼` 轉成程式碼樣式（反引號消失）、把檔案路徑轉成標籤（前面的冒號被吃掉）；
    這些不影響訊息內容。要抓的是輸入法打亂順序、漏字這類真正的錯誤。
    """
    return _FORMAT_CHARS.sub("", a or "") == _FORMAT_CHARS.sub("", b or "")


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


def set_value(control, text: str) -> bool:
    """不搶焦點、不用鍵盤：用 UIA ValuePattern 直接把文字放進輸入框（背景輸入）。

    輸入框不支援 ValuePattern 或是唯讀時回傳 False，呼叫端要改用一般的「切到前景＋貼上」。
    Chromium 對 contenteditable（Kiro 的 ProseMirror）實作 SetValue 時會改寫 DOM，編輯器再從 DOM 變化讀回內容；
    結果是否真的被編輯器接受，要由呼叫端用 input_text／送出鈕是否可按來核對，不能只看這裡的回傳值。
    """
    try:
        vp = control.GetPattern(auto.PatternId.ValuePattern)
        if not vp or bool(getattr(vp, "IsReadOnly", False)):
            return False
        vp.SetValue(text)
        return True
    except Exception:
        return False


def control_rect(control) -> Optional[Rect]:
    try:
        return _rect(control)
    except Exception:
        return None
