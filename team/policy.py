"""權限政策：危險規則 → 路徑超出工作範圍交給使用者 → 讀取類直接允許 → 審查座位拒絕寫入
→ 模型判斷，其餘交給使用者。fetch（連網讀取）不算唯讀捷徑，一律交給模型判斷。"""
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from team.events import PermissionRequest, command_text
from team.models import AllowVerdict, ModelRouter, call_ctx

READ_KINDS = {"read", "search", "think"}
MODEL_KINDS = {"fetch"}  # 審查座位也可，但要經模型判斷


@dataclass
class PermissionDecision:
    outcome: str  # allow / reject / escalate
    option_id: str
    reason: str
    verdict: Optional[AllowVerdict] = None
    # Task 20：規則擋下的原因（deny 危險規則｜scope 超出工作範圍｜limit 內容過大｜error 判斷失敗）；
    # 有值時代管也不碰（直接拒絕），模型判斷的灰色地帶／高風險為空字串
    rule: str = ""


def pick_option(options: List[Dict[str, str]], outcome: str) -> str:
    prefix = "allow" if outcome == "allow" else "reject"
    once = [o for o in options if o.get("kind") == prefix + "_once"]
    if once:
        return once[0].get("optionId", "")
    anyk = [o for o in options if str(o.get("kind", "")).startswith(prefix + "_")]
    return anyk[0].get("optionId", "") if anyk else ""


PATH_KEYS = ("path", "file_path", "filePath", "target", "destination", "paths",
             "old_path", "new_path", "file", "dir", "cwd")


class _TooBig(Exception):
    pass


MAX_DEPTH = 50
MAX_STRINGS = 10000


def _strings(obj: Any, out: List[str]) -> None:
    """迭代掃描所有字串（含 dict 鍵）；過深／過大／循環參照 → _TooBig。"""
    stack = [(obj, 0)]
    seen = set()
    count = 0
    while stack:
        cur, depth = stack.pop()
        if isinstance(cur, str):
            out.append(cur)
            count += 1
        elif isinstance(cur, (dict, list, tuple)):
            if depth > MAX_DEPTH or id(cur) in seen:
                raise _TooBig()
            seen.add(id(cur))
            if isinstance(cur, dict):
                for k, v in cur.items():
                    if isinstance(k, str):
                        out.append(k)
                        count += 1
                    stack.append((v, depth + 1))
            else:
                if cur and all(isinstance(x, (str, int, float)) for x in cur):
                    out.append(" ".join(str(x) for x in cur))
                    count += 1
                for v in cur:
                    stack.append((v, depth + 1))
        if count > MAX_STRINGS:
            raise _TooBig()


def deny_haystack(req: PermissionRequest) -> List[str]:
    """危險規則要掃的所有文字：標題、指令、raw_input 內每個字串（遞迴）。"""
    out: List[str] = [req.title or "", command_text(req)]
    _strings(req.raw_input or {}, out)
    return [t for t in out if t]


def target_paths(req: PermissionRequest) -> List[str]:
    raw = req.raw_input if isinstance(req.raw_input, dict) else {}
    paths: List[str] = []
    for key in PATH_KEYS:
        val = raw.get(key)
        if isinstance(val, str):
            paths.append(val)
        elif isinstance(val, (list, tuple)):
            paths.extend(x for x in val if isinstance(x, str))
    locs = raw.get("locations")
    for loc in locs if isinstance(locs, (list, tuple)) else []:
        if isinstance(loc, dict) and isinstance(loc.get("path"), str):
            paths.append(loc["path"])
    return [p for p in paths if p.strip()]


def _outside_scope(path: str, scope: str) -> bool:
    try:
        return _outside_scope_raw(path, scope)
    except (ValueError, OSError):
        return True


def _outside_scope_raw(path: str, scope: str) -> bool:
    root = os.path.normcase(os.path.realpath(scope))
    path = os.path.expanduser(path)
    full = path if os.path.isabs(path) else os.path.join(scope, path)
    full = os.path.normcase(os.path.realpath(full))
    return not (full == root or full.startswith(root.rstrip("/\\") + os.sep))


