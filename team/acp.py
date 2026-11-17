"""ACP（Agent Client Protocol）JSON-RPC 用戶端：以 stdio 和 CLI 代理（kiro-cli acp、agy acp）溝通。

執行緒配置：
- acp-reader：逐行讀代理 stdout，把回應交給等待者、通知轉成 AgentEvent 排入佇列。
- acp-stderr：保留 stderr 最後 50 行（程序結束時附在錯誤訊息裡）。
- acp-events：依序呼叫 on_event，慢的回呼不會卡住讀取。
- acp-perm-<id>：每個 session/request_permission 一條，呼叫 on_permission（可能要等模型或使用者）。
"""
import collections
import json
import logging
import os
import queue
import subprocess
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from team.events import AgentEvent, PermissionRequest
from team.secrets import SecretEnv, merge_env

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
CLIENT_INFO = {"name": "buttonAuto-team", "title": "AI 團隊辦公室", "version": "1.0"}
STDERR_KEEP = 50
STDERR_SHOW = 5

_UPDATE_KINDS = {
    "agent_message_chunk": "message",
    "agent_thought_chunk": "thought",
    "tool_call": "tool",
    "tool_call_update": "tool_update",
    "plan": "plan",
    "usage_update": "usage",
}


class AcpError(Exception):
    pass


class _Slot:
    __slots__ = ("done", "result", "error")

    def __init__(self):
        self.done = threading.Event()
        self.result = None  # type: Any
        self.error = None  # type: Optional[str]


def _secs(n: float) -> str:
    return "%g" % n


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass
    if proc.poll() is None:
        try:
            proc.kill()
        except Exception:
            pass


