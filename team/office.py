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
import re
import threading
import time
from dataclasses import asdict
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from team import ce_sync, gitops, kb, notes, personas, policy, prompts
from team.clis import CliInfo, discover
from team.drivers import GLM_SEATS, make_driver, seat_launch
from team.fleet import SEAT_LABELS, SEATS, Dispatcher, QuotaBook
from team.modelprefs import ACP_SEATS, CLAUDE_DETECTED_MODELS, DETECTABLE_SEATS, ModelPrefs, PrefsError, probe_claude_models
from team.gitops import GitError, Worktree
from team import netcerts
from team.login import check_login, login_command, open_login_window
from team.models import ModelConfig, ModelRouter, load_model_config
from team.rooms import InboxItem, Room, RoomStore, normalize_target_subdirs, slim_event
from team.runner import DELEGABLE, OPT_ERROR, RoomRunner
from team.ssh import (
    SshError,
    SshHostStore,
    has_ssh_password,
    install_ssh_public_key,
    is_ssh_auth_error,
    probe_host_clis,
    remote_dirs,
    remote_fs,
    remote_scan,
    set_ssh_password,
    validate_ssh_target,
)
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
                 model_detector: Optional[Callable[[str], Dict[str, Any]]] = None, detect_on_start: bool = True,
                 ssh_probe_runner: Optional[Callable[..., Dict[str, Any]]] = None):
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
        # LLM Wiki 專案知識庫
        self.kb = kb.KnowledgeBase(store.root, config=self.model_cfg)
        # SSH 遠端開發主機管理與 CLI 偵測快取
        self.ssh_store = SshHostStore(
            os.path.join(base_dir, "ssh_hosts.local.json"),
            os.path.join(store.root, "ssh_clis_cache.json"),
        )
        self.ssh_probe_runner = ssh_probe_runner
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

    def _dispatcher(self, demo: bool, ssh_host: str = "") -> Dispatcher:
        sh = (ssh_host or "").strip()
        key = ("demo" if demo else "real", sh)
        with self._lock:
            d = self._dispatchers.get(key)  # type: ignore[arg-type]
            if d is None:
                avail = (lambda: set(SEATS)) if demo else (lambda h=sh: self.available_seats(h))
                d = Dispatcher(self._router(demo), self.catalog, self.demo_quota if demo else self.quota,
                               available=avail, prefs=None if demo else self.prefs)
                self._dispatchers[key] = d  # type: ignore[index]
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

    @staticmethod
    def _has_valid_ssh_probe(probe: Optional[Dict[str, Any]]) -> bool:
        """判斷 SSH CLI 探測快取是否有效（失敗或無可用座位的快取不可當成有效結果，避免毒化座位表）。"""
        return bool(isinstance(probe, dict) and probe.get("ok") and probe.get("available_seats"))

    def _clis_for_host(self, ssh_host: str = "") -> Dict[str, CliInfo]:
        sh = (ssh_host or "").strip()
        if not sh:
            return self._clis()
        probe = self.ssh_store.get_cached_probe(sh)
        if not self._has_valid_ssh_probe(probe):
            probe = self.probe_ssh_host(sh, force=True)
        out: Dict[str, CliInfo] = {}
        for name, info in (probe.get("clis") or {}).items():
            if isinstance(info, dict) and info.get("callable") and info.get("path"):
                out[name] = CliInfo(
                    name=str(name),
                    label=str(info.get("label") or SEAT_LABELS.get(name, name)),
                    path=str(info["path"]),
                    protocol=str(info.get("protocol") or ("acp" if name in ("kiro", "agy") else "headless")),
                )
        return out

    def available_seats(self, ssh_host: str = "") -> Set[str]:
        sh = (ssh_host or "").strip()
        if sh:
            probe = self.ssh_store.get_cached_probe(sh)
            if not self._has_valid_ssh_probe(probe):
                try:
                    probe = self.probe_ssh_host(sh, force=True)
                except Exception:
                    return set()
            return {str(s) for s in (probe.get("available_seats") or []) if str(s) in SEATS}
        clis = self._clis()
        seats = {s for s in ("kiro", "agy", "claude") if s in clis}
        if "claude" in clis and self.model_cfg is not None:
            seats |= set(GLM_SEATS)
        return seats

    def _seat_problem(self, seat: str, ssh_host: str = "") -> str:
        sh = (ssh_host or "").strip()
        if sh:
            avail = self.available_seats(sh)
            if seat not in avail:
                return "遠端主機 %s 上的 %s 無法調用（未安裝、未登入或握手檢測失敗），請改選其他可用 CLI 或「自動」" % (
                    sh,
                    SEAT_LABELS.get(seat, seat),
                )
            return ""
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

    # ================= SSH 遠端開發與 CLI 偵測 =================
    def ssh_hosts_view(self) -> Dict[str, Any]:
        hosts = self.ssh_store.list_hosts()
        cached = self.ssh_store.all_cached()
        for h in hosts:
            t = h.get("target", "")
            h["has_password"] = has_ssh_password(t)
            if t in cached:
                c = cached[t]
                h["probe"] = {
                    "ok": bool(c.get("ok")),
                    "auth_required": bool(c.get("auth_required")),
                    "error": str(c.get("error") or ""),
                    "available_seats": list(c.get("available_seats") or []),
                    "clis": dict(c.get("clis") or {}),
                    "hostname": str(c.get("hostname") or ""),
                    "platform": str(c.get("platform") or ""),
                    "checked_at": c.get("checked_at", 0),
                }
        return {"ok": True, "hosts": hosts}

    def add_ssh_host(self, target: str, label: str = "") -> Dict[str, Any]:
        entry = self.ssh_store.add_host(target, label=label)
        self._changed()
        return {"ok": True, "host": entry, "hosts": self.ssh_hosts_view()["hosts"]}

    def remove_ssh_host(self, target_id: str) -> Dict[str, Any]:
        removed = self.ssh_store.remove_host(target_id)
        self._changed()
        return {"ok": True, "removed": removed, "hosts": self.ssh_hosts_view()["hosts"]}

    def probe_ssh_host(
        self,
        ssh_host: str = "",
        force: bool = True,
        password: str = "",
        install_key: bool = False,
        quick_auth: bool = False,
    ) -> Dict[str, Any]:
        sh = (ssh_host or "").strip()
        if sh:
            err = validate_ssh_target(sh)
            if err:
                raise ValueError(err)
            if password:
                set_ssh_password(sh, password)
                force = True
            if quick_auth:
                from team.ssh import remote_fs
                st = remote_fs(sh, "stat", "~", runner=self.ssh_probe_runner, timeout=15.0)
                if not st.get("ok"):
                    raise ValueError(str(st.get("error") or f"SSH 連線驗證失敗：{sh}"))
                cached = self.ssh_store.get_cached_probe(sh) or {}
                res: Dict[str, Any] = dict(cached)
                res.update({"ok": True, "ssh_host": sh, "has_password": has_ssh_password(sh), "quick_auth": True})
                if install_key:
                    try:
                        key_res = install_ssh_public_key(sh, runner=self.ssh_probe_runner)
                        res["key_installed"] = bool(key_res.get("ok"))
                        res["key_message"] = str(key_res.get("text") or "")
                    except Exception as e:
                        res["key_installed"] = False
                        res["key_message"] = str(e)
                # 若遠端 CLI 快取尚未建立或先前為失敗狀態，背景自動補做一次完整探測，不阻塞快速驗證回應
                if not self._has_valid_ssh_probe(cached):
                    def _bg_warm() -> None:
                        try:
                            self.probe_ssh_host(sh, force=True)
                        except Exception:
                            log.debug("背景補探測 SSH CLI 失敗：%s", sh, exc_info=True)
                    threading.Thread(target=_bg_warm, daemon=True, name=f"ssh-warm-{sh[:16]}").start()
                self._changed()
                return res
            if not force:
                cached = self.ssh_store.get_cached_probe(sh)
                if self._has_valid_ssh_probe(cached):
                    out = dict(cached)
                    out["has_password"] = has_ssh_password(sh)
                    return out
        res = probe_host_clis(
            sh,
            has_glm_cfg=self.model_cfg is not None,
            glm_model=self.model_cfg.glm_model if self.model_cfg else "glm-5.3",
            runner=self.ssh_probe_runner,
        )
        res["has_password"] = has_ssh_password(sh)
        if res.get("ok") and install_key and sh:
            try:
                key_res = install_ssh_public_key(sh, runner=self.ssh_probe_runner)
                res["key_installed"] = bool(key_res.get("ok"))
                res["key_message"] = str(key_res.get("text") or "")
            except Exception as e:
                res["key_installed"] = False
                res["key_message"] = str(e)
        if res.get("ok"):
            self.ssh_store.set_cached_probe(sh, res)
        self._changed()
        return res

    def remote_browse_dirs(self, ssh_host: str, path: str = "") -> Dict[str, Any]:
        sh = (ssh_host or "").strip()
        err = validate_ssh_target(sh)
        if err:
            raise ValueError(err)
        return remote_dirs(sh, path, runner=self.ssh_probe_runner)

    def remote_scan_project(self, ssh_host: str, path: str) -> Dict[str, Any]:
        sh = (ssh_host or "").strip()
        err = validate_ssh_target(sh)
        if err:
            raise ValueError(err)
        return remote_scan(sh, path, runner=self.ssh_probe_runner)

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
                if hasattr(r, "receive_user_directive"):
                    r.receive_user_directive(t)
                else:
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
        sh = (getattr(room, "ssh_host", "") or "").strip()
        try:
            rollover = float(self.context_rollover_fn())
        except Exception:
            rollover = 0.70
        rid = room.id
        overrides = self._demo_overrides(rid) if demo else None
        use_v2 = (
            not demo
            and not overrides
            and (getattr(room, "engine_version", "v2") or "v2") == "v2"
            and not self.runner_kw.get("_force_v1")
        )
        if use_v2:
            from engine.runner_v2 import EngineV2Runner

            return EngineV2Runner(
                rid,
                self.store,
                self._router(False),
                self._clis_for_host(sh),
                list(self.deny_patterns_fn() or []),
                lambda: self._runner_changed(rid),
                dispatcher=self._dispatcher(False, ssh_host=sh),
                quota=self.quota,
                model_cfg=self.model_cfg,
                plugin_dirs=list(self.plugin_dirs_fn() or []),
                on_exit=self._runner_exited,
                kb=self.kb,
                usage=self.usage,
                **dict(self.runner_kw),
            )  # type: ignore[return-value]
        return _OfficeRunner(self, rid, self.store, self._router(demo),
                             DEMO_CLIS if demo else self._clis_for_host(sh),
                             list(self.deny_patterns_fn() or []), lambda: self._runner_changed(rid),
                             cmd_overrides=overrides,
                             dispatcher=self._dispatcher(demo, ssh_host=sh),
                             quota=self.demo_quota if demo else self.quota,
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
    def sync_room_progress(self, room_id: str, extra_note: str = "") -> Dict[str, Any]:
        """即時產生並同步房間的 `progress.md`（寫入 `.ai-team/<room_id>/progress.md` 與工作區 `docs/ai-team/<slug>/progress.md`）。"""
        room = self._room(room_id)
        sh = (getattr(room, "ssh_host", "") or "").strip()
        commits: List[Tuple[str, str]] = []
        if room.worktree and (sh or os.path.isdir(room.worktree)):
            try:
                commits = gitops.commits_since(Worktree(room.repo, room.worktree, room.branch, room.base, ssh_host=sh))
            except Exception:
                commits = []
        ev_list = self.store.events(room_id, limit=40)
        open_items = [i.title for i in self._open_items(room_id)]
        in_place = bool(room.target_subdir or room.target_subdirs or room.reference_root)
        nd = room.notes_dir
        if not nd and sh:
            nd = os.path.join(self.store.root, room_id, "notes")
        elif not nd and room.repo and os.path.isdir(room.repo):
            try:
                nd = notes.notes_dir(room.repo, room.slug or room.id, in_place=in_place)
            except Exception:
                nd = ""
        return ce_sync.write_room_progress_files(
            self.store.root,
            room,
            events_list=ev_list,
            commits=commits,
            open_items=open_items,
            notes_dir=nd,
            extra_note=extra_note,
        )

    def get_room_progress(self, room_id: str) -> Dict[str, Any]:
        """讀取房間的 `progress.md` 內容與路徑資訊；若已被清空或尚未建立則回傳空內容（不自動重建）。"""
        room = self._room(room_id)
        sh = (getattr(room, "ssh_host", "") or "").strip()
        in_place = bool(room.target_subdir or room.target_subdirs or room.reference_root)
        nd = room.notes_dir
        if not nd and sh:
            nd = os.path.join(self.store.root, room_id, "notes")
        elif not nd and room.repo and os.path.isdir(room.repo):
            try:
                nd = notes.notes_dir(room.repo, room.slug or room.id, in_place=in_place)
            except Exception:
                nd = ""
        store_prog = os.path.join(self.store.root, room_id, "progress.md")
        notes_prog = os.path.join(nd, "progress.md") if nd else ""
        markdown = ""
        for p in (store_prog, notes_prog):
            if p and os.path.isfile(p):
                try:
                    with open(p, encoding="utf-8") as f:
                        markdown = f.read()
                    break
                except OSError:
                    pass
        return {
            "markdown": markdown,
            "store_path": store_prog,
            "notes_path": notes_prog,
        }

    def create_room(self, *, repo: str, goal: str, source: str, impl_cli: str = "auto",
                    review_cli: str = "auto", autonomy: str = "balanced",
                    allowed_paths: Sequence[str] = (), name: str = "", impl_model: str = "",
                    review_model: str = "", target_subdir: str = "",
                    target_subdirs: Optional[Sequence[str]] = None,
                    reference_root: str = "",
                    supervisor_cli: str = "auto", supervisor_model: str = "",
                    role_prefs: Optional[Dict[str, Dict[str, str]]] = None,
                    ssh_host: str = "",
                    task_mode: str = "standard",
                    d1_fast_review: bool = True,
                    use_worktree: bool = False,
                    engine_version: str = "v2") -> Room:
        return self._create_room(repo=repo, goal=goal, source=source, impl_cli=impl_cli, review_cli=review_cli,
                                 autonomy=autonomy, allowed_paths=allowed_paths, name=name,
                                 impl_model=impl_model, review_model=review_model,
                                 target_subdir=target_subdir, target_subdirs=target_subdirs,
                                 reference_root=reference_root,
                                 supervisor_cli=supervisor_cli, supervisor_model=supervisor_model,
                                 role_prefs=role_prefs,
                                 ssh_host=ssh_host,
                                 task_mode=task_mode,
                                 d1_fast_review=d1_fast_review,
                                 use_worktree=use_worktree,
                                 engine_version=engine_version)

    def _create_room(self, *, repo: str, goal: str, source: str, impl_cli: str, review_cli: str,
                     autonomy: str, allowed_paths: Sequence[str], name: str = "", demo: bool = False,
                     slug: str = "", cmd_overrides: Optional[Dict[str, List[str]]] = None,
                     impl_model: str = "", review_model: str = "",
                     target_subdir: str = "",
                     target_subdirs: Optional[Sequence[str]] = None,
                     reference_root: str = "",
                     supervisor_cli: str = "auto", supervisor_model: str = "",
                     role_prefs: Optional[Dict[str, Dict[str, str]]] = None,
                     ssh_host: str = "",
                     task_mode: str = "standard",
                     d1_fast_review: bool = True,
                     use_worktree: bool = False,
                     engine_version: str = "v2") -> Room:
        ssh_host = (ssh_host or "").strip()
        if ssh_host:
            err = validate_ssh_target(ssh_host)
            if err:
                raise ValueError(err)
        repo = (repo or "").strip()
        if not repo:
            raise ValueError("請選擇專案資料夾")
        if ssh_host:
            try:
                st = remote_fs(ssh_host, "stat", repo, runner=self.ssh_probe_runner)
            except SshError as e:
                raise ValueError(str(e))
            if not st.get("isdir"):
                raise ValueError("遠端主機 %s 找不到資料夾：%s" % (ssh_host, repo))
            repo = str(st.get("abspath") or repo)
        else:
            repo = os.path.abspath(repo)
            if not os.path.isdir(repo):
                raise ValueError("找不到資料夾：%s" % repo)
        explicit_ref_root = (reference_root or "").strip()
        if explicit_ref_root and not ssh_host and os.path.isdir(explicit_ref_root):
            explicit_ref_root = os.path.abspath(explicit_ref_root)
        subs = normalize_target_subdirs(target_subdir, list(target_subdirs) if target_subdirs else None)
        reference_root = explicit_ref_root
        for sub in subs:
            if sub.startswith("/") or ":" in sub or ".." in sub.split("/"):
                raise ValueError("目標子資料夾必須是母資料夾內的相對路徑：%s" % sub)
            base_for_sub = explicit_ref_root or repo
            if ssh_host:
                sep = "\\" if ("\\" in base_for_sub and "/" not in base_for_sub) else "/"
                sub_full = base_for_sub.rstrip("/\\") + sep + sub.replace("/", sep)
                st_sub = remote_fs(ssh_host, "stat", sub_full, runner=self.ssh_probe_runner)
                if not st_sub.get("isdir"):
                    raise ValueError("遠端主機 %s 找不到目標子資料夾：%s" % (ssh_host, sub_full))
            else:
                sub_full = os.path.abspath(os.path.join(base_for_sub, sub))
                if not os.path.isdir(sub_full):
                    raise ValueError("找不到目標子資料夾：%s" % sub_full)
        if len(subs) == 1:
            base_for_sub = explicit_ref_root or repo
            reference_root = base_for_sub
            if ssh_host:
                sep = "\\" if ("\\" in base_for_sub and "/" not in base_for_sub) else "/"
                repo = base_for_sub.rstrip("/\\") + sep + subs[0].replace("/", sep)
            else:
                repo = os.path.abspath(os.path.join(base_for_sub, subs[0]))
            target_subdir = subs[0]
            gitops.ensure_local_git_repo(repo, ssh_host=ssh_host)
        elif len(subs) > 1:
            # 複數目標資料夾（例如前後端雙專案 frontend + backend 同時為目標）：
            # 以母資料夾作為工作根目錄，並將複數目標資料夾設為允許修改範圍，其餘兄弟資料夾設為唯讀參考
            base_for_sub = explicit_ref_root or repo
            reference_root = base_for_sub
            repo = base_for_sub
            target_subdir = ", ".join(subs)
            gitops.ensure_local_git_repo(repo, ssh_host=ssh_host)
        else:
            target_subdir = ""
        prob = gitops.repo_problem(repo, ssh_host=ssh_host)
        if prob:
            raise ValueError("%s（%s）" % (prob, repo))
        goal = (goal or "").strip()
        if not goal:
            raise ValueError("請輸入目標")
        if len(goal) > MAX_GOAL:
            raise ValueError("目標太長（上限 %d 字）" % MAX_GOAL)
        allowed_seats = set(SEATS) | {"litellm"}
        impl_cli = (impl_cli or "auto").strip()
        review_cli = (review_cli or "auto").strip()
        supervisor_cli = (supervisor_cli or "auto").strip()
        supervisor_model = (supervisor_model or "").strip()
        if supervisor_model == "auto":
            supervisor_model = ""
        for c in (impl_cli, review_cli, supervisor_cli):
            if c != "auto" and c not in allowed_seats:
                raise ValueError("不認得的座位：%s（可選：auto、%s）" % (c, "、".join(sorted(allowed_seats))))
        if impl_cli not in ("auto", "litellm") and impl_cli == review_cli:
            raise ValueError("實作與審查要用不同的座位")
        if not demo:
            if ssh_host and not self.available_seats(ssh_host):
                raise ValueError("遠端主機 %s 上沒有任何可調用的 CLI（claude / agy / kiro）" % ssh_host)
            for c in (impl_cli, review_cli, supervisor_cli):
                if c not in ("auto", "litellm"):
                    p = self._seat_problem(c, ssh_host=ssh_host)
                    if p:
                        raise ValueError(p)
        prefs = {}  # type: Dict[str, Dict[str, str]]
        for role, c, m in (("impl", impl_cli, impl_model), ("review", review_cli, review_model)):
            m = (m or "").strip()
            if m == "auto":
                m = ""
            if m and (c == "auto" or demo):
                raise ValueError("示範房不能指定模型" if demo else "要指定模型，請先選座位")
            if m and not ssh_host and c != "litellm":
                p = self.prefs.problem(c, m)
                if p:
                    raise ValueError(p)
            if c != "auto":
                prefs[role] = {"seat": c, "model": m}
        sup_pref: Dict[str, str] = {}
        if supervisor_cli != "auto":
            sup_pref = {"seat": supervisor_cli, "model": supervisor_model}
        clean_role_prefs: Dict[str, Dict[str, str]] = {}
        if isinstance(role_prefs, dict):
            for r_key, r_val in role_prefs.items():
                if r_key not in self._MODEL_ROLE_LABELS or not isinstance(r_val, dict):
                    continue
                r_seat = str(r_val.get("seat") or "auto").strip() or "auto"
                r_mod = str(r_val.get("model") or "").strip()
                if r_mod == "auto":
                    r_mod = ""
                if r_seat != "auto" and r_seat in allowed_seats:
                    if not demo and r_seat != "litellm":
                        p = self._seat_problem(r_seat, ssh_host=ssh_host)
                        if p:
                            raise ValueError(p)
                    clean_role_prefs[r_key] = {"seat": r_seat, "model": r_mod}
        for k_role, v_pref in prefs.items():
            clean_role_prefs[k_role] = v_pref
        if sup_pref:
            clean_role_prefs["supervisor"] = sup_pref
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
        if len(subs) > 1:
            for s in subs:
                sp = f"{s.rstrip('/')}/"
                if sp not in paths:
                    paths.append(sp)
            if "docs/" not in paths:
                paths.append("docs/")
        repo_base = os.path.basename(repo.rstrip("/\\"))
        name = (name or "").strip() or (" + ".join(subs) if len(subs) > 1 else repo_base)
        in_place = bool(target_subdir or reference_root)
        with self._lock:
            slug = slug or self._unique_slug(repo, goal, in_place=in_place, ssh_host=ssh_host)
            room = self.store.create(name=name, repo=repo, slug=slug, goal=goal,
                                     source=(source or "goal").strip()[:2000], impl_cli=impl_cli,
                                     review_cli=review_cli, autonomy=autonomy, allowed_paths=paths,
                                     reference_root=reference_root, target_subdir=target_subdir,
                                     target_subdirs=subs, ssh_host=ssh_host,
                                     status="queued", phase="goal", activity="排隊中", demo=bool(demo),
                                     user_pinned=(
                                         impl_cli != "auto"
                                         or review_cli != "auto"
                                         or supervisor_cli != "auto"
                                         or bool(clean_role_prefs)
                                     ),
                                     impl_seat=impl_cli if impl_cli != "auto" else "",
                                     impl_model=prefs.get("impl", {}).get("model", ""),
                                     review_seat=review_cli if review_cli != "auto" else "",
                                     review_model=prefs.get("review", {}).get("model", ""),
                                     supervisor_cli=supervisor_cli,
                                     supervisor_seat=supervisor_cli if supervisor_cli != "auto" else "",
                                     supervisor_model=supervisor_model if supervisor_cli != "auto" else "",
                                     supervisor_pref=sup_pref,
                                     role_prefs=clean_role_prefs,
                                     impl_pref=prefs.get("impl", {}), review_pref=prefs.get("review", {}),
                                     engine_version="v1" if demo else (engine_version if engine_version in ("v1", "v2") else "v2"),
                                     task_mode="triage" if task_mode == "triage" else "standard",
                                     d1_fast_review=bool(d1_fast_review),
                                     use_worktree=bool(use_worktree))
            if cmd_overrides:
                path = os.path.join(self.store.root, room.id, DEMO_FILE)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"cmd_overrides": cmd_overrides}, f, ensure_ascii=False, indent=2)
        self._event(room.id, "status", "已派工：%s" % goal[:200], status="queued")
        try:
            self.sync_room_progress(room.id, extra_note="房間已建立並排隊派工")
        except Exception:
            pass
        self._changed()
        self.schedule()
        return self.store.get(room.id) or room

    def _unique_slug(self, repo: str, goal: str, in_place: bool = False, ssh_host: str = "") -> str:
        base = gitops.slugify(goal)
        key = repo if ssh_host else os.path.normcase(repo)
        used = {
            r.slug
            for r in self.store.list(include_archived=True)
            if (r.repo if ssh_host else os.path.normcase(r.repo)) == key
            and (getattr(r, "ssh_host", "") or "") == (ssh_host or "")
        }
        wt_root = gitops.worktree_root(repo, in_place=in_place)
        n = 1
        while True:
            cand = base if n == 1 else "%s-%d" % (base, n)
            exists = True
            try:
                gitops.git(repo, "show-ref", "--verify", "-q", "refs/heads/ai-team/" + cand,
                           timeout=10, ssh_host=ssh_host)
            except GitError:
                exists = False
            wt_cand = os.path.join(wt_root, "%s-%s" % (os.path.basename(repo.rstrip("/\\")), cand))
            wt_exists = False if ssh_host else os.path.exists(wt_cand)
            if cand not in used and not exists and not wt_exists:
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

    def archive(self, room_id: str, force: bool = False) -> None:
        room = self._room(room_id)
        if force:
            r_alive = self._alive(room_id)
            if r_alive is not None:
                try:
                    self.pause(room_id)
                    r_alive.join(2.5)
                except Exception:
                    pass
            room = self._room(room_id)
        else:
            if self._alive(room_id) is not None:
                raise ValueError("房間還在運作或停止中，先暫停並等它停下來再封存")
            if room.status not in ("done", "paused", "error"):
                raise ValueError("只有已完成、暫停或錯誤的房間可以封存")
            if self._open_items(room_id, ("merge",)):
                raise ValueError("這間房還有待合併的成果，先合併或選『先不要』")

        # 封存前將房間完整開發紀錄與 Commits 寫入 handoff.md 與專案 LLM Wiki，確保後續開新房間絕不流失紀錄
        sh = (getattr(room, "ssh_host", "") or "").strip()
        commits: List[Tuple[str, str]] = []
        if room.worktree and (sh or os.path.isdir(room.worktree)):
            try:
                commits = gitops.commits_since(Worktree(room.repo, room.worktree, room.branch, room.base, ssh_host=sh))
            except Exception:
                commits = []
        try:
            ev_list = self.store.events(room_id, limit=120)
            handoff_md = ce_sync.build_handoff_markdown(room, ev_list, commits=commits, reason="房間封存")
            room_dir = os.path.join(self.store.root, room_id)
            os.makedirs(room_dir, exist_ok=True)
            with open(os.path.join(room_dir, "handoff.md"), "w", encoding="utf-8", newline="\n") as f:
                f.write(handoff_md)
            if room.repo and not room.demo:
                self.kb.record(
                    room.repo,
                    room_id,
                    "rooms",
                    handoff_md,
                    fingerprint=f"handoff:{room_id}:{len(ev_list)}",
                    wait=False,
                )
        except Exception:
            log.debug("寫入房間封存交接紀錄失敗", exc_info=True)

        if room.worktree and (sh or os.path.isdir(room.worktree)):
            try:
                gitops.remove_worktree(Worktree(room.repo, room.worktree, room.branch, room.base, ssh_host=sh))
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
        self._set(room_id, status="archived", persona="", activity="已封存（開發紀錄已存入 LLM Wiki）", worktree="")
        self._event(room_id, "status", "已封存（開發紀錄已存入 LLM Wiki）", status="archived")
        self._changed()

    def set_room_goal(self, room_id: str, goal: str) -> Room:
        """讓使用者自行寫入或清空房間目標（不強迫自動填入）。"""
        room = self._room(room_id)
        if room.status == "archived":
            raise ValueError("已封存的房間不能修改目標")
        clean = (goal or "").strip()
        if len(clean) > MAX_GOAL:
            raise ValueError("目標太長（上限 %d 字）" % MAX_GOAL)
        updated = self._set(room_id, goal=clean)
        if room.repo and not room.demo:
            try:
                self.kb.record(
                    room.repo,
                    room_id,
                    "rooms",
                    f"# 房間目標變更（{room.name or room_id}）\n\n- 最新目標：{clean or '（已清空）'}",
                    fingerprint=f"goal:{room_id}:{hash(clean)}",
                    wait=False,
                )
            except Exception:
                pass
        msg = "你更新了房間目標" if clean else "你清空了房間目標"
        self._event(room_id, "user", msg, goal=clean[:200])
        try:
            self.sync_room_progress(room_id, extra_note=msg)
        except Exception:
            pass
        self._changed()
        return updated

    def set_room_folders(
        self,
        room_id: str,
        *,
        repo: str = "",
        reference_root: str = "",
        target_subdir: str = "",
        target_subdirs: Optional[Sequence[str]] = None,
    ) -> Room:
        """讓使用者手動設定或切換房間的「目標可寫資料夾 B（支援單一或複數個目標資料夾）」與「唯讀參考母資料夾 A」。"""
        room = self._room(room_id)
        if room.status == "archived":
            raise ValueError("已封存的房間不能修改資料夾設定")
        next_ref = (reference_root or "").strip()
        next_repo = (repo or "").strip()

        # 支援傳入絕對路徑清單（例如直接選了兩個平行專案資料夾 D:\Proj\frontend 與 D:\Proj\backend）
        raw_subs_list = list(target_subdirs) if target_subdirs else []
        abs_targets: List[str] = []
        rel_inputs: List[str] = []
        for item in raw_subs_list:
            s = str(item or "").strip()
            if not s:
                continue
            if os.path.isabs(s) or (len(s) > 2 and s[1] == ":"):
                bad_t = local_path_problem(s)
                if bad_t:
                    raise ValueError(bad_t)
                if not os.path.isdir(s):
                    raise ValueError("找不到目標資料夾：%s" % s)
                abs_targets.append(os.path.abspath(s))
            else:
                rel_inputs.append(s)

        if len(abs_targets) >= 2 and not next_ref:
            try:
                common = os.path.commonpath(abs_targets)
                if os.path.isdir(common) and len(common) > 3:
                    next_ref = common
                    for ap in abs_targets:
                        rel_p = os.path.relpath(ap, common).replace("\\", "/").strip("/")
                        if rel_p and rel_p != ".":
                            rel_inputs.append(rel_p)
            except ValueError:
                pass
        elif len(abs_targets) == 1 and not next_repo:
            next_repo = abs_targets[0]

        subs = normalize_target_subdirs(target_subdir, rel_inputs)

        if next_ref:
            bad = local_path_problem(next_ref)
            if bad:
                raise ValueError(bad)
            if not os.path.isdir(next_ref):
                raise ValueError("找不到唯讀參考資料夾：%s" % next_ref)
            next_ref = os.path.abspath(next_ref)

        if next_ref and len(subs) > 1:
            # 複數目標子資料夾（如 frontend + backend）：以母資料夾 next_ref 作為工作根目錄
            for sub in subs:
                cand = os.path.abspath(os.path.join(next_ref, sub))
                if not os.path.isdir(cand):
                    raise ValueError("找不到目標子資料夾：%s" % cand)
            next_repo = next_ref
            next_sub = ", ".join(subs)
        elif next_ref and len(subs) == 1 and not next_repo:
            next_repo = os.path.abspath(os.path.join(next_ref, subs[0]))
            next_sub = subs[0]
        elif not next_repo:
            next_repo = room.repo
            next_sub = ", ".join(subs) if subs else ""
        else:
            next_sub = ", ".join(subs) if subs else ""

        bad_repo = local_path_problem(next_repo)
        if bad_repo:
            raise ValueError(bad_repo)
        if not os.path.isdir(next_repo):
            raise ValueError("找不到目標工作資料夾：%s" % next_repo)
        next_repo = os.path.abspath(next_repo)

        if next_ref and len(subs) <= 1:
            ref_real = os.path.normcase(os.path.realpath(next_ref))
            repo_real = os.path.normcase(os.path.realpath(next_repo))
            if repo_real.startswith(ref_real.rstrip(os.sep) + os.sep):
                next_sub = os.path.relpath(os.path.realpath(next_repo), os.path.realpath(next_ref)).replace("\\", "/")
                subs = [next_sub]
            elif repo_real == ref_real and next_sub:
                cand = os.path.abspath(os.path.join(next_ref, next_sub))
                if not os.path.isdir(cand):
                    raise ValueError("找不到目標子資料夾：%s" % cand)
                next_repo = cand
                subs = [next_sub]
            elif repo_real == ref_real and not next_sub:
                # 若參考資料夾與目標相同且未選子資料夾，則不限單一子資料夾
                next_ref = ""
                subs = []
        elif not next_ref:
            next_sub = ""
            subs = []

        # 若目標資料夾尚未有獨立 git，預先確保其本地 git 與初始 commit
        if next_ref and not gitops.has_own_git_repo(next_repo):
            gitops.ensure_local_git_repo(next_repo)

        next_allowed = list(room.allowed_paths or [])
        if len(subs) > 1:
            next_allowed = [f"{s.rstrip('/')}/" for s in subs] + ["docs/"]

        room_label = " + ".join(subs) if len(subs) > 1 else (os.path.basename(next_repo) or room.name)
        updated = self._set(
            room_id,
            repo=next_repo,
            reference_root=next_ref,
            target_subdir=next_sub,
            target_subdirs=subs,
            allowed_paths=next_allowed,
            name=room_label,
        )
        if len(subs) > 1:
            summary = f"你更新了複數目標資料夾設定：目標可寫 B = `{'、'.join(subs)}`（共 {len(subs)} 個），唯讀參考母資料夾 A = `{next_ref}`"
        elif next_ref:
            summary = f"你更新了資料夾設定：目標可寫 B = `{next_repo}`，唯讀參考 A = `{next_ref}`"
        else:
            summary = f"你更新了目標工作資料夾：`{next_repo}`（無額外唯讀母資料夾限制）"
        self._event(
            room_id,
            "user",
            summary,
            repo=next_repo,
            reference_root=next_ref,
            target_subdir=next_sub,
            target_subdirs=subs,
        )
        try:
            self.sync_room_progress(room_id, extra_note=summary)
        except Exception:
            pass
        self._changed()
        return updated

    def sync_room_plan(self, room_id: str, plan_rel: str = "", apply_goal: bool = False) -> Dict[str, Any]:
        """從專案 `docs/plans/*.md`（支援本機與 SSH 遠端）同步階段里程碑與驗證條件，並將 CE 文件寫入 LLM Wiki。"""
        room = self._room(room_id)
        sh = (getattr(room, "ssh_host", "") or "").strip()
        if sh:
            info = ce_sync.sync_remote_ce_plan(
                sh,
                room.repo,
                reference_root=room.reference_root,
                target_subdir=room.target_subdir,
                target_subdirs=list(getattr(room, "target_subdirs", None) or ()),
                plan_rel=plan_rel,
            )
        else:
            info = ce_sync.sync_room_ce_plan(room.repo, reference_root=room.reference_root, plan_rel=plan_rel)
            if room.repo and not room.demo:
                try:
                    self.kb.sync_repo_compound_docs(
                        room.repo,
                        reference_root=room.reference_root,
                        target_subdir=room.target_subdir,
                        target_subdirs=list(getattr(room, "target_subdirs", None) or ()),
                    )
                except Exception:
                    pass
        if info.get("has_plan"):
            active_rel = str((info.get("active_plan") or {}).get("rel_path") or plan_rel or "")
            raw_criteria = list(info.get("criteria") or [])
            sub_sources: List[str] = []
            if raw_criteria and room.repo:
                try:
                    raw_criteria, sub_sources = ce_sync.expand_referenced_subplan_criteria(
                        raw_criteria,
                        room.repo,
                        reference_root=room.reference_root,
                        target_subdir=room.target_subdir,
                        target_subdirs=list(getattr(room, "target_subdirs", None) or ()),
                        ssh_host=sh,
                        selected_plan_rel=active_rel,
                    )
                except Exception:
                    sub_sources = []

            def mut(r: Room) -> None:
                if active_rel:
                    r.selected_plan_rel = active_rel
                if raw_criteria:
                    r.criteria = raw_criteria
                if info.get("test_cmd"):
                    r.test_cmd = str(info["test_cmd"])
                if apply_goal and info.get("objective"):
                    r.goal = str(info["objective"])[:MAX_GOAL]
                r.graph_node = "plan"
                r.current_unit_id = ""
                r.unit_artifacts = {}
                r.attempts = {}
            room = self.store.update(room_id, mut)
            self._reset_v2_checkpoint(room_id)
            active_fn = (info.get("active_plan") or {}).get("filename", "docs/plans")
            sub_note = f"（含自動展開衍伸子計畫：{', '.join(sub_sources)}）" if sub_sources else ""
            self._event(
                room_id,
                "spec",
                "已跟隨 CE 計畫 `%s` 同步 %d 個階段里程碑%s" % (active_fn, len(raw_criteria), sub_note),
                plan=active_fn,
            )
            try:
                self.sync_room_progress(room_id, extra_note=f"已跟隨 CE 計畫 {active_fn} 同步里程碑")
            except Exception:
                pass
            if room.repo and not room.demo:
                try:
                    unit_lines = "\n".join(
                        f"- [{c.get('id', '?')}] {c.get('text', '')}" for c in (room.criteria or [])[:20]
                    )
                    self.kb.record(
                        room.repo,
                        room_id,
                        "plans",
                        f"# 房間計畫同步：{active_fn}\n\n- 目標：{room.goal or info.get('objective') or ''}\n\n## 階段單元\n{unit_lines}",
                        fingerprint=f"plan:{room_id}:{active_fn}:{len(room.criteria or [])}:{hash(unit_lines)}",
                        wait=True,
                    )
                except Exception:
                    pass
            self._changed()
        return {"ok": True, "room": asdict(room), "ce": info}

    def clear_room_plan_and_progress(self, room_id: str) -> Dict[str, Any]:
        """清空此房間的 CE 計畫單元（criteria）、重試計數與 progress.md 斷點紀錄（保留房間目標與分支）。"""
        room = self._room(room_id)
        sh = (getattr(room, "ssh_host", "") or "").strip()
        in_place = bool(room.target_subdir or room.target_subdirs or room.reference_root)
        nd = room.notes_dir
        if not nd and sh:
            nd = os.path.join(self.store.root, room_id, "notes")
        elif not nd and room.repo and os.path.isdir(room.repo):
            try:
                nd = notes.notes_dir(room.repo, room.slug or room.id, in_place=in_place)
            except Exception:
                nd = ""

        store_prog = os.path.join(self.store.root, room_id, "progress.md")
        notes_prog = os.path.join(nd, "progress.md") if nd else ""
        for p in (store_prog, notes_prog):
            if p and os.path.isfile(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

        def mut(r: Room) -> None:
            r.criteria = []
            r.test_cmd = ""
            r.pending_fix = ""
            r.unverified = ""
            r.attempts = {}
            r.rollovers = 0
            r.graph_node = ""
            r.current_unit_id = ""
            r.selected_plan_rel = ""
            r.unit_artifacts = {}
            if r.phase in ("plan", "build", "test", "review"):
                r.phase = "spec"

        room = self.store.update(room_id, mut)
        self._reset_v2_checkpoint(room_id)
        self._event(room_id, "notice", "已清空 CE 計畫單元與 progress.md 進度紀錄")
        if room.repo and not room.demo:
            try:
                self.kb.record(
                    room.repo,
                    room_id,
                    "rooms",
                    f"# 房間計畫已清空（{room.name or room_id}）\n\n- 保留目標：{room.goal or '（無）'}",
                    fingerprint=f"plan_cleared:{room_id}:{datetime.now().strftime('%Y%m%d%H%M')}",
                    wait=False,
                )
            except Exception:
                pass
        self._changed()
        ce_info = {} if sh else ce_sync.sync_room_ce_plan(room.repo, reference_root=room.reference_root)
        return {
            "ok": True,
            "text": "已清空 CE 計畫與進度紀錄",
            "room": asdict(room),
            "ce": ce_info,
            "progress": {
                "markdown": "",
                "store_path": store_prog,
                "notes_path": notes_prog,
            },
        }

    def _check_goal_clarity_d1(self, goal: str) -> Dict[str, Any]:
        """使用 Decision-1 單次 3 題快速判斷目標是否需要先進行 ce-brainstorm 互動釐清。"""
        if not (goal or "").strip() or self.model_cfg is None or not getattr(self.model_cfg, "decision1_key", ""):
            return {"used_d1": False, "needs_brainstorm": False, "scope_clear": 1.0, "acceptance_clear": 1.0}
        try:
            from ai_client import DecisionClient
            client = DecisionClient(base_url=self.model_cfg.base_url, api_key=self.model_cfg.decision1_key, timeout=8.0)
            ans = client.systemone(
                f"【使用者提供的開發目標】\n{goal[:2000]}",
                {
                    "scope_clear": {"type": "noul", "instructions": "此目標是否已明確定義開發範圍與要修改的模組/功能？"},
                    "acceptance_clear": {"type": "noul", "instructions": "此目標是否已包含明確的完成標準或驗證/測試方式？"},
                    "needs_brainstorm": {"type": "noul", "instructions": "在拆解實作計畫前，此目標是否偏模糊而需要先向使用者釐清關鍵需求？"},
                },
            )
            sc = float((ans.get("scope_clear") or {}).get("noul", 0.7))
            ac = float((ans.get("acceptance_clear") or {}).get("noul", 0.7))
            nb = float((ans.get("needs_brainstorm") or {}).get("noul", 0.3))
            return {
                "used_d1": True,
                "scope_clear": sc,
                "acceptance_clear": ac,
                "needs_brainstorm": (nb >= 0.65 or sc < 0.45 or ac < 0.45),
            }
        except Exception:
            return {"used_d1": False, "needs_brainstorm": False, "scope_clear": 1.0, "acceptance_clear": 1.0}

    def generate_room_plan_from_goal(
        self,
        room_id: str,
        goal: str = "",
        seat: str = "",
        model: str = "",
        interactive: bool = False,
    ) -> Dict[str, Any]:
        """依據房間目前目標生成 Compound Engineering 計畫（支援 interactive=True 先行 ce-brainstorm 釐清）、寫入 docs/plans/ 並同步套用階段單元。"""
        room = self._room(room_id)
        effective_goal = (goal or "").strip() or (room.goal or "").strip()
        if not effective_goal:
            raise ValueError("請先填寫房間目標，才能根據目標生成計畫")
        if effective_goal != (room.goal or "").strip():
            def _set_g(r: Room) -> None:
                r.goal = effective_goal[:MAX_GOAL]
            room = self.store.update(room_id, _set_g)

        if interactive:
            clarity = self._check_goal_clarity_d1(effective_goal)
            if clarity.get("needs_brainstorm"):
                chat_res = self.brainstorm_room(
                    room_id,
                    messages=[{"role": "user", "content": effective_goal}],
                    mode="chat",
                    seat=seat,
                    model=model,
                    goal_draft=effective_goal,
                )
                return {
                    "ok": True,
                    "needs_clarification": True,
                    "clarity": clarity,
                    "reply": chat_res.get("reply", ""),
                    "seat": chat_res.get("seat", ""),
                    "model": chat_res.get("model", ""),
                    "text": "目標尚有待釐清處，已啟動 CE Brainstorm 互動問答。",
                }

        self.store.update(
            room_id,
            lambda r: (
                setattr(r, "graph_node", "plan"),
                setattr(r, "current_unit_id", ""),
                setattr(r, "activity", "正在根據目標生成 CE 計畫…"),
            ),
        )
        self._changed()

        try:
            gen = self.brainstorm_room(
                room_id,
                messages=[{
                    "role": "user",
                    "content": (
                        "請直接根據以下目標，拆解出依序執行的 Compound Engineering 計畫與可自動驗證的階段單元"
                        "（每一項都必須明確、可獨立驗收）：\n\n" + effective_goal
                    ),
                }],
                mode="generate_plan",
                seat=seat,
                model=model,
                goal_draft=effective_goal,
            )
            content = str(gen.get("content") or "").strip()
            filename = str(gen.get("filename") or "")
            if not content:
                raise ValueError("模型未產生有效的計畫內容，請重試")
        except Exception:
            self.store.update(room_id, lambda r: setattr(r, "activity", ""))
            self._changed()
            raise

        sh = (getattr(room, "ssh_host", "") or "").strip()
        if sh:
            from team.ssh import remote_fs

            safe_name = self._sanitize_plan_filename(filename)
            subs = [s for s in (getattr(room, "target_subdirs", None) or ()) if s]
            sub = subs[0] if subs else (room.target_subdir or "").strip()
            sub_clean = sub.strip("/")
            repo_clean = room.repo.rstrip("/")
            target_base = (
                f"{repo_clean}/{sub_clean}"
                if (sub_clean and "," not in sub_clean and not sub_clean.startswith("/") and not repo_clean.endswith("/" + sub_clean))
                else room.repo
            )
            written = remote_fs(sh, "write_plan", target_base, filename=safe_name, content=content, timeout=15.0)
            if not isinstance(written, dict) or not written.get("ok"):
                raise ValueError(f"寫入遠端計畫失敗：{written}")
            actual_fn = str(written.get("filename") or safe_name)
            plan_rel = (
                f"{sub_clean}/docs/plans/{actual_fn}"
                if (sub_clean and "," not in sub_clean and not repo_clean.endswith("/" + sub_clean))
                else f"docs/plans/{actual_fn}"
            )
            sync_res = self.sync_room_plan(room_id, plan_rel=plan_rel, apply_goal=False)
            self.store.update(room_id, lambda r: setattr(r, "activity", f"已生成 CE 計畫：{plan_rel}"))
            self._changed()
            prog = self.get_room_progress(room_id)
            return {
                "ok": True,
                "text": f"已根據目標生成並寫入遠端計畫：{plan_rel}",
                "plan_rel": plan_rel,
                "seat": gen.get("seat", ""),
                "model": gen.get("model", ""),
                "room": sync_res.get("room"),
                "ce": sync_res.get("ce"),
                "progress": prog,
            }
        if room.repo and not sh and os.path.isdir(room.repo):
            saved = self.save_brainstorm_plan(
                room_id,
                filename=filename,
                content=content,
                apply_goal=False,
            )
            plan_rel = str(saved.get("plan_rel") or "")
            self.store.update(room_id, lambda r: setattr(r, "activity", f"已生成 CE 計畫：{plan_rel}"))
            self._changed()
            prog = self.get_room_progress(room_id)
            return {
                "ok": True,
                "text": f"已根據目標生成並套用 CE 計畫：{plan_rel}",
                "plan_rel": plan_rel,
                "seat": gen.get("seat", ""),
                "model": gen.get("model", ""),
                "room": saved.get("room"),
                "ce": saved.get("ce"),
                "progress": prog,
            }

        # SSH 或尚未綁定本機資料夾時：直接從產生的 Markdown 擷取階段單元寫入房間
        lines = content.splitlines()
        units: List[Dict[str, Any]] = []
        idx = 1
        in_units_section = False
        for ln in lines:
            s = ln.strip()
            if re.match(r"^#{2,3}\s+(Implementation Units|Implementation Plan|階段|單元|任務)", s, re.I):
                in_units_section = True
                continue
            if re.match(r"^#{2}\s+", s) and in_units_section:
                in_units_section = False
            m = re.match(r"^\s*[-*]\s*\[\s*[xX ]?\s*\]\s+(.+)$", ln)
            if not m and in_units_section:
                m = re.match(r"^\s*(?:\d+[.)]|[-*])\s+(.+)$", ln)
            if m:
                txt = m.group(1).strip()
                if txt and len(txt) >= 3:
                    units.append({"id": f"C{idx}", "text": txt, "check": "", "done": False})
                    idx += 1
        if not units:
            units = [{"id": "C1", "text": effective_goal[:200], "check": "", "done": False}]
        def _apply_units(r: Room) -> None:
            r.criteria = units
            r.graph_node = "plan"
            r.current_unit_id = ""
            r.unit_artifacts = {}
            r.attempts = {}
        room = self.store.update(room_id, _apply_units)
        self._reset_v2_checkpoint(room_id)
        self._event(room_id, "spec", "已根據目標直接生成 %d 個階段單元" % len(units))
        prog = self.sync_room_progress(room_id, extra_note="已根據目標生成階段計畫")
        self._changed()
        return {
            "ok": True,
            "text": f"已根據目標生成 {len(units)} 個階段單元",
            "plan_rel": "",
            "seat": gen.get("seat", ""),
            "model": gen.get("model", ""),
            "room": asdict(room),
            "ce": {},
            "progress": prog,
        }

    def set_room_d1_fast_review(self, room_id: str, enabled: bool) -> Dict[str, Any]:
        """切換此房間是否啟用 Decision-1 15 題快速審查分流。"""
        self._room(room_id)
        room = self.store.update(room_id, lambda r: setattr(r, "d1_fast_review", bool(enabled)))
        self._event(
            room_id,
            "notice",
            "已開啟 Decision-1 快速審查分流" if enabled else "已關閉 Decision-1 快速審查分流（全部單元皆走大模型深度審查）",
        )
        self._changed()
        return {"ok": True, "room": asdict(room)}

    def _reset_v2_checkpoint(self, room_id: str) -> None:
        base = self.store._dir(room_id)
        failed = False
        for name in ("graph.sqlite", "graph.sqlite-wal", "graph.sqlite-shm"):
            db_path = os.path.join(base, name)
            if os.path.isfile(db_path):
                try:
                    os.remove(db_path)
                except OSError:
                    failed = True
        with self._lock:
            alive = self._alive(room_id) is not None
        stale = bool(failed or alive)
        try:
            self.store.update(room_id, lambda r: setattr(r, "checkpoint_stale", stale))
        except Exception:
            pass

    def rerun_room_unit(self, room_id: str, unit_id: str) -> Dict[str, Any]:
        """將指定單元重置為待執行並清空其重試計數（下次啟動或續跑將從該單元開始）。"""
        room = self._room(room_id)
        uid = (unit_id or "").strip()
        if not uid:
            raise ValueError("請指定單元 id")
        found = False

        def mut(r: Room) -> None:
            nonlocal found
            new_c = []
            for c in r.criteria or []:
                item = dict(c)
                if str(item.get("id") or "") == uid:
                    item["done"] = False
                    item["status"] = "pending"
                    item["attempts"] = 0
                    item["waive_reason"] = ""
                    found = True
                new_c.append(item)
            r.criteria = new_c
            if isinstance(r.unit_artifacts, dict) and uid in r.unit_artifacts:
                arts = dict(r.unit_artifacts)
                arts.pop(uid, None)
                r.unit_artifacts = arts

        room = self.store.update(room_id, mut)
        if not found:
            raise ValueError(f"找不到單元 {uid}")
        self._reset_v2_checkpoint(room_id)
        self._event(room_id, "notice", f"已重置單元 [{uid}] 為待重跑狀態")
        self._changed()
        return {"ok": True, "text": f"已重置單元 [{uid}]，繼續執行時將從此單元重跑", "room": asdict(room)}

    def waive_room_unit(self, room_id: str, unit_id: str, reason: str = "") -> Dict[str, Any]:
        """將指定單元標記為已豁免跳過。"""
        self._room(room_id)
        uid = (unit_id or "").strip()
        if not uid:
            raise ValueError("請指定單元 id")
        why = (reason or "").strip() or "使用者手動豁免跳過"
        found = False

        def mut(r: Room) -> None:
            nonlocal found
            new_c = []
            for c in r.criteria or []:
                item = dict(c)
                if str(item.get("id") or "") == uid:
                    item["done"] = True
                    item["status"] = "waived"
                    item["waive_reason"] = why
                    found = True
                new_c.append(item)
            r.criteria = new_c

        room = self.store.update(room_id, mut)
        if not found:
            raise ValueError(f"找不到單元 {uid}")
        self._reset_v2_checkpoint(room_id)
        self._event(room_id, "notice", f"已豁免跳過單元 [{uid}]：{why}")
        self._changed()
        return {"ok": True, "text": f"已豁免單元 [{uid}]", "room": asdict(room)}

    def edit_room_unit(self, room_id: str, unit_id: str, check_cmd: Optional[str] = None, text: Optional[str] = None) -> Dict[str, Any]:
        """修改指定單元的驗證指令或描述。"""
        self._room(room_id)
        uid = (unit_id or "").strip()
        if not uid:
            raise ValueError("請指定單元 id")
        found = False

        def mut(r: Room) -> None:
            nonlocal found
            new_c = []
            for c in r.criteria or []:
                item = dict(c)
                if str(item.get("id") or "") == uid:
                    if check_cmd is not None:
                        item["check"] = str(check_cmd).strip()
                    if text is not None and str(text).strip():
                        item["text"] = str(text).strip()
                    found = True
                new_c.append(item)
            r.criteria = new_c

        room = self.store.update(room_id, mut)
        if not found:
            raise ValueError(f"找不到單元 {uid}")
        self._reset_v2_checkpoint(room_id)
        self._event(room_id, "notice", f"已更新單元 [{uid}] 的驗證條件")
        self._changed()
        return {"ok": True, "text": f"已更新單元 [{uid}]", "room": asdict(room)}

    def clear_room_wiki(self, room_id: str) -> Dict[str, Any]:
        """清空此房間對應專案的 LLM Wiki 知識庫與教訓紀錄。"""
        room = self._room(room_id)
        wiki_view = self.kb.clear_repo_wiki(room.repo) if room.repo else {
            "repo_id": "", "index_md": "", "pages": [], "namespaces": {}
        }
        self._event(room_id, "notice", "已清空專案 LLM Wiki 知識庫")
        self._changed()
        return {"ok": True, "text": "已清空專案 LLM Wiki 知識庫", "wiki": wiki_view}

    def _resolve_brainstorm_seat(self, room: Room, seat: str, model: str) -> Tuple[str, str]:
        """解析 Brainstorm / 計畫生成座位與模型：未指定時由 Decision-1 排除付費 LiteLLM 後選出最強免費模型。"""
        sh = (getattr(room, "ssh_host", "") or "").strip()
        clis = self._clis_for_host(sh)
        req_seat = (seat or "").strip()
        req_model = (model or "").strip()
        glm_ok = room.demo or (self.model_cfg is not None)
        if not req_seat or req_seat == "auto":
            if not room.demo and (clis or self.model_cfg is not None):
                try:
                    from team.model_catalog import select_strongest_planner
                    p_seat, p_model, _ = select_strongest_planner(
                        clis,
                        store_root=self.store.root,
                        model_cfg=self.model_cfg,
                    )
                    if (p_seat in GLM_SEATS and glm_ok) or (p_seat in clis):
                        return p_seat, req_model or p_model
                except Exception:
                    pass
            if glm_ok:
                return "glm", req_model or (self.model_cfg.glm_model if self.model_cfg else "glm-5.3")
            for cand in ("kiro", "agy", "claude"):
                if cand in clis:
                    return cand, req_model
            raise ValueError("目前沒有可用的 GLM 或 CLI（claude / kiro / agy）可進行 Brainstorm")
        if req_seat in GLM_SEATS and not glm_ok:
            raise ValueError("缺少 litellm.local.json，GLM 座位不可用，請改選其他 CLI")
        if req_seat not in GLM_SEATS and req_seat not in clis and not room.demo:
            raise ValueError("找不到 %s CLI，請改選其他可用座位" % req_seat)
        if req_seat in GLM_SEATS and not req_model:
            req_model = self.model_cfg.glm_model if self.model_cfg else "glm-5.3"
        if req_seat == "agy" and not req_model:
            req_model = "gemini-4-hydrogen"
        return req_seat, req_model

    @staticmethod
    def _clean_plan_markdown(raw: str) -> str:
        text = (raw or "").strip()
        m = re.match(r"^```(?:markdown|md)?\s*\n(.*)\n```\s*$", text, re.S | re.I)
        if m:
            return m.group(1).strip() + "\n"
        return text + ("\n" if text and not text.endswith("\n") else "")

    @staticmethod
    def _suggest_plan_filename(content: str, goal_draft: str = "") -> str:
        ts = datetime.now().strftime("%Y-%m-%d-%H%M")
        title = ""
        m = re.search(r'^title:\s*["\']?([^\n"\']+)["\']?', content or "", re.M)
        if m:
            title = m.group(1).strip()
        if not title:
            m2 = re.search(r"^#\s+(.+)$", content or "", re.M)
            if m2:
                title = m2.group(1).strip()
        if not title:
            title = goal_draft or "brainstorm"
        slug = re.sub(r"^(?:feat|fix|chore|docs|refactor)\s*:\s*", "", title, flags=re.I)
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", slug).strip("-").lower()[:40] or "brainstorm"
        return f"{ts}-feat-{slug}-plan.md"

    @staticmethod
    def _sanitize_plan_filename(filename: str) -> str:
        base = os.path.basename((filename or "").strip())
        if not base or base in (".", "..") or "/" in filename or "\\" in filename:
            raise ValueError("計畫檔名不合法")
        safe = re.sub(r"[^A-Za-z0-9._-]", "-", base).strip(".-")
        if not safe:
            raise ValueError("計畫檔名不合法")
        if not safe.lower().endswith(".md"):
            safe = f"{safe}.md"
        return safe

    def brainstorm_room(
        self,
        room_id: str,
        messages: Sequence[Dict[str, Any]],
        mode: str = "chat",
        seat: str = "",
        model: str = "",
        goal_draft: str = "",
    ) -> Dict[str, Any]:
        """針對房間粗略目標進行多輪 Brainstorm 討論，或將討論內容收斂為 CE 計畫草稿。"""
        room = self._room(room_id)
        if mode not in ("chat", "generate_plan"):
            raise ValueError("mode 必須是 chat 或 generate_plan")
        norm_msgs: List[Dict[str, str]] = []
        for item in messages or []:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "user").strip().lower()
            if role not in ("user", "assistant"):
                role = "user"
            content = str(item.get("content") or "").strip()
            if content:
                norm_msgs.append({"role": role, "content": content})
        if mode == "chat" and not norm_msgs:
            raise ValueError("請輸入要討論的內容")

        eff_seat, eff_model = self._resolve_brainstorm_seat(room, seat, model)
        draft = (goal_draft or room.goal or "").strip()

        if room.demo:
            if mode == "generate_plan":
                demo_goal = draft or (norm_msgs[-1]["content"] if norm_msgs else "完成專案核心功能")
                demo_md = (
                    "---\n"
                    f'title: "feat: {demo_goal[:48]}"\n'
                    "type: feat\n"
                    "status: draft\n"
                    f"date: {datetime.now().strftime('%Y-%m-%d')}\n"
                    "---\n\n"
                    f"# {demo_goal[:60]}\n\n"
                    "## Goal Capsule\n\n"
                    f"- **Objective:** {demo_goal}\n\n"
                    "## Implementation Units\n\n"
                    "- [ ] 釐清核心資料結構與介面契約\n"
                    "- [ ] 實作主要功能邏輯並加入錯誤處理\n"
                    "- [ ] 補齊單元測試並驗證結果\n"
                )
                return {
                    "ok": True,
                    "mode": mode,
                    "seat": eff_seat,
                    "model": eff_model,
                    "reply": "已根據討論內容整理出計畫草稿，請預覽確認後寫入。",
                    "filename": self._suggest_plan_filename(demo_md, draft),
                    "content": demo_md,
                }
            last_q = norm_msgs[-1]["content"]
            return {
                "ok": True,
                "mode": mode,
                "seat": eff_seat,
                "model": eff_model,
                "reply": f"（示範回應）針對「{last_q[:60]}」，建議先確認：1) 主要使用對象與觸發情境為何？2) 是否有必須保留的既有行為或限制？你可以繼續補充，或隨時點擊「生成計畫」。",
            }

        convo_lines = [f"[{m['role'].upper()}]\n{m['content']}" for m in norm_msgs]
        convo_text = "\n\n".join(convo_lines) if convo_lines else "（尚未有對話，請根據粗略目標開始）"
        ctx_header = (
            f"專案名稱：{room.name}\n"
            f"工作目錄：{room.repo or '(未指定)'}\n"
            f"目前粗略目標：{draft or '(尚未填寫)'}\n"
        )
        from team.ce_skills import role_ce_directive
        ce_block = role_ce_directive(
            "po" if mode == "chat" else "tech_lead",
            notes_dir=self.store._dir(room_id),
            reference_root=room.reference_root,
            target_subdir=room.target_subdir,
            target_subdirs=list(getattr(room, "target_subdirs", None) or ()),
        )
        if mode == "chat":
            prompt = (
                "你是一位務實的產品與軟體架構 Brainstorm 夥伴，請嚴格依 `ce-brainstorm` 與 `ce-strategy` 規範使用繁體中文互動。\n"
                "目標是透過簡潔的來回討論，幫使用者把粗略想法收斂成清楚的需求、範圍邊界（Non-goals）、驗收條件與關鍵取捨。\n"
                "每次回覆請保持精簡（約 150-300 字），先整理目前共識或盲點，再提出「1 個」最關鍵的釐清問題並附帶 2-3 個建議選項（標出推薦項）。\n\n"
                f"{ce_block}\n\n"
                f"=== 專案脈絡 ===\n{ctx_header}\n"
                f"=== 對話紀錄 ===\n{convo_text}\n\n"
                "請直接輸出你的下一輪回覆："
            )
        else:
            today = datetime.now().strftime("%Y-%m-%d")
            prompt = (
                "請依 `ce-plan` 與 `ce-doc-review` 規範，根據以下專案脈絡與 Brainstorm 對話紀錄，整理出一份可直接寫入 `docs/plans/` 的 Compound Engineering 實作計畫（繁體中文 Markdown）。\n"
                "格式必須嚴格包含：\n"
                "1. YAML frontmatter（含 `title`, `type: feat`, `status: draft`, `date: " + today + "`）\n"
                "2. `## Goal Capsule` 與 `- **Objective:** <一句話明確總目標>`\n"
                "3. `## Scope`（說明包含與排除範圍）\n"
                "4. `## Implementation Units`（使用 `- [ ] ...` 任務清單列出 3 到 8 個具體可執行步驟，供計畫進度追蹤）\n"
                "5. `## Verification`（列出測試或驗收方式；純文件單元用輕量檔案檢查，程式碼單元優先對應模組測試）\n"
                "嚴格禁止事項：\n"
                "- **禁止把已完成或已作廢的歷史舊計畫當成待辦單元**，也嚴禁在未完成項目中引用舊計畫路徑（歷史背景只能寫在文字摘要或標為 `[x]`）。\n"
                "- **禁止把 `Goal:`、`Requirements:`、`Dependencies:`、`Patterns to follow:`、`Verification:` 等說明欄位拆成獨立的 `- [ ]` 單元**。\n"
                "- 若本計畫是某個父計畫的延伸子計畫，請於檔頭標註 `status: active` 與 `> 衍生自（Parent Plan）：<父計畫路徑>`。\n"
                "請只輸出純 Markdown 計畫內容，不要加額外開場白。\n\n"
                f"{ce_block}\n\n"
                f"=== 專案脈絡 ===\n{ctx_header}\n"
                f"=== 對話紀錄 ===\n{convo_text}\n"
            )

        reply = ""
        if eff_seat in GLM_SEATS and self.model_cfg is not None:
            router = self.router_factory(False)
            if router is None:
                raise ValueError("ModelRouter 未初始化，無法呼叫 GLM")
            effort = "high" if eff_seat == "glm_high" else "medium"
            reply = str(router.write(prompt, effort=effort, ctx={"room": room_id, "purpose": f"brainstorm_{mode}"}) or "").strip()
        else:
            sh = (getattr(room, "ssh_host", "") or "").strip()
            clis = self._clis_for_host(sh)
            workdir = room.worktree or room.repo or self.store.root
            if not sh and not os.path.isdir(workdir):
                workdir = self.store.root
            tmpdir = os.path.join(self.store._dir(room_id), "tmp_brainstorm")
            os.makedirs(tmpdir, exist_ok=True)
            cli, env, extra = seat_launch(
                eff_seat, clis, self.model_cfg, (), workdir, tmpdir,
                reference_root=room.reference_root, ssh_host=sh,
            )
            msg_chunks: List[str] = []

            def _on_bs_ev(ev: AgentEvent) -> None:
                if ev.kind == "message" and ev.text:
                    msg_chunks.append(ev.text)

            drv = make_driver(
                cli, workdir, _on_bs_ev, lambda _req: "deny",
                read_only=True, env=env, extra_args=extra, model=eff_model, ssh_host=sh,
            )
            try:
                drv.start()
                raw_stop = str(drv.prompt(prompt, timeout=180) or "").strip()
                msg_txt = "".join(msg_chunks).strip()
                if msg_txt:
                    reply = msg_txt
                elif raw_stop not in ("end_turn", "max_tokens", "stop_sequence", "cancelled", "refusal"):
                    reply = raw_stop
                else:
                    reply = ""
            except Exception as cli_err:
                if self.model_cfg is None:
                    raise
                log.warning("Brainstorm CLI %s 失敗（%s），改用免費 glm-5.3 (medium) 備援", eff_seat, type(cli_err).__name__)
                reply = ""
            finally:
                try:
                    drv.close()
                except Exception:
                    pass
            if (not reply or reply.strip() in ("end_turn", "max_tokens", "stop_sequence", "cancelled", "refusal")) and self.model_cfg is not None:
                router = self.router_factory(False)
                if router is not None:
                    eff_seat = "glm"
                    eff_model = self.model_cfg.glm_model or "glm-5.3"
                    reply = str(router.write(prompt, effort="medium", ctx={"room": room_id, "purpose": f"brainstorm_{mode}_fallback"}) or "").strip()

        if not reply or reply.strip() in ("end_turn", "max_tokens", "stop_sequence", "cancelled", "refusal"):
            raise ValueError("模型未回傳有效的計畫或對話文字（僅回傳結束訊號），請重試或切換其他 CLI／模型")

        if mode == "generate_plan":
            cleaned = self._clean_plan_markdown(reply)
            filename = self._suggest_plan_filename(cleaned, draft)
            return {
                "ok": True,
                "mode": mode,
                "seat": eff_seat,
                "model": eff_model,
                "reply": "已根據討論內容生成計畫草稿，請預覽確認後寫入。",
                "filename": filename,
                "content": cleaned,
            }
        return {
            "ok": True,
            "mode": mode,
            "seat": eff_seat,
            "model": eff_model,
            "reply": reply,
        }

    def save_brainstorm_plan(
        self,
        room_id: str,
        filename: str,
        content: str,
        apply_goal: bool = True,
    ) -> Dict[str, Any]:
        """將預覽確認後的 Brainstorm 計畫寫入專案 `docs/plans/` 並同步套用至房間。"""
        room = self._room(room_id)
        if not room.repo:
            raise ValueError("此房間未綁定專案資料夾，無法寫入 docs/plans/")
        sh = (getattr(room, "ssh_host", "") or "").strip()
        if sh:
            raise ValueError("目前僅支援在本機專案資料夾寫入 docs/plans/")
        if not os.path.isdir(room.repo):
            raise ValueError("專案資料夾不存在：%s" % room.repo)
        body = (content or "").strip()
        if not body:
            raise ValueError("計畫內容不可為空")

        safe_name = self._sanitize_plan_filename(filename)
        plans_dir = os.path.realpath(os.path.join(room.repo, "docs", "plans"))
        os.makedirs(plans_dir, exist_ok=True)

        stem, ext = os.path.splitext(safe_name)
        candidate = os.path.realpath(os.path.join(plans_dir, safe_name))
        idx = 2
        while os.path.exists(candidate):
            candidate = os.path.realpath(os.path.join(plans_dir, f"{stem}-{idx}{ext}"))
            idx += 1
        if os.path.commonpath([plans_dir, candidate]) != plans_dir:
            raise ValueError("計畫檔名超出 docs/plans/ 範圍")

        with open(candidate, "w", encoding="utf-8") as f:
            f.write(body + ("\n" if not body.endswith("\n") else ""))

        plan_rel = os.path.relpath(candidate, room.repo).replace("\\", "/")
        sync_res = self.sync_room_plan(room_id, plan_rel=plan_rel, apply_goal=apply_goal)
        return {
            "ok": True,
            "plan_rel": plan_rel,
            "path": candidate,
            "room": sync_res.get("room"),
            "ce": sync_res.get("ce"),
        }

    def reset_room(self, room_id: str) -> Room:
        """將房間與專案恢復至 AI 工程助理介入前之乾淨狀態：暫停執行器、移除 ai-team 分支與 worktree、清空目標與舊驗收條件。"""
        room = self._room(room_id)
        r_alive = self._alive(room_id)
        if r_alive is not None:
            try:
                self.pause(room_id)
                r_alive.join(2.5)
            except Exception:
                pass
        room = self._room(room_id)
        sh = (getattr(room, "ssh_host", "") or "").strip()
        old_branch = room.branch
        if room.worktree and (sh or os.path.isdir(room.worktree)):
            try:
                gitops.remove_worktree(Worktree(room.repo, room.worktree, room.branch, room.base, ssh_host=sh))
            except Exception:
                pass
        if old_branch and old_branch.startswith("ai-team/") and room.repo and (sh or os.path.isdir(room.repo)):
            try:
                cur_br = gitops.git(room.repo, "rev-parse", "--abbrev-ref", "HEAD", ssh_host=sh).strip()
                if cur_br == old_branch:
                    target = room.target_branch or "main"
                    gitops.git(room.repo, "checkout", target, ssh_host=sh)
                gitops.git(room.repo, "worktree", "prune", ssh_host=sh)
                gitops.git(room.repo, "branch", "-D", old_branch, ssh_host=sh)
            except Exception:
                pass
        for it in self._open_items(room_id):
            self.store.resolve(it.id, "reset", "專案已恢復至介入前狀態")
        with self._lock:
            self._resume_wanted.discard(room_id)
            self._sends.pop(room_id, None)

        def mut(r: Room) -> None:
            r.status = "paused"
            r.phase = "goal"
            r.persona = ""
            r.activity = "已恢復至介入前狀態（可自行輸入目標或跟隨 CE 計畫）"
            r.goal = ""
            r.branch = ""
            r.worktree = ""
            r.criteria = []
            r.test_cmd = ""
            r.pending_fix = ""
            r.unverified = ""
            r.error = ""
            r.attempts = {}
            if r.impl_cli != "auto":
                r.impl_seat = r.impl_cli
                r.impl_model = (r.impl_pref or {}).get("model", "")
            else:
                r.impl_seat = ""
                r.impl_model = ""
            r.impl_model_actual = ""
            r.review_model_actual = ""

        room = self.store.update(room_id, mut)
        self._event(room_id, "status", "已恢復專案至介入前狀態（移除分支與舊驗收條件）", status="paused")
        self._changed()
        return room

    def handoff_to_new_room(self, room_id: str, new_goal: str = "", name: str = "") -> Room:
        """關閉並封存舊房間（完整保留開發紀錄至 LLM Wiki 與 handoff.md），並以相同專案設定開啟新房間接續開發。"""
        old = self._room(room_id)
        if old.status != "archived":
            self.archive(room_id, force=True)
            old = self._room(room_id)

        sh = (getattr(old, "ssh_host", "") or "").strip()
        ce_info = {} if sh else ce_sync.sync_room_ce_plan(old.repo, reference_root=old.reference_root)
        goal_text = (new_goal or "").strip()
        if not goal_text:
            goal_text = (
                old.goal.strip()
                or ce_info.get("objective")
                or ("接續房間 %s（%s）繼續開發" % (old.name or old.slug, old.id))
            )
        goal_text = goal_text[:MAX_GOAL]
        impl_cli = old.impl_cli or "auto"
        review_cli = old.review_cli or "auto"
        impl_model = (old.impl_pref or {}).get("model", "") if impl_cli != "auto" else ""
        review_model = (old.review_pref or {}).get("model", "") if review_cli != "auto" else ""
        paths = list(old.allowed_paths or [])
        room_name = (name or "").strip() or old.name or os.path.basename(old.repo.rstrip("/\\"))
        slug = self._unique_slug(old.repo, goal_text, in_place=bool(old.target_subdir or old.reference_root), ssh_host=sh)
        new_room = self.store.create(
            name=room_name,
            repo=old.repo,
            slug=slug,
            goal=goal_text,
            source="handoff:%s" % old.id,
            impl_cli=impl_cli,
            review_cli=review_cli,
            autonomy=old.autonomy or "balanced",
            allowed_paths=paths,
            reference_root=old.reference_root,
            target_subdir=old.target_subdir,
            target_subdirs=list(old.target_subdirs or []),
            ssh_host=sh,
            status="paused",
            phase="goal",
            activity="已從舊房間 %s 接續建立（過去開發紀錄已載入 LLM Wiki）" % old.id,
            user_pinned=impl_cli != "auto" or review_cli != "auto",
            impl_pref={"seat": impl_cli, "model": impl_model} if impl_cli != "auto" else {},
            review_pref={"seat": review_cli, "model": review_model} if review_cli != "auto" else {},
            impl_seat=impl_cli if impl_cli != "auto" else "",
            impl_model=impl_model,
            criteria=list(ce_info.get("criteria") or old.criteria or []),
            test_cmd=str(ce_info.get("test_cmd") or old.test_cmd or ""),
        )
        try:
            self.sync_room_progress(new_room.id, extra_note=f"從舊房間 {old.id} 接續建立")
        except Exception:
            pass
        # 複製舊房間的 handoff.md 到新房間 notes
        old_handoff = os.path.join(self.store.root, old.id, "handoff.md")
        if os.path.isfile(old_handoff):
            try:
                new_dir = os.path.join(self.store.root, new_room.id)
                os.makedirs(new_dir, exist_ok=True)
                with open(old_handoff, "r", encoding="utf-8") as src, open(
                    os.path.join(new_dir, "prev_handoff.md"), "w", encoding="utf-8", newline="\n"
                ) as dst:
                    dst.write(src.read())
            except OSError:
                pass
        self._event(
            new_room.id,
            "status",
            "已接續舊房間 `%s` 建立新房間（LLM Wiki 與 CE 計畫已掛載，確認目標後按「繼續」即可開工）" % old.id,
            status="paused",
            prev_room_id=old.id,
        )
        self._changed()
        return self.store.get(new_room.id) or new_room

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

    def _is_goal_update_directive(self, room: Room, text: str) -> bool:
        """由高層主管（Decision-1）判斷使用者透過對話框傳入的指示是否屬於「追加／修改目標或驗收條件」。"""
        if not getattr(room, "demo", False):
            router = getattr(self, "router", None)
            if router is None and hasattr(self, "_router") and getattr(self, "router_factory", None) is not None:
                try:
                    router = self._router(False)
                except Exception:
                    router = None
            if router is not None:
                try:
                    p = router.probability(
                        "【現有任務目標】\n%s\n\n【使用者新傳入的對話指示】\n%s" % (room.goal or "(無)", text),
                        "使用者這句話是在「追加／修改專案目標、新增驗收條件、要求額外檢查步驟或改變需求範圍」嗎？（而非單純催促、問進度或閒聊）",
                    )
                    if p is not None:
                        return p >= 0.65
                except Exception:
                    log.debug("Decision-1 判定目標追加失敗，改用關鍵字備援", exc_info=True)
        return bool(
            re.search(
                r"(目標|加上一條|新增一條|追加|補上|驗收條件|需求|改為|改成|也要|務必|最後要|完成後要)",
                text,
            )
        )

    def send(self, room_id: str, text: str) -> Dict[str, Any]:
        text = (text or "").strip()
        if not text:
            return {"ok": False, "text": "請輸入要說的話"}
        if len(text) > MAX_SAY:
            return {"ok": False, "text": "太長了（上限 %d 字）" % MAX_SAY}
        room = self._room(room_id)
        if room.status in ("done", "archived") or room.phase == "done":
            return {"ok": False, "text": "這間房已結束或封存，訊息不會轉告代理"}
        is_goal_update = self._is_goal_update_directive(room, text)
        if room.phase in ("test", "review") and not is_goal_update:
            return {"ok": False, "text": "這間房目前在測試／審查階段；若要追加目標請明確說明要新增的目標或驗收條件"}

        added_cid = ""
        updated_goal = room.goal
        if is_goal_update:
            def _append_goal_and_criteria(rm: Room) -> None:
                nonlocal added_cid, updated_goal
                base_goal = (rm.goal or "").rstrip()
                entry = "- [使用者追加目標] " + text
                if entry not in base_goal:
                    rm.goal = (base_goal + "\n" + entry).strip() if base_goal else text
                updated_goal = rm.goal
                if isinstance(rm.criteria, list) and rm.criteria:
                    existing_texts = {str(c.get("text") or "").strip() for c in rm.criteria if isinstance(c, dict)}
                    if text not in existing_texts:
                        used_nums = []
                        for c in rm.criteria:
                            m = re.match(r"^C(\d+)$", str(c.get("id") or "").strip(), re.I)
                            if m:
                                used_nums.append(int(m.group(1)))
                        next_num = (max(used_nums) + 1) if used_nums else (len(rm.criteria) + 1)
                        added_cid = f"C{next_num}"
                        rm.criteria = list(rm.criteria) + [{"id": added_cid, "text": text, "check": "manual"}]

            self.store.update(room_id, _append_goal_and_criteria)

        self._event(room_id, "user", "【你】" + text)
        r = self._alive(room_id)
        if r is not None and not r.stopping:
            r.receive_user_directive(text, goal_updated=is_goal_update, added_criterion_id=added_cid)
            where = (
                f"高層主管已透過 Decision-1 判定並寫入目標{f'（新增驗收條件 {added_cid}）' if added_cid else ''}，已同步轉交研發與審查工程師"
                if is_goal_update
                else "高層主管已收到並記錄決策，下一回合會交辦研發工程師"
            )
        else:
            with self._lock:
                self._sends.setdefault(room_id, []).append(text)
            where = (
                f"高層主管已寫入房間目標{f'（新增驗收條件 {added_cid}）' if added_cid else ''}，房間繼續後將立即交辦研發與審查工程師"
                if is_goal_update
                else "房間繼續後的第一個回合會交辦研發工程師"
            )
        if is_goal_update:
            self._event(
                room_id,
                "say",
                personas.sign(
                    "supervisor",
                    "已接收你的新目標並正式寫入房間目標%s：%s；已同步通知研發工程師實作並納入審查工程師驗收範圍。"
                    % (f"與驗收條件 {added_cid}" if added_cid else "", text[:180]),
                ),
                role="supervisor",
                goal_updated=True,
                added_criterion_id=added_cid,
            )
        else:
            self._event(
                room_id,
                "say",
                personas.sign("supervisor", "已收到你的指示，已記錄至三方共享決策並交辦研發工程師：%s" % text[:180]),
                role="supervisor",
            )
        self._changed()
        return {
            "ok": True,
            "text": where,
            "goal_updated": is_goal_update,
            "goal": updated_goal,
            "added_criterion_id": added_cid,
        }

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
        sh = (getattr(room, "ssh_host", "") or "").strip()
        wt = Worktree(room.repo, room.worktree, room.branch, room.base, room.target_branch, ssh_host=sh)
        try:
            nd = room.notes_dir or (
                os.path.join(self.store.root, rid, "notes") if sh else notes.notes_dir(room.repo, room.slug)
            )
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
        if not wt.path or (not wt.ssh_host and not os.path.isdir(wt.path)):
            return
        try:
            gitops.remove_worktree(wt)
        except GitError as e:
            self._event(rid, "notice", "已合併，但無法移除工作目錄：%s" % e)

    # ================= 快照 =================
    def snapshot(self) -> Dict[str, Any]:
        all_rooms = self.store.list(include_archived=True)
        rooms = [r for r in all_rooms if r.status != "archived"]
        live = {r.id for r in rooms}
        with self._lock:
            wanted = set(self._resume_wanted)
        inflight = self.inflight()
        out_rooms = []
        for r in rooms:
            d = asdict(r)
            d["events"] = [slim_event(e) for e in reversed(self.store.events(r.id, limit=40))]  # 快照不帶 detail
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
        archived_rooms = [
            {
                "id": r.id,
                "name": r.name or r.slug,
                "repo": r.repo,
                "target_subdir": r.target_subdir,
                "reference_root": r.reference_root,
                "goal": (r.goal or "")[:300],
                "branch": r.branch,
                "phase": r.phase,
                "status": r.status,
                "updated_at": r.updated_at,
                "criteria_count": len(r.criteria or []),
            }
            for r in all_rooms
            if r.status == "archived" and not r.demo
        ][:20]
        return {"rooms": out_rooms, "archived_rooms": archived_rooms, "inbox": inbox, "roles": roles,
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
    def models_view(self, ssh_host: str = "") -> Dict[str, Any]:
        """GET /api/team/models：每個座位的可用模型、你的選擇、偵測狀態，以及判斷用途的選項。"""
        sh = (ssh_host or "").strip()
        with self._lock:
            detecting = set(self._detecting)
        avail = self.available_seats(sh)
        view = self.prefs.view(avail, detecting if not sh else set(), SEAT_LABELS)
        view["ssh_host"] = sh
        view["available_seats"] = [s for s in SEATS if s in avail]
        if sh:
            probe = self.ssh_store.get_cached_probe(sh) or {}
            s_models = probe.get("seats_models") or {}
            for s, sdata in (view.get("seats") or {}).items():
                if not isinstance(sdata, dict):
                    continue
                if s in avail and s in s_models:
                    rm = s_models[s]
                    sdata["available"] = True
                    if isinstance(sdata.get("detected"), dict):
                        sdata["detected"]["models"] = list(rm.get("models") or [])
                        sdata["detected"]["current"] = str(rm.get("current") or "")
                        sdata["detected"]["error"] = ""
                    if rm.get("models"):
                        sdata["models"] = list(rm.get("models") or [])
                elif s not in avail:
                    sdata["available"] = False
        return view

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
        """背景偵測 CLI 座位的可用模型（ACP initialize＋session/new 或 Claude CLI 探測，不送提示、不花 token）。
        每個座位同時最多一個偵測；回傳這次有開始的座位。"""
        avail = self.available_seats()
        if seats is None:
            requested = list(ACP_SEATS)
        elif "all" in seats:
            requested = list(DETECTABLE_SEATS)
        else:
            requested = list(seats)
        want = [s for s in requested if s in DETECTABLE_SEATS and s in avail]
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
        """開一個 ACP 對話（initialize＋session/new）讀 availableModels，或探測 Claude CLI 版本後回傳可用模型；不送 prompt。
        公司網路憑證環境（make_driver）照樣套用。"""
        if seat == "claude":
            clis = self._clis()
            cli = clis.get("claude")
            if cli is None or not cli.path:
                raise RuntimeError("找不到 claude CLI")
            return probe_claude_models(cli.path)
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

    _MODEL_ROLE_LABELS: Dict[str, str] = {
        "supervisor": "高階主管／Scrum",
        "impl": "實作",
        "review": "審查",
        "po": "PO",
        "tech_lead": "Tech Lead",
        "scrum": "Scrum Master",
        "qa": "QA",
    }

    def set_room_model(self, room_id: str, role: str, seat: str, model: str) -> Room:
        """房間中途改各職位（supervisor／impl／review／po／tech_lead／scrum／qa）的座位＋模型（空＝自動）；下一回合生效。"""
        room = self._room(room_id)
        if role not in self._MODEL_ROLE_LABELS:
            raise ValueError("role 只能是 %s" % "、".join(self._MODEL_ROLE_LABELS.keys()))
        seat = (seat or "").strip() or "auto"
        model = (model or "").strip()
        if model == "auto":
            model = ""
        if room.demo:
            raise ValueError("示範房使用假代理，不能指定模型")
        if room.status in ("done", "archived"):
            raise ValueError("這間房已經結束，不能再改模型")
        allowed_seats = set(SEATS) | {"litellm"}
        if seat != "auto" and seat not in allowed_seats:
            raise ValueError("不認得的座位：%s（可選：auto、%s）" % (seat, "、".join(sorted(allowed_seats))))
        if model and seat == "auto":
            raise ValueError("要指定模型，請先選座位")
        if role in ("impl", "review"):
            other = room.review_cli if role == "impl" else room.impl_cli
            if seat != "auto" and seat == other:
                raise ValueError("實作與審查要用不同的座位")
        sh = (getattr(room, "ssh_host", "") or "").strip()
        if seat != "auto" and seat != "litellm":
            p = self._seat_problem(seat, ssh_host=sh)
            if p:
                raise ValueError(p)
        if model and not sh and seat != "litellm":
            p = self.prefs.problem(seat, model)
            if p:
                raise ValueError(p)
        is_idle = self._alive(room.id) is None

        def mut(r: Room) -> None:
            pref_val = {"seat": seat, "model": model} if seat != "auto" else {}
            prefs_map = dict(getattr(r, "role_prefs", None) or {})
            if pref_val:
                prefs_map[role] = pref_val
            else:
                prefs_map.pop(role, None)
            r.role_prefs = prefs_map

            if role == "impl":
                r.impl_cli = seat
                r.impl_pref = pref_val
                if is_idle:
                    r.impl_seat = seat if seat != "auto" else ""
                    r.impl_model = model
                    r.impl_model_actual = ""
            elif role == "review":
                r.review_cli = seat
                r.review_pref = pref_val
                if is_idle:
                    r.review_seat = seat if seat != "auto" else ""
                    r.review_model = model
                    r.review_model_actual = ""
            elif role == "supervisor":
                r.supervisor_cli = seat
                r.supervisor_pref = pref_val
                if is_idle:
                    r.supervisor_seat = seat if seat != "auto" else ""
                    r.supervisor_model = model
                    r.supervisor_model_actual = ""
            r.user_pinned = (
                r.impl_cli != "auto"
                or r.review_cli != "auto"
                or getattr(r, "supervisor_cli", "auto") != "auto"
                or bool(r.role_prefs)
            )
        room = self.store.update(room.id, mut)
        what = self._MODEL_ROLE_LABELS[role]
        seat_label = "LiteLLM Proxy" if seat == "litellm" else SEAT_LABELS.get(seat, seat)
        if seat == "auto":
            text = "你把%s改成自動（%s）" % (what, "Decision-1 派工" if role in ("impl", "review") else "Decision-1 / GLM-5.3")
        elif model:
            text = "你把%s模型改成 %s（%s）" % (what, model, seat_label)
        else:
            text = "你把%s座位改成 %s（模型依設定）" % (what, seat_label)
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
