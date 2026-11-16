"""辨識 Kiro / Antigravity 視窗開啟的是哪個專案。

資料來源：
- 視窗標題：Kiro 是「檔名 - 專案 - Kiro」；Antigravity 是「專案 - Antigravity IDE - 檔名」，
  遠端專案名稱後面會帶「[SSH: 主機]」。
- %APPDATA%/<App>/User/workspaceStorage/*/workspace.json：記錄開過的資料夾完整路徑，
  用專案名稱（資料夾名）對回完整路徑。
"""
import json
import os
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import win_utils as wu


@dataclass(frozen=True)
class AppSpec:
    key: str
    label: str
    exe: str
    storage_dir: str  # %APPDATA% 底下的資料夾名稱


APPS: Tuple[AppSpec, ...] = (
    AppSpec("kiro", "Kiro", "Kiro.exe", "Kiro"),
    AppSpec("antigravity", "Antigravity", "Antigravity IDE.exe", "Antigravity IDE"),
)
_BY_EXE = {a.exe.lower(): a for a in APPS}


@dataclass
class WorkspaceEntry:
    name: str  # 資料夾名稱
    path: str  # 本機為 Windows 路徑；遠端為 /home/...
    remote: str  # 遠端主機名稱，本機為空字串
    mtime: float


@dataclass
class ProjectWindow:
    hwnd: int
    app: str  # "kiro" / "antigravity"
    app_label: str
    title: str
    project: str  # 從標題取出的專案名稱；空字串代表沒開專案（例如歡迎頁）
    remote: str
    path: str  # 解析出的完整路徑；找不到時為空字串
    ambiguous: bool  # 同名專案有多個候選路徑
    iconic: bool

    @property
    def key(self) -> str:
        return session_key(self.app, self.project, self.path, self.remote)

    @property
    def location(self) -> str:
        if self.remote:
            return f"SSH: {self.remote}:{self.path or '?'}"
        return self.path or "（找不到路徑）"


ASSISTANT_DIR = os.path.normcase(os.path.dirname(os.path.abspath(__file__)))


def contains_assistant(path: str, remote: str = "") -> bool:
    """這個專案是不是 AI 工程助理自己（或在它的資料夾裡）。在這種視窗裡自動操作，會干擾控制助理的那個對話。"""
    if remote or not path:
        return False
    p = os.path.normcase(os.path.abspath(path))
    return p == ASSISTANT_DIR or p.startswith(ASSISTANT_DIR + os.sep)


def session_key(app: str, project: str, path: str, remote: str) -> str:
    """工作階段的穩定識別：App + 完整路徑（找不到路徑時退回專案名稱）。"""
    where = path or project
    if remote:
        where = f"ssh://{remote}{path or '/' + project}"
    return f"{app}|{where}".lower()


_SSH_SUFFIX = re.compile(r"\s*\[(?:SSH|WSL|Dev Container)[^\]]*?:\s*(?P<host>[^\]]+)\]\s*$", re.I)
_DIRTY = re.compile(r"^[●•\*]\s*")


def _split_remote(name: str) -> Tuple[str, str]:
    m = _SSH_SUFFIX.search(name)
    if not m:
        return name.strip(), ""
    return name[: m.start()].strip(), m.group("host").strip()


def parse_title(app: str, title: str) -> Tuple[str, str]:
    """回傳 (專案名稱, 遠端主機)。看不出專案時專案名稱為空字串。"""
    t = _DIRTY.sub("", (title or "").strip())
    if app == "kiro":
        parts = t.split(" - ")
        if len(parts) < 2 or parts[-1].strip() != "Kiro":
            return "", ""
        # 「檔名 - 專案 - Kiro」取倒數第二段；只有兩段時可能是「專案 - Kiro」或「Getting started - Kiro」，交給呼叫端比對
        return _split_remote(parts[-2])
    if app == "antigravity":
        idx = t.find(" - Antigravity IDE")
        if idx <= 0:
            return "", ""
        return _split_remote(t[:idx])
    return "", ""


def _decode_remote_authority(authority: str) -> str:
    """ssh-remote+<hex 編碼的 JSON 或主機名稱> → 主機名稱。"""
    raw = authority.split("+", 1)[1] if "+" in authority else authority
    if re.fullmatch(r"[0-9a-fA-F]+", raw) and len(raw) % 2 == 0:
        try:
            obj = json.loads(bytes.fromhex(raw).decode("utf-8"))
            return str(obj.get("hostName") or obj.get("host") or raw)
        except (ValueError, UnicodeDecodeError):
            pass
    return raw


