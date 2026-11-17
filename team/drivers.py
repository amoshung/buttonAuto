"""統一驅動介面：ACP（kiro／agy）與 Claude headless（claude、GLM 座位）用同一組方法操作。

- make_driver：依 CliInfo.protocol 建立驅動。
- seat_launch：把座位（kiro／agy／claude／glm／glm_high）換算成要啟動的 CLI、環境變數覆蓋值與額外參數。
  環境變數可能含金鑰：回傳的 SecretEnv 在 repr／str 時遮蔽值。
  GLM 座位會把 Anthropic 自己的憑證設為 None（子程序環境中移除），不送到 LiteLLM。
"""
import json
import os
import shutil
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

try:
    from typing import Protocol
except ImportError:  # pragma: no cover
    Protocol = object  # type: ignore

from team import netcerts
from team.acp import AcpClient
from team.clis import CliInfo, launch_cmd
from team.events import AgentEvent, PermissionRequest
from team.headless import HeadlessClaude
from team.secrets import SecretEnv

MODELS_WAIT = 3.0  # 等代理回報可用模型的上限（秒）
SEATS = ("kiro", "agy", "claude", "glm", "glm_high")
GLM_SEATS = ("glm", "glm_high")
STRIP_FOR_GLM = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
SERENA_ARGS = ["--from", "git+https://github.com/oraios/serena", "serena", "start-mcp-server",
               "--context", "claude-code", "--project"]


class Driver(Protocol):
    label: str
    last_usage: Optional[Tuple[int, int]]
    chars: int

    def start(self) -> None: ...

    def prompt(self, text: str, timeout: float = 1800) -> str: ...

    def cancel(self) -> None: ...

    def close(self) -> None: ...


class AcpDriver:
    def __init__(self, cmd: List[str], cwd: str, label: str,
                 on_event: Callable[[AgentEvent], None],
                 on_permission: Callable[[PermissionRequest], str],
                 env: Optional[Dict[str, str]] = None, model: str = ""):
        self.label = label
        self.model = model
        self.model_failed = False  # 指定的模型切換失敗（Task 18：你指定的模型不默默改用別的）
        self.chars = 0
        self._on_event = on_event
        self._cost_seen = None  # type: Optional[Tuple[float, str]]
        self.client = AcpClient(cmd, cwd, self._event, on_permission, env=env)

    def __repr__(self) -> str:
        return "AcpDriver(label=%r)" % self.label

    @property
    def last_usage(self) -> Optional[Tuple[int, int]]:
        return self.client.last_usage

    def _event(self, ev: AgentEvent) -> None:
        if ev.kind in ("message", "thought"):
            self.chars += len(ev.text)
        self._on_event(ev)

    def start(self) -> None:
        self.client.start()
        self.client.new_session()
        if self.model:
            self.client.wait_models(MODELS_WAIT)  # Kiro v3 在 session/new 之後才用 config_option_update 回報清單
        if self.model and not self.set_model(self.model):
            # 切換模型失敗：驅動本身不中斷；是不是你指定的模型由執行器判斷（model_failed）
            try:
                self._on_event(AgentEvent("notice", "無法切換模型 %s" % self.model))
            except Exception:
                pass

    def set_model(self, model: str) -> bool:
        """session/set_model（對話中途換模型，下一回合生效）。不在可用清單或代理拒絕 → False。"""
        avail = {m["id"] for m in self.client.available_models}
        ok = (not avail or model in avail) and self.client.set_model(model)
        self.model_failed = not ok
        if ok:
            self.model = model
        return ok

    def prompt(self, text: str, timeout: float = 1800) -> str:
        self.chars += len(text)
        return self.client.prompt(text, timeout)

    def turn_usage(self) -> Optional[Dict[str, Any]]:
        """最近一回合的用量：代理有回報才有數字，其餘 None（kiro 預設不回報 token 與模型）。

        費用：usage_update 的 cost 是整個對話的累計值，這裡換算成本回合增加的部分。"""
        c = self.client
        u = c.prompt_usage if isinstance(c.prompt_usage, dict) else {}

        def num(k: str) -> Optional[int]:
            v = u.get(k)
            return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        cost = cur = None
        if c.cost is not None and getattr(c, "cost_updated", True):
            total, cur = c.cost
            prev = self._cost_seen if self._cost_seen is not None and self._cost_seen[1] == cur else (0.0, cur)
            cost = round(max(0.0, total - prev[0]), 6)
            self._cost_seen = c.cost
        return {"model": c.current_model, "models": [c.current_model] if c.current_model else [],
                "in": num("inputTokens"), "out": num("outputTokens"), "in_cache": num("cachedReadTokens"),
                "cost": cost, "currency": cur or "", "context_pct": c.context_pct}

    def cancel(self) -> None:
        self.client.cancel()

    def close(self) -> None:
        self.client.close()


