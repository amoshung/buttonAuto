"""權限政策：危險規則 → 路徑超出工作範圍交給使用者 → 讀取類直接允許 → 審查座位拒絕寫入
→ 模型判斷，其餘交給使用者。fetch（連網讀取）不算唯讀捷徑，一律交給模型判斷。"""
import logging
import os
import posixpath
import re
import tempfile
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


_REDIRECT_RE = re.compile(
    r"""(?:(?<![-=<>])(?:1|2)?>+|(?:\btee\s+(?:-a\s+)?|\bOut-File\s+(?:-FilePath\s+)?|\bSet-Content\s+(?:-Path\s+)?|\bAdd-Content\s+(?:-Path\s+)?))\s*['"]?([^\s'";|&><]+)""",
    re.I,
)

_READONLY_SHELL_RE = re.compile(
    r"^\s*(?:git\s+(?:status|log|diff|show|branch|rev-parse|ls-files)|grep|egrep|fgrep|rg|cat|head|tail|ls|dir|stat|wc|find|which|where|pwd|pytest|python(?:\d?(?:\.\d+)?)?\s+-m\s+pytest|cargo\s+(?:test|check|clippy))\b",
    re.I,
)


def _strip_quoted_interior(cmd: str) -> str:
    """保留重導向目標引號外的語法，將字串字面值內部的 '>' 替換為空白，避免把 grep/程式碼字串裡的 '->' 誤判成寫入重導向。"""
    out: List[str] = []
    quote = ""
    esc = False
    for ch in cmd:
        if quote:
            if esc:
                esc = False
                out.append(" ")
            elif ch == "\\" and quote == '"':
                esc = True
                out.append(" ")
            elif ch == quote:
                quote = ""
                out.append(ch)
            else:
                out.append(" " if ch in "><" else ch)
        else:
            if ch in ("'", '"'):
                quote = ch
            out.append(ch)
    return "".join(out)


def _extract_write_paths(cmd: str) -> List[str]:
    if not cmd:
        return []
    cleaned = _strip_quoted_interior(cmd)
    out: List[str] = []
    for m in _REDIRECT_RE.finditer(cleaned):
        cand = m.group(1).strip()
        if cand and not cand.startswith("-") and cand not in ("/dev/null", "NUL", "nul", "&1", "&2"):
            out.append(cand)
    return out


def _is_readonly_shell_cmd(cmd: str) -> bool:
    """判斷是否為無寫入重導向的純查詢／測試 shell 指令，避免把 raw_input 中的唯讀參考路徑誤判為寫入。"""
    c = (cmd or "").strip()
    if not c or any(op in c for op in ("&&", "||", ";", "`", "$(")):
        return False
    return bool(_READONLY_SHELL_RE.match(c))


def _is_temp_scratch_path(path: str) -> bool:
    """系統暫存目錄（/tmp、/var/tmp、本機 tempdir）允許作為 git commit 訊息或測試暫存，不應視為修改唯讀參考區。"""
    p = (path or "").strip()
    if not p:
        return False
    if p.startswith("/"):
        norm_p = posixpath.normpath(p)
        for prefix in ("/tmp", "/var/tmp", "/private/tmp"):
            if norm_p == prefix or norm_p.startswith(prefix + "/"):
                return True
        return False
    try:
        td = os.path.normcase(os.path.realpath(tempfile.gettempdir()))
        full = os.path.normcase(os.path.realpath(os.path.expanduser(p)))
        return full == td or full.startswith(td.rstrip("/\\") + os.sep)
    except (ValueError, OSError):
        return False


def _outside_scope(path: str, scope: str, base_dir: str = "") -> bool:
    try:
        return _outside_scope_raw(path, scope, base_dir=base_dir)
    except (ValueError, OSError):
        return True


def _outside_scope_raw(path: str, scope: str, base_dir: str = "") -> bool:
    if "\x00" in path or "\x00" in scope:
        raise ValueError("NUL byte in path")
    if scope.startswith("/"):
        # SSH 遠端 Linux POSIX 路徑：避免在 Windows 主機上套用 os.path.expanduser / realpath 造成盤符或家目錄錯位
        root = os.path.normcase(posixpath.normpath(scope)).replace("\\", "/")
        p = path.strip()
        if p.startswith("~"):
            return True
        if p.startswith("/"):
            full = posixpath.normpath(p)
        else:
            anchor = base_dir if (base_dir and base_dir.startswith("/")) else scope
            full = posixpath.normpath(posixpath.join(anchor, p.replace("\\", "/")))
        full = os.path.normcase(full).replace("\\", "/")
        return not (full == root or full.startswith(root.rstrip("/") + "/"))
    root = os.path.normcase(os.path.realpath(scope))
    path = os.path.expanduser(path)
    anchor = base_dir or scope
    full = path if os.path.isabs(path) else os.path.join(anchor, path)
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


