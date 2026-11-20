"""CLI 探測：找出 kiro-cli、agy、claude 的完整路徑（CLI 一律以完整路徑啟動）。"""
import os
import shutil
from dataclasses import dataclass
from typing import Dict, List, Optional

# name -> (顯示名稱, 執行檔名, 協定)
CLI_SPECS = {
    "kiro": ("Kiro", "kiro-cli", "acp"),
    "agy": ("Antigravity", "agy", "acp"),
    "claude": ("Claude", "claude", "headless"),
}


@dataclass(frozen=True)
class CliInfo:
    name: str
    label: str
    path: str
    protocol: str  # "acp" 或 "headless"


def fresh_path() -> str:
    """os.environ["PATH"] 加上登錄檔最新的使用者與系統 PATH（非 Windows 只回傳環境變數）。"""
    parts = [os.environ.get("PATH", "")]
    try:
        import winreg
        for root, sub in ((winreg.HKEY_CURRENT_USER, "Environment"),
                          (winreg.HKEY_LOCAL_MACHINE,
                           r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
            try:
                with winreg.OpenKey(root, sub) as k:
                    parts.append(os.path.expandvars(winreg.QueryValueEx(k, "Path")[0]))
            except OSError:
                pass
    except ImportError:
        pass
    return os.pathsep.join(p for p in parts if p)


def _fallbacks() -> Dict[str, str]:
    """已知安裝位置；每次呼叫時才讀環境變數（方便測試改寫）。"""
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    return {
        "kiro": os.path.join(local, "Kiro-Cli", "kiro-cli.exe"),
        "agy": os.path.join(local, "agy", "bin", "agy.exe"),
        "claude": os.path.join(home, ".local", "bin", "claude.exe"),
    }


def discover(path_env: Optional[str] = None) -> Dict[str, CliInfo]:
    """回傳找得到的 CLI；先查 PATH，再試已知安裝位置。"""
    search = path_env or fresh_path()
    fallbacks = _fallbacks()
    found: Dict[str, CliInfo] = {}
    for name, (label, exe, protocol) in CLI_SPECS.items():
        try:
            p = shutil.which(exe, path=search)
            if not p:
                cand = fallbacks.get(name, "")
                p = cand if cand and os.path.isfile(cand) else None
        except Exception:
            p = None
        if p:
            found[name] = CliInfo(name, label, p, protocol)
    return found


# kiro：V3 引擎＋CLI 自己的登入（不走 acp-callback 向用戶端要權杖）
KIRO_ACP_ARGS = ["acp", "--agent-engine", "v3", "--auth-method", "cli"]


def launch_cmd(info: CliInfo) -> List[str]:
    if info.protocol != "acp":
        return [info.path]
    return [info.path] + (KIRO_ACP_ARGS if info.name == "kiro" else ["acp"])
