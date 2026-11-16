"""工作階段資料：每個「App＋專案」在助理資料夾有自己的目錄。

sessions/<App>__<專案>/
  meta.json      這個目錄屬於哪個工作階段（key、路徑）
  state.json     資料來源（目標、驗收條件、進度、建議 prompt…）
  progress.md    由 state.json 產生的可讀版本（請用面板修改目標，不要手改）
  history.jsonl  每一筆觀察、建議、操作的完整紀錄
"""
import datetime
import hashlib
import json
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
REPORTS_DIR = os.path.join(BASE_DIR, "reports")
MAX_INTERACTIONS = 20


def now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe(name: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")
    return s[:60] or "unnamed"


@dataclass
class SessionState:
    key: str = ""
    app: str = ""
    project: str = ""
    path: str = ""
    remote: str = ""
    # 目標：由使用者訂定；agent_goal_proposal 是代理回答「最終目標」後擷取的內容，需使用者採用
    goal: str = ""
    goal_confirmed_at: str = ""
    agent_goal_proposal: str = ""
    goal_question_sent_at: str = ""
    goal_inferred_at: str = ""   # 助理已依計畫文件推測過目標（只推測一次，使用者採用或改寫後就不再用）
    current_task: str = ""       # 代理目前正在做的任務（推測），顯示在詳情與清單上
    # 正在進行的計畫文件（專案內相對路徑；spec 用 .kiro/specs/<名稱>）。由使用者勾選，或採用代理的判斷
    active_docs: List[str] = field(default_factory=list)
    active_docs_by: str = ""  # user / agent
    agent_active_docs_proposal: List[str] = field(default_factory=list)
    docs_question_sent_at: str = ""
    # 驗收條件（P2 與代理討論定稿）：[{"text", "how", "status": todo|claimed|verified, "evidence"}]
    acceptance: List[Dict[str, str]] = field(default_factory=list)
    acceptance_confirmed_at: str = ""
    # 進度
    agent_state: str = ""
    status_summary: str = ""
    claims_unverified: List[str] = field(default_factory=list)
    evidence: List[Dict[str, str]] = field(default_factory=list)
    commits: List[Dict[str, str]] = field(default_factory=list)
    # 下一步（P1 只建議、不自動送出）
    suggested_action: str = ""
    suggested_prompt: str = ""
    suggested_reason: str = ""
    suggested_at: str = ""
    last_panel_hash: str = ""
    pending_human: List[str] = field(default_factory=list)
    # 代理正在等使用者按 Allow 時的指令內容與可按的按鈕
    pending_approval: str = ""
    approval_buttons: List[str] = field(default_factory=list)
    interactions: List[Dict[str, str]] = field(default_factory=list)
    updated_at: str = ""
    # ---- P2：自動駕駛與階段
    autopilot: bool = False           # 這個工作階段允許自動送出與依規則代按
    paused_reason: str = ""           # 自動駕駛被暫停的原因（要不到證據、原地打轉等）
    auto_send_times: List[str] = field(default_factory=list)
    # 驗收 spec 討論
    spec_requested_at: str = ""
    spec_round: int = 0
    spec_issues: List[str] = field(default_factory=list)
    # 證據與測試報告
    evidence_requested_at: str = ""
    evidence_attempts: int = 0
    report_path: str = ""
    mutation_seen_at: str = ""
    report_wait_hash: str = ""  # 報告還沒寫完時上一輪看到的內容指紋（連續兩輪不變才查核）  # 曾經在報告中看到「故意弄壞」的結果（之後的補正報告不必重做）
    last_issues: List[str] = field(default_factory=list)
    done_at: str = ""
    done_hash: str = ""      # 達成目標當下的聊天內容指紋；之後內容變了，代表代理在做新的工作
    goal_source: str = ""    # user / agent / inferred
    chat_title: str = ""     # 目標綁定的聊天對話標題（同一個 IDE 視窗裡可能有很多對話分頁）
    # 對話過長、原地打轉
    sends_in_conversation: int = 0
    recent_tails: List[str] = field(default_factory=list)
    rollovers: int = 0
    rollover_pending: bool = False  # 已開新對話、接續摘要還沒送出
    # 查核用的 git repo（專案根目錄不是 repo、實作在子資料夾時指定）；空字串代表用專案路徑
    repo_path: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionState":
        s = cls()
        for k, v in data.items():
            if hasattr(s, k) and isinstance(v, type(getattr(s, k))):
                setattr(s, k, v)
        return s

    def add_interaction(self, kind: str, text: str) -> None:
        self.interactions.append({"time": now_str(), "kind": kind, "text": text[:300]})
        self.interactions = self.interactions[-MAX_INTERACTIONS:]


def render_progress(s: SessionState, specs: Optional[List[Dict[str, Any]]] = None,
                    plans: Optional[List[Dict[str, Any]]] = None) -> str:
    def lines(items: List[str], empty: str = "（無）") -> str:
        return "\n".join(f"- {x}" for x in items) if items else empty

    where = f"SSH: {s.remote}:{s.path}" if s.remote else (s.path or "（找不到路徑）")
    out = [
        f"# 進度：{s.project}（{s.app}）",
        f"專案路徑：{where}",
        f"最後更新：{s.updated_at or '—'}",
        "> 這份檔案由 AI 工程助理產生，請在面板上修改目標，不要直接改這裡。",
        "",
        "## 目標",
        s.goal or "（尚未設定。請在面板上輸入目標；進行中的專案可先請代理說明最終目標。）",
    ]
    if s.goal_confirmed_at:
        out.append(f"（{s.goal_confirmed_at} 由使用者確認）")
    if s.agent_goal_proposal and not s.goal:
        out += ["", "### 代理說明的最終目標（待你採用）", s.agent_goal_proposal]
    out += ["", "## 階段", f"- {phase_label(s)}｜自動駕駛：{'開' if s.autopilot else '關'}"]
    if s.paused_reason:
        out.append(f"- ⏸ 自動駕駛已暫停：{s.paused_reason}")
    out += ["", "## 完成定義（驗收條件）"]
    if s.acceptance:
        mark = {"verified": "x", "claimed": "~", "todo": " "}
        for a in s.acceptance:
            extra = []
            if a.get("how"):
                extra.append(f"如何查核：{a['how']}")
            if a.get("test"):
                extra.append(f"測試：{a['test']}")
            tag = "" if is_automated(a) else "👁 手動檢查："
            out.append(f"- [{mark.get(a.get('status', 'todo'), ' ')}] {tag}{a.get('text', '')}"
                       + (f"（{'；'.join(extra)}）" if extra else ""))
        out.append(f"（{s.acceptance_confirmed_at} 你確認）" if s.acceptance_confirmed_at else "（草稿，等你確認後才開始實作）")
    else:
        out.append("（尚未定稿：助理會請代理提出詳細、可檢查的驗收 spec）")
    if s.spec_issues and not s.acceptance_confirmed_at:
        out += ["", "### 助理對驗收 spec 草稿的意見"] + [f"- {x}" for x in s.spec_issues]
    if s.last_issues:
        out += ["", "## 最近一次查核發現的問題"] + [f"- {x}" for x in s.last_issues]
    if s.report_path:
        out.append(f"\n最近的測試報告：`{s.report_path}`")
    out += ["", "## 正在進行的計畫"]
    if s.active_docs:
        who = "你勾選" if s.active_docs_by == "user" else "採用代理的判斷"
        out += [f"- `{d}`" for d in s.active_docs] + [f"（{who}）"]
    else:
        out.append("（尚未確認。請在面板上勾選，或請代理判斷）")
    if s.agent_active_docs_proposal and s.agent_active_docs_proposal != s.active_docs:
        out += ["", "### 代理認為正在進行（待你採用）"] + [f"- `{d}`" for d in s.agent_active_docs_proposal]
    if plans:
        out += ["", "## 專案的 plan / progress 文件（候選，依修改時間）"]
        for p in plans:
            box = f"，勾選 {p['done']}/{p['total']}" if p["total"] else ""
            out.append(f"- `{p['relpath']}`（{p['modified']}{box}）")
    if s.pending_approval:
        out += ["", "## 代理正在等你允許", s.pending_approval]
    if specs:
        out += ["", "## Kiro spec（參考，勾選是代理自己勾的，不算證據）"]
        for sp in specs:
            out.append(f"- `{sp['name']}`：tasks {sp['tasks_done']}/{sp['tasks_total']}")
    out += [
        "",
        "## 目前狀態",
        f"- 代理狀態：{s.agent_state or '—'}",
        f"- 摘要：{s.status_summary or '—'}",
        "",
        "## 里程碑 commit",
    ]
    if s.commits:
        out += ["| 時間 | 分支 | commit | 內容 | 查核 |", "|---|---|---|---|---|"]
        out += [f"| {c.get('time', '')} | {c.get('branch', '')} | {c.get('hash', '')} | {c.get('desc', '')} | {c.get('check', '')} |"
                for c in s.commits]
    else:
        out.append("（無）")
    out += [
        "",
        "## 已查核的證據",
        lines([f"{e.get('time', '')} {e.get('text', '')}" for e in s.evidence]),
        "",
        "## 代理宣稱、尚未查核",
        lines(s.claims_unverified),
        "",
        "## 下一步（建議，尚未送出）",
    ]
    if s.suggested_prompt:
        out += [f"- 動作：{s.suggested_action}（{s.suggested_at}）", f"- 理由：{s.suggested_reason}", "", "```text",
                s.suggested_prompt, "```"]
    else:
        out.append("（無）")
    out += ["", "## 待使用者決定", lines(s.pending_human), "", "## 互動摘要（最近 20 筆，完整紀錄在 history.jsonl）"]
    out.append(lines([f"{i['time']} [{i['kind']}] {i['text']}" for i in reversed(s.interactions)]))
    return "\n".join(out) + "\n"


def read_kiro_specs(project_path: str, max_req_chars: int = 1200) -> List[Dict[str, Any]]:
    """讀 .kiro/specs/*/：tasks.md 的勾選數與 requirements.md（或 bugfix.md）開頭。只讀不寫。"""
    base = os.path.join(project_path or "", ".kiro", "specs")
    if not project_path or not os.path.isdir(base):
        return []
    out = []
    for name in sorted(os.listdir(base)):
        d = os.path.join(base, name)
        if not os.path.isdir(d):
            continue
        done = total = 0
        tasks = os.path.join(d, "tasks.md")
        if os.path.isfile(tasks):
            try:
                text = open(tasks, encoding="utf-8", errors="replace").read()
                done = len(re.findall(r"^\s*[-*]\s*\[[xX]\]", text, re.M))
                total = done + len(re.findall(r"^\s*[-*]\s*\[ \]", text, re.M))
            except OSError:
                pass
        head = ""
        for fn in ("requirements.md", "bugfix.md"):
            p = os.path.join(d, fn)
            if os.path.isfile(p):
                try:
                    head = open(p, encoding="utf-8", errors="replace").read()[:max_req_chars]
                except OSError:
                    pass
                break
        out.append({"name": name, "tasks_done": done, "tasks_total": total, "requirements_head": head,
                    "mtime": os.path.getmtime(d)})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


# 整字比對（避免 plant、plantuml 這類檔名被當成 plan）；底線、連字號、點都算分隔
_PLAN_NAME = re.compile(r"(?:^|[^a-z])(?:plans?|roadmaps?|todo|progress|walkthrough|milestones?|tasks?)(?:[^a-z]|$)", re.I)
_PLAN_DIR = re.compile(r"^(plans?|roadmaps?)$", re.I)
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", ".pytest_cache", ".mypy_cache",
              "dist", "build", "bin", "obj", "target", ".next", ".idea", ".vs", "packages", "site-packages"}