def format_plan_context(
    criteria: Optional[List[Dict[str, Any]]] = None,
    supervisor_decisions: Optional[List[str]] = None,
    *,
    phase: str = "",
    reference_root: str = "",
    max_chars: int = 1400,
) -> str:
    """整理本次任務的已核定計畫／驗收條件、主管決策、階段與唯讀參考區，供權限模型依任務動態評估。"""
    parts: List[str] = []
    if phase:
        parts.append(f"- 目前開發階段：{phase}")
    if reference_root:
        parts.append(f"- 唯讀參考母區（可自由讀取此目錄進行比對／稽核；寫入僅限允許的工作範圍）：{reference_root}")
    if criteria:
        crit_lines: List[str] = []
        for c in criteria[:12]:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("id") or "").strip()
            ctext = str(c.get("text") or "").strip()
            ccheck = str(c.get("check") or "").strip()
            if not ctext:
                continue
            line = f"  * {cid + ': ' if cid else ''}{ctext[:160]}"
            if ccheck and ccheck != "manual":
                line += f"（驗證：{ccheck[:80]}）"
            crit_lines.append(line)
        if crit_lines:
            parts.append("- 計畫單元與驗收條件：\n" + "\n".join(crit_lines))
    if supervisor_decisions:
        dec_lines = [f"  * {str(d).strip()[:160]}" for d in supervisor_decisions[-5:] if str(d).strip()]
        if dec_lines:
            parts.append("- 高層主管已核定決策：\n" + "\n".join(dec_lines))
    text = "\n".join(parts).strip()
    return text[:max_chars] if len(text) > max_chars else text


def _resolve_display_path(path: str, scope: str) -> str:
    """將相對目標路徑依 scope 展開為完整路徑，避免權限模型誤判相對路徑是否落在工作範圍內。"""
    p = (path or "").strip()
    if not p or not scope:
        return p
    if scope.startswith("/"):
        if p.startswith("/"):
            return p
        return f"{scope.rstrip('/')}/{p.lstrip('./')}"
    if os.path.isabs(p):
        return p
    return os.path.normpath(os.path.join(scope, p))


def _decide(req: PermissionRequest, *, goal: str, scope: str, deny_patterns: List[str],
           router: ModelRouter, read_only: bool, ctx: Optional[Dict[str, Any]] = None,
           reference_root: str = "", plan_context: str = "") -> PermissionDecision:
    # 延後匯入：automation 會載入 Windows UI 自動化模組。
    from automation import build_approval_state, check_deny
    cmd = command_text(req)
    hit = check_deny(deny_haystack(req), deny_patterns)
    if hit:
        return PermissionDecision("escalate", "", f"命中危險規則：{hit}", rule="deny")
    paths = target_paths(req)
    write_redir = _extract_write_paths(cmd)
    is_pure_read = (req.kind in READ_KINDS) and not write_redir
    is_readonly_exec = (req.kind == "execute") and not write_redir and _is_readonly_shell_cmd(cmd)
    if scope:
        if is_pure_read or is_readonly_exec:
            for p in paths:
                out_wt = _outside_scope(p, scope, base_dir=scope)
                out_ref = _outside_scope(p, reference_root, base_dir=scope) if reference_root else True
                if out_wt and out_ref:
                    return PermissionDecision("escalate", "", f"路徑超出工作範圍：{p}", rule="scope")
        else:
            for p in list(paths) + list(write_redir):
                if _is_temp_scratch_path(p):
                    continue
                if _outside_scope(p, scope, base_dir=scope):
                    if reference_root:
                        return PermissionDecision(
                            "reject",
                            pick_option(req.options, "reject"),
                            f"禁止修改目標資料夾以外的路徑（唯讀參考區）：{p}",
                            rule="scope",
                        )
                    return PermissionDecision("escalate", "", f"路徑超出工作範圍：{p}", rule="scope")
    if is_pure_read:
        return PermissionDecision("allow", pick_option(req.options, "allow"), "唯讀操作")
    if read_only and req.kind not in MODEL_KINDS:
        return PermissionDecision("reject", pick_option(req.options, "reject"), "審查座位只能讀")
    extra: List[str] = []
    _strings(req.raw_input or {}, extra)
    combined = "\n".join(dict.fromkeys([cmd] + extra))
    if paths:
        resolved = [_resolve_display_path(p, scope) for p in paths]
        combined += "\n目標路徑：" + "、".join(resolved)
    state = build_approval_state(
        "AI 代理",
        {"pending_request": req.title, "command": combined, "summary": req.title},
        goal,
        scope,
        plan_context=plan_context,
    )
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


def deterministic_gate(
    *,
    tests_passed: bool,
    security_critical: bool = False,
    review_version: str = "",
    head_commit: str = "",
    retries: int = 0,
    max_retries: int = 3,
    blocking: Optional[List[str]] = None,
) -> str:
    """確定性放行閘門：先於模型判斷執行，回傳 PASS / REWORK / BLOCK / ESCALATE。"""
    if security_critical:
        return "BLOCK"
    if retries > max_retries:
        return "ESCALATE"
    if not tests_passed:
        return "REWORK"
    if review_version and head_commit and review_version != head_commit:
        return "REWORK"
    if blocking:
        return "REWORK"
    return "PASS"
