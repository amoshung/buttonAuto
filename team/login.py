"""登入流程：辨識「需要登入」的錯誤、開一個看得見的登入視窗、確認是否已登入、記住座位旗標。

- is_login_error：只看錯誤文字（不看代理的回覆內容，避免代理在談論驗證程式碼時誤判）；額度錯誤優先。
- login_command：要在新主控台視窗執行的指令與給使用者的一句說明；GLM 座位不需要登入（回 None）。
- open_login_window：Windows 以 CREATE_NEW_CONSOLE 開 cmd /k（視窗留著，登入失敗也看得到訊息）。
- check_login：kiro → kiro-cli whoami；claude → claude -p 極短訊息；agy → ACP initialize＋session/new＋極短 prompt。
- 所有子程序（登入視窗、確認指令）都帶公司網路憑證環境變數（drivers.cert_env）。

輸出內容（可能含帳號資訊）一律不寫入日誌或事件；只回傳「已登入／還沒登入」與簡短原因。
"""
import json
import logging
import os
import re
import subprocess
import shutil
import tempfile
from typing import Any, Callable, Dict, List, Optional, Tuple

from team.clis import CliInfo, fresh_path
from team.drivers import cert_env
from team.fleet import detect_quota

log = logging.getLogger(__name__)

LOGIN_RE = re.compile(
    r"sign in|signed out|not logged in|log ?in to|login required|unauthori[sz]ed|"
    r"authenticat(?:e|ion) (?:required|failed)|(?<!\d)401(?!\d)|token (?:expired|invalid)|"
    r"run /login|invalid api key|請先登入|未登入",
    re.I)
# 網路／憑證問題（公司 TLS 檢查、斷線）：即使錯誤也說「Sign in」，開登入視窗也沒有用
NETWORK_RE = re.compile(
    r"UNABLE_TO_GET_ISSUER_CERT(?:_LOCALLY)?|SELF_SIGNED_CERT_IN_CHAIN|unable to verify the first certificate|"
    r"UnknownIssuer|InvalidCertificate|certificate verify failed|dispatch failure|ECONNRESET|ENOTFOUND|"
    r"ETIMEDOUT|EAI_AGAIN|fetch failed|Connection error",
    re.I)
LOGIN_SEATS = ("kiro", "agy", "claude")
CHECK_PROMPT = "回覆 OK"
IS_WINDOWS = os.name == "nt"
_TITLE_BAD = re.compile(r'[&|<>^"%\r\n]')

Runner = Callable[[List[str], float], Tuple[int, str]]


def is_login_error(text: str) -> bool:
    """需要登入的錯誤；同時像額度錯誤時算額度（額度優先）。"""
    if not text or detect_quota(text):
        return False
    return LOGIN_RE.search(text) is not None


def is_network_error(text: str) -> bool:
    return bool(text) and NETWORK_RE.search(text) is not None


def login_command(seat: str, clis: Dict[str, CliInfo]) -> Optional[Tuple[List[str], str]]:
    """回傳（指令, 說明）；GLM 座位回 None；找不到 CLI 丟 ValueError。"""
    if seat not in LOGIN_SEATS:
        return None
    info = clis.get(seat)
    if info is None:
        raise ValueError("找不到 %s 的 CLI，無法開啟登入視窗" % seat)
    if seat == "kiro":
        return [info.path, "login"], "會開一個執行 kiro-cli login 的視窗，瀏覽器會開啟公司 IAM Identity Center 登入頁"
    if seat == "claude":
        return [info.path], "會開一個 claude 視窗，視窗開啟後輸入 /login 依指示登入"
    return [info.path], "會開一個 agy 視窗，依畫面指示用 Google 帳號登入"


def _title(title: str) -> str:
    return _TITLE_BAD.sub(" ", title or "登入").strip() or "登入"


