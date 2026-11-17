"""辦公室：所有房間的調度者（排隊、同時運作上限、暫停／繼續／封存、收件匣回答、合併、用量試探）。

執行緒：
- Office 的公開方法由 HTTP 執行緒呼叫，不做耗時工作（最多幾個 git 指令，例如合併）；房間的工作都在 RoomRunner 執行緒。
- 同一間房同時最多一個 RoomRunner：舊的還活著（暫停中）時要求繼續 → 記下「要繼續」，等舊執行緒結束才排隊。
- 執行緒結束時（_OfficeRunner.run 的 finally）通知 Office，空出名額並依 order 啟動排隊中的房間。
- 背景執行緒每 probe_interval 秒：對到期的用量已滿座位跑一次極短試探（每座位同時最多一個），成功 → mark_ok。
- 登入（kind "login"，同一座位所有房間共用一個項目）：open → 開登入視窗，背景每 login_interval 秒確認，
  最多 login_timeout 秒；check → 立刻確認一次；確認成功 → 解決項目、受影響的房間繼續。switch → 座位暫時排除
  （原因「未登入」）、房間改由派工主管另選座位。確認都在背景執行緒，不卡 HTTP 執行緒。

合併只在使用者對收件匣「合併」項目回答 merge 時執行（gitops.merge_into_repo），任何情況都不自動合併。
"""
import json
import logging
import os
import threading
import time
from dataclasses import asdict
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from team import gitops, notes, personas, policy, prompts
from team.clis import CliInfo, discover
from team.drivers import GLM_SEATS, make_driver, seat_launch
from team.fleet import SEAT_LABELS, SEATS, Dispatcher, QuotaBook
from team.modelprefs import ACP_SEATS, ModelPrefs, PrefsError
from team.gitops import GitError, Worktree
from team import netcerts
from team.login import check_login, login_command, open_login_window
from team.models import ModelConfig, ModelRouter, load_model_config
from team.rooms import InboxItem, Room, RoomStore, slim_event
from team.runner import DELEGABLE, OPT_ERROR, RoomRunner
from team.usage import UsageBook, attach as attach_usage

log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO_FILE = "demo.json"  # 房間資料夾裡：示範房的假代理指令（重啟後繼續也要用）
AUTONOMY = ("careful", "balanced", "bold")
CLI_CACHE_SEC = 30.0
DELIVER_POLL = 0.2
DELIVER_DISK_CHECK = 5.0  # 每隔幾秒才從磁碟確認項目是否已被別的路徑解決
PROBE_PROMPT = "回覆 OK"
PROBE_TIMEOUT = 90.0
LOGIN_INTERVAL = 5.0
LOGIN_TIMEOUT = 600.0
RESTART_ACTIVITY = "後端重新啟動"  # rooms.recover_after_restart 寫的 activity 開頭
LOGIN_OPENED = "已開啟登入視窗，登入完成後會自動繼續（每 5 秒檢查一次）"
MAX_GOAL = 4000
MAX_SAY = 4000
# 示範房：所有座位都由假代理扮演，不需要真 CLI
DEMO_CLIS = {
    "kiro": CliInfo("kiro", "Kiro（示範）", "demo-kiro", "acp"),
    "agy": CliInfo("agy", "Antigravity（示範）", "demo-agy", "acp"),
    "claude": CliInfo("claude", "Claude（示範）", "demo-claude", "headless"),
}
DEMO_CFG = ModelConfig("http://demo.invalid", "demo", "demo")

_UNSET = object()


class _MemoryQuota(QuotaBook):
    """示範房專用的用量狀態：只在記憶體，不碰真的 quota.json。"""

    def __init__(self) -> None:
        super().__init__("demo-quota.json")

    def _load(self) -> None:
        pass

    def _save(self) -> None:
        pass


class _OfficeRunner(RoomRunner):
    """RoomRunner 結束時通知 Office（還在執行緒內，但已不再做任何事）。"""

    def __init__(self, office: "Office", *a: Any, **k: Any):
        super().__init__(*a, **k)
        self._office = office

    def run(self) -> None:
        try:
            super().run()
        finally:
            self._office._runner_exited(self)


