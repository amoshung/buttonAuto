"""Windows 系統通知（WinRT Toast）；失敗時靜默回傳 False，由瀏覽器 Notification 作為備援（Task 10）。"""
import os
import subprocess
from typing import Callable
from xml.sax.saxutils import escape

MAX_LEN = 120


def _clean(s: str) -> str:
    flat = " ".join((s or "").split())[:MAX_LEN]
    return escape(flat, {'"': "&quot;", "'": "&apos;"})


def toast(title: str, text: str, *, run: Callable = subprocess.run, force_nt: bool = False) -> bool:
    """在 Windows 跳出系統 Toast 通知（只含房名與一句原因，不含程式輸出）；失敗回 False 不丟例外。"""
    if os.name != "nt" and not force_nt:
        return False
    t_esc = _clean(title)
    b_esc = _clean(text)
    ps_script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; "
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null; "
        '$xml = New-Object Windows.Data.Xml.Dom.XmlDocument; '
        '$xml.LoadXml(\'<toast><visual><binding template="ToastText02">'
        '<text id="1">%s</text><text id="2">%s</text>'
        "</binding></visual></toast>'); "
        '$toast = [Windows.UI.Notifications.ToastNotification]::new($xml); '
        '[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("AI Engineering Assistant").Show($toast)'
        % (t_esc, b_esc)
    )
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        res = run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            **kw,
        )
        return getattr(res, "returncode", 1) == 0
    except Exception:
        return False