class HeadlessDriver:
    def __init__(self, claude: HeadlessClaude, label: str):
        self.claude = claude
        self.label = label

    def __repr__(self) -> str:
        return "HeadlessDriver(label=%r)" % self.label

    @property
    def last_usage(self) -> Optional[Tuple[int, int]]:
        return self.claude.last_usage

    @property
    def chars(self) -> int:
        return self.claude.chars

    def start(self) -> None:
        pass  # 每回合才啟動程序

    def set_model(self, model: str) -> bool:
        """下一回合帶 --model（--resume 接續同一段對話）。"""
        args = list(self.claude.extra_args)
        while "--model" in args:
            i = args.index("--model")
            del args[i:i + 2]
        self.claude.extra_args = args + (["--model", model] if model else [])
        return True

    def set_allowed_tools(self, tools: Sequence[str]) -> None:
        """下一回合起額外放行的 Bash 樣式（房間的測試指令）；審查座位（plan）不受影響。"""
        self.claude.allowed_tools = [t for t in tools if t]

    def prompt(self, text: str, timeout: float = 1800) -> str:
        return self.claude.prompt(text, timeout)

    def turn_usage(self) -> Optional[Dict[str, Any]]:
        return self.claude.turn_usage()

    def cancel(self) -> None:
        self.claude.cancel()

    def close(self) -> None:
        self.claude.close()


def with_cert_env(env: Optional[Dict[str, Optional[str]]]) -> SecretEnv:
    """所有 CLI 子程序都經過這裡：公司網路憑證（netcerts.cert_env）優先序最低，座位自己的環境變數蓋過它。"""
    merged = SecretEnv(cert_env())
    merged.update(env or {})
    return merged


def cert_env() -> Dict[str, str]:
    try:
        return netcerts.cert_env()
    except Exception:  # 憑證匯出失敗不擋住 CLI 啟動
        return {}


def make_driver(cli: CliInfo, cwd: str, on_event: Callable[[AgentEvent], None],
                on_permission: Callable[[PermissionRequest], str], read_only: bool,
                cmd_override: Optional[List[str]] = None,
                env: Optional[Dict[str, str]] = None, extra_args: Sequence[str] = (),
                model: str = "", allowed_tools: Sequence[str] = ()) -> Driver:
    """acp → AcpDriver（start = initialize + session/new [+ set_model]）；
    headless → HeadlessDriver（read_only 用 plan 模式；model 非空加 --model；
    allowed_tools 是寫入座位額外放行的 Bash 樣式，例如房間的測試指令）。"""
    cmd = list(cmd_override) if cmd_override else launch_cmd(cli)
    env = with_cert_env(env)
    if cli.protocol == "acp":
        return AcpDriver(cmd + list(extra_args), cwd, cli.label, on_event, on_permission,
                         env=env, model=model)
    extra = list(extra_args) + (["--model", model] if model else [])
    return HeadlessDriver(HeadlessClaude(cmd, cwd, on_event, read_only, env=env,
                                         extra_args=extra, allowed_tools=allowed_tools), cli.label)


def seat_launch(seat: str, clis: Dict[str, CliInfo], cfg: Optional[Any],
                plugin_dirs: Sequence[str], worktree: str,
                tmpdir: str) -> Tuple[CliInfo, Dict[str, Optional[str]], List[str]]:
    """cfg 需有 base_url、glm_key、glm_model（ModelConfig）；glm 座位以外可為 None。

    回傳的 env（SecretEnv）是覆蓋值：值為 None 表示「從子程序環境移除」（GLM 座位用來去掉
    Anthropic 憑證）。請交給 make_driver／HeadlessClaude／AcpClient（以 merge_env 合併），
    不要直接當 subprocess 的 env。"""
    if seat not in SEATS:
        raise ValueError("不認得的座位：%s" % seat)
    if seat in ("kiro", "agy"):
        if seat not in clis:
            raise ValueError("找不到 %s CLI" % seat)
        return clis[seat], SecretEnv(), []
    if seat in GLM_SEATS and cfg is None:
        raise ValueError("缺少 litellm.local.json，GLM 座位不可用")
    if "claude" not in clis:
        raise ValueError("找不到 claude CLI")
    env = SecretEnv()
    if seat in GLM_SEATS:
        env["ANTHROPIC_BASE_URL"] = str(cfg.base_url)
        env["ANTHROPIC_AUTH_TOKEN"] = str(cfg.glm_key)
        env["ANTHROPIC_MODEL"] = str(cfg.glm_model)
        # 不把 Anthropic 憑證送到 LiteLLM
        for k in STRIP_FOR_GLM:
            env[k] = None
        if seat == "glm_high":
            env["MAX_THINKING_TOKENS"] = "16000"
    env["DISABLE_AUTO_COMPACT"] = "1"
    extra = []  # type: List[str]
    for d in plugin_dirs:
        if d and os.path.exists(d):
            extra += ["--plugin-dir", d]
    uvx = shutil.which("uvx")
    if uvx:
        os.makedirs(tmpdir, exist_ok=True)
        path = os.path.join(tmpdir, "serena.mcp.json")
        conf = {"mcpServers": {"serena": {"command": uvx,
                                          "args": SERENA_ARGS + [worktree]}}}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(conf, f, ensure_ascii=False, indent=2)
        extra += ["--mcp-config", path]
    return clis["claude"], env, extra