def decide(req: PermissionRequest, **kw) -> PermissionDecision:
    """失敗即關閉：任何例外都交給使用者，絕不自動允許。"""
    try:
        return _decide(req, **kw)
    except _TooBig:
        return PermissionDecision("escalate", "", "請求內容過深或過大，交給你決定", rule="limit")
    except Exception as e:
        return PermissionDecision("escalate", "", f"權限判斷失敗，交給你決定：{type(e).__name__}", rule="error")


def verdict_text(v: AllowVerdict) -> str:
    """判定的數字與模型版本，例：allow 0.93 risk 0.04；模型 Decision-1。"""
    nums = v.detail or "allow %.2f risk %.2f" % (v.allow, v.risk)
    return nums if v.detail else "%s；模型 %s" % (nums, v.by)


def _decide(req: PermissionRequest, *, goal: str, scope: str, deny_patterns: List[str],
           router: ModelRouter, read_only: bool, ctx: Optional[Dict[str, Any]] = None) -> PermissionDecision:
    # 延後匯入：automation 會載入 Windows UI 自動化模組。
    from automation import build_approval_state, check_deny
    cmd = command_text(req)
    hit = check_deny(deny_haystack(req), deny_patterns)
    if hit:
        return PermissionDecision("escalate", "", f"命中危險規則：{hit}", rule="deny")
    paths = target_paths(req)
    if scope:
        for p in paths:
            if _outside_scope(p, scope):
                return PermissionDecision("escalate", "", f"路徑超出工作範圍：{p}", rule="scope")
    if req.kind in READ_KINDS:
        return PermissionDecision("allow", pick_option(req.options, "allow"), "唯讀操作")
    if read_only and req.kind not in MODEL_KINDS:
        return PermissionDecision("reject", pick_option(req.options, "reject"), "審查座位只能讀")
    extra: List[str] = []
    _strings(req.raw_input or {}, extra)
    combined = "\n".join(dict.fromkeys([cmd] + extra))
    if paths:
        combined += "\n目標路徑：" + "、".join(paths)
    state = build_approval_state(
        "AI 代理", {"pending_request": req.title, "command": combined, "summary": req.title}, goal, scope)
    v = call_ctx(router.decide_allow, state, ctx=ctx)
    if v.by == "none":
        return PermissionDecision("escalate", "", f"{v.reason}（allow={v.allow:.2f}, risk={v.risk:.2f}）", v)
    if v.auto:
        return PermissionDecision("allow", pick_option(req.options, "allow"), f"{v.reason}（{verdict_text(v)}）", v)
    return PermissionDecision(
        "escalate", "", f"{v.reason}（{verdict_text(v)}）", v)


class PendingPermission:
    """交給使用者的等待物；第一個結果為準。wait() 回 allow／reject／timeout（逾時，呼叫端當拒絕並記錄）。"""

    def __init__(self, timeout: float = 1800):
        self.timeout = timeout
        self._ev = threading.Event()
        self._lock = threading.Lock()
        self._done = False
        self._outcome = "timeout"

    def answer(self, outcome: str) -> bool:
        """回 True 表示這個回答生效（第一個）；已有結果（含逾時）回 False。"""
        if outcome not in ("allow", "reject"):
            raise ValueError(f"outcome 只能是 allow 或 reject：{outcome!r}")
        with self._lock:
            if self._done:
                return False
            self._done = True
            self._outcome = outcome
        self._ev.set()
        return True

    def wait(self, on_tick: Optional[Any] = None, tick: float = 0.2) -> str:
        """on_tick：等待中每隔 tick 秒呼叫一次（代管開啟時由它替使用者回答）；逾時仍以 timeout 為準。"""
        if on_tick is not None:
            end = time.monotonic() + self.timeout
            while not self._ev.wait(max(0.0, min(tick, end - time.monotonic()))):
                if time.monotonic() >= end:
                    break
                try:
                    on_tick()
                except Exception:
                    logging.getLogger(__name__).exception("等待權限時的代管處理失敗")
            if self._ev.is_set():
                return self._outcome
        elif self._ev.wait(self.timeout):
            return self._outcome
        with self._lock:
            if not self._done:
                self._done = True
                self._outcome = "timeout"
            return self._outcome