class Office:
    def __init__(self, store: RoomStore, router_factory: Callable[[bool], ModelRouter],
                 deny_patterns_fn: Callable[[], List[str]], max_running_fn: Callable[[], int],
                 on_change: Callable[[], None], clis_fn: Callable[[], Dict[str, CliInfo]] = discover,
                 *, base_dir: str = BASE_DIR, model_cfg: Any = _UNSET,
                 plugin_dirs_fn: Optional[Callable[[], Sequence[str]]] = None,
                 cost_weight_fn: Optional[Callable[[], float]] = None,
                 context_rollover_fn: Optional[Callable[[], float]] = None,
                 prober: Optional[Callable[[str], bool]] = None, probe_interval: float = 60.0,
                 runner_kw: Optional[Dict[str, Any]] = None,
                 login_launcher: Optional[Callable[[List[str], str], None]] = None,
                 login_checker: Optional[Callable[[str], Tuple[bool, str]]] = None,
                 login_interval: float = LOGIN_INTERVAL, login_timeout: float = LOGIN_TIMEOUT,
                 model_detector: Optional[Callable[[str], Dict[str, Any]]] = None, detect_on_start: bool = True):
        self.store = store
        self.router_factory = router_factory
        self.deny_patterns_fn = deny_patterns_fn
        self.max_running_fn = max_running_fn
        self.on_change = on_change
        self.clis_fn = clis_fn
        self.base_dir = base_dir
        self.model_cfg = (load_model_config(os.path.join(base_dir, "litellm.local.json"))
                          if model_cfg is _UNSET else model_cfg)  # type: Optional[ModelConfig]
        self.plugin_dirs_fn = plugin_dirs_fn or (lambda: [])
        self.cost_weight_fn = cost_weight_fn or (lambda: 0.3)
        self.context_rollover_fn = context_rollover_fn or (lambda: 0.70)
        self.prober = prober or self._probe_seat
        self.probe_interval = probe_interval
        self.runner_kw = dict(runner_kw or {})
        self.quota = QuotaBook(os.path.join(store.root, "quota.json"))
        self.demo_quota = _MemoryQuota()  # 示範房的假「用量已滿」不影響真座位
        # Task 18：使用者選模型（team_models.json）與 ACP 可用模型偵測快取（<team root>/models_cache.json）
        self.prefs = ModelPrefs(os.path.join(base_dir, "team_models.json"), os.path.join(store.root, "models_cache.json"),
                                glm_model=lambda: self.model_cfg.glm_model if self.model_cfg else "glm-5.3")
        self.catalog = self.prefs.catalog  # 同一個 dict：存設定時就地更新，Dispatcher 立刻看到
        self.model_detector = model_detector or self._detect_seat
        self.detect_on_start = detect_on_start
        self._detecting = set()  # type: Set[str]
        self._detect_drivers = {}  # type: Dict[str, Any]  # 偵測中的驅動（stop() 時關掉）
        # Task 17：所有模型呼叫與 CLI 回合的用量帳（sessions/team/usage-YYYYMMDD.jsonl；示範房只在記憶體）
        self.usage = UsageBook(os.path.join(store.root, "usage.jsonl"))
        # Task 7：角色提示詞三層儲存
        self.prompts = prompts.PromptStore(store.root)
        # 登入：注入的 launcher／checker 給測試用（示範房與真座位都用它）；沒注入時示範房不開視窗、視為已登入
        self.login_launcher = login_launcher
        self.login_checker = login_checker
        self.login_interval = login_interval
        self.login_timeout = login_timeout

        self._lock = threading.RLock()
        self._runners = {}  # type: Dict[str, RoomRunner]
        self._resume_wanted = set()  # type: Set[str]
        self._sends = {}  # type: Dict[str, List[str]]
        self._probing = set()  # type: Set[str]
        self._remove_after_exit = {}  # type: Dict[str, Worktree]
        self._waiting = {}  # type: Dict[str, bool]  # 房間是否在等你（不占運作名額）
        self._pending = {}  # type: Dict[str, Tuple[str, str, str]]  # item_id -> (room_id, answer, text)
        self._merging = set()  # type: Set[str]
        self._started = False
        self._routers = {}  # type: Dict[bool, ModelRouter]
        self._dispatchers = {}  # type: Dict[bool, Dispatcher]
        self._clis_cache = None  # type: Optional[Dict[str, CliInfo]]
        self._clis_at = 0.0
        self._stop = threading.Event()
        self._loop_thread = None  # type: Optional[threading.Thread]
        self._login_watch = {}  # type: Dict[str, Dict[str, Any]]  # item_id -> {deadline, ev, long}
        self._login_finished = set()  # type: Set[str]

    # ================= 共用 =================
    def _changed(self) -> None:
        try:
            self.on_change()
        except Exception:
            log.debug("on_change 失敗", exc_info=True)

    def _event(self, room_id: str, kind: str, text: str, **data: Any) -> None:
        try:
            self.store.event(room_id, kind, text, **data)
        except Exception:
            log.exception("寫入事件失敗")

    def _room(self, room_id: str) -> Room:
        try:
            room = self.store.get(room_id)
        except ValueError:
            room = None
        if room is None:
            raise ValueError("找不到房間：%s" % room_id)
        return room

    def _set(self, room_id: str, **fields: Any) -> Room:
        def mut(r: Room) -> None:
            for k, v in fields.items():
                setattr(r, k, v)
        return self.store.update(room_id, mut)

    def _alive(self, room_id: str) -> Optional[RoomRunner]:
        with self._lock:
            r = self._runners.get(room_id)
            return r if r is not None and r.is_alive() else None

    def _router(self, demo: bool) -> ModelRouter:
        with self._lock:
            if demo not in self._routers:
                router = self.router_factory(demo)
                if hasattr(router, "listeners"):
                    attach_usage(router, self.store, self.usage, self._changed)
                if not demo:
                    try:
                        router.purposes = self.prefs  # 判斷用途依設定頁選的模型
                    except Exception:
                        pass
                self._routers[demo] = router
            return self._routers[demo]

    def _dispatcher(self, demo: bool) -> Dispatcher:
        with self._lock:
            d = self._dispatchers.get(demo)
            if d is None:
                avail = (lambda: set(SEATS)) if demo else self.available_seats
                d = Dispatcher(self._router(demo), self.catalog, self.demo_quota if demo else self.quota,
                               available=avail, prefs=None if demo else self.prefs)
                self._dispatchers[demo] = d
            try:
                d.cost_weight = float(self.cost_weight_fn())
            except Exception:
                d.cost_weight = 0.3
            return d

    def _clis(self) -> Dict[str, CliInfo]:
        now = time.monotonic()
        with self._lock:
            if self._clis_cache is not None and now - self._clis_at < CLI_CACHE_SEC:
                return self._clis_cache
        try:
            found = dict(self.clis_fn() or {})
        except Exception:
            log.exception("CLI 探測失敗")
            found = {}
        with self._lock:
            self._clis_cache, self._clis_at = found, now
        return found

    def available_seats(self) -> Set[str]:
        clis = self._clis()
        seats = {s for s in ("kiro", "agy", "claude") if s in clis}
        if "claude" in clis and self.model_cfg is not None:
            seats |= set(GLM_SEATS)
        return seats

    def _seat_problem(self, seat: str) -> str:
        clis = self._clis()
        if seat in ("kiro", "agy", "claude"):
            if seat not in clis:
                exe = {"kiro": "kiro-cli", "agy": "agy", "claude": "claude"}[seat]
                return "找不到 %s（%s），請先安裝並登入，或改選「自動」" % (SEAT_LABELS[seat], exe)
            return ""
        if "claude" not in clis:
            return "%s 需要 claude CLI，找不到 claude，請先安裝或改選「自動」" % SEAT_LABELS[seat]
        if self.model_cfg is None:
            return "%s 需要 litellm.local.json（base_url、key），目前讀不到，請先設定或改選「自動」" % SEAT_LABELS[seat]
        return ""

    def _max_running(self) -> int:
        try:
            return max(0, int(self.max_running_fn()))
        except Exception:
            return 4

    # ================= 啟動／停止 =================
    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        ids = self.store.recover_after_restart()
        if ids:
            log.info("後端重新啟動：%d 間運作中的房間改為暫停", len(ids))
        for it in self.store.inbox():  # 重啟後背景確認已停止：不要再顯示「每 5 秒檢查一次」
            if it.kind == "login" and it.state == "opened":
                self.store.update_item(it.id, lambda x: (setattr(x, "state", ""), setattr(
                    x, "note", "後端重新啟動，已停止自動確認；登入完成後按「我已登入，重新檢查」，或再開一次登入視窗")))
        self._changed()
        self.schedule()
        if self.detect_on_start:
            self.detect_models()
        with self._lock:
            if self._loop_thread is None or not self._loop_thread.is_alive():
                self._stop.clear()
                self._loop_thread = threading.Thread(target=self._loop, name="office-loop", daemon=True)
                self._loop_thread.start()

    def stop(self, timeout: float = 0) -> None:
        """停止背景執行緒並要求所有房間暫停；timeout > 0 時等它們結束（不要在 HTTP 執行緒用）。"""
        self._stop.set()
        with self._lock:
            runners = list(self._runners.values())
            self._resume_wanted.clear()
            detecting = list(self._detect_drivers.values())
        for d in detecting:  # 偵測中的 CLI 程序一起關掉
            try:
                d.close()
            except Exception:
                log.debug("關閉偵測驅動失敗", exc_info=True)
        for r in runners:
            try:
                r.pause()
            except Exception:
                log.debug("暫停失敗", exc_info=True)
        if timeout > 0:
            end = time.time() + timeout
            for r in runners:
                r.join(max(0.0, end - time.time()))

    def _loop(self) -> None:
        while not self._stop.wait(self.probe_interval):
            try:
                self._probe_due()
            except Exception:
                log.exception("用量試探失敗")
            try:
                self.schedule()
            except Exception:
                log.exception("排程失敗")

    # ================= 排程 =================
    @staticmethod
    def _counts(r: RoomRunner) -> bool:
        """占運作名額：還沒啟動或活著、而且不是在等你。"""
        if r.ident is not None and not r.is_alive():
            return False
        return getattr(r.room, "status", "") != "waiting_user"

    def schedule(self) -> None:
        """運作中（不含等你）的房間少於上限時，依 order 啟動排隊中的房間。

        鎖的順序：持有 Office._lock 時不呼叫任何 RoomRunner 方法（send／answer／pause 都在鎖外），
        因為執行器可能在持有自己的鎖時呼叫 on_change → schedule。"""
        to_start: List[Tuple[RoomRunner, List[str]]] = []
        failed: List[Tuple[str, Exception]] = []
        with self._lock:
            if self._stop.is_set() and self._loop_thread is not None:
                return
            for rid, r in list(self._runners.items()):
                if r.ident is not None and not r.is_alive():
                    self._runners.pop(rid, None)
            active = sum(1 for r in self._runners.values() if self._counts(r))
            limit = self._max_running()
            if active < limit:
                for room in self.store.list():
                    if active >= limit:
                        break
                    if room.status != "queued" or room.id in self._runners:
                        continue
                    try:
                        r = self._make_runner(room)
                    except Exception as e:
                        log.exception("無法啟動房間")
                        failed.append((room.id, e))
                        continue
                    self._runners[room.id] = r
                    self._waiting[room.id] = False
                    to_start.append((r, self._sends.pop(room.id, [])))
                    active += 1
        for rid, e in failed:
            self._start_failed(rid, e)
        for r, texts in to_start:
            for t in texts:
                r.send(t)
            try:
                self._set(r.room_id, activity="準備中")
            except Exception:
                log.debug("寫入準備中失敗", exc_info=True)
            r.start()
        if to_start or failed:
            self._changed()

    def _start_failed(self, rid: str, e: Exception) -> None:
        msg = "無法啟動房間：%s：%s" % (type(e).__name__, e)
        try:
            room = self._set(rid, status="error", error=msg, persona="", activity="錯誤")
            self._event(rid, "status", msg, status="error")
            if not self._open_items(rid, ("error",)):
                self.store.add_inbox(rid, "error", "%s 停下來了" % (room.name or room.slug),
                                     "%s\n\n建議：依上面的說明處理後按「重試」。" % msg, OPT_ERROR)
                self._event(rid, "inbox", "停下來了，等你處理", inbox_kind="error")
        except Exception:
            log.exception("無法寫入啟動失敗")

    def _runner_changed(self, rid: str) -> None:
        """執行器的 on_change：房間進入或離開「等你」時重新排程（等你的房間不占名額）。"""
        with self._lock:
            r = self._runners.get(rid)
            waiting = r is not None and getattr(r.room, "status", "") == "waiting_user"
            flipped = r is not None and self._waiting.get(rid) != waiting
            if flipped:
                self._waiting[rid] = waiting
        if flipped:
            try:
                self.schedule()
            except Exception:
                log.exception("排程失敗")
        self._changed()

    def _demo_overrides(self, room_id: str) -> Dict[str, List[str]]:
        try:
            with open(os.path.join(self.store.root, room_id, DEMO_FILE), encoding="utf-8") as f:
                data = json.load(f)
            return {str(k): [str(x) for x in v] for k, v in data.get("cmd_overrides", {}).items()}
        except (OSError, ValueError, AttributeError):
            return {}

    def _make_runner(self, room: Room) -> RoomRunner:
        demo = bool(room.demo)
        try:
            rollover = float(self.context_rollover_fn())
        except Exception:
            rollover = 0.70
        rid = room.id
        return _OfficeRunner(self, rid, self.store, self._router(demo), DEMO_CLIS if demo else self._clis(),
                             list(self.deny_patterns_fn() or []), lambda: self._runner_changed(rid),
                             cmd_overrides=self._demo_overrides(rid) if demo else None,
                             dispatcher=self._dispatcher(demo), quota=self.demo_quota if demo else self.quota,
                             model_cfg=DEMO_CFG if demo else self.model_cfg,
                             plugin_dirs=[] if demo else list(self.plugin_dirs_fn() or []),
                             context_rollover=rollover,
                             login_checker=lambda seat: self._check_login(seat, demo), usage=self.usage,
                             prefs=None if demo else self.prefs, **dict(self.runner_kw))

    def _runner_exited(self, runner: RoomRunner) -> None:
        rid = runner.room_id
        try:
            left = runner.drain_user_msgs()  # 在 Office._lock 外呼叫執行器
            with self._lock:
                if self._runners.get(rid) is runner:
                    del self._runners[rid]
                    self._waiting.pop(rid, None)
                if left:
                    self._sends.setdefault(rid, [])[:0] = left
                again = rid in self._resume_wanted
                self._resume_wanted.discard(rid)
                wt = self._remove_after_exit.pop(rid, None)
            if wt is not None:
                self._remove_worktree(rid, wt)
            if again and not self._stop.is_set():
                room = self.store.get(rid)
                if room is not None and room.status in ("paused", "error"):
                    self._queue(rid, "繼續")
            elif not self._stop.is_set():
                room = self.store.get(rid)
                if room is not None and room.delegate and room.status == "error":
                    errs = [i for i in self._open_items(rid, ("error",)) if not i.handoff]
                    if errs:
                        self._delegate_error(rid, errs[0])
            self.schedule()
        except Exception:
            log.exception("房間結束後的排程失敗")
        self._changed()

    def _queue(self, room_id: str, why: str) -> None:
        for it in self._open_items(room_id, ("error",)):
            self.store.resolve(it.id, "retry", "已重新開始")
        self._set(room_id, status="queued", error="", activity="排隊中")
        self._event(room_id, "status", "%s（排隊中）" % why, status="queued")

    # ================= 房間 =================
    def create_room(self, *, repo: str, goal: str, source: str, impl_cli: str = "auto",
                    review_cli: str = "auto", autonomy: str = "balanced",
                    allowed_paths: Sequence[str] = (), name: str = "", impl_model: str = "",
                    review_model: str = "") -> Room:
        return self._create_room(repo=repo, goal=goal, source=source, impl_cli=impl_cli, review_cli=review_cli,
                                 autonomy=autonomy, allowed_paths=allowed_paths, name=name,
                                 impl_model=impl_model, review_model=review_model)

    def _create_room(self, *, repo: str, goal: str, source: str, impl_cli: str, review_cli: str,
                     autonomy: str, allowed_paths: Sequence[str], name: str = "", demo: bool = False,
                     slug: str = "", cmd_overrides: Optional[Dict[str, List[str]]] = None,
                     impl_model: str = "", review_model: str = "") -> Room:
        repo = (repo or "").strip()
        if not repo:
            raise ValueError("請選擇專案資料夾")
        repo = os.path.abspath(repo)
        if not os.path.isdir(repo):
            raise ValueError("找不到資料夾：%s" % repo)
        prob = gitops.repo_problem(repo)
        if prob:
            raise ValueError("%s（%s）" % (prob, repo))
        goal = (goal or "").strip()
        if not goal:
            raise ValueError("請輸入目標")
        if len(goal) > MAX_GOAL:
            raise ValueError("目標太長（上限 %d 字）" % MAX_GOAL)
        impl_cli = (impl_cli or "auto").strip()
        review_cli = (review_cli or "auto").strip()
        for c in (impl_cli, review_cli):
            if c != "auto" and c not in SEATS:
                raise ValueError("不認得的座位：%s（可選：auto、%s）" % (c, "、".join(SEATS)))
        if impl_cli != "auto" and impl_cli == review_cli:
            raise ValueError("實作與審查要用不同的座位")
        if not demo:
            for c in (impl_cli, review_cli):
                if c != "auto":
                    p = self._seat_problem(c)
                    if p:
                        raise ValueError(p)
        prefs = {}  # type: Dict[str, Dict[str, str]]
        for role, c, m in (("impl", impl_cli, impl_model), ("review", review_cli, review_model)):
            m = (m or "").strip()
            if m == "auto":
                m = ""
            if m and (c == "auto" or demo):
                raise ValueError("示範房不能指定模型" if demo else "要指定模型，請先選座位")
            if m:
                p = self.prefs.problem(c, m)
                if p:
                    raise ValueError(p)
            if c != "auto":
                prefs[role] = {"seat": c, "model": m}
        if autonomy not in AUTONOMY:
            raise ValueError("自主程度只能是 careful、balanced、bold")
        paths = []
        for p in allowed_paths or ():
            p = str(p).strip().replace("\\", "/")
            if not p:
                continue
            if p.startswith("/") or ":" in p or ".." in p.split("/"):
                raise ValueError("可修改範圍要用 repo 內的相對路徑：%s" % p)
            paths.append(p)
        name = (name or "").strip() or os.path.basename(repo)
        with self._lock:
            slug = slug or self._unique_slug(repo, goal)
            room = self.store.create(name=name, repo=repo, slug=slug, goal=goal,
                                     source=(source or "goal").strip()[:2000], impl_cli=impl_cli,
                                     review_cli=review_cli, autonomy=autonomy, allowed_paths=paths,
                                     status="queued", phase="goal", activity="排隊中", demo=bool(demo),
                                     user_pinned=impl_cli != "auto" or review_cli != "auto",
                                     impl_pref=prefs.get("impl", {}), review_pref=prefs.get("review", {}))
            if cmd_overrides:
                path = os.path.join(self.store.root, room.id, DEMO_FILE)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"cmd_overrides": cmd_overrides}, f, ensure_ascii=False, indent=2)
        self._event(room.id, "status", "已派工：%s" % goal[:200], status="queued")
        self._changed()
        self.schedule()
        return self.store.get(room.id) or room

    def _unique_slug(self, repo: str, goal: str) -> str:
        base = gitops.slugify(goal)
        key = os.path.normcase(repo)
        used = {r.slug for r in self.store.list(include_archived=True) if os.path.normcase(r.repo) == key}
        wt_root = os.path.join(os.path.dirname(repo), gitops.WT_DIR)
        n = 1
        while True:
            cand = base if n == 1 else "%s-%d" % (base, n)
            exists = True
            try:
                gitops.git(repo, "show-ref", "--verify", "-q", "refs/heads/ai-team/" + cand, timeout=10)
            except GitError:
                exists = False
            if (cand not in used and not exists
                    and not os.path.exists(os.path.join(wt_root, "%s-%s" % (os.path.basename(repo), cand)))):
                return cand
            n += 1

    def pause(self, room_id: str) -> None:
        room = self._room(room_id)
        if room.status in ("done", "archived"):
            raise ValueError("已完成或已封存的房間不能暫停")
        for it in self.store.inbox():  # 你暫停的房間：登入成功後不自動繼續
            if it.kind == "login" and room_id in it.rooms and room_id not in it.paused:
                self.store.update_item(it.id, lambda x: x.paused.append(room_id) if room_id not in x.paused else None)
        with self._lock:
            self._resume_wanted.discard(room_id)
            r = self._alive(room_id)
        if r is not None:
            r.pause()  # 立刻回傳；執行緒結束時 status 變 paused
            self._changed()
            return
        if room.status == "paused":
            return
        self._set(room_id, status="paused", activity="已暫停")
        self._event(room_id, "status", "你暫停了這間房", status="paused")
        self._changed()

    def resume(self, room_id: str) -> None:
        room = self._room(room_id)
        if room.status in ("done", "archived"):
            raise ValueError("已完成或已封存的房間不能繼續")
        with self._lock:
            r = self._alive(room_id)
            if r is not None:
                if not r.stopping and room.status not in ("paused", "error"):
                    raise ValueError("房間已經在運作中")
                self._resume_wanted.add(room_id)  # 舊執行器還在收尾：結束後再排隊
                self._set(room_id, activity="暫停中…停下來後會繼續")
                self._changed()
                return
            if room.status == "queued":
                return
            if room.status not in ("paused", "error", "running", "waiting_user"):
                raise ValueError("只有暫停或錯誤的房間可以繼續")
            self._queue(room_id, "繼續")
        self._changed()
        self.schedule()

    def archive(self, room_id: str) -> None:
        room = self._room(room_id)
        if self._alive(room_id) is not None:
            raise ValueError("房間還在運作或停止中，先暫停並等它停下來再封存")
        if room.status not in ("done", "paused", "error"):
            raise ValueError("只有已完成、暫停或錯誤的房間可以封存")
        if self._open_items(room_id, ("merge",)):
            raise ValueError("這間房還有待合併的成果，先合併或選『先不要』")
        if room.worktree and os.path.isdir(room.worktree):
            try:
                gitops.remove_worktree(Worktree(room.repo, room.worktree, room.branch, room.base))
                self._event(room_id, "worktree", "已移除工作目錄（分支 %s 保留）" % room.branch)
            except GitError as e:
                self._event(room_id, "notice", "無法移除工作目錄：%s" % e)
        for it in self._open_items(room_id):
            self.store.resolve(it.id, "archived", "房間已封存")
        for it in self.store.inbox():  # 別的房間的登入項目：不再列這間房
            if it.kind == "login" and room_id in it.rooms:
                self.store.update_item(it.id, lambda x: x.rooms.remove(room_id) if room_id in x.rooms else None)
        with self._lock:
            self._resume_wanted.discard(room_id)
            self._sends.pop(room_id, None)
            for iid in [k for k, v in self._pending.items() if v[0] == room_id]:
                self._pending.pop(iid, None)
        self._set(room_id, status="archived", persona="", activity="已封存", worktree="")
        self._event(room_id, "status", "已封存", status="archived")
        self._changed()

    # ================= 代管（Task 20） =================
    def set_delegate(self, room_id: str, on: bool) -> Room:
        """切換代管。開啟：清掉「交給你」標記，正在等的可代管項目立刻處理（執行器在等就由它處理；
        房間停著但有待決項目 → 讓房間繼續來處理；出錯 → 自動重試一次）。關閉：恢復原本由你決定。"""
        room = self._room(room_id)
        if room.status == "archived":
            raise ValueError("已封存的房間不能切換代管")
        on = bool(on)
        if bool(room.delegate) == on:
            return room

        def mut(r: Room) -> None:
            r.delegate = on
            if on:
                att = dict(r.attempts or {})
                att["delegate_error"] = 0
                r.attempts = att
                r.delegate_last = ""
        self.store.update(room_id, mut)
        if on:
            for it in self._open_items(room_id):
                if it.handoff:
                    self.store.update_item(it.id, lambda x: setattr(x, "handoff", False))
        self._event(room_id, "delegate", "你開啟代管" if on else "你關閉代管", on=on, demo=bool(room.demo))
        r = self._alive(room_id)
        if r is not None:
            r.set_delegate(on)  # 在 Office._lock 外
        elif on:
            self._delegate_idle(room_id)
        self._changed()
        return self._room(room_id)

    def _delegate_idle(self, room_id: str) -> None:
        """沒有執行器在跑的房間開啟代管：出錯 → 自動重試一次；有待決的問題／驗收條件／測試待確認 → 讓房間繼續。"""
        room = self.store.get(room_id)
        if room is None or room.status in ("done", "archived", "queued"):
            return
        items = [i for i in self._open_items(room_id) if not i.handoff]
        errs = [i for i in items if i.kind == "error"]
        if errs:
            self._delegate_error(room_id, errs[0])
            return
        todo = [i for i in items if i.kind in DELEGABLE]
        if todo:
            self._event(room_id, "decision", "代管決定：讓房間繼續，處理等你的「%s」" % todo[0].title, delegate=True,
                        demo=bool(room.demo), detail={"item": "房間", "choice": "繼續",
                                                      "reason": "開啟代管時有 %d 個等你的項目" % len(todo)})
            try:
                self.resume(room_id)
            except ValueError as e:
                self._event(room_id, "notice", "代管無法讓房間繼續：%s" % e)

    def _delegate_error(self, room_id: str, item: InboxItem) -> None:
        """錯誤項目：代管只自動按一次「重試」；之後的錯誤交給你。"""
        room = self.store.get(room_id)
        if room is None:
            return
        n = int((room.attempts or {}).get("delegate_error", 0))
        demo = bool(room.demo)
        if n >= 1:
            self.store.update_item(item.id, lambda x: setattr(x, "handoff", True))
            self._event(room_id, "decision", "代管決定：已經自動重試過一次仍出錯，交給你", delegate=True, demo=demo,
                        detail={"item": "出錯", "title": item.title, "choice": "交給你",
                                "reason": "錯誤只自動重試一次"})
            return

        def bump(r: Room) -> None:
            att = dict(r.attempts or {})
            att["delegate_error"] = n + 1
            r.attempts = att
        self.store.update(room_id, bump)
        self._event(room_id, "decision", "代管決定：出錯了，自動按「重試」（只自動重試一次）", delegate=True, demo=demo,
                    detail={"item": "出錯", "title": item.title, "choice": "重試",
                            "reason": "代管對錯誤只自動重試一次", "error": (room.error or "")[:500]})
        self.store.resolve(item.id, "retry", "代管：自動重試一次")
        try:
            self.resume(room_id)
        except ValueError as e:
            self._event(room_id, "notice", "代管無法重試：%s" % e)

    def send(self, room_id: str, text: str) -> Dict[str, Any]:
        text = (text or "").strip()
        if not text:
            return {"ok": False, "text": "請輸入要說的話"}
        if len(text) > MAX_SAY:
            return {"ok": False, "text": "太長了（上限 %d 字）" % MAX_SAY}
        room = self._room(room_id)
        if room.status in ("done", "archived") or room.phase in ("test", "review", "done"):
            return {"ok": False, "text": "這間房已經不在實作階段，訊息不會轉告代理"}
        r = self._alive(room_id)
        if r is not None and not r.stopping:
            r.send(text)  # 在 Office._lock 外
            where = "下一回合會轉告代理"
        else:
            with self._lock:
                self._sends.setdefault(room_id, []).append(text)
            where = "房間繼續後的第一個回合會轉告代理"
        self._event(room_id, "user", "【你】" + text)
        self._changed()
        return {"ok": True, "text": where}

    # ================= 收件匣 =================
    def _open_items(self, room_id: str, kinds: Optional[Sequence[str]] = None) -> List[InboxItem]:
        return [i for i in self.store.inbox() if i.room_id == room_id and (kinds is None or i.kind in kinds)]

    def _find_item(self, item_id: str) -> Optional[InboxItem]:
        for it in self.store.inbox(include_resolved=True):
            if it.id == item_id:
                return it
        return None

    def answer(self, item_id: str, answer: str, text: str = "") -> Dict[str, Any]:
        item = self._find_item(item_id) if isinstance(item_id, str) else None
        if item is None:
            return {"ok": False, "text": "找不到這個項目"}
        if item.resolved:
            return {"ok": False, "text": "這個項目已經處理過了"}
        valid = {str(o.get("id")) for o in item.options or []}
        if answer not in valid:
            return {"ok": False, "text": "不認得的選項：%s" % answer}
        text = (text or "")[:MAX_SAY]
        if item.kind == "merge":
            return self._answer_merge(item, answer, text)
        if item.kind == "login":
            return self._answer_login(item, answer)
        if item.kind == "error":
            self.store.resolve(item.id, answer, text)
            self._event(item.room_id, "answer", "你回答了「%s」：%s" % (item.title, answer), item_id=item.id)
            if answer == "retry":
                try:
                    self.resume(item.room_id)
                except ValueError as e:
                    self._changed()
                    return {"ok": True, "text": str(e)}
            self._changed()
            return {"ok": True, "text": "好，重新開始" if answer == "retry" else "好，先放著"}
        r = self._alive(item.room_id)
        if r is not None and r.answer(item.id, answer, text):
            with self._lock:
                self._pending.pop(item.id, None)
            return {"ok": True, "text": "已轉告"}
        if item.kind == "permission":
            # 等這個請求的執行器已經不在：請求失效（代理那邊早已被拒絕）
            self.store.resolve(item.id, "reject", "請求已失效")
            self._changed()
            return {"ok": False, "text": "這個請求已失效（房間沒有在等），代理那邊已拒絕"}
        if r is None:
            room = self.store.get(item.room_id)
            if answer == "pause" or room is None or room.status in ("done", "archived"):
                self.store.resolve(item.id, answer, text)
                self._event(item.room_id, "answer", "你回答了「%s」：%s" % (item.title, answer), item_id=item.id)
                self._changed()
                return {"ok": True, "text": "好，先放著"}
            # 沒有執行器在等：先讓房間繼續，執行器會沿用這個項目再等，答案隨後送到
            self._deliver_later(item, answer, text)
            try:
                self.resume(item.room_id)
            except ValueError:
                pass
            return {"ok": True, "text": "房間繼續後會照你的回答進行"}
        self._deliver_later(item, answer, text)  # 執行器還沒開始等這個項目
        return {"ok": True, "text": "已轉告"}

    def _deliver_later(self, item: InboxItem, answer: str, text: str) -> None:
        """記住答案（只在記憶體），一有執行器在等這個項目就送過去；送到或項目被解決就結束。"""
        with self._lock:
            fresh = item.id not in self._pending
            self._pending[item.id] = (item.room_id, answer, text)
        if not fresh:
            return  # 已有輪詢執行緒，會送最新的答案

        def run() -> None:
            last_disk = time.time()
            while not self._stop.is_set():
                with self._lock:
                    rec = self._pending.get(item.id)
                    r = self._runners.get(item.room_id)
                if rec is None:
                    return
                if r is not None and r.is_alive() and r.waiting_on(item.id) and r.answer(item.id, rec[1], rec[2]):
                    with self._lock:
                        if self._pending.get(item.id) == rec:
                            self._pending.pop(item.id, None)
                    return
                if time.time() - last_disk >= DELIVER_DISK_CHECK:
                    last_disk = time.time()
                    it = self._find_item(item.id)
                    if it is None or it.resolved:
                        with self._lock:
                            self._pending.pop(item.id, None)
                        return
                time.sleep(DELIVER_POLL)
        threading.Thread(target=run, name="deliver-%s" % item.id, daemon=True).start()

    def _answer_merge(self, item: InboxItem, answer: str, text: str) -> Dict[str, Any]:
        rid = item.room_id
        if answer != "merge":
            self.store.resolve(item.id, answer, text)
            self._event(rid, "answer", "先不合併", item_id=item.id, answer=answer)
            self._set(rid, activity="全部關卡通過；你選擇先不合併")
            self._changed()
            return {"ok": True, "text": "好，先不合併"}
        with self._lock:
            if item.id in self._merging:
                return {"ok": False, "text": "正在合併，請稍候"}
            self._merging.add(item.id)
        try:
            return self._do_merge(item, text)
        finally:
            with self._lock:
                self._merging.discard(item.id)

    def _do_merge(self, item: InboxItem, text: str) -> Dict[str, Any]:
        rid = item.room_id
        cur = self._find_item(item.id)
        if cur is None or cur.resolved:
            return {"ok": False, "text": "這個項目已經處理過了"}
        room = self.store.get(rid)
        if room is None or not room.branch:
            return {"ok": False, "text": "找不到這間房的分支"}
        r = self._alive(rid)
        if r is not None:
            if room.status != "done":
                return {"ok": False, "text": "房間還在運作，稍後再合併"}
            r.join(0.8)  # 已完成、執行器正在關閉代理程序：稍等（不超過 1 秒）
            r = self._alive(rid)
        wt = Worktree(room.repo, room.worktree, room.branch, room.base, room.target_branch)
        try:
            nd = room.notes_dir or notes.notes_dir(room.repo, room.slug)
            for p in notes.purge_worktree(wt, nd):
                self._event(rid, "notes_purge", "移除助理工作文件：%s" % p, path=p)
            sha = gitops.merge_into_repo(wt)
        except GitError as e:
            self._event(rid, "notice", "合併失敗：%s" % e)
            self._changed()
            return {"ok": False, "text": str(e)}
        self.store.resolve(item.id, "merge", sha)
        short = sha[:8]
        with self._lock:
            later = self._runners.get(rid) is not None  # 代理程序還在關閉：執行器結束後再移除
            if later:
                self._remove_after_exit[rid] = wt
        if not later:
            self._remove_worktree(rid, wt)
        self._set(rid, activity="已合併 %s" % short, worktree="")
        self._event(rid, "merge", "已合併到 %s：%s" % (room.target_branch or "主線", sha), sha=sha)
        self._changed()
        return {"ok": True, "text": "已合併 %s" % short, "sha": sha}

    def _remove_worktree(self, rid: str, wt: Worktree) -> None:
        if not wt.path or not os.path.isdir(wt.path):
            return
        try:
            gitops.remove_worktree(wt)
        except GitError as e:
            self._event(rid, "notice", "已合併，但無法移除工作目錄：%s" % e)

    # ================= 快照 =================
    def snapshot(self) -> Dict[str, Any]:
        rooms = self.store.list()
        live = {r.id for r in rooms}
        with self._lock:
            wanted = set(self._resume_wanted)
        inflight = self.inflight()
        out_rooms = []
        for r in rooms:
            d = asdict(r)
            d["events"] = [slim_event(e) for e in reversed(self.store.events(r.id, limit=5))]  # 快照不帶 detail
            d["resume_requested"] = r.id in wanted
            d["inflight"] = [c for c in inflight if c["room_id"] == r.id]
            out_rooms.append(d)
        inbox = [asdict(i) for i in self.store.inbox() if i.room_id in live]
        roles = {}
        for role in personas.ROLES:
            roles[role] = next((r.id for r in rooms if r.persona == role
                                and r.status in ("running", "waiting_user")), "")
        clis = self._clis()
        avail = self.available_seats()
        qs = self.quota.snapshot()
        fleet = {}
        for seat in SEATS:
            using = [r.id for r in rooms if r.status in ("running", "waiting_user")
                     and (r.impl_seat == seat or (r.phase == "review" and r.review_seat == seat))]
            fleet[seat] = {"label": SEAT_LABELS.get(seat, seat), "available": seat in avail,
                           "quota": qs.get(seat, {"state": "ok", "until": "", "since": ""}), "rooms": using}
        try:
            usage = self._router(False).usage()
        except Exception:
            usage = {}
        return {"rooms": out_rooms, "inbox": inbox, "roles": roles,
                "clis": {name: info.label for name, info in clis.items()},
                "max_running": self._max_running(), "usage": usage, "fleet": fleet,
                "certs": netcerts.status(), "inflight": inflight}

    def inflight(self) -> List[Dict[str, Any]]:
        """所有進行中的模型呼叫（真模型與示範假模型）：[{room_id, role, model, effort, purpose, since}]。"""
        with self._lock:
            routers = list(self._routers.values())
        out = []  # type: List[Dict[str, Any]]
        for r in routers:
            try:
                out.extend(r.inflight())
            except Exception:
                log.debug("讀取進行中的模型呼叫失敗", exc_info=True)
        return sorted(out, key=lambda c: c.get("since", ""))

    def usage_summary(self) -> Dict[str, Any]:
        """用量抽屜：今日／本次啟動，依模型、CLI 座位、房間、角色。數字都來自實際紀錄。"""
        names = {r.id: (r.name or r.slug) for r in self.store.list(include_archived=True)}
        out = self.usage.summary(names)
        out["models_ready"] = self.model_cfg is not None
        out["glm_model"] = self.model_cfg.glm_model if self.model_cfg else "glm-5.3"
        out["d1_model"] = self.model_cfg.d1_model if self.model_cfg else "Decision-1"
        out["inflight"] = self.inflight()
        return out

    # ================= 用量試探 =================
    def _probe_due(self) -> None:
        for seat in self.quota.due_probes(datetime.now()):
            with self._lock:
                if seat in self._probing:
                    continue
                self._probing.add(seat)
            threading.Thread(target=self._probe_one, args=(seat,), name="probe-%s" % seat, daemon=True).start()

    def probe_now(self, seat: str) -> bool:
        """立刻在背景試探一個座位（不等到期）。已有試探在跑 → False。"""
        if seat not in SEATS:
            raise ValueError("不認得的座位：%s" % seat)
        if self._stop.is_set():
            return False
        with self._lock:
            if seat in self._probing:
                return False
            self._probing.add(seat)
        threading.Thread(target=self._probe_one, args=(seat,), name="probe-%s" % seat, daemon=True).start()
        return True

    def reset_seat(self, seat: str) -> None:
        """手動把座位標為可用（清除用量已滿狀態）。"""
        if seat not in SEATS:
            raise ValueError("不認得的座位：%s" % seat)
        self.quota.mark_ok(seat)
        self._changed()
        self.schedule()

    def _probe_one(self, seat: str) -> None:
        try:
            try:
                ok = bool(self.prober(seat))
            except Exception:
                log.info("試探 %s 失敗", seat, exc_info=True)
                ok = False
            if ok:
                self.quota.mark_ok(seat)
                log.info("座位 %s 已恢復", seat)
            else:
                self.quota.mark_exhausted(seat, "", datetime.now(), reason=self.quota.reason(seat))  # 下次再試
        finally:
            with self._lock:
                self._probing.discard(seat)
        self._changed()

    def _probe_seat(self, seat: str) -> bool:
        """以目錄裡最便宜的模型送一則極短訊息；成功回 True。不寫房間事件、不記錄回覆內容。"""
        entries = self.catalog.get(seat) or [{"id": "", "cost": 0}]
        model = str(min(entries, key=lambda e: e.get("cost", 0)).get("id", ""))
        tmp = os.path.join(self.store.root, "probe", seat)
        os.makedirs(tmp, exist_ok=True)
        try:
            cli, env, extra = seat_launch(seat, self._clis(), self.model_cfg, (), tmp, tmp)
        except ValueError:
            return False
        if "--mcp-config" in extra:  # 試探不需要 Serena
            i = extra.index("--mcp-config")
            extra = extra[:i] + extra[i + 2:]
        errors = []  # type: List[str]
        drv = make_driver(cli, tmp, lambda ev: errors.append(ev.kind) if ev.kind == "error" else None,
                          lambda req: policy.pick_option(req.options, "reject"), True,
                          env=env, extra_args=extra, model=model)
        try:
            drv.start()
            stop = drv.prompt(PROBE_PROMPT, PROBE_TIMEOUT)
        except Exception:
            return False
        finally:
            try:
                drv.close()
            except Exception:
                pass
        return stop != "error" and not errors

    # ================= Task 18：使用者選模型 =================
    def models_view(self) -> Dict[str, Any]:
        """GET /api/team/models：每個座位的可用模型、你的選擇、偵測狀態，以及判斷用途的選項。"""
        with self._lock:
            detecting = set(self._detecting)
        return self.prefs.view(self.available_seats(), detecting, SEAT_LABELS)

    def save_models(self, body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            warnings = self.prefs.update(body)
        except PrefsError as e:
            return {"ok": False, "text": str(e)}
        self._changed()
        text = "已儲存模型設定（進行中的房間下一回合生效）"
        if warnings:
            text += "。注意：" + "；".join(warnings)
        return {"ok": True, "text": text, "warnings": warnings, "models": self.models_view()}

    def detect_models(self, seats: Optional[Sequence[str]] = None) -> List[str]:
        """背景偵測 ACP 座位的可用模型（initialize＋session/new，不送提示、不花 token）。
        每個座位同時最多一個偵測；回傳這次有開始的座位。"""
        avail = self.available_seats()
        want = [s for s in (seats or ACP_SEATS) if s in ACP_SEATS and s in avail]
        started = []
        for seat in want:
            with self._lock:
                if seat in self._detecting or self._stop.is_set():
                    continue
                self._detecting.add(seat)
            started.append(seat)
            threading.Thread(target=self._detect_one, args=(seat,), name="detect-%s" % seat, daemon=True).start()
        if started:
            self._changed()
        return started

    def _detect_one(self, seat: str) -> None:
        try:
            try:
                res = self.model_detector(seat) or {}
                models = res.get("models") or []
                if not models:
                    self.prefs.note_detected(seat, [], error="代理沒有回報可用模型（可能不支援選模型）")
                else:
                    self.prefs.note_detected(seat, models, str(res.get("current") or ""))
            except Exception as e:
                log.info("偵測 %s 的模型失敗：%s", seat, type(e).__name__)
                self.prefs.note_detected(seat, [], error="偵測失敗：%s" % str(e)[:200])
        finally:
            with self._lock:
                self._detecting.discard(seat)
            self._changed()

    def _detect_seat(self, seat: str) -> Dict[str, Any]:
        """開一個 ACP 對話（initialize＋session/new）讀 availableModels 後立刻關閉；不送 prompt。
        公司網路憑證環境（make_driver）照樣套用。"""
        tmp = os.path.join(self.store.root, "probe", seat)
        os.makedirs(tmp, exist_ok=True)
        cli, env, extra = seat_launch(seat, self._clis(), self.model_cfg, (), tmp, tmp)
        drv = make_driver(cli, tmp, lambda ev: None, lambda req: policy.pick_option(req.options, "reject"), True,
                          env=env, extra_args=extra)
        with self._lock:
            if self._stop.is_set():
                raise RuntimeError("辦公室已停止")
            self._detect_drivers[seat] = drv
        try:
            drv.start()
            client = getattr(drv, "client", None)
            wait = getattr(client, "wait_models", None)
            if wait is not None:
                wait(3.0)  # Kiro v3：清單在 session/new 之後的 config_option_update 才來
            return {"models": list(getattr(client, "available_models", []) or []),
                    "current": getattr(client, "current_model", "")}
        finally:
            with self._lock:
                self._detect_drivers.pop(seat, None)
            try:
                drv.close()
            except Exception:
                pass

    def set_room_model(self, room_id: str, role: str, seat: str, model: str) -> Room:
        """房間中途改實作／審查的座位＋模型（空＝自動）；下一回合生效。驗證不過 → ValueError（中文）。"""
        room = self._room(room_id)
        if role not in ("impl", "review"):
            raise ValueError("role 只能是 impl 或 review")
        seat = (seat or "").strip() or "auto"
        model = (model or "").strip()
        if model == "auto":
            model = ""
        if room.demo:
            raise ValueError("示範房使用假代理，不能指定模型")
        if room.status in ("done", "archived"):
            raise ValueError("這間房已經結束，不能再改模型")
        if seat != "auto" and seat not in SEATS:
            raise ValueError("不認得的座位：%s（可選：auto、%s）" % (seat, "、".join(SEATS)))
        if model and seat == "auto":
            raise ValueError("要指定模型，請先選座位")
        other = room.review_cli if role == "impl" else room.impl_cli
        if seat != "auto" and seat == other:
            raise ValueError("實作與審查要用不同的座位")
        if seat != "auto":
            p = self._seat_problem(seat)
            if p:
                raise ValueError(p)
        if model:
            p = self.prefs.problem(seat, model)
            if p:
                raise ValueError(p)
        cli_key, pref_key = ("impl_cli", "impl_pref") if role == "impl" else ("review_cli", "review_pref")

        def mut(r: Room) -> None:
            setattr(r, cli_key, seat)
            setattr(r, pref_key, {"seat": seat, "model": model} if seat != "auto" else {})
            r.user_pinned = r.impl_cli != "auto" or r.review_cli != "auto"
        room = self.store.update(room.id, mut)
        what = "實作" if role == "impl" else "審查"
        if seat == "auto":
            text = "你把%s改成自動（Decision-1 派工）" % what
        elif model:
            text = "你把%s模型改成 %s（%s）" % (what, model, SEAT_LABELS.get(seat, seat))
        else:
            text = "你把%s座位改成 %s（模型依設定）" % (what, SEAT_LABELS.get(seat, seat))
        self._event(room.id, "prefs", text + "；下一回合生效", role=role, seat=seat, model=model)
        self._changed()
        return room

    # ================= 登入 =================
    def _check_login(self, seat: str, demo: bool) -> Tuple[bool, str]:
        if self.login_checker is not None:
            return self.login_checker(seat)
        if demo:
            return True, "示範座位不需要登入"
        return check_login(seat, self._clis())

    def _login_rooms(self, item: InboxItem) -> List[str]:
        out = []
        for rid in list(item.rooms or []) or [item.room_id]:
            room = self.store.get(rid)
            if room is not None and room.status != "archived" and rid not in out:
                out.append(rid)
        return out

    def _answer_login(self, item: InboxItem, answer: str) -> Dict[str, Any]:
        demo = item.scope == "demo"
        seat = item.seat
        label = SEAT_LABELS.get(seat, seat)
        rooms = self._login_rooms(item)
        if answer == "open":
            try:
                spec = login_command(seat, DEMO_CLIS if demo else self._clis())
            except ValueError as e:
                return {"ok": False, "text": str(e)}
            if spec is None:
                return {"ok": False, "text": "%s 不需要登入" % label}
            try:
                if self.login_launcher is not None:
                    self.login_launcher(list(spec[0]), "%s 登入" % label)
                elif not demo:
                    open_login_window(spec[0], "%s 登入" % label)
            except Exception as e:
                log.info("無法開啟登入視窗：%s", type(e).__name__)
                return {"ok": False, "text": "無法開啟登入視窗：%s" % e}
            self.store.update_item(item.id, lambda x: (setattr(x, "state", "opened"), setattr(x, "note", "")))
            for rid in rooms:
                self._event(rid, "notice", "已開啟 %s 登入視窗：%s" % (label, spec[1]))
            self._watch_login(item.id, self.login_timeout)
            self._changed()
            return {"ok": True, "text": LOGIN_OPENED}
        if answer == "check":
            self._watch_login(item.id, 0)
            return {"ok": True, "text": "正在檢查 %s 的登入狀態…" % label}
        # switch：座位暫時排除（原因「未登入」），房間改由派工主管另選座位
        with self._lock:
            if item.id in self._login_finished:
                return {"ok": False, "text": "這個項目已經處理過了"}
            self._login_finished.add(item.id)
            self._login_watch.pop(item.id, None)
        quota = self.demo_quota if demo else self.quota
        quota.mark_exhausted(seat, "", datetime.now(), reason="login")
        for rid in rooms:
            self._set(rid, user_pinned=False)
        self.store.resolve(item.id, "switch", "改用其他座位")
        cur = self.store.get_item(item.id) or item
        for rid in rooms:
            self._event(rid, "answer", "%s 未登入，改用其他座位（房間改成自動派座位）" % label,
                        item_id=item.id, answer="switch")
            self._wake_login(rid, cur, "switch")
        self._changed()
        return {"ok": True, "text": "好，受影響的房間改成自動派座位；%s 先暫停（所有房間），登入成功或 60 分鐘後再試"
                % label}

    def _watch_login(self, item_id: str, duration: float) -> None:
        """背景確認登入：duration > 0 → 每 login_interval 秒確認直到逾時；0 → 只確認一次。同一項目只有一條執行緒。"""
        with self._lock:
            st = self._login_watch.get(item_id)
            if st is not None:
                if duration > 0:
                    st["deadline"] = max(st["deadline"], time.monotonic() + duration)
                    st["long"] = True
                st["ev"].set()  # 立刻再確認一次
                return
            st = {"deadline": time.monotonic() + duration, "ev": threading.Event(), "long": duration > 0}
            self._login_watch[item_id] = st
        threading.Thread(target=self._login_loop, args=(item_id, st), name="login-%s" % item_id,
                         daemon=True).start()

    def _login_loop(self, item_id: str, st: Dict[str, Any]) -> None:
        try:
            while not self._stop.is_set():
                st["ev"].clear()
                item = self.store.get_item(item_id)
                if item is None or item.resolved:
                    return
                try:
                    ok, why = self._check_login(item.seat, item.scope == "demo")
                except Exception as e:
                    ok, why = False, "檢查失敗：%s" % type(e).__name__
                if ok:
                    self._login_succeeded(item)
                    return
                stamp = datetime.now().strftime("%H:%M:%S")
                with self._lock:
                    done = time.monotonic() >= st["deadline"]
                    long_ = st["long"]
                    if done:
                        self._login_watch.pop(item_id, None)
                if done and long_:
                    note = ("還沒偵測到登入（已等 %s，最後檢查 %s：%s）。登入完成後按「我已登入，重新檢查」，或再開一次登入視窗"
                            % (_minutes(self.login_timeout), stamp, why))
                    self.store.update_item(item_id, lambda x: (setattr(x, "state", ""), setattr(x, "note", note)))
                else:
                    note = "還沒偵測到登入（%s 檢查：%s）" % (stamp, why)
                    self.store.update_item(item_id, lambda x: setattr(x, "note", note))
                self._changed()
                if done:
                    return
                st["ev"].wait(self.login_interval)
        finally:
            with self._lock:
                if self._login_watch.get(item_id) is st:
                    self._login_watch.pop(item_id, None)

    def _login_succeeded(self, item: InboxItem) -> None:
        with self._lock:
            if item.id in self._login_finished:
                return
            self._login_finished.add(item.id)
        cur = self.store.get_item(item.id)
        if cur is None or cur.resolved:
            return
        label = SEAT_LABELS.get(cur.seat, cur.seat)
        quota = self.demo_quota if cur.scope == "demo" else self.quota
        if quota.reason(cur.seat) == "login":
            quota.mark_ok(cur.seat)
        self.store.resolve(cur.id, "ok", "已登入")
        for rid in self._login_rooms(cur):
            if self._wake_login(rid, cur, "ok"):
                self._event(rid, "notice", "%s 已登入，繼續工作" % label)
            else:
                self._event(rid, "notice", "%s 已登入，按繼續接著做" % label)
        self._changed()
        self.schedule()

    def _wake_login(self, rid: str, item: InboxItem, answer: str) -> bool:
        """叫醒等這個登入項目的房間；回傳是否會自動繼續。

        自動繼續只限：執行器正在等這個項目、還在等你（waiting_user）、或因後端重新啟動而暫停的房間。
        你自己暫停的房間（item.paused）不動。"""
        if rid in (item.paused or []):
            return False
        r = self._alive(rid)
        if r is not None and r.waiting_on(item.id) and r.notify_login(item.id, answer):
            return True
        if r is not None and not r.stopping:
            return True  # 執行器還活著：它會從磁碟看到項目已解決
        room = self.store.get(rid)
        if room is None:
            return False
        restarted = room.status == "paused" and room.activity.startswith(RESTART_ACTIVITY)
        if not (room.status == "waiting_user" or restarted):
            return False
        try:
            self.resume(rid)
        except ValueError:
            return False
        return True


def _minutes(sec: float) -> str:
    return "%d 分鐘" % round(sec / 60) if sec >= 60 else "%g 秒" % sec