class AcpClient:
    def __init__(self, cmd: List[str], cwd: str,
                 on_event: Callable[[AgentEvent], None],
                 on_permission: Callable[[PermissionRequest], str],
                 env: Optional[Dict[str, str]] = None):
        self.cmd = list(cmd)
        self.cwd = cwd
        self.on_event = on_event
        self.on_permission = on_permission
        # env 是覆蓋值，合併在目前環境之上（不寫入日誌）
        self._env = SecretEnv(env) if env is not None else None
        self.session_id = ""
        # 最近一次 usage_update 的（used, size）
        self.last_usage = None  # type: Optional[Tuple[int, int]]
        # Task 17：代理有回報才記（不猜）。session/new 的 models.currentModelId（或 set_model 成功的 id）、
        # usage_update 的累計 cost、kiro session_info_update 的 _meta.kiro.contextUsage.usagePercentage、
        # session/prompt 結果的 usage。
        self.current_model = ""
        # Task 18：session/new 回報的可用模型 [{id, name, description}]（沒回報 → []）
        self.available_models = []  # type: List[Dict[str, str]]
        # Kiro v3：模型清單不在 session/new 的 models，而是 session/update 的 config_option_update（configOptions，
        # category／id 為 "model" 的 select）；切換用 session/set_config_option。model_config_id 非空 ＝ 走這條路。
        self.config_options = []  # type: List[Dict[str, Any]]
        self.model_config_id = ""
        self._models_ev = threading.Event()
        self.cost = None  # type: Optional[Tuple[float, str]]
        self.cost_updated = False  # 這一回合有沒有收到 usage_update（沒有 → 本回合費用未回報，不是 0）
        self.context_pct = None  # type: Optional[float]
        self.prompt_usage = None  # type: Optional[Dict[str, Any]]
        self._proc = None  # type: Optional[subprocess.Popen]
        self._write_lock = threading.Lock()
        self._lock = threading.Lock()
        self._next_id = 0
        self._pending = {}  # type: Dict[int, _Slot]
        self._perm_open = set()  # 還沒回覆的權限請求 id
        self._stderr = collections.deque(maxlen=STDERR_KEEP)
        self._stderr_thread = None  # type: Optional[threading.Thread]
        self._reader = None  # type: Optional[threading.Thread]
        self._events = queue.Queue()  # type: queue.Queue
        self._death = None  # type: Optional[str]
        self._closed = False
        # initialize 回傳的登入方式（只記 id／name，給診斷用）
        self.auth_methods = []  # type: List[Dict[str, str]]
        self.rejected_methods = []  # type: List[str]  # 我們回 -32601 的代理請求方法名（不含參數）

    # ---- 公開介面 ----
    @property
    def alive(self) -> bool:
        p = self._proc
        return p is not None and p.poll() is None and self._death is None

    def start(self, timeout: float = 60) -> Dict[str, Any]:
        if self._proc is None:
            self._spawn()
        res = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            # terminal: False（不提供 terminal/* 方法）；但宣告支援「在終端機登入」的登入方式：
            # 未登入時代理可回報 terminal 類型的 authMethods，由辦公室開登入視窗。
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False},
                                   "terminal": False, "auth": {"terminal": True},
                                   "_meta": {"terminal-auth": True}},
            "clientInfo": CLIENT_INFO,
        }, timeout) or {}
        methods = res.get("authMethods") if isinstance(res, dict) else None
        if isinstance(methods, list):
            self.auth_methods = [{"id": str(m.get("id", "")), "name": str(m.get("name", ""))}
                                 for m in methods if isinstance(m, dict)]
            if self.auth_methods:
                log.info("代理提供的登入方式：%s", ", ".join(m["id"] or m["name"] for m in self.auth_methods))
        return res

    def new_session(self, timeout: float = 60) -> str:
        res = self._request("session/new", {"cwd": self.cwd, "mcpServers": []}, timeout) or {}
        sid = res.get("sessionId")
        if not sid:
            raise AcpError("代理沒有回傳 sessionId")
        self.session_id = str(sid)
        models = res.get("models")
        if isinstance(models, dict) and isinstance(models.get("currentModelId"), str):
            self.current_model = models["currentModelId"].strip()[:120]
        if isinstance(models, dict) and isinstance(models.get("availableModels"), list):
            self.available_models = [
                {"id": str(m.get("modelId")).strip()[:120], "name": str(m.get("name") or "")[:120],
                 "description": str(m.get("description") or "")[:300]}
                for m in models["availableModels"]
                if isinstance(m, dict) and isinstance(m.get("modelId"), str) and m["modelId"].strip()][:60]
            if self.available_models:
                self._models_ev.set()
        if isinstance(res.get("configOptions"), list):
            self._note_config(res["configOptions"])
        return self.session_id

    def wait_models(self, timeout: float = 3.0) -> List[Dict[str, str]]:
        """等代理回報可用模型（v2：session/new 的 models；v3：config_option_update），最多 timeout 秒。"""
        if not self.available_models:
            self._models_ev.wait(timeout)
        return list(self.available_models)

    def _note_config(self, options: Any) -> None:
        """記下 configOptions；從 category／id 為 "model" 的 select 取出可用模型與目前模型。"""
        if not isinstance(options, list):
            return
        self.config_options = [o for o in options if isinstance(o, dict)][:20]
        for o in self.config_options:
            if o.get("category") != "model" and o.get("id") != "model":
                continue
            vals = []
            for v in o.get("options") or []:
                if isinstance(v, dict) and isinstance(v.get("value"), str) and v["value"].strip():
                    vals.append({"id": v["value"].strip()[:120], "name": str(v.get("name") or "")[:120],
                                 "description": str(v.get("description") or "")[:300]})
            if not vals:
                continue
            self.model_config_id = str(o.get("id") or "model")[:60]
            self.available_models = vals[:60]
            if isinstance(o.get("currentValue"), str) and o["currentValue"].strip():
                self.current_model = o["currentValue"].strip()[:120]
            self._models_ev.set()
            return

    def prompt(self, text: str, timeout: float = 1800) -> str:
        self.prompt_usage = None
        self.cost_updated = False
        res = self._request("session/prompt", {
            "sessionId": self.session_id,
            "prompt": [{"type": "text", "text": text}],
        }, timeout) or {}
        if isinstance(res, dict) and isinstance(res.get("usage"), dict):
            self.prompt_usage = dict(res["usage"])
        # 讓這一輪的事件都送完再回傳
        self._flush_events(5)
        return str(res.get("stopReason") or "end_turn")

    def set_model(self, model_id: str, timeout: float = 10) -> bool:
        """有 configOptions（Kiro v3）→ session/set_config_option；否則 session/set_model。
        任何錯誤（不支援、逾時、程序結束、JSON-RPC 錯誤）都回 False。"""
        try:
            if self.model_config_id:
                self._request("session/set_config_option",
                              {"sessionId": self.session_id, "configId": self.model_config_id, "value": model_id},
                              timeout)
            else:
                self._request("session/set_model",
                              {"sessionId": self.session_id, "modelId": model_id}, timeout)
            self.current_model = model_id
            return True
        except Exception:
            return False

    def cancel(self) -> None:
        try:
            self._notify("session/cancel", {"sessionId": self.session_id})
        except AcpError:
            pass
        # 依 ACP：取消時把還沒回覆的權限請求都回 cancelled
        with self._lock:
            open_ids = list(self._perm_open)
        for rid in open_ids:
            self._answer_permission(rid, "")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        p = self._proc
        if p is None:
            return
        with self._write_lock:
            try:
                if p.stdin:
                    p.stdin.close()
            except Exception:
                pass
        try:
            p.wait(2)
        except subprocess.TimeoutExpired:
            _kill_tree(p)
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                pass
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(5)

    # ---- 程序 ----
    def _spawn(self) -> None:
        env = merge_env(self._env) if self._env is not None else None
        kw = {}  # type: Dict[str, Any]
        if os.name == "nt":
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kw["start_new_session"] = True  # 讓 close 能整個程序群組一起結束
        try:
            self._proc = subprocess.Popen(
                self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=self.cwd, env=env,
                text=True, encoding="utf-8", errors="replace", bufsize=1, **kw)
        except OSError as e:
            self._death = "無法啟動代理：%s" % e
            raise AcpError(self._death)
        self._stderr_thread = threading.Thread(target=self._read_stderr, name="acp-stderr",
                                               daemon=True)
        self._stderr_thread.start()
        threading.Thread(target=self._dispatch_events, name="acp-events", daemon=True).start()
        self._reader = threading.Thread(target=self._read_stdout, name="acp-reader", daemon=True)
        self._reader.start()

    def _read_stderr(self) -> None:
        p = self._proc
        try:
            for line in p.stderr:
                line = line.rstrip("\r\n")
                if line:
                    self._stderr.append(line)
        except Exception:
            pass

    def _read_stdout(self) -> None:
        p = self._proc
        try:
            for line in p.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    log.debug("ACP 非 JSON 輸出：%s", line[:200])
                    continue
                if isinstance(msg, dict):
                    try:
                        self._handle(msg)
                    except Exception:
                        log.exception("處理 ACP 訊息失敗")
        except Exception:
            pass
        self._on_exit()

    def _on_exit(self) -> None:
        p = self._proc
        code = None
        try:
            code = p.wait(5)
        except subprocess.TimeoutExpired:
            _kill_tree(p)
            code = p.poll()
        if self._stderr_thread is not None:
            self._stderr_thread.join(2)
        tail = "\n".join(list(self._stderr)[-STDERR_SHOW:])
        death = "代理程序結束了（exit %s）：%s" % (code, tail)
        with self._lock:
            self._death = death
            pending = list(self._pending.values())
            self._pending.clear()
            self._perm_open.clear()
        for slot in pending:
            slot.error = death
            slot.done.set()
        self._events.put(None)  # 結束事件分派執行緒

    # ---- 傳送 ----
    def _write(self, msg: Dict[str, Any]) -> None:
        p = self._proc
        if p is None:
            raise AcpError("代理尚未啟動")
        data = json.dumps(msg, ensure_ascii=False) + "\n"
        with self._write_lock:
            try:
                p.stdin.write(data)
                p.stdin.flush()
            except (OSError, ValueError, AttributeError) as e:
                raise AcpError("無法寫入代理：%s" % e)

    def _notify(self, method: str, params: Dict[str, Any]) -> None:
        if self._death:
            raise AcpError(self._death)
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: Dict[str, Any], timeout: float) -> Any:
        slot = _Slot()
        with self._lock:
            if self._death:
                raise AcpError(self._death)
            if self._proc is None:
                raise AcpError("代理尚未啟動")
            self._next_id += 1
            rid = self._next_id
            self._pending[rid] = slot
        try:
            self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        except AcpError:
            # 寫入失敗多半是程序已結束：等讀取執行緒收尾，拿到含 exit code 的訊息
            if self._reader is not None:
                self._reader.join(7)
            with self._lock:
                self._pending.pop(rid, None)
                death = self._death
            if death:
                raise AcpError(death)
            raise
        if not slot.done.wait(timeout):
            with self._lock:
                self._pending.pop(rid, None)
            raise AcpError("代理在 %s 秒內沒有回應" % _secs(timeout))
        if slot.error is not None:
            raise AcpError(slot.error)
        return slot.result

    # ---- 接收 ----
    def _handle(self, msg: Dict[str, Any]) -> None:
        method = msg.get("method")
        rid = msg.get("id")
        if method is None:
            if rid is None:
                return
            with self._lock:
                slot = self._pending.pop(rid, None)
            if slot is None:
                return  # 已逾時或不認得
            err = msg.get("error")
            if err is not None:
                if isinstance(err, dict):
                    slot.error = "代理回傳錯誤（%s）：%s" % (err.get("code"), err.get("message", ""))
                else:
                    slot.error = "代理回傳錯誤：%s" % err
            else:
                slot.result = msg.get("result")
            slot.done.set()
            return
        params = msg.get("params") or {}
        if rid is None:
            if method == "session/update":
                self._note_usage(params.get("update") or {})
                ev = self._to_event(params.get("update") or {})
                if ev is not None:
                    if ev.kind == "usage":
                        self.last_usage = (ev.data["used"], ev.data["size"])
                    self._events.put(ev)
            return
        if method == "session/request_permission":
            with self._lock:
                self._perm_open.add(rid)
            threading.Thread(target=self._permission_worker, args=(rid, params),
                             name="acp-perm-%s" % rid, daemon=True).start()
            return
        name = str(method)[:120]
        first = name not in self.rejected_methods
        if first:
            self.rejected_methods.append(name)
            # 只記方法名（不記參數，可能含權杖）；例如 _kiro/auth/getAccessToken 代表代理想向我們要登入權杖
            log.info("拒絕代理請求（不支援的方法）：%s", name)
            self._events.put(AgentEvent("notice", "代理要求用戶端不支援的功能：%s（已拒絕）" % name,
                                        {"method": name}))
        try:
            self._write({"jsonrpc": "2.0", "id": rid,
                         "error": {"code": -32601, "message": "Method not found: %s" % method}})
        except AcpError:
            pass

    def _note_usage(self, upd: Any) -> None:
        """記下代理回報的費用與 kiro 的對話用量（只記數字，不記內容）。"""
        if not isinstance(upd, dict):
            return
        kind = upd.get("sessionUpdate")
        if kind == "usage_update" and isinstance(upd.get("cost"), dict):
            amt = upd["cost"].get("amount")
            if isinstance(amt, (int, float)) and not isinstance(amt, bool):
                self.cost = (float(amt), str(upd["cost"].get("currency") or ""))
                self.cost_updated = True
        elif kind == "config_option_update":
            self._note_config(upd.get("configOptions"))
        elif kind == "session_info_update":
            meta = upd.get("_meta")
            cu = ((meta.get("kiro") or {}).get("contextUsage") if isinstance(meta, dict)
                  and isinstance(meta.get("kiro"), dict) else None)
            pct = cu.get("usagePercentage") if isinstance(cu, dict) else None
            if isinstance(pct, (int, float)) and not isinstance(pct, bool):
                self.context_pct = float(pct)

    @staticmethod
    def _to_event(upd: Dict[str, Any]) -> Optional[AgentEvent]:
        kind = _UPDATE_KINDS.get(upd.get("sessionUpdate", ""))
        if kind is None:
            return None
        text = ""
        if kind in ("message", "thought"):
            content = upd.get("content")
            if isinstance(content, dict) and content.get("type") == "text":
                text = str(content.get("text", ""))
        elif kind in ("tool", "tool_update"):
            text = str(upd.get("title") or "")
        elif kind == "usage":
            try:
                return AgentEvent("usage", "", {"used": int(upd.get("used") or 0),
                                                "size": int(upd.get("size") or 0)})
            except (TypeError, ValueError):
                return None
        return AgentEvent(kind, text, dict(upd))

    def _dispatch_events(self) -> None:
        while True:
            item = self._events.get()
            if item is None:
                return
            if isinstance(item, threading.Event):
                item.set()
                continue
            try:
                self.on_event(item)
            except Exception:
                log.exception("on_event 回呼失敗")

    def _flush_events(self, timeout: float) -> None:
        if self._reader is None or self._death:
            return
        marker = threading.Event()
        self._events.put(marker)
        marker.wait(timeout)

    # ---- 權限 ----
    def _permission_worker(self, rid: Any, params: Dict[str, Any]) -> None:
        tc = params.get("toolCall") or {}
        raw = tc.get("rawInput")
        req = PermissionRequest(
            tool_call_id=str(tc.get("toolCallId", "")),
            title=str(tc.get("title") or ""),
            kind=str(tc.get("kind") or "other"),
            raw_input=raw if isinstance(raw, dict) else {},
            options=[o for o in (params.get("options") or []) if isinstance(o, dict)],
        )
        # 先讓之前的事件（例如 tool_call）送到 on_event
        self._flush_events(2)
        option_id = ""
        try:
            option_id = self.on_permission(req) or ""
        except Exception:
            log.exception("on_permission 回呼失敗，視為取消")
        self._answer_permission(rid, str(option_id))

    def _answer_permission(self, rid: Any, option_id: str) -> None:
        with self._lock:
            if rid not in self._perm_open:
                return  # 已回覆（例如已取消）
            self._perm_open.discard(rid)
        if option_id:
            outcome = {"outcome": "selected", "optionId": option_id}
        else:
            outcome = {"outcome": "cancelled"}
        try:
            self._write({"jsonrpc": "2.0", "id": rid, "result": {"outcome": outcome}})
        except AcpError:
            pass
