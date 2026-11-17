"""房間資料與持久化儲存（每間房一個資料夾：room.json、events.jsonl、inbox.json）。"""
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

PHASES = ["goal", "spec", "build", "test", "review", "done"]
STATUSES = ["queued", "running", "waiting_user", "stuck", "paused", "error", "done", "archived"]
INBOX_KINDS = ["approve_spec", "permission", "question", "merge", "error", "unverified", "login", "stuck", "recommend"]


_ROOM_ID = re.compile(r"^r[0-9a-f]{8}$")
_ITEM_ID = re.compile(r"^i[0-9a-f]{8}$")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class InboxItem:
    id: str
    room_id: str
    kind: str
    title: str
    detail: str
    options: List[Dict[str, str]]
    created_at: str
    resolved: str = ""
    answer: str = ""
    text: str = ""
    # 登入項目（kind "login"）：哪個座位、受影響的房間（多間房共用一個項目）、示範或真座位、目前狀態
    seat: str = ""
    rooms: List[str] = field(default_factory=list)
    scope: str = ""
    state: str = ""  # "" | "opened"（已開登入視窗，背景確認中）
    note: str = ""   # 最近一次確認的結果（給使用者看）
    paused: List[str] = field(default_factory=list)  # 等這次登入時被你暫停的房間（登入後不自動繼續）
    handoff: bool = False  # Task 20：代管沒有決定（同一題連續 2 次、修訂達上限、模型不可用…），交給你


@dataclass
class Room:
    id: str
    name: str
    repo: str
    slug: str
    goal: str
    source: str
    impl_cli: str
    review_cli: str
    autonomy: str = "balanced"  # careful | balanced | bold
    allowed_paths: List[str] = field(default_factory=list)
    worktree: str = ""
    branch: str = ""
    base: str = ""
    phase: str = "goal"
    status: str = "queued"
    criteria: List[Dict] = field(default_factory=list)
    test_cmd: str = ""
    attempts: Dict[str, int] = field(default_factory=dict)
    persona: str = ""
    activity: str = ""
    error: str = ""
    impl_session: str = ""
    created_at: str = ""
    updated_at: str = ""
    order: int = 0
    demo: bool = False
    # 附錄 A
    impl_seat: str = ""
    impl_model: str = ""
    review_seat: str = ""
    review_model: str = ""
    fallback_from: str = ""
    user_pinned: bool = False
    impl_by: str = ""  # 誰派的座位：user｜Decision-1｜rule｜fallback（取自 Assignment.by）
    review_by: str = ""
    impl_reason: str = ""  # 派工理由（Decision-1 的分數或規則）
    review_reason: str = ""
    # Task 18：這間房你指定的（座位＋模型）：{"seat", "model"}；空＝自動。座位同步寫在 impl_cli／review_cli。
    impl_pref: Dict[str, str] = field(default_factory=dict)
    review_pref: Dict[str, str] = field(default_factory=dict)
    # Task 17：CLI 回報的實際模型 id（沒回報＝""，畫面顯示「CLI 預設（未回報）」）與本房各座位累計用量
    impl_model_actual: str = ""
    review_model_actual: str = ""
    tokens: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # seat -> {in, out, in_cache, cost, currency, turns, failed}
    tokens_models: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # "seat|model" -> 同上＋seat, model, reported
    # 附錄 B
    context_pct: float = 0.0
    context_estimated: bool = False
    rollovers: int = 0
    # 控制器追加
    target_branch: str = ""
    unverified: str = ""
    pending_fix: str = ""  # 關卡沒過、還沒送給代理的修正提示（重啟後先送它，不重跑關卡）
    # Task 20：代管（原本要你按的事改由 Decision-1 與審查模型決定；合併、危險指令、登入仍歸你）
    delegate: bool = False
    delegate_last: str = ""  # 代管最近一次決定的問題標題（同一題連續第 2 次 → 交給你）
    # 全權交付（進展量測、升級階梯、預算、停下等你、能力庫、專案知識、自動改寫）
    rounds: List[Dict] = field(default_factory=list)
    ladder: Dict = field(default_factory=dict)
    budget: Dict = field(default_factory=dict)
    spent: Dict = field(default_factory=dict)
    baseline: Dict = field(default_factory=dict)
    env_accepted: List[str] = field(default_factory=list)
    user_info: str = ""
    notes_dir: str = ""
    capabilities: Optional[List[str]] = None
    caps_loaded: Dict[str, str] = field(default_factory=dict)
    lessons_used: List[str] = field(default_factory=list)
    autotune: Dict = field(default_factory=dict)
    stuck_reason: str = ""


