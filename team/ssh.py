"""SSH 遠端開發支援與遠端 CLI 嚴格握手偵測（claude、agy、kiro-cli）。

核心原則：
1. 自動解析本機 ~/.ssh/config（支援 Include）列出非萬用字元的 Host 別名，並結合 ssh_hosts.local.json 自訂主機。
2. 嚴格防範 SSH Option Injection：拒絕以 '-' 開頭或含空白／控制字元的主機目標。
3. 透過系統 OpenSSH（`ssh -T -o BatchMode=yes`）將標準庫單檔 Python 探針由 stdin 送往遠端執行：
   - 瀏覽遠端資料夾（remote_dirs）
   - 掃描遠端專案與測試指令（remote_scan）
   - 尋找遠端 claude、agy、kiro-cli 並執行嚴格 ACP / Headless 握手測試（probe_host_clis）；
     握手失敗或未安裝的 CLI 一律不回傳在可調用清單中（「不能調用的就別顯示出來」）。
4. 支援遠端指令與長連線 stdio 轉發（build_ssh_stdio_cmd），透過 stdin 封包傳遞環境變數與金鑰，不在遠端 ps aux 暴露機密。
"""
import atexit
import glob
import json
import logging
import os
import queue
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

_HOST_TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]+(?:\@[A-Za-z0-9._-]+)?(?:\:\d{1,5})?$")
_SAFE_ALIAS_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SSH_PASS_LOCK = threading.RLock()
_SSH_PASSWORDS: Dict[str, str] = {}
_ASKPASS_HELPER_PATH: Optional[str] = None


class SshError(ValueError):
    """SSH 設定、目標驗證或連線錯誤。"""


def set_ssh_password(ssh_host: str, password: str) -> None:
    """在記憶體暫存指定 SSH 主機的登入密碼（絕不寫入版控或日誌）。"""
    key = (ssh_host or "").strip()
    if not key:
        return
    with _SSH_PASS_LOCK:
        if password:
            _SSH_PASSWORDS[key] = password
        else:
            _SSH_PASSWORDS.pop(key, None)
    close_ssh_channels(key)


def get_ssh_password(ssh_host: str) -> str:
    key = (ssh_host or "").strip()
    if not key:
        return ""
    with _SSH_PASS_LOCK:
        return _SSH_PASSWORDS.get(key, "")


def has_ssh_password(ssh_host: str) -> bool:
    return bool(get_ssh_password(ssh_host))


def clear_ssh_passwords() -> None:
    """清除記憶體中暫存的所有 SSH 密碼。"""
    with _SSH_PASS_LOCK:
        _SSH_PASSWORDS.clear()
    close_ssh_channels()


def is_ssh_auth_error(text: str) -> bool:
    """判斷 SSH 錯誤輸出是否為需要密碼／金鑰通行碼的驗證失敗。"""
    low = (text or "").lower()
    return any(
        token in low
        for token in (
            "permission denied",
            "password:",
            "passphrase",
            "keyboard-interactive",
            "authentication failed",
            "publickey,password",
            "gssapi-with-mic,password",
            "connection closed by",
            "too many authentication failures",
        )
    )


def ensure_askpass_helper() -> str:
    """產生跨平台 OpenSSH SSH_ASKPASS 輔助腳本（Windows 使用 .cmd 包裝 Python，POSIX 使用 .sh）。"""
    global _ASKPASS_HELPER_PATH
    with _SSH_PASS_LOCK:
        if _ASKPASS_HELPER_PATH and os.path.isfile(_ASKPASS_HELPER_PATH):
            return _ASKPASS_HELPER_PATH
        tmp_dir = tempfile.gettempdir()
        py_path = os.path.join(tmp_dir, "aea_ssh_askpass_helper.py")
        py_code = (
            "import os, sys\n"
            "prompt = ' '.join(sys.argv[1:]).lower()\n"
            "if 'yes/no' in prompt or 'continue connecting' in prompt or 'fingerprint' in prompt:\n"
            "    sys.stdout.write('yes\\n')\n"
            "else:\n"
            "    sys.stdout.write(os.environ.get('BUTTONAUTO_SSH_PASS', '') + '\\n')\n"
            "sys.stdout.flush()\n"
        )
        try:
            with open(py_path, "w", encoding="utf-8") as f:
                f.write(py_code)
        except OSError:
            pass
        if os.name == "nt":
            cmd_path = os.path.join(tmp_dir, "aea_ssh_askpass_helper.cmd")
            try:
                with open(cmd_path, "w", encoding="utf-8") as f:
                    f.write(f'@"{sys.executable}" "{py_path}" %*\r\n')
            except OSError:
                pass
            _ASKPASS_HELPER_PATH = cmd_path
        else:
            sh_path = os.path.join(tmp_dir, "aea_ssh_askpass_helper.sh")
            try:
                with open(sh_path, "w", encoding="utf-8") as f:
                    f.write(f'#!/bin/sh\nexec "{sys.executable}" "{py_path}" "$@"\n')
                os.chmod(sh_path, 0o700)
            except OSError:
                pass
            _ASKPASS_HELPER_PATH = sh_path
        return _ASKPASS_HELPER_PATH


