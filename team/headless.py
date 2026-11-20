"""Claude headless 驅動：每回合跑一次 `claude -p ... --output-format stream-json`，逐行解析輸出。

- 第一回合從 system/init 記下 session_id，之後的回合帶 `--resume <id>` 接續同一對話。
- 事件在呼叫 prompt() 的執行緒上依序送出（on_event）。
- 取消或逾時會結束整個程序樹（Windows taskkill /T /F；POSIX killpg）。
- env 是覆蓋值（可能含金鑰），以 SecretEnv 保存，只交給子程序，不寫入事件、例外或日誌；
  值為 None 的鍵會從子程序環境移除。
- 在沒有回合執行時呼叫 cancel() 不做任何事；回合之間的暫停由呼叫端負責
  （房間執行器在每次 prompt 前檢查自己的暫停旗標）。
- 提示詞從 stdin 送（UTF-8，寫完就關），不放在命令列：審查提示詞可能有數萬字的 diff，
  Windows 命令列上限約 32K（WinError 206），npm 的 .cmd 外殼還會在換行處截斷。
- 寫入座位用 acceptEdits，headless 模式下要核准的 Bash 會被默默拒絕，所以用 --allowedTools
  放行 git（status/diff/log/add/commit）、房間的測試指令與唯讀小工具；其他指令（安裝套件等）
  仍會被拒絕。DISALLOWED 一定附上（拒絕優先）。審查座位維持 plan 模式，不加 allowedTools。
- 已收到 result 行就以它為準（即使之後逾時或程序拖著不結束）；沒有 result 就結束一律是 AcpError。
- 用量（Task 17）：init 行的 model 是實際模型 id；result 行的 usage／total_cost_usd／modelUsage 是這一回合
  （這一次 claude -p）的 token 與費用。turn_usage() 回傳最近一回合的數字；沒回報的欄位是 None，不補。
"""
import collections
import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from team.acp import AcpError, _kill_tree, _secs
from team.events import AgentEvent
from team.secrets import SecretEnv, merge_env

log = logging.getLogger(__name__)

DISALLOWED = ["--disallowedTools", "Bash(git push:*)", "Bash(git reset --hard:*)",
              "Bash(git clean:*)", "Bash(rm -rf:*)", "Bash(Remove-Item:*)"]
ALLOWED = ["Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git add:*)",
           "Bash(git commit:*)", "Bash(ls:*)", "Bash(cat:*)", "Bash(type:*)", "Bash(dir:*)",
           "Bash(findstr:*)", "Bash(grep:*)"]
STDERR_KEEP = 50
STDERR_SHOW = 5
# 只放行這些測試工具（第一個字）；其餘（python -c、node、bash、npx…）等於任意程式碼，不放行
_SINGLE = {"pytest", "py.test", "tox", "nox", "jest", "vitest", "mocha"}
_PY_MODULES = {"pytest", "unittest", "tox", "nox"}
_SUB_TEST = {"pnpm", "yarn", "go", "cargo", "dotnet", "mvn", "gradle", "gradlew"}
_PY_RE = re.compile(r"^(python(\d+(\.\d+)*)?|py)$")
_INTERP_RE = re.compile(r"^(python(\d+(\.\d+)*)?|py|node|nodejs|bash|sh|zsh|dash|powershell|pwsh|cmd|npx|"
                        r"deno|bun|ruby|perl|php|wsl)$")
_BAD_CHARS = re.compile(r"[;&|`$<>%^()\r\n]")
_TOKEN_OK = re.compile(r"^[\w.\-/\\: ]+$")
_NPM_SCRIPT = re.compile(r"^[\w:.-]+$")


def _tokens(test_cmd: str) -> List[str]:
    try:
        return shlex.split(test_cmd or "", posix=os.name != "nt")
    except ValueError:
        return []


def _base(tok: str) -> str:
    b = os.path.basename(tok.strip("\"'").replace("\\", "/")).lower()
    for ext in (".exe", ".cmd", ".bat"):
        if b.endswith(ext):
            return b[:-len(ext)]
    return b