def open_login_window(cmd: List[str], title: str) -> None:
    """開一個使用者看得見的主控台視窗執行 cmd（Windows）；其他系統丟 RuntimeError。"""
    if not cmd:
        raise ValueError("沒有登入指令")
    if not IS_WINDOWS:
        raise RuntimeError("目前只支援在 Windows 開啟登入視窗，請自己在終端機執行：%s" % " ".join(cmd))
    line = subprocess.list2cmdline(cmd)
    # cmd /s /k "title X& "C:\path\kiro-cli.exe" login"：/s 只拿掉最外層引號，視窗在指令結束後留著
    full = 'cmd.exe /s /k "title %s& %s"' % (_title(title), line)
    env = _child_env()
    subprocess.Popen(full, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10), env=env,
                     close_fds=True)


def _child_env() -> Dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = fresh_path()
    for k, v in cert_env().items():
        env.setdefault(k, v)
    return env


def _run(cmd: List[str], timeout: float) -> Tuple[int, str]:
    kw = {}  # type: Dict[str, Any]
    if IS_WINDOWS:
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    env = _child_env()
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, stdin=subprocess.DEVNULL, env=env, **kw)
    return p.returncode, (p.stdout or "") + "\n" + (p.stderr or "")


def _check_acp(info: CliInfo, timeout: float) -> Tuple[Optional[bool], str]:
    from team.acp import AcpClient
    from team.clis import launch_cmd
    errors = []  # type: List[str]
    cwd = tempfile.mkdtemp(prefix="login-check-")
    client = AcpClient(launch_cmd(info), cwd, lambda ev: errors.append(ev.kind) if ev.kind == "error" else None,
                       lambda req: "", env=_child_env())
    try:
        client.start(timeout)
        client.new_session(timeout)
        stop = client.prompt(CHECK_PROMPT, max(timeout, 60))
    except Exception as e:
        if is_login_error(str(e)) and not is_network_error(str(e)):
            return False, "還沒偵測到登入"
        return None, "無法確認登入狀態，可能是網路或憑證（%s）" % type(e).__name__
    finally:
        client.close()
        shutil.rmtree(cwd, ignore_errors=True)
    if stop == "error" or errors:
        return False, "還沒偵測到登入"
    return True, "已登入"


def check_login(seat: str, clis: Dict[str, CliInfo], timeout: float = 20,
                run: Optional[Runner] = None) -> Tuple[Optional[bool], str]:
    """回傳（True 已登入／False 沒登入／None 無法確認（逾時、例外、網路）, 簡短說明）。不記錄 CLI 的輸出內容。"""
    if seat not in LOGIN_SEATS:
        return True, "不需要登入"
    info = clis.get(seat)
    if info is None:
        return False, "找不到 %s 的 CLI" % seat
    run = run or _run
    try:
        if seat == "kiro":
            code, out = run([info.path, "whoami"], timeout)
            ok = code == 0 and "not logged" not in out.lower()
            return (True, "已登入") if ok else (False, "還沒偵測到登入")
        if seat == "claude":
            code, out = run([info.path, "-p", CHECK_PROMPT, "--output-format", "json", "--max-turns", "1"],
                            max(timeout, 60))
            if code != 0:
                if is_network_error(out):
                    return None, "無法確認登入狀態，可能是網路或憑證"
                return False, "還沒偵測到登入"
            try:
                data = json.loads(out.strip().splitlines()[0]) if out.strip() else {}
            except (ValueError, IndexError):
                data = {}
            if isinstance(data, dict) and data.get("is_error"):
                if is_network_error(str(data.get("result", ""))):
                    return None, "無法確認登入狀態，可能是網路或憑證"
                return False, "還沒偵測到登入"
            return True, "已登入"
        return _check_acp(info, timeout)
    except subprocess.TimeoutExpired:
        return None, "無法確認登入狀態，可能是網路或憑證（檢查逾時 %g 秒）" % timeout
    except Exception as e:
        log.info("登入檢查失敗：%s %s", seat, type(e).__name__)
        return None, "無法確認登入狀態，可能是網路或憑證（%s）" % type(e).__name__