def with_ssh_askpass_env(ssh_host: str, base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """為本機 ssh 子程序附加 SSH_ASKPASS 環境變數（若該主機有設定密碼則自動帶入 BUTTONAUTO_SSH_PASS）。"""
    env = dict(base_env if base_env is not None else os.environ)
    if not ssh_host:
        return env
    pw = get_ssh_password(ssh_host)
    helper = ensure_askpass_helper()
    env["SSH_ASKPASS"] = helper
    env["SSH_ASKPASS_REQUIRE"] = "force"
    if not env.get("DISPLAY"):
        env["DISPLAY"] = "1"
    if pw:
        env["BUTTONAUTO_SSH_PASS"] = pw
    return env


def validate_ssh_target(target: str) -> str:
    """驗證 SSH 目標字串（alias 或 user@host[:port]）。合法回傳 ""，否則回傳中文錯誤原因。"""
    if not isinstance(target, str) or not target.strip():
        return "請提供 SSH 主機名稱或 user@host"
    s = target.strip()
    if len(s) > 256:
        return "SSH 主機名稱太長"
    if s.startswith("-"):
        return "SSH 主機名稱不可由 '-' 開頭"
    if any(c.isspace() or ord(c) < 32 for c in s):
        return "SSH 主機名稱不可包含空白或控制字元"
    if not _HOST_TOKEN_RE.match(s):
        return "SSH 主機格式不正確（請使用 Host 別名或 user@host[:port]）"
    host_part, port = split_ssh_target(s)
    if host_part.startswith("-") or ("@" in host_part and host_part.split("@", 1)[1].startswith("-")):
        return "SSH 主機名稱不可由 '-' 開頭"
    if port is not None and not (1 <= port <= 65535):
        return "SSH 連接埠必須在 1 到 65535 之間"
    return ""


def split_ssh_target(target: str) -> Tuple[str, Optional[int]]:
    """將 'user@host:2222' 拆為 ('user@host', 2222)；無 port 則回傳 (target, None)。"""
    s = (target or "").strip()
    if ":" in s:
        host_part, port_str = s.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            return s, -1
        return host_part, port
    return s, None


def build_ssh_cmd(
    ssh_host: str,
    remote_argv: Sequence[str],
    *,
    connect_timeout: int = 10,
    identity_file: str = "",
    use_password: Optional[bool] = None,
) -> List[str]:
    """建構安全的本機 ssh 命令列。若已設定該主機密碼（或 use_password=True），啟用 SSH_ASKPASS 密碼驗證。"""
    err = validate_ssh_target(ssh_host)
    if err:
        raise SshError(err)
    host_part, port = split_ssh_target(ssh_host)
    allow_pw = has_ssh_password(ssh_host) if use_password is None else bool(use_password)
    cmd = [
        "ssh",
        "-T",
        "-o",
        f"BatchMode={'no' if allow_pw else 'yes'}",
        "-o",
        f"ConnectTimeout={max(1, int(connect_timeout))}",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "GSSAPIAuthentication=no",
        "-o",
        "VerifyHostKeyDNS=no",
    ]
    if allow_pw:
        cmd += [
            "-o",
            "NumberOfPasswordPrompts=1",
            "-o",
            "PreferredAuthentications=publickey,keyboard-interactive,password",
        ]
    if port is not None:
        cmd += ["-p", str(port)]
    if identity_file and os.path.isfile(identity_file):
        cmd += ["-i", identity_file]
    cmd.append(host_part)
    cmd.extend(str(a) for a in remote_argv)
    return cmd


def parse_ssh_config(config_path: str = "", _visited: Optional[set] = None) -> List[Dict[str, Any]]:
    """解析 OpenSSH ~/.ssh/config（支援 Include），回傳非萬用字元的 Host 清單。"""
    path = config_path or os.path.expanduser("~/.ssh/config")
    path = os.path.abspath(os.path.expanduser(path))
    visited = _visited if _visited is not None else set()
    norm = os.path.normcase(path)
    if norm in visited or not os.path.isfile(path):
        return []
    visited.add(norm)

    base_dir = os.path.dirname(path)
    out: List[Dict[str, Any]] = []
    seen_ids = set()
    current_aliases: List[str] = []
    current_opts: Dict[str, str] = {}

    def _flush() -> None:
        for alias in current_aliases:
            if not alias or "*" in alias or "?" in alias or alias.startswith("!"):
                continue
            if validate_ssh_target(alias) != "":
                continue
            if alias in seen_ids:
                continue
            seen_ids.add(alias)
            port_val = None
            if "port" in current_opts:
                try:
                    port_val = int(current_opts["port"])
                except ValueError:
                    port_val = None
            out.append(
                {
                    "id": alias,
                    "label": alias,
                    "source": "ssh_config",
                    "hostname": current_opts.get("hostname", alias),
                    "user": current_opts.get("user", ""),
                    "port": port_val,
                    "identity_file": current_opts.get("identityfile", ""),
                }
            )

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []

    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = re.split(r"\s*=\s*|\s+", line, maxsplit=1)
        if len(parts) < 2:
            continue
        key, val = parts[0].lower(), parts[1].strip()
        if key == "include":
            _flush()
            current_aliases = []
            current_opts = {}
            for pat in val.split():
                pat = os.path.expanduser(pat.strip("\"'"))
                if not os.path.isabs(pat):
                    pat = os.path.join(base_dir, pat)
                for inc_path in sorted(glob.glob(pat)):
                    for item in parse_ssh_config(inc_path, visited):
                        if item["id"] not in seen_ids:
                            seen_ids.add(item["id"])
                            out.append(item)
        elif key == "host":
            _flush()
            current_aliases = [a.strip("\"'") for a in val.split() if a.strip("\"'")]
            current_opts = {}
        elif current_aliases and key in ("hostname", "user", "port", "identityfile"):
            current_opts.setdefault(key, val.strip("\"'"))

    _flush()
    return out


class SshHostStore:
    """管理 SSH 主機清單（~/.ssh/config + ssh_hosts.local.json）與遠端 CLI 握手偵測快取。"""

    def __init__(
        self,
        custom_path: str,
        cache_path: str,
        ssh_config_path: str = "",
    ) -> None:
        self.custom_path = custom_path
        self.cache_path = cache_path
        self.ssh_config_path = ssh_config_path or os.path.expanduser("~/.ssh/config")
        self._lock = threading.RLock()
        self._custom: List[Dict[str, Any]] = []
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        with self._lock:
            self._custom = self._read_json(self.custom_path, [])
            if not isinstance(self._custom, list):
                self._custom = []
            self._cache = self._read_json(self.cache_path, {})
            if not isinstance(self._cache, dict):
                self._cache = {}

    @staticmethod
    def _read_json(path: str, default: Any) -> Any:
        if not path or not os.path.isfile(path):
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    @staticmethod
    def _write_json(path: str, data: Any) -> None:
        if not path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def list_hosts(self) -> List[Dict[str, Any]]:
        """回傳合併後的 SSH 主機清單（含快取的 CLI 偵測摘要）。"""
        with self._lock:
            cfg_hosts = parse_ssh_config(self.ssh_config_path)
            seen = set()
            merged: List[Dict[str, Any]] = []
            for h in cfg_hosts + list(self._custom):
                if not isinstance(h, dict):
                    continue
                hid = str(h.get("id") or "").strip()
                if not hid or hid in seen or validate_ssh_target(hid) != "":
                    continue
                seen.add(hid)
                item = dict(h)
                item.setdefault("target", hid)
                cached = self._cache.get(hid)
                if isinstance(cached, dict):
                    item["probe"] = {
                        "reachable": bool(cached.get("reachable")),
                        "available_seats": list(cached.get("available_seats") or []),
                        "clis": dict(cached.get("clis") or {}),
                        "probed_at": str(cached.get("probed_at") or ""),
                        "error": str(cached.get("error") or ""),
                    }
                merged.append(item)
            return merged

    def add_host(self, target: str, label: str = "") -> Dict[str, Any]:
        clean = (target or "").strip()
        err = validate_ssh_target(clean)
        if err:
            raise SshError(err)
        host_part, port = split_ssh_target(clean)
        user = ""
        hostname = host_part
        if "@" in host_part:
            user, hostname = host_part.split("@", 1)
        entry = {
            "id": clean,
            "target": clean,
            "label": (label or "").strip() or clean,
            "source": "custom",
            "hostname": hostname,
            "user": user,
            "port": port,
        }
        with self._lock:
            self._custom = [h for h in self._custom if isinstance(h, dict) and h.get("id") != clean]
            self._custom.append(entry)
            self._write_json(self.custom_path, self._custom)
        return entry

    def remove_host(self, target_id: str) -> bool:
        clean = (target_id or "").strip()
        with self._lock:
            before = len(self._custom)
            self._custom = [h for h in self._custom if isinstance(h, dict) and h.get("id") != clean]
            changed = len(self._custom) != before
            if changed:
                self._write_json(self.custom_path, self._custom)
            return changed

    def get_cached_probe(self, ssh_host: str) -> Optional[Dict[str, Any]]:
        key = (ssh_host or "").strip() or "__local__"
        with self._lock:
            c = self._cache.get(key)
            return dict(c) if isinstance(c, dict) else None

    def save_probe(self, ssh_host: str, result: Dict[str, Any]) -> None:
        key = (ssh_host or "").strip() or "__local__"
        with self._lock:
            self._cache[key] = dict(result)
            try:
                self._write_json(self.cache_path, self._cache)
            except OSError:
                pass

    set_cached_probe = save_probe

    def all_cached(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._cache.items() if isinstance(v, dict)}


# ---------------------------------------------------------------- 遠端單檔 Python 探針
# 透過 `ssh -T <host> python3 -` 將此腳本與 JSON 參數送至遠端執行，零外部套件依賴。
REMOTE_PROBE_SCRIPT = r'''
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

IGNORE_SUBDIRS = {
    ".git", ".ai-team-worktrees", ".ai-team-mutation", "node_modules",
    "__pycache__", ".venv", "venv", ".next", "dist", "build", ".pytest_cache"
}

CLAUDE_DEFAULT_MODELS = [
    {"id": "opus", "name": "opus（Claude 最新旗艦別名）", "description": "高階深度推理 Claude 模型"},
    {"id": "sonnet", "name": "sonnet（Claude 最新標準別名）", "description": "標準平衡型 Claude 模型"},
    {"id": "haiku", "name": "haiku（Claude 最新輕量別名）", "description": "快速經濟型 Claude 模型"},
    {"id": "claude-opus-5-5", "name": "claude-opus-5-5", "description": "Claude Opus 5.5"},
    {"id": "claude-sonnet-5-5", "name": "claude-sonnet-5-5", "description": "Claude Sonnet 5.5"},
    {"id": "claude-haiku-5-5", "name": "claude-haiku-5-5", "description": "Claude Haiku 5.5"},
    {"id": "claude-sonnet-4-6", "name": "claude-sonnet-4-6", "description": "Claude Sonnet 4.6"},
    {"id": "claude-sonnet-4-5", "name": "claude-sonnet-4-5", "description": "Claude Sonnet 4.5"},
]


def _expanded_path(extra_paths, isolate=False):
    parts = [p for p in (extra_paths or []) if p]
    if isolate:
        return os.pathsep.join(parts)
    home = os.path.expanduser("~")
    common = [
        os.path.join(home, ".local", "bin"),
        os.path.join(home, ".cargo", "bin"),
        os.path.join(home, "bin"),
        os.path.join(home, ".npm-global", "bin"),
        os.path.join(home, ".agy", "bin"),
        os.path.join(home, ".kiro", "bin"),
        "/usr/local/bin",
        "/opt/homebrew/bin",
        "/usr/bin",
        "/bin",
    ]
    for nvm_bin in sorted(glob.glob(os.path.join(home, ".nvm", "versions", "node", "*", "bin")), reverse=True):
        common.insert(0, nvm_bin)
    if os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        common.extend([
            os.path.join(local_app, "Kiro-Cli"),
            os.path.join(local_app, "agy", "bin"),
            os.path.join(home, ".local", "bin"),
        ])
    env_path = os.environ.get("PATH", "")
    for p in common + env_path.split(os.pathsep):
        if p and p not in parts:
            parts.append(p)
    return os.pathsep.join(parts)


def _find_exe(name, search_path):
    p = shutil.which(name, path=search_path)
    if p:
        return os.path.abspath(p)
    return ""


def _kill_proc(p):
    if p.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True, timeout=5)
        else:
            import signal
            os.killpg(p.pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        p.kill()
    except Exception:
        pass


def _probe_acp(exe_path, seat, env, timeout=10.0):
    args = [exe_path] + (["acp", "--agent-engine", "v3", "--auth-method", "cli"] if seat == "kiro" else ["acp"])
    cwd = tempfile.mkdtemp(prefix=f"probe-{seat}-")
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kw["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **kw,
        )
    except OSError as e:
        shutil.rmtree(cwd, ignore_errors=True)
        return {"callable": False, "error": str(e)}

    state = {
        "init_ok": False,
        "session_id": "",
        "error": "",
        "models": [],
        "current": "",
    }
    done_ev = threading.Event()
    models_ev = threading.Event()

    def _parse_config_options(options):
        if not isinstance(options, list):
            return
        for o in options:
            if not isinstance(o, dict):
                continue
            if o.get("category") != "model" and o.get("id") != "model":
                continue
            vals = []
            for v in o.get("options") or []:
                if isinstance(v, dict) and isinstance(v.get("value"), str) and v["value"].strip():
                    vals.append({
                        "id": v["value"].strip()[:120],
                        "name": str(v.get("name") or v["value"]).strip()[:120],
                        "description": str(v.get("description") or "")[:300],
                    })
            if vals:
                state["models"] = vals[:60]
                if isinstance(o.get("currentValue"), str) and o["currentValue"].strip():
                    state["current"] = o["currentValue"].strip()[:120]
                models_ev.set()
                return

    def _reader():
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                rid = msg.get("id")
                method = msg.get("method")
                if rid == 1 and method is None:
                    if msg.get("error"):
                        state["error"] = str(msg["error"])
                        done_ev.set()
                        return
                    state["init_ok"] = True
                    req2 = {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "session/new",
                        "params": {"cwd": cwd, "mcpServers": []},
                    }
                    try:
                        proc.stdin.write(json.dumps(req2) + "\n")
                        proc.stdin.flush()
                    except Exception as e:
                        state["error"] = str(e)
                        done_ev.set()
                        return
                elif rid == 2 and method is None:
                    if msg.get("error"):
                        state["error"] = str(msg["error"])
                        done_ev.set()
                        return
                    res = msg.get("result") or {}
                    sid = res.get("sessionId") if isinstance(res, dict) else ""
                    if not sid:
                        state["error"] = "missing sessionId"
                        done_ev.set()
                        return
                    state["session_id"] = str(sid)
                    models = res.get("models") if isinstance(res, dict) else None
                    if isinstance(models, dict):
                        if isinstance(models.get("currentModelId"), str):
                            state["current"] = models["currentModelId"].strip()[:120]
                        if isinstance(models.get("availableModels"), list):
                            avail = []
                            for m in models["availableModels"]:
                                if isinstance(m, dict) and isinstance(m.get("modelId"), str) and m["modelId"].strip():
                                    avail.append({
                                        "id": m["modelId"].strip()[:120],
                                        "name": str(m.get("name") or m["modelId"]).strip()[:120],
                                        "description": str(m.get("description") or "")[:300],
                                    })
                            if avail:
                                state["models"] = avail[:60]
                                models_ev.set()
                    if isinstance(res.get("configOptions"), list):
                        _parse_config_options(res["configOptions"])
                    done_ev.set()
                elif method == "session/update":
                    params = msg.get("params") or {}
                    upd = params.get("update") if isinstance(params, dict) else None
                    if isinstance(upd, dict) and upd.get("sessionUpdate") == "config_option_update":
                        _parse_config_options(upd.get("configOptions"))
        except Exception as e:
            state["error"] = str(e)
        finally:
            done_ev.set()

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    req1 = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
                "auth": {"terminal": True},
                "_meta": {"terminal-auth": True},
            },
            "clientInfo": {"name": "buttonAuto-probe", "title": "CLI Probe", "version": "1.0"},
        },
    }
    try:
        proc.stdin.write(json.dumps(req1) + "\n")
        proc.stdin.flush()
    except Exception as e:
        _kill_proc(proc)
        shutil.rmtree(cwd, ignore_errors=True)
        return {"callable": False, "error": str(e)}

    done_ev.wait(timeout)
    if state["session_id"] and not state["models"]:
        models_ev.wait(1.2)
    _kill_proc(proc)
    shutil.rmtree(cwd, ignore_errors=True)

    if not state["session_id"] or state["error"]:
        return {"callable": False, "error": state["error"] or "ACP 握手未取得 sessionId"}
    return {
        "callable": True,
        "path": exe_path,
        "models": state["models"],
        "current": state["current"],
    }


def _probe_claude(exe_path, env, timeout=10.0):
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kw["start_new_session"] = True

    # 1. 先驗證執行檔可正常啟動 (--version)
    try:
        p_ver = subprocess.run(
            [exe_path, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=min(timeout, 6.0),
            env=env,
            **kw,
        )
        bin_ok = p_ver.returncode == 0
    except Exception as e:
        return {"callable": False, "bin_ok": False, "error": str(e)}
    if not bin_ok:
        return {"callable": False, "bin_ok": False, "error": "claude --version 失敗"}

    # 2. 嚴格握手：啟動 headless stream-json 驗證能初始化且未報未登入錯誤
    cwd = tempfile.mkdtemp(prefix="probe-claude-")
    cmd = [
        exe_path,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "plan",
        "--max-turns",
        "1",
    ]
    init_ok = False
    is_err = False
    err_msg = ""
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **kw,
        )
        try:
            proc.stdin.write("OK\n")
            proc.stdin.close()
        except Exception:
            pass

        def _read_lines():
            nonlocal init_ok, is_err, err_msg
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") == "system" and msg.get("subtype") == "init" and msg.get("session_id"):
                    init_ok = True
                elif msg.get("type") == "result":
                    if msg.get("is_error"):
                        is_err = True
                        err_msg = str(msg.get("result") or "claude error")
                    else:
                        init_ok = True
                    break

        t = threading.Thread(target=_read_lines, daemon=True)
        t.start()
        t.join(min(timeout, 6.0))
        _kill_proc(proc)
    except Exception as e:
        err_msg = str(e)
    finally:
        shutil.rmtree(cwd, ignore_errors=True)

    if not init_ok or is_err:
        return {"callable": False, "bin_ok": True, "error": err_msg or "claude 握手失敗或未登入"}

    models = list(CLAUDE_DEFAULT_MODELS)
    current = "sonnet"
    spath = os.path.expanduser("~/.claude/settings.json")
    if os.path.isfile(spath):
        try:
            with open(spath, encoding="utf-8") as f:
                sdata = json.load(f)
            if isinstance(sdata, dict) and isinstance(sdata.get("model"), str) and sdata["model"].strip():
                current = sdata["model"].strip()
                if current not in {m["id"] for m in models}:
                    models.insert(0, {"id": current, "name": current, "description": "settings.json 預設模型"})
        except Exception:
            pass
    return {
        "callable": True,
        "bin_ok": True,
        "path": exe_path,
        "models": models,
        "current": current,
    }


def do_probe_clis(req):
    search_path = _expanded_path(req.get("extra_search_paths"), bool(req.get("isolate_path")))
    env_path = search_path
    if os.name == "nt":
        sys_root = os.environ.get("SystemRoot", r"C:\Windows")
        sys32 = os.path.join(sys_root, "System32")
        if sys32 not in env_path:
            env_path = env_path + os.pathsep + sys32
    env = dict(os.environ, PATH=env_path, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    specs = [
        ("kiro", "Kiro CLI", "kiro-cli", "acp"),
        ("agy", "Antigravity CLI", "agy", "acp"),
        ("claude", "Claude CLI", "claude", "headless"),
    ]
    results = {}
    threads = []

    def _worker(seat, label, exe_name, proto):
        exe = _find_exe(exe_name, search_path)
        if not exe:
            results[seat] = {"callable": False, "bin_ok": False, "error": f"找不到 {exe_name}"}
            return
        if proto == "acp":
            r = _probe_acp(exe, seat, env)
        else:
            r = _probe_claude(exe, env)
        r["label"] = label
        r["protocol"] = proto
        results[seat] = r

    for seat, label, exe_name, proto in specs:
        t = threading.Thread(target=_worker, args=(seat, label, exe_name, proto))
        t.start()
        threads.append(t)
    for t in threads:
        t.join(14.0)

    callable_clis = {}
    for seat, _, _, _ in specs:
        r = results.get(seat) or {}
        if r.get("callable"):
            callable_clis[seat] = {
                "name": seat,
                "label": r["label"],
                "path": r["path"],
                "protocol": r["protocol"],
                "callable": True,
                "models": r.get("models") or [],
                "current": r.get("current") or "",
            }
    claude_bin_ok = bool((results.get("claude") or {}).get("bin_ok"))
    return {
        "ok": True,
        "clis": callable_clis,
        "claude_bin_ok": claude_bin_ok,
    }


def do_dirs(req):
    raw = str(req.get("path") or "").strip()
    cur = os.path.abspath(os.path.expanduser(raw)) if raw else os.path.expanduser("~")
    if not os.path.isdir(cur):
        cur = os.path.expanduser("~")
    parent = os.path.dirname(cur.rstrip("/\\"))
    if parent == cur:
        parent = ""
    drives = []
    if os.name == "nt":
        for d in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            dp = f"{d}:\\"
            if os.path.isdir(dp):
                drives.append(dp)
    else:
        for d in ("/", os.path.expanduser("~")):
            if d not in drives and os.path.isdir(d):
                drives.append(d)
    dirs = []
    try:
        for entry in sorted(os.scandir(cur), key=lambda e: e.name.lower()):
            if entry.is_dir(follow_symlinks=False) and not entry.name.startswith(".") and entry.name not in IGNORE_SUBDIRS:
                dirs.append({"name": entry.name, "path": entry.path})
    except OSError:
        pass
    return {"ok": True, "path": cur, "parent": parent, "dirs": dirs[:120], "drives": drives}


def _detect_checks(path):
    checks = []
    if os.path.isfile(os.path.join(path, "pytest.ini")) or os.path.isfile(os.path.join(path, "pyproject.toml")) or os.path.isdir(os.path.join(path, "tests")):
        checks.append("pytest")
    pkg = os.path.join(path, "package.json")
    if os.path.isfile(pkg):
        try:
            with open(pkg, encoding="utf-8") as f:
                pdata = json.load(f)
            scripts = (pdata.get("scripts") or {}) if isinstance(pdata, dict) else {}
            if "test" in scripts:
                checks.append("npm test")
            if "lint" in scripts:
                checks.append("npm run lint")
        except Exception:
            pass
    if os.path.isfile(os.path.join(path, "Cargo.toml")):
        checks.append("cargo test")
    if os.path.isfile(os.path.join(path, "go.mod")):
        checks.append("go test ./...")
    return checks


def do_scan(req):
    raw = str(req.get("path") or "").strip()
    full = os.path.abspath(os.path.expanduser(raw)) if raw else ""
    name = os.path.basename(os.path.normpath(full)) if full else ""
    out = {
        "ok": True,
        "exists": False,
        "is_git": False,
        "path": full,
        "name": name,
        "problem": "",
        "branch": "",
        "dirty": [],
        "subdirs": [],
        "checks": [],
        "progress": {"file": "", "task": "", "excerpt": ""},
        "docs": [],
    }
    if not full or not os.path.isdir(full):
        out["problem"] = f"找不到遠端資料夾：{raw or '（空白）'}"
        return out
    out["exists"] = True
    try:
        for entry in os.listdir(full):
            if entry.startswith(".") or entry in IGNORE_SUBDIRS:
                continue
            if os.path.isdir(os.path.join(full, entry)):
                out["subdirs"].append(entry)
        out["subdirs"].sort(key=lambda s: s.lower())
    except OSError:
        pass
    out["checks"] = _detect_checks(full)
    p_top = subprocess.run(["git", "-C", full, "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True)
    if p_top.returncode != 0:
        out["problem"] = "不是 git repo，請先 git init 並提交一次"
    else:
        out["is_git"] = True
        p_head = subprocess.run(["git", "-C", full, "rev-parse", "--verify", "-q", "HEAD"], capture_output=True, text=True)
        if p_head.returncode != 0:
            out["problem"] = "repo 還沒有任何 commit"
        p_br = subprocess.run(["git", "-C", full, "symbolic-ref", "--short", "-q", "HEAD"], capture_output=True, text=True)
        out["branch"] = p_br.stdout.strip() if p_br.returncode == 0 else ""
        p_st = subprocess.run(["git", "-C", full, "status", "--porcelain", "--untracked-files=normal"], capture_output=True, text=True)
        if p_st.returncode == 0:
            for line in p_st.stdout.splitlines()[:20]:
                fn = line[3:].strip()
                if fn:
                    out["dirty"].append(fn.strip('"'))
    for rel_prog in ("progress.md", "docs/progress.md"):
        p_prog = os.path.join(full, rel_prog)
        if os.path.isfile(p_prog):
            try:
                with open(p_prog, encoding="utf-8", errors="replace") as f:
                    txt = f.read(8000)
                first_task = ""
                for ln in txt.splitlines():
                    s_ln = ln.strip()
                    if s_ln.startswith("- [ ]"):
                        first_task = s_ln[5:].strip()[:240]
                        break
                out["progress"] = {"file": rel_prog, "task": first_task, "excerpt": txt[:600]}
                break
            except Exception:
                pass
    plans_dir = os.path.join(full, "docs", "plans")
    if os.path.isdir(plans_dir):
        try:
            for fn in sorted(os.listdir(plans_dir), reverse=True)[:15]:
                if not fn.endswith(".md"):
                    continue
                fp = os.path.join(plans_dir, fn)
                if not os.path.isfile(fp):
                    continue
                title = fn
                try:
                    with open(fp, encoding="utf-8", errors="replace") as f:
                        for ln in f.read(2000).splitlines():
                            if ln.lstrip().startswith("#"):
                                title = ln.lstrip("#").strip()[:160] or fn
                                break
                except Exception:
                    pass
                out["docs"].append({"relpath": f"docs/plans/{fn}", "title": title, "modified": ""})
        except Exception:
            pass
    return out


def do_exec(req):
    import signal as _sig
    cwd = os.path.expanduser(str(req.get("cwd") or "."))
    cmd = req.get("cmd")
    shell = bool(req.get("shell"))
    timeout = float(req.get("timeout") or 60.0)
    extra_env = req.get("env") or {}
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", GIT_TERMINAL_PROMPT="0")
    home = os.path.expanduser("~")
    extra_path_dirs = [
        os.path.join(home, ".cargo", "bin"),
        os.path.join(home, ".local", "bin"),
        os.path.join(home, ".npm-global", "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ]
    cur_path = env.get("PATH", "")
    cur_parts = cur_path.split(os.pathsep) if cur_path else []
    prepend = [d for d in extra_path_dirs if os.path.isdir(d) and d not in cur_parts]
    if prepend:
        env["PATH"] = os.pathsep.join(prepend + cur_parts)
    for k, v in extra_env.items():
        if v is None:
            env.pop(k, None)
        else:
            env[str(k)] = str(v)
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kw["start_new_session"] = True
    try:
        p = subprocess.Popen(
            cmd,
            cwd=cwd,
            shell=shell,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            **kw,
        )
        try:
            out_b, err_b = p.communicate(timeout=timeout)
            return {
                "ok": True,
                "returncode": p.returncode,
                "stdout": (out_b or b"").decode("utf-8", errors="replace"),
                "stderr": (err_b or b"").decode("utf-8", errors="replace"),
                "timed_out": False,
            }
        except subprocess.TimeoutExpired:
            try:
                if os.name != "nt":
                    os.killpg(p.pid, _sig.SIGKILL)
                else:
                    p.kill()
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
            try:
                out_b, err_b = p.communicate(timeout=5)
            except Exception:
                out_b, err_b = b"", b""
            return {
                "ok": True,
                "returncode": -1,
                "stdout": (out_b or b"").decode("utf-8", errors="replace"),
                "stderr": (err_b or b"").decode("utf-8", errors="replace"),
                "timed_out": True,
            }
    except FileNotFoundError as e:
        return {"ok": False, "error_kind": "FileNotFoundError", "error": str(e)}
    except OSError as e:
        return {"ok": False, "error_kind": "OSError", "error": str(e)}


def do_fs(req):
    action = str(req.get("action") or "stat")
    target = os.path.expanduser(str(req.get("path") or ""))
    if action == "stat":
        exists = os.path.exists(target)
        isdir = os.path.isdir(target)
        isfile = os.path.isfile(target)
        real = os.path.realpath(target) if exists else os.path.abspath(target)
        return {
            "ok": True,
            "exists": exists,
            "isdir": isdir,
            "isfile": isfile,
            "abspath": os.path.abspath(target),
            "realpath": real,
        }
    if action == "makedirs":
        os.makedirs(target, exist_ok=True)
        return {"ok": True, "abspath": os.path.abspath(target)}
    if action == "rmtree":
        shutil.rmtree(target, ignore_errors=True)
        return {"ok": True}
    if action == "rmdir":
        try:
            os.rmdir(target)
        except OSError:
            pass
        return {"ok": True}
    if action == "ensure_exclude":
        pattern = str(req.get("pattern") or ".ai-team-worktrees/")
        info_dir = os.path.join(target, "info")
        os.makedirs(info_dir, exist_ok=True)
        excl = os.path.join(info_dir, "exclude")
        existing = ""
        if os.path.isfile(excl):
            with open(excl, "r", encoding="utf-8", errors="replace") as f:
                existing = f.read()
        lines = [ln.strip() for ln in existing.splitlines()]
        if pattern not in lines and pattern.rstrip("/") not in lines:
            with open(excl, "a", encoding="utf-8") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write(pattern + "\n")
        return {"ok": True}
    if action == "purge_dir":
        rel = str(req.get("rel") or "").replace("\\", "/").strip("/")
        full = os.path.join(target, rel.replace("/", os.sep)) if rel else target
        if not os.path.exists(full):
            return {"ok": True, "exists": False, "files": {}}
        files = {}
        if os.path.isfile(full):
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    files[os.path.basename(full)] = f.read()
            except OSError:
                pass
            try:
                os.remove(full)
            except OSError:
                pass
        else:
            for dp, _dns, fns in os.walk(full):
                rdir = os.path.relpath(dp, full).replace("\\", "/")
                for fn in fns:
                    fp = os.path.join(dp, fn)
                    rk = fn if rdir == "." else f"{rdir}/{fn}"
                    try:
                        with open(fp, "r", encoding="utf-8", errors="replace") as f:
                            files[rk] = f.read()
                    except OSError:
                        pass
            shutil.rmtree(full, ignore_errors=True)
        parent = os.path.dirname(rel.replace("/", os.sep))
        while parent:
            pfull = os.path.join(target, parent)
            try:
                os.rmdir(pfull)
            except OSError:
                break
            parent = os.path.dirname(parent)
        return {"ok": True, "exists": True, "files": files}
    if action == "list_plans":
        roots = [target]
        ref = os.path.expanduser(str(req.get("reference_root") or ""))
        if ref and os.path.normcase(ref) != os.path.normcase(target):
            roots.append(ref)
        for sub in req.get("target_subdirs") or []:
            s = str(sub or "").strip().strip("/\\")
            if s:
                roots.append(os.path.join(target, s))
        sub_single = str(req.get("target_subdir") or "").strip()
        if sub_single:
            for part in re.split(r"[,;]+", sub_single):
                s = part.strip().strip("/\\")
                if s:
                    roots.append(os.path.join(target, s))
        seen = set()
        plans_out = []
        for rpath in roots:
            pdir = os.path.join(rpath, "docs", "plans")
            if not os.path.isdir(pdir):
                continue
            for dp, _dns, fns in os.walk(pdir):
                for fn in sorted(fns):
                    if not fn.lower().endswith(".md"):
                        continue
                    full = os.path.join(dp, fn)
                    norm = os.path.normcase(os.path.realpath(full))
                    if norm in seen or not os.path.isfile(full):
                        continue
                    seen.add(norm)
                    try:
                        with open(full, "r", encoding="utf-8", errors="replace") as f:
                            txt = f.read(65536)
                        st = os.stat(full)
                        rel = os.path.relpath(full, target).replace("\\", "/")
                        plans_out.append({"filename": fn, "rel_path": rel, "content": txt, "mtime": st.st_mtime})
                    except OSError:
                        pass
        return {"ok": True, "plans": plans_out}
    if action == "write_plan":
        filename = os.path.basename(str(req.get("filename") or "plan.md"))
        content = str(req.get("content") or "")
        plans_dir = os.path.realpath(os.path.join(target, "docs", "plans"))
        os.makedirs(plans_dir, exist_ok=True)
        stem, ext = os.path.splitext(filename)
        if not ext:
            ext = ".md"
        cand = os.path.realpath(os.path.join(plans_dir, f"{stem}{ext}"))
        idx = 2
        while os.path.exists(cand):
            cand = os.path.realpath(os.path.join(plans_dir, f"{stem}-{idx}{ext}"))
            idx += 1
        with open(cand, "w", encoding="utf-8", newline="\n") as f:
            f.write(content + ("\n" if content and not content.endswith("\n") else ""))
        return {"ok": True, "filename": os.path.basename(cand), "path": cand}
    if action == "install_pubkey":
        pubkey = str(req.get("pubkey") or "").strip()
        if not pubkey:
            return {"ok": False, "error": "empty pubkey"}
        ssh_dir = os.path.expanduser("~/.ssh")
        os.makedirs(ssh_dir, exist_ok=True)
        try:
            os.chmod(ssh_dir, 0o700)
        except OSError:
            pass
        auth_file = os.path.join(ssh_dir, "authorized_keys")
        existing = ""
        if os.path.isfile(auth_file):
            try:
                with open(auth_file, "r", encoding="utf-8", errors="replace") as f:
                    existing = f.read()
            except OSError:
                existing = ""
        lines = [ln.strip() for ln in existing.splitlines() if ln.strip()]
        if pubkey not in lines:
            with open(auth_file, "a", encoding="utf-8") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write(pubkey + "\n")
        try:
            os.chmod(auth_file, 0o600)
        except OSError:
            pass
        return {"ok": True, "installed": True}
    return {"ok": False, "error": f"unknown fs action: {action}"}


def _dispatch(req):
    op = req.get("op")
    if op == "dirs":
        return do_dirs(req)
    if op == "scan":
        return do_scan(req)
    if op == "exec":
        return do_exec(req)
    if op == "fs":
        return do_fs(req)
    return do_probe_clis(req)


def _serve_rpc():
    sys.stdout.buffer.write(b"__AEA_RPC_READY__\n")
    sys.stdout.buffer.flush()
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            break
        s = line.decode("utf-8", errors="replace").strip()
        if not s:
            continue
        try:
            req = json.loads(s)
            res = _dispatch(req)
        except Exception as e:
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        out = "__AEA_RPC_RES__:" + json.dumps(res, ensure_ascii=False) + "\n"
        sys.stdout.buffer.write(out.encode("utf-8"))
        sys.stdout.buffer.flush()


def main():
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    req = json.loads(raw) if raw.strip() else {}
    res = _dispatch(req)
    sys.stdout.buffer.write((json.dumps(res, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
elif __name__ == "__aea_rpc__":
    _serve_rpc()
'''


ProbeRunner = Callable[[str, Dict[str, Any], float], Dict[str, Any]]

_RPC_READY_TOKEN = "__AEA_RPC_READY__"
_RPC_RES_PREFIX = "__AEA_RPC_RES__:"
_RPC_PY_BOOTSTRAP = (
    "import sys; s=sys.stdin.buffer.readline(); n=int(s); "
    "code=sys.stdin.buffer.read(n).decode('utf-8'); "
    "exec(compile(code,'<aea_rpc>','exec'), {'__name__':'__aea_rpc__'})"
)
_RPC_BOOTSTRAP_CMD = (
    "P=$(command -v python3 || command -v python); "
    '[ -n "$P" ] && exec "$P" -u -c ' + shlex.quote(_RPC_PY_BOOTSTRAP) + " || exit 127"
)
_SSH_CHANNELS_LOCK = threading.RLock()
_SSH_CHANNELS: Dict[str, "_PersistentSshChannel"] = {}


def _is_transient_ssh_error(text: str) -> bool:
    """判斷是否為可退避重試的暫時性 SSH 連線錯誤（如 banner exchange 逾時、瞬斷）。"""
    low = (text or "").lower()
    if any(t in low for t in ("permission denied", "authentication failed", "passphrase")):
        return False
    return any(
        t in low
        for t in (
            "banner exchange",
            "timed out",
            "connection closed",
            "connection reset",
            "broken pipe",
            "unexpected eof",
            "通道已中斷",
        )
    )


class _PersistentSshChannel:
    """單一 ssh_host 的常駐 JSON-RPC 連線通道：以 1 條 ssh -T 複用所有 fs / exec / dirs / scan 呼叫，徹底避免 MaxStartups 連線風暴。"""

    def __init__(self, ssh_host: str) -> None:
        self.ssh_host = ssh_host
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._out_q: "queue.Queue[Optional[str]]" = queue.Queue()
        self._stderr_lines: List[str] = []
        self._last_used: float = 0.0

    def _close_locked(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        while not self._out_q.empty():
            try:
                self._out_q.get_nowait()
            except queue.Empty:
                break

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _stderr_tail(self) -> str:
        return "\n".join(self._stderr_lines[-8:]).strip()[-400:]

    def _ensure_started_locked(self, connect_timeout: float) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        self._close_locked()
        self._stderr_lines = []
        self._out_q = queue.Queue()

        kw: Dict[str, Any] = {}
        if os.name == "nt":
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        cmd = build_ssh_cmd(self.ssh_host, ["sh", "-c", shlex.quote(_RPC_BOOTSTRAP_CMD)], connect_timeout=15)
        run_env = with_ssh_askpass_env(self.ssh_host)
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=run_env,
                **kw,
            )
        except OSError as e:
            raise SshError(f"無法執行 SSH：{e}")

        self._proc = proc
        out_q = self._out_q
        stderr_buf = self._stderr_lines

        def _read_stdout() -> None:
            try:
                assert proc.stdout is not None
                for raw in proc.stdout:
                    out_q.put(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
            except Exception:
                pass
            finally:
                out_q.put(None)

        def _read_stderr() -> None:
            try:
                assert proc.stderr is not None
                for raw in proc.stderr:
                    ln = raw.decode("utf-8", errors="replace").strip()
                    if ln:
                        stderr_buf.append(ln)
                        if len(stderr_buf) > 20:
                            del stderr_buf[:-20]
            except Exception:
                pass

        threading.Thread(target=_read_stdout, daemon=True, name=f"ssh-out-{self.ssh_host[:16]}").start()
        threading.Thread(target=_read_stderr, daemon=True, name=f"ssh-err-{self.ssh_host[:16]}").start()

        script_bytes = REMOTE_PROBE_SCRIPT.encode("utf-8")
        header = f"{len(script_bytes)}\n".encode("ascii")
        try:
            assert proc.stdin is not None
            proc.stdin.write(header)
            proc.stdin.write(script_bytes)
            proc.stdin.flush()
        except OSError as e:
            self._close_locked()
            raise SshError(f"SSH 通道啟動寫入失敗：{self._stderr_tail() or e}")

        deadline = time.monotonic() + max(10.0, min(connect_timeout, 30.0))
        while True:
            rem = deadline - time.monotonic()
            if rem <= 0:
                err_s = self._stderr_tail()
                self._close_locked()
                raise SshError(f"SSH 連線或探測逾時（{connect_timeout:g} 秒）：{err_s or self.ssh_host}")
            try:
                line = out_q.get(timeout=min(rem, 0.5))
            except queue.Empty:
                if proc.poll() is not None:
                    # 短暫等待 stderr 執行緒讀完錯誤訊息
                    time.sleep(0.05)
                    err_s = self._stderr_tail()
                    rc = proc.returncode
                    self._close_locked()
                    raise SshError(f"SSH 連線失敗（exit {rc}）：{err_s or '無法連線或遠端未安裝 Python'}")
                continue
            if line is None:
                time.sleep(0.05)
                err_s = self._stderr_tail()
                rc = proc.poll()
                self._close_locked()
                raise SshError(f"SSH 連線失敗（exit {rc if rc is not None else 255}）：{err_s or '通道已中斷'}")
            if line.strip() == _RPC_READY_TOKEN:
                self._last_used = time.monotonic()
                return

    def call(self, payload: Dict[str, Any], timeout: float = 25.0) -> Dict[str, Any]:
        last_err: Optional[SshError] = None
        for attempt in range(3):
            with self._lock:
                try:
                    self._ensure_started_locked(connect_timeout=max(timeout, 20.0))
                    proc = self._proc
                    assert proc is not None and proc.stdin is not None
                    req_line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
                    proc.stdin.write(req_line)
                    proc.stdin.flush()

                    deadline = time.monotonic() + max(1.0, timeout)
                    while True:
                        rem = deadline - time.monotonic()
                        if rem <= 0:
                            self._close_locked()
                            raise SshError(f"SSH 遠端操作逾時（{timeout:g} 秒）：{self.ssh_host}")
                        try:
                            line = self._out_q.get(timeout=min(rem, 0.5))
                        except queue.Empty:
                            if proc.poll() is not None:
                                time.sleep(0.05)
                                err_s = self._stderr_tail()
                                rc = proc.returncode
                                self._close_locked()
                                raise SshError(f"SSH 連線中斷（exit {rc}）：{err_s or '通道已中斷'}")
                            continue
                        if line is None:
                            time.sleep(0.05)
                            err_s = self._stderr_tail()
                            rc = proc.poll()
                            self._close_locked()
                            raise SshError(f"SSH 連線中斷（exit {rc if rc is not None else 255}）：{err_s or '通道已中斷'}")
                        if line.startswith(_RPC_RES_PREFIX):
                            self._last_used = time.monotonic()
                            try:
                                return json.loads(line[len(_RPC_RES_PREFIX):])
                            except ValueError as e:
                                raise SshError(f"解析遠端探測結果失敗：{e}")
                except OSError as e:
                    err_s = self._stderr_tail()
                    self._close_locked()
                    last_err = SshError(f"SSH 通道讀寫失敗：{err_s or e}")
                except SshError as e:
                    last_err = e

            # 僅針對暫時性連線錯誤（banner exchange 逾時、瞬斷）進行退避重試；驗證錯誤立即返回
            if last_err is not None and attempt < 2 and _is_transient_ssh_error(str(last_err)):
                backoff = 1.0 if attempt == 0 else 2.5
                log.info("SSH 暫時性連線失敗（%s），%.1f 秒後重試（第 %d 次）：%s", self.ssh_host, backoff, attempt + 1, last_err)
                time.sleep(backoff)
                continue
            assert last_err is not None
            raise last_err
        assert last_err is not None
        raise last_err


def _get_ssh_channel(ssh_host: str) -> _PersistentSshChannel:
    key = ssh_host.strip()
    with _SSH_CHANNELS_LOCK:
        ch = _SSH_CHANNELS.get(key)
        if ch is None:
            ch = _PersistentSshChannel(key)
            _SSH_CHANNELS[key] = ch
        return ch


def close_ssh_channels(ssh_host: str = "") -> None:
    """關閉指定（或全部）主機的常駐 SSH JSON-RPC 通道。"""
    key = (ssh_host or "").strip()
    with _SSH_CHANNELS_LOCK:
        if key:
            ch = _SSH_CHANNELS.pop(key, None)
            if ch is not None:
                ch.close()
        else:
            chs = list(_SSH_CHANNELS.values())
            _SSH_CHANNELS.clear()
            for ch in chs:
                ch.close()


atexit.register(close_ssh_channels)


def run_remote_probe(ssh_host: str, payload: Dict[str, Any], timeout: float = 25.0) -> Dict[str, Any]:
    """在指定 ssh_host（空字串代表本機）執行 REMOTE_PROBE_SCRIPT 並回傳解析後的 JSON。
    遠端模式自動複用每主機單一條常駐 SSH JSON-RPC 通道（避免頻繁握手觸發 MaxStartups／banner exchange 逾時）。
    """
    if ssh_host:
        err = validate_ssh_target(ssh_host)
        if err:
            raise SshError(err)

    if ssh_host and os.environ.get("BUTTONAUTO_FAKE_SSH_LOCAL") != "1":
        return _get_ssh_channel(ssh_host).call(payload, timeout=timeout)

    payload_json = json.dumps(payload, ensure_ascii=False)
    full_stdin = (
        REMOTE_PROBE_SCRIPT.replace(
            "raw = sys.stdin.buffer.read().decode(\"utf-8\", errors=\"replace\")\n    req = json.loads(raw) if raw.strip() else {}",
            f"req = json.loads({payload_json!r})",
        )
    ).encode("utf-8")

    kw: Dict[str, Any] = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    cmd = [sys.executable, "-"]
    try:
        p = subprocess.run(
            cmd,
            input=full_stdin,
            capture_output=True,
            timeout=timeout,
            **kw,
        )
    except subprocess.TimeoutExpired:
        raise SshError(f"SSH 連線或探測逾時（{timeout:g} 秒）：{ssh_host or '本機'}")
    except OSError as e:
        raise SshError(f"無法執行 SSH：{e}")

    out_str = p.stdout.decode("utf-8", errors="replace").strip()
    if p.returncode != 0 and not out_str:
        err_str = p.stderr.decode("utf-8", errors="replace").strip()[-400:]
        raise SshError(f"SSH 連線失敗（exit {p.returncode}）：{err_str or '無法連線或遠端未安裝 Python'}")

    lines = [ln.strip() for ln in out_str.splitlines() if ln.strip().startswith("{")]
    if not lines:
        err_str = p.stderr.decode("utf-8", errors="replace").strip()[-400:]
        raise SshError(f"遠端未回傳有效探測結果：{err_str or out_str[:200]}")
    try:
        return json.loads(lines[-1])
    except ValueError as e:
        raise SshError(f"解析遠端探測結果失敗：{e}")


def ensure_local_ssh_pubkey() -> str:
    """取得本機 ~/.ssh/id_ed25519.pub 或 id_rsa.pub；若皆不存在則自動產生一把免通行碼 ed25519 金鑰並回傳公鑰內容。"""
    ssh_dir = os.path.expanduser("~/.ssh")
    for name in ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub"):
        p = os.path.join(ssh_dir, name)
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read().strip()
                if content.startswith("ssh-"):
                    return content
            except OSError:
                pass
    try:
        os.makedirs(ssh_dir, exist_ok=True)
        key_path = os.path.join(ssh_dir, "id_ed25519")
        if not os.path.exists(key_path):
            subprocess.run(
                ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", key_path, "-q"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
            )
        pub_path = key_path + ".pub"
        if os.path.isfile(pub_path):
            with open(pub_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read().strip()
    except Exception:
        pass
    return ""


def install_ssh_public_key(ssh_host: str, *, runner: Optional[ProbeRunner] = None) -> bool:
    """將本機 SSH 公鑰部署至遠端 ~/.ssh/authorized_keys，讓後續連線免密碼。"""
    if not ssh_host:
        return False
    pub = ensure_local_ssh_pubkey()
    if not pub:
        return False
    try:
        res = remote_fs(ssh_host, "install_pubkey", "~", pubkey=pub, runner=runner)
        return bool(res.get("ok"))
    except Exception:
        return False


def probe_host_clis(
    ssh_host: str,
    *,
    has_glm_cfg: bool = False,
    glm_model: str = "glm-5.3",
    runner: Optional[ProbeRunner] = None,
    extra_search_paths: Optional[Sequence[str]] = None,
    isolate_path: bool = False,
    timeout: float = 25.0,
) -> Dict[str, Any]:
    """對指定主機（本機或遠端 SSH）執行嚴格 CLI 握手偵測。不能調用的 CLI 絕不放入 available_seats 與 clis。"""
    run_fn = runner or run_remote_probe
    payload = {
        "op": "probe_clis",
        "extra_search_paths": list(extra_search_paths or []),
        "isolate_path": bool(isolate_path),
    }
    now_str = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        raw = run_fn(ssh_host, payload, timeout)
    except Exception as e:
        err_msg = str(e)
        return {
            "ok": False,
            "ssh_host": ssh_host,
            "reachable": False,
            "auth_required": is_ssh_auth_error(err_msg),
            "available_seats": [],
            "clis": {},
            "probed_at": now_str,
            "error": err_msg,
        }

    callable_clis = {
        k: v
        for k, v in (raw.get("clis") or {}).items()
        if isinstance(v, dict) and v.get("callable")
    }
    avail: List[str] = []
    seats_models: Dict[str, Any] = {}
    for seat in ("claude", "kiro", "agy"):
        if seat in callable_clis:
            avail.append(seat)
            cinfo = callable_clis[seat]
            seats_models[seat] = {
                "available": True,
                "models": list(cinfo.get("models") or []),
                "current": str(cinfo.get("current_model") or cinfo.get("current") or ""),
            }

    claude_bin_ok = bool(raw.get("claude_bin_ok")) or ("claude" in callable_clis)
    if has_glm_cfg and claude_bin_ok:
        avail.extend(["glm", "glm_high"])
        for gseat in ("glm", "glm_high"):
            seats_models[gseat] = {
                "available": True,
                "models": [{"id": glm_model, "name": glm_model, "description": "LiteLLM GLM"}],
                "current": glm_model,
            }

    return {
        "ok": True,
        "ssh_host": ssh_host,
        "reachable": True,
        "hostname": str(raw.get("hostname") or ""),
        "platform": str(raw.get("platform") or ""),
        "home": str(raw.get("home") or ""),
        "available_seats": avail,
        "seats_models": seats_models,
        "clis": callable_clis,
        "claude_bin_ok": claude_bin_ok,
        "glm_model": glm_model,
        "probed_at": now_str,
        "error": "",
    }


def remote_dirs(
    ssh_host: str,
    path: str = "",
    *,
    runner: Optional[ProbeRunner] = None,
    timeout: float = 15.0,
) -> Dict[str, Any]:
    run_fn = runner or run_remote_probe
    return run_fn(ssh_host, {"op": "dirs", "path": path}, timeout)


def remote_scan(
    ssh_host: str,
    path: str,
    *,
    runner: Optional[ProbeRunner] = None,
    timeout: float = 20.0,
) -> Dict[str, Any]:
    run_fn = runner or run_remote_probe
    return run_fn(ssh_host, {"op": "scan", "path": path}, timeout)


def remote_exec(
    ssh_host: str,
    cwd: str,
    cmd: Any,
    *,
    shell: bool = False,
    timeout: float = 60.0,
    env: Optional[Dict[str, Optional[str]]] = None,
    runner: Optional[ProbeRunner] = None,
) -> Dict[str, Any]:
    """在遠端 ssh_host 的 cwd 執行指令（shell=False 時 cmd 為參數 list；shell=True 時為字串）。"""
    run_fn = runner or run_remote_probe
    payload = {
        "op": "exec",
        "cwd": cwd,
        "cmd": cmd,
        "shell": bool(shell),
        "timeout": float(timeout),
        "env": dict(env or {}),
    }
    return run_fn(ssh_host, payload, max(timeout + 15.0, 25.0))


def remote_fs(
    ssh_host: str,
    action: str,
    path: str,
    *,
    runner: Optional[ProbeRunner] = None,
    timeout: float = 15.0,
    **extra: Any,
) -> Dict[str, Any]:
    """在遠端 ssh_host 執行輕量檔案系統操作（stat / makedirs / rmtree / rmdir / ensure_exclude / purge_dir）。"""
    run_fn = runner or run_remote_probe
    payload = {"op": "fs", "action": action, "path": path}
    payload.update(extra)
    return run_fn(ssh_host, payload, timeout)


REMOTE_STDIO_STUB = r'''
import json
import os
import shutil
import signal
import subprocess
import sys
import threading

def _kill(proc):
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass

def main():
    buf = bytearray()
    while True:
        b = os.read(0, 1)
        if not b or b == b"\n":
            break
        buf.extend(b)
    if not buf:
        sys.exit(1)
    req = json.loads(buf.decode("utf-8", errors="replace"))
    cwd = os.path.expanduser(str(req.get("cwd") or "."))
    cmd = [str(x) for x in (req.get("cmd") or [])]
    env_over = req.get("env") or {}
    kill_on_eof = bool(req.get("kill_on_eof"))

    env = os.environ.copy()
    home = os.path.expanduser("~")
    extra_dirs = [
        os.path.join(home, ".local", "bin"),
        os.path.join(home, ".cargo", "bin"),
        os.path.join(home, ".npm-global", "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ]
    cur_path = env.get("PATH", "")
    env["PATH"] = os.pathsep.join([d for d in extra_dirs if os.path.isdir(d)] + ([cur_path] if cur_path else []))
    for k, v in env_over.items():
        if v is None:
            env.pop(str(k), None)
        else:
            env[str(k)] = str(v)

    if not cmd:
        sys.exit(1)
    resolved = shutil.which(cmd[0], path=env.get("PATH"))
    if resolved:
        cmd[0] = resolved
    else:
        exp = os.path.expanduser(cmd[0])
        if os.path.isfile(exp):
            cmd[0] = exp

    kw = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kw["start_new_session"] = True

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=1,
        stderr=2,
        **kw,
    )

    def _on_sig(signum, _frame):
        _kill(proc)
        sys.exit(128 + int(signum))

    for sname in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, sname, None)
        if sig is not None:
            try:
                signal.signal(sig, _on_sig)
            except Exception:
                pass

    def _pump():
        try:
            while True:
                chunk = os.read(0, 4096)
                if not chunk:
                    break
                proc.stdin.write(chunk)
                proc.stdin.flush()
        except Exception:
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
            if kill_on_eof and proc.poll() is None:
                _kill(proc)

    t = threading.Thread(target=_pump, daemon=True)
    t.start()
    rc = proc.wait()
    sys.exit(rc)

if __name__ == "__main__":
    main()
'''

LOCAL_STDIO_BRIDGE_SCRIPT = r'''
import base64
import json
import os
import shlex
import subprocess
import sys
import threading

REMOTE_STUB = __REMOTE_STUB_REPR__

def main():
    ssh_host = sys.argv[1]
    remote_cwd = sys.argv[2]
    kill_on_eof = sys.argv[3] == "1"
    remote_cmd = sys.argv[5:]

    fwd_env = {}
    if os.environ.get("ANTHROPIC_BASE_URL"):
        for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL", "MAX_THINKING_TOKENS", "DISABLE_AUTO_COMPACT"):
            if k in os.environ:
                fwd_env[k] = os.environ[k]
        fwd_env["ANTHROPIC_API_KEY"] = None
        fwd_env["CLAUDE_CODE_OAUTH_TOKEN"] = None
    else:
        for k in ("DISABLE_AUTO_COMPACT", "MAX_THINKING_TOKENS"):
            if k in os.environ:
                fwd_env[k] = os.environ[k]
    for k, v in os.environ.items():
        if k.startswith("FAKE_"):
            fwd_env[k] = v

    kw = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    if os.environ.get("BUTTONAUTO_FAKE_SSH_LOCAL") == "1" or not ssh_host:
        spawn_cmd = [sys.executable, "-u", "-c", REMOTE_STUB]
    else:
        host_part = ssh_host
        port_part = None
        if not host_part.startswith("[") and host_part.count(":") == 1:
            h, p = host_part.rsplit(":", 1)
            if p.isdigit():
                host_part, port_part = h, p
        b64 = base64.b64encode(REMOTE_STUB.encode("utf-8")).decode("ascii")
        py_snippet = f"import base64;exec(base64.b64decode('{b64}'))"
        remote_sh = (
            "P=$(command -v python3 || command -v python); "
            '[ -n "$P" ] && exec "$P" -u -c ' + shlex.quote(py_snippet) + " || exit 127"
        )
        has_pw = bool(os.environ.get("BUTTONAUTO_SSH_PASS"))
        spawn_cmd = [
            "ssh", "-T",
            "-o", f"BatchMode={'no' if has_pw else 'yes'}",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "GSSAPIAuthentication=no",
            "-o", "VerifyHostKeyDNS=no",
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
        ]
        if has_pw:
            spawn_cmd += [
                "-o", "NumberOfPasswordPrompts=1",
                "-o", "PreferredAuthentications=publickey,keyboard-interactive,password",
            ]
        if port_part:
            spawn_cmd += ["-p", port_part]
        spawn_cmd += ["--", host_part, "sh", "-c", shlex.quote(remote_sh)]

    proc = subprocess.Popen(
        spawn_cmd,
        stdin=subprocess.PIPE,
        stdout=1,
        stderr=2,
        **kw,
    )
    header = json.dumps({
        "cwd": remote_cwd,
        "cmd": remote_cmd,
        "env": fwd_env,
        "kill_on_eof": kill_on_eof,
    }, ensure_ascii=False) + "\n"
    proc.stdin.write(header.encode("utf-8"))
    proc.stdin.flush()

    def _pump():
        try:
            while True:
                chunk = os.read(0, 4096)
                if not chunk:
                    break
                proc.stdin.write(chunk)
                proc.stdin.flush()
        except Exception:
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    t = threading.Thread(target=_pump, daemon=True)
    t.start()
    rc = proc.wait()
    sys.exit(rc)

if __name__ == "__main__":
    main()
'''.replace("__REMOTE_STUB_REPR__", repr(REMOTE_STDIO_STUB))


def wrap_remote_stdio_cmd(
    ssh_host: str,
    remote_cwd: str,
    cmd: Sequence[str],
    *,
    kill_on_eof: bool = False,
) -> Tuple[List[str], str]:
    """將遠端 CLI 啟動指令包裝為本機 stdio 橋接命令，回傳 (wrapped_cmd, local_cwd)。"""
    if not ssh_host:
        return list(cmd), remote_cwd
    err = validate_ssh_target(ssh_host)
    if err:
        raise SshError(err)
    wrapped = [
        sys.executable,
        "-u",
        "-c",
        LOCAL_STDIO_BRIDGE_SCRIPT,
        ssh_host,
        remote_cwd,
        "1" if kill_on_eof else "0",
        "--",
    ] + list(cmd)
    return wrapped, os.getcwd()