def read_plans(project_path: str, limit: int = 10, max_depth: int = 5, head_chars: int = 1500) -> List[Dict[str, Any]]:
    """找專案裡的 plan / progress 類文件（.kiro/specs 另外處理），依修改時間由新到舊。只讀不寫。

    最新的 plan 往往才是代理真正在執行的工作；spec 可能已經過時。
    """
    if not project_path or not os.path.isdir(project_path):
        return []
    own = (os.path.normcase(SESSIONS_DIR), os.path.normcase(REPORTS_DIR))
    found: List[Dict[str, Any]] = []
    base_depth = project_path.rstrip("\\/").count(os.sep)
    for root, dirs, files in os.walk(project_path):
        depth = root.count(os.sep) - base_depth
        rel_root = os.path.relpath(root, project_path)
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not (rel_root == "." and d == ".kiro")
                   and not os.path.normcase(os.path.join(root, d)).startswith(own)]
        if depth >= max_depth:
            dirs[:] = []
        in_plan_dir = bool(_PLAN_DIR.match(os.path.basename(root)))
        for fn in files:
            if not fn.lower().endswith(".md"):
                continue
            if not (in_plan_dir or _PLAN_NAME.search(fn)):
                continue
            full = os.path.join(root, fn)
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                continue
            found.append({"path": full, "mtime": mtime})
    found.sort(key=lambda x: x["mtime"], reverse=True)
    out = []
    for f in found[:limit]:
        try:
            text = open(f["path"], encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        done = len(re.findall(r"^\s*[-*]\s*\[[xX]\]", text, re.M))
        todo = len(re.findall(r"^\s*[-*]\s*\[ \]", text, re.M))
        out.append({
            "relpath": os.path.relpath(f["path"], project_path),
            "modified": datetime.datetime.fromtimestamp(f["mtime"]).strftime("%Y-%m-%d %H:%M"),
            "done": done, "total": done + todo, "head": text[:head_chars],
        })
    return out


_MANUAL_TEST = re.compile(r"^\s*(無|none|n/?a|-|—)|手動|manual|快照比對|人工", re.I)


def is_automated(item: Dict[str, str]) -> bool:
    """驗收條件是否有自動化測試（測試欄寫了測試名稱或執行指令，且不是「無／手動」）。

    只有自動化項目由助理查核證據；手動項目在目標達成時列給使用者自己確認，避免卡在一份報告證明不了的條件上。
    """
    test = (item.get("test") or "").strip()
    return bool(test) and not _MANUAL_TEST.search(test)


def core_items(s: SessionState) -> List[Dict[str, str]]:
    auto = [a for a in s.acceptance if is_automated(a)]
    return auto or list(s.acceptance)


def phase_of(s: SessionState) -> str:
    if not s.goal:
        return "goal"
    if not s.acceptance_confirmed_at:
        return "spec"
    if s.done_at or (s.acceptance and all(a.get("status") == "verified" for a in core_items(s))):
        return "done"
    return "build"


_PHASE_LABEL = {"goal": "① 確認目標", "spec": "② 討論驗收 spec", "build": "③ 實作與查核證據", "done": "④ 已達成目標"}


def phase_label(s: SessionState) -> str:
    return _PHASE_LABEL[phase_of(s)]


class Session:
    def __init__(self, directory: str) -> None:
        self.dir = directory
        self._lock = threading.RLock()
        # 最近一次讀到的 Kiro spec 與 plan 文件，產生 progress.md 時使用
        self.specs: List[Dict[str, Any]] = []
        self.plans: List[Dict[str, Any]] = []

    @property
    def progress_path(self) -> str:
        return os.path.join(self.dir, "progress.md")

    @property
    def reports_dir(self) -> str:
        return os.path.join(self.dir, "reports")

    def load(self) -> SessionState:
        p = os.path.join(self.dir, "state.json")
        try:
            with open(p, "r", encoding="utf-8") as f:
                return SessionState.from_dict(json.load(f))
        except (OSError, ValueError):
            return SessionState()

    def save(self, state: SessionState, specs: Optional[List[Dict[str, Any]]] = None,
             plans: Optional[List[Dict[str, Any]]] = None) -> None:
        if specs is not None:
            self.specs = specs
        if plans is not None:
            self.plans = plans
        state.updated_at = now_str()
        with self._lock:
            os.makedirs(self.dir, exist_ok=True)
            tmp = os.path.join(self.dir, "state.json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(asdict(state), f, ensure_ascii=False, indent=2)
            os.replace(tmp, os.path.join(self.dir, "state.json"))
            with open(self.progress_path, "w", encoding="utf-8") as f:
                f.write(render_progress(state, self.specs, self.plans))

    def update(self, mutate: Callable[[SessionState], None],
               specs: Optional[List[Dict[str, Any]]] = None,
               plans: Optional[List[Dict[str, Any]]] = None) -> SessionState:
        """在鎖內「讀取 → 修改 → 存檔」，避免面板與背景執行緒互相覆蓋。"""
        with self._lock:
            st = self.load()
            mutate(st)
            self.save(st, specs, plans)
            return st

    def start_new_goal(self, goal: str = "", source: str = "user", chat_title: str = "") -> SessionState:
        """封存目前的進度（state.json 與 progress.md 複製到 archive/），換成新的目標從頭開始。"""
        with self._lock:
            old = self.load()
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            arch = os.path.join(self.dir, "archive")
            os.makedirs(arch, exist_ok=True)
            with open(os.path.join(arch, f"state-{stamp}.json"), "w", encoding="utf-8") as f:
                json.dump(asdict(old), f, ensure_ascii=False, indent=2)
            if os.path.exists(self.progress_path):
                with open(self.progress_path, encoding="utf-8") as src, \
                        open(os.path.join(arch, f"progress-{stamp}.md"), "w", encoding="utf-8") as dst:
                    dst.write(src.read())
            new = SessionState(key=old.key, app=old.app, project=old.project, path=old.path, remote=old.remote,
                               repo_path=old.repo_path, active_docs=list(old.active_docs),
                               active_docs_by=old.active_docs_by)
            new.chat_title = chat_title
            new.goal, new.goal_source = goal, source if goal else ""
            new.goal_confirmed_at = now_str() if goal else ""
            new.add_interaction("new_goal", f"封存 {stamp}，開始新目標：{goal[:100] or '（未設定）'}")
            self.save(new)
        self.history("new_goal", archived=stamp, goal=goal, source=source)
        return new

    def history(self, kind: str, **data: Any) -> None:
        rec = {"time": now_str(), "kind": kind}
        rec.update(data)
        with self._lock:
            os.makedirs(self.dir, exist_ok=True)
            with open(os.path.join(self.dir, "history.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def history_on(self, date: str) -> List[Dict[str, Any]]:
        out = []
        try:
            with open(os.path.join(self.dir, "history.jsonl"), "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith('{"time": "' + date):
                        try:
                            out.append(json.loads(line))
                        except ValueError:
                            continue
        except OSError:
            pass
        return out


class SessionStore:
    def __init__(self, root: str = SESSIONS_DIR) -> None:
        self.root = root
        self._cache: Dict[str, Session] = {}
        self._lock = threading.Lock()

    def _dir_for(self, key: str, app_label: str, project: str) -> str:
        name = f"{_safe(app_label)}__{_safe(project)}"
        d = os.path.join(self.root, name)
        meta = os.path.join(d, "meta.json")
        if os.path.exists(meta):
            try:
                with open(meta, encoding="utf-8") as f:
                    if json.load(f).get("key") == key:
                        return d
            except (OSError, ValueError):
                pass
            # 同名但不同路徑的專案：加上短雜湊區分
            return os.path.join(self.root, f"{name}__{hashlib.sha1(key.encode('utf-8')).hexdigest()[:6]}")
        return d

    def get(self, key: str, app_label: str, project: str, path: str = "", remote: str = "") -> Session:
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            d = self._dir_for(key, app_label, project)
            os.makedirs(d, exist_ok=True)
            meta = os.path.join(d, "meta.json")
            if not os.path.exists(meta):
                with open(meta, "w", encoding="utf-8") as f:
                    json.dump({"key": key, "app": app_label, "project": project, "path": path, "remote": remote},
                              f, ensure_ascii=False, indent=2)
            sess = Session(d)
            st = sess.load()
            if not st.key:
                st.key, st.app, st.project, st.path, st.remote = key, app_label, project, path, remote
                sess.save(st)
            self._cache[key] = sess
            return sess

    def all(self) -> List[Session]:
        out = []
        if not os.path.isdir(self.root):
            return out
        for name in sorted(os.listdir(self.root)):
            d = os.path.join(self.root, name)
            if os.path.isfile(os.path.join(d, "state.json")):
                out.append(Session(d))
        return out


def build_daily_summary(store: SessionStore, date: str) -> str:
    """每日摘要：完全由 state.json 與當天的 history.jsonl 產生，不經 AI，數字可直接對帳。"""
    out = [f"# AI 工程助理 每日摘要 {date}", ""]
    sessions = store.all()
    if not sessions:
        return "\n".join(out + ["今天沒有任何工作階段。", ""])
    waiting: List[str] = []
    for sess in sessions:
        st = sess.load()
        hist = sess.history_on(date)
        kinds: Dict[str, int] = {}
        for h in hist:
            kinds[h.get("kind", "")] = kinds.get(h.get("kind", ""), 0) + 1
        verified = [a for a in st.acceptance if a.get("status") == "verified"]
        out += [
            f"## {st.app}｜{st.project}",
            f"- 路徑：{('SSH: ' + st.remote + ':') if st.remote else ''}{st.path or '—'}",
            f"- 目標：{st.goal or '（尚未設定）'}",
            f"- 驗收條件：已查核 {len(verified)}/{len(st.acceptance)}" if st.acceptance else "- 驗收條件：尚未定稿",
            f"- 目前狀態：{st.agent_state or '—'}｜{st.status_summary or '—'}",
            f"- 今天的紀錄：觀察 {kinds.get('observe', 0)} 次、建議 {kinds.get('suggest', 0)} 次、"
            f"實際操作 {kinds.get('action', 0)} 次、交給你 {kinds.get('ask_human', 0)} 次",
        ]
        reasons: Dict[str, int] = {}
        for h in hist:
            if h.get("kind") == "ask_human":
                r = re.sub(r"[\d.]+", "#", str(h.get("text", "")))[:40]
                reasons[r] = reasons.get(r, 0) + 1
        if reasons:
            top = sorted(reasons.items(), key=lambda kv: -kv[1])[:3]
            out.append("- 交給你的原因（前 3 名，可據此調整門檻）：" + "；".join(f"{r}（{n} 次）" for r, n in top))
        today_commits = [c for c in st.commits if c.get("time", "").startswith(date)]
        if today_commits:
            out.append("- 今天的里程碑 commit：" + "、".join(f"{c.get('hash', '')} {c.get('desc', '')}" for c in today_commits))
        today_ev = [e for e in st.evidence if e.get("time", "").startswith(date)]
        out.append(f"- 今天查核通過的證據：{len(today_ev)} 筆")
        if st.claims_unverified:
            out.append("- 尚未查核的宣稱：" + "；".join(st.claims_unverified[:5]))
        if st.suggested_prompt:
            out.append(f"- 建議的下一步：{st.suggested_action}｜{st.suggested_prompt[:80]}")
        for p in st.pending_human:
            waiting.append(f"- {st.app}｜{st.project}：{p}")
        anomalies = [h for h in hist if h.get("kind") in ("blocked", "loop", "error")]
        if anomalies:
            out.append("- 異常：" + "；".join(f"{h.get('kind')} {str(h.get('text', ''))[:40]}" for h in anomalies[:5]))
        out.append("")
    out += ["## 等你決定", "\n".join(waiting) if waiting else "（無）", ""]
    return "\n".join(out)


def write_daily_summary(store: SessionStore, date: str, reports_dir: str = REPORTS_DIR) -> str:
    os.makedirs(reports_dir, exist_ok=True)
    path = os.path.join(reports_dir, f"daily-{date}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(build_daily_summary(store, date))
    return path