def interpreter_head(test_cmd: str) -> str:
    """測試指令以直譯器／外殼開頭（python -c、node、bash、powershell、cmd、npx…）且不是允許清單 → 回該字。"""
    toks = _tokens(test_cmd)
    if not toks or runner_tool_pattern(test_cmd):
        return ""
    head = _base(toks[0])
    return head if _INTERP_RE.match(head) else ""


def runner_tool_pattern(test_cmd: str) -> str:
    """房間測試指令 → allowedTools 樣式，只認允許清單：pytest、py.test、tox、nox、jest、vitest、mocha、
    python -m pytest|unittest|tox|nox、npm test、npm run <名稱>、pnpm／yarn／go／cargo／dotnet／mvn／gradle(w) test。
    整個指令不可含 ; & | ` $ < > 等外殼字元；其他一律回 ""（代理不能自己跑，助理仍會在測試關卡執行）。"""
    cmd = (test_cmd or "").strip()
    if not cmd or _BAD_CHARS.search(cmd):
        return ""
    toks = [t.strip("\"'") for t in _tokens(cmd)]
    if not toks or not all(_TOKEN_OK.match(t) for t in toks[:3]):
        return ""
    head = _base(toks[0])
    sub = toks[1] if len(toks) > 1 else ""
    if head in _SINGLE:
        words = toks[:1]
    elif _PY_RE.match(head) and sub == "-m" and len(toks) > 2 and toks[2] in _PY_MODULES:
        words = toks[:3]
    elif head == "npm" and sub == "test":
        words = toks[:2]
    elif head == "npm" and sub == "run" and len(toks) > 2 and _NPM_SCRIPT.match(toks[2]):
        words = toks[:3]
    elif head in _SUB_TEST and sub == "test":
        words = toks[:2]
    else:
        return ""
    return "Bash(%s:*)" % " ".join(words)


