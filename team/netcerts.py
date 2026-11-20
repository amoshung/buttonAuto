"""公司網路憑證：把 Windows 憑證存放區（ROOT＋CA）匯出成 PEM，交給 CLI 子程序（SSL_CERT_FILE／NODE_EXTRA_CA_CERTS）。

為什麼：公司網路做 TLS 檢查時，kiro-cli 等 CLI 自帶的 TLS 不認得公司根憑證（InvalidCertificate(UnknownIssuer)），
kiro 會把它回報成「Sign in to Kiro」。用 Windows 憑證存放區匯出的 PEM 就能連線。

- windows_ca_bundle(path)：只在 Windows（或測試注入 enumerator）匯出；檔案超過 24 小時才重新產生；回傳路徑或 None。
- cert_env(base_dir)：{"SSL_CERT_FILE": pem, "NODE_EXTRA_CA_CERTS": pem}，逐鍵：使用者自己設定的鍵（環境變數或
  登錄檔 HKCU\\Environment／系統環境）不填。這些值優先序最低：座位自己的環境變數（SecretEnv）會蓋過它。
- status()：最近一次匯出的結果（state ok／failed／off／user、path、reason），給座位面板顯示。
"""
import logging
import os
import random
import ssl
import threading
import time
from typing import Callable, Dict, Iterable, Optional, Tuple

log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORES = ("ROOT", "CA")
MAX_AGE = 24 * 3600
ENV_KEYS = ("SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS")

Enumerator = Callable[[str], Iterable[Tuple[bytes, str, object]]]
_lock = threading.Lock()
_status = {"state": "unknown", "path": "", "reason": ""}  # type: Dict[str, str]


def _set_status(state: str, path: str = "", reason: str = "") -> None:
    _status.update(state=state, path=path, reason=reason)


def status() -> Dict[str, str]:
    """最近一次匯出結果：state = ok／failed／off（不是 Windows）／user（用使用者自己的憑證檔）／unknown（還沒匯出）。"""
    with _lock:
        return dict(_status)


def bundle_path(base_dir: str = BASE_DIR) -> str:
    return os.path.join(base_dir, "sessions", "windows-ca.pem")


def _default_enumerator() -> Optional[Enumerator]:
    return getattr(ssl, "enum_certificates", None)  # 只有 Windows 有


def _valid(path: str) -> bool:
    try:
        with open(path, encoding="ascii", errors="replace") as f:
            return "BEGIN CERTIFICATE" in f.read(4096)
    except OSError:
        return False


def windows_ca_bundle(path: str, enumerator: Optional[Enumerator] = None,
                      max_age: float = MAX_AGE) -> Optional[str]:
    """匯出 ROOT＋CA 憑證（x509_asn、去重）成 PEM；成功回傳 path，不支援或失敗回 None。
    重新產生失敗但舊檔還在且有效 → 回傳舊檔。"""
    enum = enumerator or _default_enumerator()
    with _lock:
        if enum is None:
            _set_status("off", "", "不是 Windows，沒有憑證存放區可匯出")
            return None
        try:
            if os.path.isfile(path) and time.time() - os.path.getmtime(path) < max_age and _valid(path):
                _set_status("ok", path)
                return path
        except OSError:
            pass
        old = path if _valid(path) else None
        seen = set()
        pems = []
        for store in STORES:
            try:
                certs = list(enum(store))
            except Exception as e:  # 某個存放區讀不到：略過
                log.info("讀不到憑證存放區 %s：%s", store, type(e).__name__)
                continue
            for cert, encoding, _trust in certs:
                if encoding != "x509_asn" or not cert or cert in seen:
                    continue
                seen.add(cert)
                try:
                    pems.append(ssl.DER_cert_to_PEM_cert(cert))
                except Exception:
                    continue
        if not pems:
            _set_status("ok" if old else "failed", old or "", "沒有讀到任何憑證" + ("，沿用舊檔" if old else ""))
            return old
        tmp = "%s.%d.%06d.tmp" % (path, os.getpid(), random.randrange(1000000))
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(tmp, "w", encoding="ascii", newline="\n") as f:
                f.write("".join(pems))
            os.replace(tmp, path)
        except OSError as e:
            log.warning("無法寫入憑證檔：%s", type(e).__name__)
            try:
                os.remove(tmp)
            except OSError:
                pass
            reason = "無法寫入憑證檔（%s）" % type(e).__name__
            _set_status("ok" if old else "failed", old or "", reason + ("，沿用舊檔" if old else ""))
            return old
        log.info("已匯出 %d 張 Windows 憑證到 %s", len(pems), path)
        _set_status("ok", path, "已匯出 %d 張憑證" % len(pems))
        return path


def _registry_env(key: str) -> str:
    """登錄檔裡使用者（HKCU\\Environment）或系統的環境變數值（新設定的值不一定已在 os.environ）。"""
    try:
        import winreg
    except ImportError:
        return ""
    for root, sub in ((winreg.HKEY_CURRENT_USER, "Environment"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(root, sub) as k:
                v = winreg.QueryValueEx(k, key)[0]
                if v:
                    return os.path.expandvars(str(v))
        except OSError:
            continue
    return ""


def user_env(key: str) -> str:
    """使用者自己設定的值：先看 os.environ，再看登錄檔。"""
    return os.environ.get(key) or _registry_env(key)


def user_cert_file() -> str:
    """使用者自己設定的憑證檔（SSL_CERT_FILE 或 NODE_EXTRA_CA_CERTS）；沒有回空字串。"""
    for k in ENV_KEYS:
        v = user_env(k)
        if v:
            return v
    return ""


def cert_env(base_dir: str = BASE_DIR, enumerator: Optional[Enumerator] = None) -> Dict[str, str]:
    """逐鍵：使用者已設定的鍵用使用者的值（只在登錄檔、還沒進 os.environ 的也明確傳下去）；其餘填匯出的 PEM。"""
    env = {}  # type: Dict[str, str]
    missing = []
    for k in ENV_KEYS:
        v = user_env(k)
        if not v:
            missing.append(k)
        elif not os.environ.get(k):
            env[k] = v
    if not missing:
        with _lock:
            _set_status("user", user_cert_file(), "使用你設定的憑證檔")
        return env
    pem = windows_ca_bundle(bundle_path(base_dir), enumerator)
    if pem:
        env.update({k: pem for k in missing})
    return env