def parse_folder_uri(uri: str) -> Optional[Tuple[str, str]]:
    """workspace.json 的 folder URI → (路徑, 遠端主機)。"""
    try:
        u = urllib.parse.urlparse(uri)
    except ValueError:
        return None
    path = urllib.parse.unquote(u.path or "")
    if u.scheme == "file":
        if re.match(r"^/[A-Za-z]:", path):
            path = path[1:]
        win = path.replace("/", "\\")
        if len(win) >= 2 and win[1] == ":":
            win = win[0].upper() + win[1:]
        return win, ""
    if u.scheme == "vscode-remote":
        return path, _decode_remote_authority(urllib.parse.unquote(u.netloc))
    return None


def load_workspaces(storage_dir: str, appdata: Optional[str] = None) -> List[WorkspaceEntry]:
    base = os.path.join(appdata or os.environ.get("APPDATA", ""), storage_dir, "User", "workspaceStorage")
    out: List[WorkspaceEntry] = []
    if not os.path.isdir(base):
        return out
    for d in os.listdir(base):
        f = os.path.join(base, d, "workspace.json")
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            mtime = os.path.getmtime(os.path.join(base, d))
        except (OSError, ValueError):
            continue
        uri = data.get("folder") if isinstance(data, dict) else None
        if not isinstance(uri, str):
            continue
        parsed = parse_folder_uri(uri)
        if not parsed:
            continue
        path, remote = parsed
        name = re.split(r"[\\/]", path.rstrip("\\/"))[-1] if path.strip("\\/") else ""
        if name:
            out.append(WorkspaceEntry(name, path, remote, mtime))
    out.sort(key=lambda e: e.mtime, reverse=True)
    return out


def resolve_path(project: str, remote: str, workspaces: List[WorkspaceEntry]) -> Tuple[str, bool]:
    """專案名稱 → (完整路徑, 是否有多個候選)。取最近使用的那個。"""
    if not project:
        return "", False
    cands = [w for w in workspaces if w.name.lower() == project.lower() and w.remote.lower() == remote.lower()]
    if not cands:
        return "", False
    distinct = {c.path.lower() for c in cands}
    return cands[0].path, len(distinct) > 1


class ProjectDetector:
    """列出所有 Kiro / Antigravity 視窗並解析專案。workspaceStorage 結果快取 30 秒。"""

    def __init__(self, cache_sec: float = 30.0) -> None:
        self.cache_sec = cache_sec
        self._cache: Dict[str, Tuple[float, List[WorkspaceEntry]]] = {}

    def _workspaces(self, spec: AppSpec) -> List[WorkspaceEntry]:
        now = time.monotonic()
        hit = self._cache.get(spec.key)
        if hit and now - hit[0] < self.cache_sec:
            return hit[1]
        ws = load_workspaces(spec.storage_dir)
        self._cache[spec.key] = (now, ws)
        return ws

    def describe(self, w: wu.WindowInfo) -> Optional[ProjectWindow]:
        spec = _BY_EXE.get(w.exe.lower())
        if spec is None:
            return None
        ws = self._workspaces(spec)
        project, remote = parse_title(spec.key, w.title)
        path, ambiguous = resolve_path(project, remote, ws)
        if project and not path:
            # 剛開的新專案：IDE 才剛寫入 workspaceStorage，快取還是舊的。立刻重讀（最多每 3 秒一次），
            # 否則前 30 秒會用專案名稱當識別，之後換成完整路徑，使用者在這段時間設定的模式會遺失
            hit = self._cache.get(spec.key)
            if hit and time.monotonic() - hit[0] > 3.0:
                self._cache.pop(spec.key, None)
                ws = self._workspaces(spec)
                path, ambiguous = resolve_path(project, remote, ws)
        if spec.key == "kiro" and project and not path and len(w.title.split(" - ")) == 2:
            # 「Getting started - Kiro」這類只有兩段、又對不到任何資料夾的標題，視為沒開專案
            project = ""
        return ProjectWindow(w.hwnd, spec.key, spec.label, w.title, project, remote, path, ambiguous, w.iconic)

    def detect(self) -> List[ProjectWindow]:
        out = []
        for w in wu.list_top_windows():
            pw = self.describe(w)
            if pw is not None:
                out.append(pw)
        return out

    def describe_hwnd(self, hwnd: int) -> Optional[ProjectWindow]:
        if not wu.is_window(hwnd):
            return None
        info = wu.WindowInfo(hwnd, wu.window_title(hwnd), wu.process_name(hwnd), wu.is_iconic(hwnd), wu.window_pid(hwnd))
        return self.describe(info)