class HeadlessClaude:
    def __init__(self, base_cmd: List[str], cwd: str,
                 on_event: Callable[[AgentEvent], None], read_only: bool,
                 env: Optional[Dict[str, str]] = None, extra_args: Sequence[str] = (),
                 allowed_tools: Sequence[str] = ()):
        self.base_cmd = list(base_cmd)
        self.cwd = cwd
        self.on_event = on_event
        self.read_only = read_only
        self._env = SecretEnv(env) if env else None
        self.extra_args = list(extra_args)
        self.allowed_tools = list(allowed_tools)  # 額外放行（房間的測試指令）；只用於寫入座位
        self.session_id = ""
        self.last_usage = None  # type: Optional[Tuple[int, int]]
        self.chars = 0
        self.model_actual = ""  # init 行回報的模型 id
        self._turn = None  # type: Optional[Dict[str, Any]]
        self._proc = None  # type: Optional[subprocess.Popen]
        self._lock = threading.Lock()
        self._cancelled = False
        self._timed_out = False

    def __repr__(self) -> str:
        return "HeadlessClaude(cwd=%r, read_only=%r, session_id=%r)" % (
            self.cwd, self.read_only, self.session_id)

    def build_cmd(self) -> List[str]:
        """不含提示詞（從 stdin 送）。"""
        allowed = [] if self.read_only else ["--allowedTools"] + list(dict.fromkeys(
            ALLOWED + [t for t in self.allowed_tools if t]))
        return (self.base_cmd
                + ["-p", "--output-format", "stream-json", "--verbose",
                   "--permission-mode", "plan" if self.read_only else "acceptEdits"]
                + (["--resume", self.session_id] if self.session_id else [])
                + allowed + DISALLOWED + self.extra_args)

    # ---- 執行 ----
    def prompt(self, text: str, timeout: float = 1800) -> str:
        env = merge_env(self._env) if self._env is not None else None
        kw = {}  # type: Dict[str, Any]
        if os.name == "nt":
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kw["start_new_session"] = True  # 讓取消時能整個程序群組一起結束
        self._turn = None  # 先清掉：程序起不來時不要沿用上一回合的用量
        spawn_cwd = (
            None
            if (len(self.base_cmd) > 3 and self.base_cmd[1:3] == ["-u", "-c"] and "REMOTE_STUB" in self.base_cmd[3] and not os.path.isdir(self.cwd))
            else self.cwd
        )
        with self._lock:
            self._cancelled = False
            self._timed_out = False
            try:
                proc = subprocess.Popen(
                    self.build_cmd(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, cwd=spawn_cwd, env=env,
                    text=True, encoding="utf-8", errors="replace", bufsize=1, **kw)
            except OSError as e:
                raise AcpError("無法啟動 claude：%s" % e)
            self._proc = proc
        self.chars += len(text)
        self._turn = None
        writer = threading.Thread(target=self._write_prompt, args=(proc, text),
                                  name="claude-stdin", daemon=True)
        writer.start()
        stderr = collections.deque(maxlen=STDERR_KEEP)
        err_thread = threading.Thread(target=self._read_stderr, args=(proc, stderr),
                                      name="claude-stderr", daemon=True)
        err_thread.start()
        stop_watch = threading.Event()
        started = time.monotonic()
        last_activity = [started]
        idle_limit = float(timeout)
        max_total = max(idle_limit * 4.0, 1800.0)

        def _watchdog() -> None:
            while not stop_watch.wait(min(2.0, max(0.05, idle_limit / 4.0))):
                now = time.monotonic()
                if (now - last_activity[0]) >= idle_limit or (now - started) >= max_total:
                    self._on_timeout(proc)
                    break

        timer = threading.Thread(target=_watchdog, name="claude-watchdog", daemon=True)
        timer.start()
        state = {"result": None, "used": None, "size": None}  # type: Dict[str, Any]
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                last_activity[0] = time.monotonic()
                try:
                    msg = json.loads(line)
                except ValueError:
                    log.debug("claude 非 JSON 輸出：%s", line[:200])
                    continue
                if isinstance(msg, dict):
                    try:
                        self._handle(msg, state)
                    except Exception:
                        log.exception("處理 claude 輸出失敗")
                if state["result"] is not None:
                    stop_watch.set()  # 已拿到結果：不再逾時
                    break
        except Exception:
            log.debug("讀取 claude 輸出中斷", exc_info=True)
        finally:
            stop_watch.set()
        try:
            code = proc.wait(5)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            code = proc.poll()
        err_thread.join(2)
        with self._lock:
            self._proc = None
        if state["result"] is not None:
            return state["result"]
        if self._timed_out:
            raise AcpError("claude 在 %s 秒內沒有完成" % _secs(timeout))
        if self._cancelled:
            return "cancelled"
        tail = "\n".join(list(stderr)[-STDERR_SHOW:])
        raise AcpError("claude 沒有回傳結果就結束了（exit %s）：%s" % (code, tail))

    def turn_usage(self) -> Optional[Dict[str, Any]]:
        """最近一回合：{model, models, in, out, in_cache, cost, currency}；沒有 result 行 → None。"""
        return dict(self._turn) if self._turn is not None else None

    def cancel(self) -> None:
        with self._lock:
            p = self._proc
            if p is None:
                return  # 沒有回合在跑：不做事
            self._cancelled = True
        _kill_tree(p)

    def close(self) -> None:
        # 只結束進行中的回合
        with self._lock:
            p = self._proc
            if p is None:
                return
            self._cancelled = True
        _kill_tree(p)

    # ---- 內部 ----
    def _on_timeout(self, proc: subprocess.Popen) -> None:
        self._timed_out = True
        _kill_tree(proc)

    @staticmethod
    def _write_prompt(proc: subprocess.Popen, text: str) -> None:
        """另開執行緒寫 stdin，避免子程序先輸出塞滿管線時互相卡住。寫原始 UTF-8 位元組（不轉換換行）。"""
        try:
            stream = proc.stdin
            raw = getattr(stream, "buffer", stream)
            raw.write(text.encode("utf-8"))
            raw.flush()
        except (OSError, ValueError):
            log.debug("寫入 claude stdin 失敗", exc_info=True)
        finally:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass

    @staticmethod
    def _read_stderr(proc: subprocess.Popen, keep: "collections.deque") -> None:
        try:
            for line in proc.stderr:
                line = line.rstrip("\r\n")
                if line:
                    keep.append(line)
        except Exception:
            pass

    def _emit(self, ev: AgentEvent) -> None:
        try:
            self.on_event(ev)
        except Exception:
            log.exception("on_event 回呼失敗")

    def _handle(self, msg: Dict[str, Any], state: Dict[str, Any]) -> None:
        typ = msg.get("type")
        if typ == "system":
            if msg.get("subtype") == "init" and msg.get("session_id"):
                self.session_id = str(msg["session_id"])
            if msg.get("subtype") == "init" and isinstance(msg.get("model"), str) and msg["model"].strip():
                self.model_actual = msg["model"].strip()[:120]
            return
        if typ == "assistant":
            message = msg.get("message") or {}
            usage = message.get("usage")
            if isinstance(usage, dict):
                used = 0
                for k in ("input_tokens", "cache_read_input_tokens",
                          "cache_creation_input_tokens"):
                    try:
                        used += int(usage.get(k) or 0)
                    except (TypeError, ValueError):
                        pass
                state["used"] = used
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    t = str(block.get("text", ""))
                    self.chars += len(t)
                    self._emit(AgentEvent("message", t, dict(block)))
                elif block.get("type") == "tool_use":
                    name = str(block.get("name") or "")
                    raw = block.get("input")
                    self._emit(AgentEvent("tool", name, {
                        "sessionUpdate": "tool_call",
                        "toolCallId": str(block.get("id") or ""),
                        "title": name, "kind": "other", "status": "pending",
                        "rawInput": raw if isinstance(raw, dict) else {}}))
            return
        if typ == "user":
            # Task 19：claude 把工具結果放在 user 訊息的 tool_result 區塊（以 tool_use_id 對回 tool_use）
            message = msg.get("message") or {}
            content = message.get("content") if isinstance(message, dict) else None
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                out = block.get("content")
                if isinstance(out, str) and len(out) > 20000:
                    out = out[-20000:]  # 只需要尾端（事件會再截斷）
                self._emit(AgentEvent("tool_update", "", {
                    "sessionUpdate": "tool_call_update", "toolCallId": str(block.get("tool_use_id") or ""),
                    "status": "failed" if block.get("is_error") else "completed", "content": out}))
            return
        if typ == "result":
            if msg.get("session_id"):
                self.session_id = str(msg["session_id"])
            mu = msg.get("modelUsage")
            if isinstance(mu, dict):
                for v in mu.values():
                    if isinstance(v, dict) and v.get("contextWindow"):
                        try:
                            state["size"] = int(v["contextWindow"])
                            break
                        except (TypeError, ValueError):
                            pass
            self._turn = self._result_usage(msg)
            if state["used"] is not None and state["size"] is not None:
                self.last_usage = (state["used"], state["size"])
                self._emit(AgentEvent("usage", "", {"used": state["used"],
                                                    "size": state["size"]}))
            if msg.get("is_error"):
                state["result"] = "error"
                self._emit(AgentEvent("error", str(msg.get("result") or msg.get("subtype")
                                                   or "claude 回報錯誤"), {}))
            else:
                state["result"] = "end_turn"

    def _result_usage(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        def num(v: Any) -> Optional[int]:
            if isinstance(v, bool):
                return None
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
        u = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
        base = num(u.get("input_tokens"))
        cache = [num(u.get(k)) for k in ("cache_creation_input_tokens", "cache_read_input_tokens")]
        cache_sum = sum(c for c in cache if c is not None) if any(c is not None for c in cache) else None
        tin = None if base is None and cache_sum is None else (base or 0) + (cache_sum or 0)
        mu = msg.get("modelUsage") if isinstance(msg.get("modelUsage"), dict) else {}
        models = [str(k)[:120] for k in mu.keys()]
        cost = msg.get("total_cost_usd")
        cost = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
        return {"model": self.model_actual or (models[0] if models else ""), "models": models,
                "in": tin, "out": num(u.get("output_tokens")), "in_cache": cache_sum,
                "cost": cost, "currency": "USD" if cost is not None else ""}