def _from_dict(cls, d: Dict[str, Any]):
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in names})


def slim_event(e: Dict[str, Any]) -> Dict[str, Any]:
    """清單與快照用的事件：拿掉 detail（大），留 has_detail；細節用事件 id 另外讀。沒有 id 的舊事件保留原樣。"""
    if not isinstance(e, dict) or "detail" not in e or not e.get("id"):
        return e
    out = {k: v for k, v in e.items() if k != "detail"}
    out["has_detail"] = bool(e.get("detail"))
    return out


class RoomStore:
    # 每間房 events.jsonl 最多保留的行數；超過（加一點緩衝）就把舊的壓縮到 events-<n>.jsonl.gz
    events_cap = 5000
    events_slack = 500

    def __init__(self, root: str):
        self.root = root
        self._lock = threading.RLock()
        self._ev_lines = {}  # type: Dict[str, int]  # 房間 → events.jsonl 目前行數（第一次寫入時數一次）
        os.makedirs(root, exist_ok=True)

    # ---- 檔案 ----
    def _dir(self, room_id: str) -> str:
        if not isinstance(room_id, str) or not _ROOM_ID.match(room_id):
            raise ValueError("無效的房間 id：%r" % (room_id,))
        return os.path.join(self.root, room_id)

    def _path(self, room_id: str, name: str) -> str:
        return os.path.join(self._dir(room_id), name)

    def _write_json(self, path: str, data: Any) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(5):
                try:
                    os.replace(tmp, path)
                    return
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.05)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def _read_json(self, path: str, default: Any) -> Any:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    def _load(self, room_id: str) -> Optional[Room]:
        try:
            path = self._path(room_id, "room.json")
        except ValueError:
            return None
        d = self._read_json(path, None)
        if not isinstance(d, dict):
            return None
        try:
            return _from_dict(Room, d)
        except TypeError:
            return None

    def _save(self, room: Room) -> None:
        self._write_json(self._path(room.id, "room.json"), asdict(room))

    # ---- 房間 ----
    def create(self, **fields_: Any) -> Room:
        with self._lock:
            rid = "r" + uuid.uuid4().hex[:8]
            while os.path.exists(self._dir(rid)):
                rid = "r" + uuid.uuid4().hex[:8]
            order = max([r.order for r in self.list(include_archived=True)] + [0]) + 1
            now = _now()
            fields_.setdefault("created_at", now)
            fields_.setdefault("updated_at", now)
            fields_.setdefault("order", order)
            room = _from_dict(Room, dict(fields_, id=rid))
            self._save(room)
            return room

    def get(self, room_id: str) -> Optional[Room]:
        with self._lock:
            return self._load(room_id)

    def list(self, include_archived: bool = False) -> List[Room]:
        with self._lock:
            out = []
            try:
                names = os.listdir(self.root)
            except OSError:
                return []
            for n in names:
                if not _ROOM_ID.match(n):
                    continue  # 同一個資料夾裡的其他檔案（例如 quota.json）
                if os.path.isfile(self._path(n, "room.json")):
                    r = self._load(n)
                    if r and (include_archived or r.status != "archived"):
                        out.append(r)
            out.sort(key=lambda r: (r.order, r.created_at))
            return out

    def update(self, room_id: str, mutate: Callable[[Room], None]) -> Room:
        self._dir(room_id)  # 驗證 id
        with self._lock:
            room = self._load(room_id)
            if room is None:
                raise KeyError(room_id)
            orig_id = room.id
            mutate(room)
            room.id = orig_id
            room.updated_at = _now()
            self._save(room)
            return room

    # ---- 事件 ----
    def _require_room(self, room_id: str) -> None:
        if not os.path.isfile(self._path(room_id, "room.json")):
            raise KeyError(room_id)

    def event(self, room_id: str, kind: str, text: str, **data: Any) -> None:
        rec = {"ts": _now(), "kind": kind, "text": text, "id": uuid.uuid4().hex[:12]}
        rec.update(data)
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with self._lock:
            self._require_room(room_id)
            path = self._path(room_id, "events.jsonl")
            prefix = ""
            try:
                with open(path, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    if f.tell() > 0:
                        f.seek(-1, os.SEEK_END)
                        if f.read(1) != b"\n":
                            prefix = "\n"  # 上次寫到一半當掉，先補換行
            except OSError:
                pass
            with open(path, "a", encoding="utf-8", newline="") as f:
                f.write(prefix + line)
            n = self._ev_lines.get(room_id)
            if n is None:
                n = self._count_lines(path)
            else:
                n += 1
            self._ev_lines[room_id] = n
            if n > self.events_cap + self.events_slack:
                self._rotate(room_id, path)

    @staticmethod
    def _count_lines(path: str) -> int:
        try:
            with open(path, "rb") as f:
                return sum(1 for ln in f if ln.strip())
        except OSError:
            return 0

    def _rotate(self, room_id: str, path: str) -> None:
        """只留最後 events_cap 行；舊的寫到 events-<n>.jsonl.gz（呼叫端持有鎖）。"""
        import gzip
        try:
            with open(path, "rb") as f:
                lines = [ln for ln in f.read().split(b"\n") if ln.strip()]
        except OSError:
            return
        old, keep = lines[:-self.events_cap], lines[-self.events_cap:]
        if not old:
            self._ev_lines[room_id] = len(keep)
            return
        n = 1
        while os.path.exists(self._path(room_id, "events-%d.jsonl.gz" % n)):
            n += 1
        try:
            with gzip.open(self._path(room_id, "events-%d.jsonl.gz" % n), "wb") as g:
                g.write(b"\n".join(old) + b"\n")
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(b"\n".join(keep) + b"\n")
            os.replace(tmp, path)
        except OSError:
            log.warning("事件檔輪替失敗：%s", room_id, exc_info=True)
            return
        self._ev_lines[room_id] = len(keep)

    def events_by_id(self, room_id: str, ids: List[str], scan: int = 5000) -> List[Dict]:
        """依事件 id 讀完整事件（含 detail）；只找目前的 events.jsonl 最後 scan 筆。"""
        want = {i for i in ids if isinstance(i, str) and i}
        if not want:
            return []
        found = {}
        for e in self.events(room_id, limit=scan):
            if e.get("id") in want and e["id"] not in found:
                found[e["id"]] = e
                if len(found) == len(want):
                    break
        return [found[i] for i in ids if i in found]

    def events(self, room_id: str, limit: int = 100) -> List[Dict]:
        try:
            path = self._path(room_id, "events.jsonl")
        except ValueError:
            return []
        with self._lock:
            try:
                size = os.path.getsize(path)
            except OSError:
                return []
        out: List[Dict] = []
        block = 8192
        pos = size
        carry = b""  # 尚未確定完整的開頭片段
        try:
            with open(path, "rb") as f:
                while pos > 0 and len(out) < limit:
                    start = max(0, pos - block)
                    with self._lock:
                        f.seek(start)
                        chunk = f.read(pos - start)
                    pos = start
                    parts = (chunk + carry).split(b"\n")
                    carry = parts[0]
                    for raw in reversed(parts[1:]):
                        if len(out) >= limit:
                            break
                        self._parse_event(raw, out)
                if pos == 0 and len(out) < limit:
                    self._parse_event(carry, out)
        except OSError:
            pass
        return out

    @staticmethod
    def _parse_event(raw: bytes, out: List[Dict]) -> None:
        if not raw.strip():
            return
        try:
            rec = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            return
        if isinstance(rec, dict):
            out.append(rec)

    # ---- 收件匣 ----
    def _load_inbox(self, room_id: str) -> List[InboxItem]:
        raw = self._read_json(self._path(room_id, "inbox.json"), [])
        items = []
        for d in raw if isinstance(raw, list) else []:
            if isinstance(d, dict):
                try:
                    items.append(_from_dict(InboxItem, d))
                except TypeError:
                    pass
        return items

    def _save_inbox(self, room_id: str, items: List[InboxItem]) -> None:
        self._write_json(self._path(room_id, "inbox.json"), [asdict(i) for i in items])

    def add_inbox(self, room_id: str, kind: str, title: str, detail: str,
                  options: List[Dict[str, str]]) -> InboxItem:
        with self._lock:
            self._require_room(room_id)
            items = self._load_inbox(room_id)
            used = {i.id for i in self.inbox(include_resolved=True)}
            iid = "i" + uuid.uuid4().hex[:8]
            while iid in used:
                iid = "i" + uuid.uuid4().hex[:8]
            item = InboxItem(id=iid, room_id=room_id, kind=kind,
                             title=title, detail=detail, options=list(options or []),
                             created_at=datetime.now().isoformat())
            items.append(item)
            self._save_inbox(room_id, items)
            return item

    def join_login(self, room_id: str, seat: str, scope: str, title: str, detail: str,
                   options: List[Dict[str, str]]) -> Tuple[InboxItem, bool]:
        """同一座位（同一 scope）只有一個未解決的登入項目：有就把房間加進去，沒有就新增。回傳（項目, 是否新增）。"""
        with self._lock:
            for it in self.inbox():
                if it.kind == "login" and it.seat == seat and it.scope == scope:
                    if room_id not in it.rooms or room_id in it.paused:
                        def mut(x: InboxItem) -> None:
                            if room_id not in x.rooms:
                                x.rooms.append(room_id)
                            if room_id in x.paused:
                                x.paused.remove(room_id)
                        it = self.update_item(it.id, mut) or it
                    return it, False
            item = self.add_inbox(room_id, "login", title, detail, options)
            item = self.update_item(item.id, lambda x: (setattr(x, "seat", seat), setattr(x, "scope", scope),
                                                        setattr(x, "rooms", [room_id]))) or item
            return item, True

    def update_item(self, item_id: str, mut: Callable[[InboxItem], Any]) -> Optional[InboxItem]:
        """修改一個項目（不論是否已解決）並存檔；找不到回 None。"""
        if not isinstance(item_id, str) or not _ITEM_ID.match(item_id):
            raise ValueError("無效的項目 id：%r" % (item_id,))
        with self._lock:
            for r in self.list(include_archived=True):
                items = self._load_inbox(r.id)
                for it in items:
                    if it.id == item_id:
                        mut(it)
                        self._save_inbox(r.id, items)
                        return it
        return None

    def get_item(self, item_id: str) -> Optional[InboxItem]:
        for it in self.inbox(include_resolved=True):
            if it.id == item_id:
                return it
        return None

    def inbox(self, include_resolved: bool = False) -> List[InboxItem]:
        with self._lock:
            out: List[InboxItem] = []
            for r in self.list(include_archived=True):
                out.extend(self._load_inbox(r.id))
        if not include_resolved:
            out = [i for i in out if not i.resolved]
        out.sort(key=lambda i: i.created_at)  # 穩定排序，同秒維持寫入順序
        return out

    def resolve(self, item_id: str, answer: str, text: str = "") -> Optional[InboxItem]:
        if not isinstance(item_id, str) or not _ITEM_ID.match(item_id):
            raise ValueError("無效的項目 id：%r" % (item_id,))
        with self._lock:
            for r in self.list(include_archived=True):
                items = self._load_inbox(r.id)
                for it in items:
                    if it.id == item_id:
                        if not it.resolved:
                            it.resolved = _now()
                            it.answer = answer
                            it.text = text
                            self._save_inbox(r.id, items)
                        return it
        return None

    # ---- 重啟恢復 ----
    def recover_after_restart(self) -> List[str]:
        ids: List[str] = []
        with self._lock:
            for r in self.list(include_archived=True):
                if r.status in ("running", "waiting_user"):
                    def mut(x: Room) -> None:
                        x.status = "paused"
                        x.activity = "後端重新啟動，已暫停；按繼續接著做"
                    self.update(r.id, mut)
                    self.event(r.id, "notice", "後端重新啟動，已暫停")
                    ids.append(r.id)
                items = self._load_inbox(r.id)
                changed = False
                for it in items:
                    if it.kind == "permission" and not it.resolved:
                        it.resolved = _now()
                        it.answer = "reject"
                        it.text = "後端重啟，請求已失效"
                        changed = True
                if changed:
                    self._save_inbox(r.id, items)
        return ids
